"""Build the supplier's WhatsApp message from PriceList (the "share" button).

Same shape as the supplier's own message so their customers see nothing new.
Inactive products and inactive offers are left out.
"""

from __future__ import annotations

from decimal import Decimal

from app.pricelist.model import Offer, PriceList, Product
from app.pricelist.parser import PRODUCT_MARK, SECTION_MARK


def money(value: Decimal) -> str:
    """125 -> "125", 24.50 -> "24.50", 54.25 -> "54.25"."""
    if value == value.to_integral_value():
        return str(value.quantize(Decimal(1)))
    return f"{value:.2f}"


def offer_line(offer: Offer) -> str:
    first = money(offer.price) if offer.mrp is None else f"{money(offer.price)}/{money(offer.mrp)}"
    parts = [f"({first})"]
    if offer.units_per_box is not None:
        parts.append(f"({offer.units_per_box})")
    parts.extend(f"({x})" for x in offer.extra)
    line = "".join(parts)
    return f"{line} {offer.label}" if offer.label else line


def _product_lines(product: Product) -> list[str]:
    offers = [o for o in product.offers if o.active]
    if not offers:
        return []
    out = [f"{PRODUCT_MARK}{product.heading}"]
    out.extend(product.notes_before)
    out.extend(offer_line(o) for o in offers)
    out.extend(product.notes_after)
    return out


def render(price_list: PriceList) -> str:
    out: list[str] = []
    if price_list.header:
        out.extend(price_list.header)
        out.append("")
    for section in price_list.sections:
        blocks = [b for p in section.products if p.active for b in [_product_lines(p)] if b]
        if not blocks and not section.notes:
            continue
        out.append(f"{SECTION_MARK}{SECTION_MARK} {section.title} {SECTION_MARK}{SECTION_MARK}")
        out.append("")
        for block in blocks:
            out.extend(block)
        if section.notes:
            out.append("")
            out.extend(section.notes)
        out.append("")
    if price_list.footer:
        out.extend(price_list.footer)
    return "\n".join(out).rstrip() + "\n"
