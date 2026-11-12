"""Read a supplier's WhatsApp price list into PriceList.

Format (Nishi Palace, 2026-11-12; legend in the message itself):

    🛑🛑සබන් වර්ග 🛑🛑                section
    🟡බේබි ශෙරමි සබන් 75g              product heading
    (125/165)(150)                      (our price / selling price)(units per box)
    (424/530)(36) 4+1 free pack         another offer; the tail is its label

Rules:
- Lines before the first section are the header; lines after the block that holds the
  last offer (from the next blank line on) are the footer. Both are kept verbatim.
- A blank line ends the current product once it has at least one offer.
- A plain line with no current product is a heading when the next non-blank line is an
  offer ("කොකාකෝලා,ස්ප්‍රයිට්"), otherwise a section note.
- Anything the parser is not sure of is FLAGGED on the offer, never guessed.
"""

from __future__ import annotations

import re
from decimal import Decimal

from app.pricelist.model import (
    FLAG_BAD_FORMAT,
    FLAG_BOX_UNCLEAR,
    FLAG_NO_BOX,
    FLAG_NO_MRP,
    FLAG_PRICE_ABOVE_MRP,
    Offer,
    PriceList,
    Product,
    Section,
)

SECTION_MARK = "🛑"
PRODUCT_MARK = "🟡"

_NUM = r"\d+(?:\.\d+)?"
_GROUP = re.compile(r"\(([^()]*)\)")
_PRICE_ONLY = re.compile(rf"\s*({_NUM})\s*")
_PRICE_MRP = re.compile(rf"\s*({_NUM})\s*/\s*({_NUM})\s*")
_WHOLE = re.compile(r"\s*(\d+)\s*")
# Zero-width space and BOM only. NEVER strip ZWJ/ZWNJ (U+200D/U+200C): Sinhala needs
# them for yansaya/rakaransaya ("බ්‍රෂ්" would become "බ්රෂ්").
_INVISIBLE = dict.fromkeys(map(ord, "​﻿"), None)


def _clean(line: str) -> str:
    return line.rstrip("\r\n").strip()


def is_offer_line(line: str) -> bool:
    """True when the line starts with a price bracket: "(125/165)..." or "(660)...".

    Must be the very first thing on the line, so notes like "(NMRA Approve) 4pcs"
    or "( Rathmal/Delum/Almond )" are not taken for prices.
    """
    s = _clean(line)
    if not s.startswith("("):
        return False
    m = _GROUP.match(s)
    if not m:
        return False
    inner = m.group(1)
    return bool(_PRICE_MRP.fullmatch(inner) or _PRICE_ONLY.fullmatch(inner))


def parse_offer(line: str) -> Offer:
    s = _clean(line)
    groups = _GROUP.findall(s)
    label = _GROUP.sub("", s).strip()
    flags: list[str] = []
    extra: list[str] = []

    first = groups[0]
    m = _PRICE_MRP.fullmatch(first)
    if m:
        price, mrp = Decimal(m.group(1)), Decimal(m.group(2))
    else:
        price, mrp = Decimal(_PRICE_ONLY.fullmatch(first).group(1)), None
        flags.append(FLAG_NO_MRP)

    units: int | None = None
    rest = groups[1:]
    if not rest:
        flags.append(FLAG_NO_BOX)
    else:
        box = _WHOLE.fullmatch(rest[0])
        if box:
            units = int(box.group(1))
        else:
            flags.append(FLAG_BOX_UNCLEAR)
            extra.append(rest[0].strip())
        if len(rest) > 1:
            flags.append(FLAG_BAD_FORMAT)
            extra.extend(g.strip() for g in rest[1:])

    if mrp is not None and price > mrp:
        flags.append(FLAG_PRICE_ABOVE_MRP)

    return Offer(
        price=price, mrp=mrp, units_per_box=units, label=label, raw=s, flags=flags, extra=extra
    )


def _section_title(line: str) -> str:
    return line.replace(SECTION_MARK, "").strip()


def parse(text: str) -> PriceList:
    lines = [ln.translate(_INVISIBLE).rstrip("\r") for ln in text.split("\n")]
    result = PriceList()

    first_section = next(
        (i for i, ln in enumerate(lines) if _clean(ln).startswith(SECTION_MARK)), None
    )
    if first_section is None:
        result.header = lines
        return result

    last_offer = max(i for i, ln in enumerate(lines) if is_offer_line(ln))
    footer_start = next(
        (i for i in range(last_offer + 1, len(lines)) if not _clean(lines[i])), len(lines)
    )
    result.header = lines[:first_section]
    result.footer = lines[footer_start + 1 :] if footer_start < len(lines) else []
    _trim_blank(result.header)
    _trim_blank(result.footer)

    body = lines[first_section:footer_start]
    section: Section | None = None
    product: Product | None = None

    def next_nonblank(idx: int) -> str:
        for j in range(idx + 1, len(body)):
            if _clean(body[j]):
                return body[j]
        return ""

    for i, raw in enumerate(body):
        s = _clean(raw)
        if not s:
            # A heading still waiting for its offers survives a blank line
            # ("කොකාකෝලා,ස්ප්‍රයිට්" is followed by one before its prices).
            if product is not None and product.offers:
                product = None
            continue
        if s.startswith(SECTION_MARK):
            section = Section(title=_section_title(s))
            result.sections.append(section)
            product = None
            continue
        assert section is not None  # body starts at a section line
        if s.startswith(PRODUCT_MARK):
            product = Product(heading=s[len(PRODUCT_MARK) :].strip())
            section.products.append(product)
            continue
        if is_offer_line(s):
            if product is None:
                # Offer with no heading at all: keep it, under an empty heading to review.
                product = Product(heading="")
                section.products.append(product)
            offer = parse_offer(s)
            offer.line_no = first_section + i + 1
            product.offers.append(offer)
            continue
        # Plain text.
        if product is None:
            if is_offer_line(next_nonblank(i)):
                product = Product(heading=s)
                section.products.append(product)
            else:
                section.notes.append(s)
            continue
        (product.notes_after if product.offers else product.notes_before).append(s)

    return result


def _trim_blank(block: list[str]) -> None:
    while block and not block[0].strip():
        block.pop(0)
    while block and not block[-1].strip():
        block.pop()
