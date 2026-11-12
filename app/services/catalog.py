"""A supplier's own catalogue: sections, products, offers (PLAN.md decisions 4, 13, 14).

The supplier app is the source of truth; a pasted WhatsApp list is an import that
creates or updates rows and never deletes or deactivates anything (items missing from a
paste are reported, not touched). Every price change is kept in supplier_offer_prices.

Every function takes the supplier_id of the CALLER (from the token, or the staff URL) and
looks rows up together with it: another supplier's id answers 404, never 403, so ids
cannot be probed.
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Supplier,
    SupplierOffer,
    SupplierOfferPrice,
    SupplierProduct,
    SupplierSection,
)
from app.pricelist import parse, render
from app.pricelist.model import Offer, PriceList, Product, Section
from app.services import audit
from app.services.errors import Conflict, NotFound, SupplyError


@dataclass(frozen=True)
class Actor:
    """Who makes a change: a supplier-app user or a staff user (onboarding on their behalf)."""

    supplier_user_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None

    @property
    def source(self) -> str:
        return "app" if self.supplier_user_id else "staff"


@dataclass
class Resolution:
    """What the supplier says a flagged line really means (or skip it)."""

    skip: bool = False
    price: Decimal | None = None
    mrp: Decimal | None = None
    units_per_box: int | None = None
    label: str | None = None


@dataclass
class ImportResult:
    sections_new: int = 0
    products_new: int = 0
    offers_new: int = 0
    offers_changed: int = 0
    offers_same: int = 0
    offers_skipped: int = 0
    not_in_paste: int = 0
    changed: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------- lookups
async def _supplier(db: AsyncSession, supplier_id: uuid.UUID) -> Supplier:
    supplier = await db.get(Supplier, supplier_id)
    if supplier is None:
        raise NotFound("supplier not found")
    return supplier


async def _section(db, supplier_id, section_id) -> SupplierSection:
    row = await db.scalar(select(SupplierSection).where(
        SupplierSection.section_id == section_id, SupplierSection.supplier_id == supplier_id))
    if row is None:
        raise NotFound("section not found")
    return row


async def _product(db, supplier_id, product_id) -> SupplierProduct:
    row = await db.scalar(select(SupplierProduct).where(
        SupplierProduct.product_id == product_id, SupplierProduct.supplier_id == supplier_id))
    if row is None:
        raise NotFound("product not found")
    return row


async def _offer(db, supplier_id, offer_id) -> SupplierOffer:
    row = await db.scalar(select(SupplierOffer).where(
        SupplierOffer.offer_id == offer_id, SupplierOffer.supplier_id == supplier_id))
    if row is None:
        raise NotFound("offer not found")
    return row


async def _lock(db: AsyncSession, supplier_id: uuid.UUID) -> None:
    """One catalogue writer per supplier at a time (imports must not interleave)."""
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
                     {"k": f"catalog:{supplier_id}"})


def _history(offer: SupplierOffer, actor: Actor, source: str) -> SupplierOfferPrice:
    return SupplierOfferPrice(
        offer_id=offer.offer_id, supplier_id=offer.supplier_id, price=offer.price,
        mrp=offer.mrp, units_per_box=offer.units_per_box, source=source,
        supplier_user_id=actor.supplier_user_id, user_id=actor.user_id,
    )


MAX_UNITS_PER_BOX = 100_000


def _check_money(price: Decimal, mrp: Decimal | None, units: int | None) -> None:
    if price is None or price <= 0:
        raise SupplyError("price must be more than 0")
    if mrp is not None and mrp <= 0:
        raise SupplyError("selling price must be more than 0")
    if mrp is not None and price > mrp:
        raise SupplyError("our price is above the selling price")
    if units is not None and not 0 < units <= MAX_UNITS_PER_BOX:
        raise SupplyError(f"units per box must be between 1 and {MAX_UNITS_PER_BOX:,}")


def _no_nulls(changes: dict, nullable: tuple[str, ...] = ()) -> dict:
    """PATCH bodies: an explicit null is refused except where "unknown" is meaningful
    (review #6: it used to end in a 500)."""
    bad = [k for k, v in changes.items() if v is None and k not in nullable]
    if bad:
        raise SupplyError(f"{', '.join(bad)} cannot be empty")
    return changes


# ---------------------------------------------------------------- read
async def load(db: AsyncSession, supplier_id: uuid.UUID, *, active_only: bool = False):
    """(supplier, sections, products by section, offers by product), in display order."""
    supplier = await _supplier(db, supplier_id)
    sections = (await db.scalars(
        select(SupplierSection).where(SupplierSection.supplier_id == supplier_id)
        .order_by(SupplierSection.sort_order, SupplierSection.created_at))).all()
    products = (await db.scalars(
        select(SupplierProduct).where(SupplierProduct.supplier_id == supplier_id)
        .order_by(SupplierProduct.sort_order, SupplierProduct.created_at))).all()
    offers = (await db.scalars(
        select(SupplierOffer).where(SupplierOffer.supplier_id == supplier_id)
        .order_by(SupplierOffer.sort_order, SupplierOffer.created_at))).all()
    by_section: dict[uuid.UUID, list[SupplierProduct]] = defaultdict(list)
    for p in products:
        if not active_only or p.is_active:
            by_section[p.section_id].append(p)
    by_product: dict[uuid.UUID, list[SupplierOffer]] = defaultdict(list)
    for o in offers:
        if not active_only or o.is_active:
            by_product[o.product_id].append(o)
    if active_only:
        sections = [s for s in sections if s.is_active]
    return supplier, sections, by_section, by_product


def _lines(block: str) -> list[str]:
    return block.split("\n") if block else []


async def as_price_list(db: AsyncSession, supplier_id: uuid.UUID) -> PriceList:
    supplier, sections, by_section, by_product = await load(db, supplier_id, active_only=True)
    pl = PriceList(header=_lines(supplier.price_list_header),
                   footer=_lines(supplier.price_list_footer))
    for s in sections:
        sec = Section(title=s.title, notes=list(s.notes))
        for p in by_section[s.section_id]:
            sec.products.append(Product(
                heading=p.heading, notes_before=list(p.notes_before),
                notes_after=list(p.notes_after),
                offers=[Offer(price=o.price, mrp=o.mrp, units_per_box=o.units_per_box,
                              label=o.label, extra=list(o.extra))
                        for o in by_product[p.product_id]],
            ))
        pl.sections.append(sec)
    return pl


async def message(db: AsyncSession, supplier_id: uuid.UUID) -> str:
    """The WhatsApp message the supplier shares with their other customers."""
    return render(await as_price_list(db, supplier_id))


# ---------------------------------------------------------------- import
def _product_key(section_title: str, p: Product | SupplierProduct, nth: int) -> tuple:
    return (section_title.strip().lower(), p.heading.strip(), tuple(p.notes_before), nth)


async def _existing_index(db, supplier_id):
    _, sections, by_section, by_product = await load(db, supplier_id)
    titles = {s.section_id: s.title for s in sections}
    by_title = {s.title.strip().lower(): s for s in sections}
    products: dict[tuple, SupplierProduct] = {}
    seen: dict[tuple, int] = defaultdict(int)
    for s in sections:
        for p in by_section[s.section_id]:
            base = _product_key(titles[s.section_id], p, 0)[:3]
            products[(*base, seen[base])] = p
            seen[base] += 1
    return by_title, products, by_product


def _offer_keys(offers) -> list[tuple[str, int]]:
    seen: dict[str, int] = defaultdict(int)
    keys = []
    for o in offers:
        keys.append((o.label, seen[o.label]))
        seen[o.label] += 1
    return keys


def _resolve(o: Offer, resolutions: dict[int, Resolution]) -> Offer | None:
    """Apply the supplier's answer to a flagged line. None = skipped."""
    if not o.needs_review:
        return o
    r = resolutions.get(o.line_no)
    if r is None:
        raise SupplyError(f"line {o.line_no} needs your check: {o.raw}")
    if r.skip:
        return None
    price = r.price if r.price is not None else o.price
    mrp = r.mrp if r.mrp is not None else o.mrp
    units = r.units_per_box if r.units_per_box is not None else o.units_per_box
    try:
        _check_money(price, mrp, units)
    except SupplyError as exc:
        raise SupplyError(f"line {o.line_no}: {exc.message}") from None
    return Offer(price=price, mrp=mrp, units_per_box=units,
                 label=r.label if r.label is not None else o.label, raw=o.raw,
                 line_no=o.line_no)


def _same(o: Offer, row: SupplierOffer) -> bool:
    return (o.price == row.price and o.mrp == row.mrp and o.units_per_box == row.units_per_box
            and list(o.extra) == list(row.extra))


async def preview(db: AsyncSession, supplier_id: uuid.UUID, pasted: str) -> dict:
    """What an import would do. Changes nothing."""
    pl = parse(pasted)
    if not pl.sections:
        raise SupplyError("no price list found: lines like '🛑🛑 Section 🛑🛑' and "
                          "'(125/165)(150)' are needed")
    by_title, products, by_product = await _existing_index(db, supplier_id)
    flagged, changed, reshaped = [], [], []
    new_products = new_offers = same = 0
    seen: dict[tuple, int] = defaultdict(int)
    for s in pl.sections:
        for p in s.products:
            base = _product_key(s.title, p, 0)[:3]
            row = products.get((*base, seen[base]))
            seen[base] += 1
            existing = {}
            if row is not None:
                rows = by_product[row.product_id]
                existing = dict(zip(_offer_keys(rows), rows, strict=True))
                # Offers are matched by (note, position): when a heading gains or loses
                # lines, unlabelled ones may shift onto a sibling (review #5) - say so.
                # Lines sharing a note are told apart only by position, so when such a
                # group grows or shrinks a price can shift onto its sibling.
                was = Counter(r.label for r in rows if r.is_active)
                now = Counter(o.label for o in p.offers if not o.needs_review)
                for lbl in set(was) | set(now):
                    if was[lbl] != now[lbl] and max(was[lbl], now[lbl]) >= 2:
                        reshaped.append({"heading": p.heading, "section": s.title,
                                         "label": lbl, "before": was[lbl], "after": now[lbl]})
            else:
                new_products += 1
            for key, o in zip(_offer_keys(p.offers), p.offers, strict=True):
                if o.needs_review:
                    flagged.append({"line_no": o.line_no, "raw": o.raw, "heading": p.heading,
                                    "section": s.title, "flags": o.flags, "price": o.price,
                                    "mrp": o.mrp, "units_per_box": o.units_per_box,
                                    "label": o.label, "extra": o.extra})
                    continue
                old = existing.get(key)
                if old is None:
                    new_offers += 1
                elif _same(o, old):
                    same += 1
                else:
                    changed.append({"line_no": o.line_no, "heading": p.heading,
                                    "label": o.label, "old_price": old.price,
                                    "new_price": o.price, "old_mrp": old.mrp, "new_mrp": o.mrp,
                                    "old_units": old.units_per_box,
                                    "new_units": o.units_per_box})
    return {
        "sections": len(pl.sections),
        "products": len(pl.products()),
        "offers": len(pl.offers()),
        "new_sections": sum(1 for s in pl.sections if s.title.strip().lower() not in by_title),
        "new_products": new_products,
        "new_offers": new_offers,
        "changed_offers": changed,
        "same_offers": same,
        "flagged": flagged,
        "reshaped": reshaped,
        "has_header": bool(pl.header),
        "has_footer": bool(pl.footer),
    }


async def import_text(
    db: AsyncSession, supplier_id: uuid.UUID, pasted: str, *,
    resolutions: dict[int, Resolution], actor: Actor, use_header_footer: bool = True,
) -> ImportResult:
    await _lock(db, supplier_id)
    supplier = await _supplier(db, supplier_id)
    pl = parse(pasted)
    if not pl.sections:
        raise SupplyError("no price list found")
    # Resolve everything first: one unanswered flag stops the whole import.
    resolved: dict[int, Offer | None] = {}
    for o in pl.offers():
        resolved[id(o)] = _resolve(o, resolutions)

    by_title, products, by_product = await _existing_index(db, supplier_id)
    result = ImportResult()
    touched_offers: set[uuid.UUID] = set()
    if use_header_footer:
        if pl.header:
            supplier.price_list_header = "\n".join(pl.header)
        if pl.footer:
            supplier.price_list_footer = "\n".join(pl.footer)

    seen: dict[tuple, int] = defaultdict(int)
    for s_idx, s in enumerate(pl.sections):
        section = by_title.get(s.title.strip().lower())
        if section is None:
            section = SupplierSection(supplier_id=supplier_id, title=s.title.strip())
            db.add(section)
            by_title[s.title.strip().lower()] = section
            result.sections_new += 1
        section.notes = list(s.notes)
        section.sort_order = s_idx * 10
        section.is_active = True
        await db.flush()

        for p_idx, p in enumerate(s.products):
            base = _product_key(s.title, p, 0)[:3]
            row = products.get((*base, seen[base]))
            seen[base] += 1
            if row is None:
                row = SupplierProduct(supplier_id=supplier_id, section_id=section.section_id,
                                      heading=p.heading.strip(),
                                      notes_before=list(p.notes_before))
                db.add(row)
                result.products_new += 1
                existing: dict = {}
            else:
                rows = by_product[row.product_id]
                existing = dict(zip(_offer_keys(rows), rows, strict=True))
            row.notes_after = list(p.notes_after)
            row.sort_order = p_idx * 10
            row.is_active = True
            await db.flush()

            for o_idx, (key, raw_offer) in enumerate(
                    zip(_offer_keys(p.offers), p.offers, strict=True)):
                o = resolved[id(raw_offer)]
                if o is None:
                    result.offers_skipped += 1
                    continue
                old = existing.get(key)
                if old is None:
                    new = SupplierOffer(
                        supplier_id=supplier_id, product_id=row.product_id, label=o.label,
                        price=o.price, mrp=o.mrp, units_per_box=o.units_per_box,
                        extra=list(o.extra), sort_order=o_idx * 10)
                    db.add(new)
                    await db.flush()
                    db.add(_history(new, actor, "import"))
                    touched_offers.add(new.offer_id)
                    result.offers_new += 1
                    continue
                touched_offers.add(old.offer_id)
                old.sort_order = o_idx * 10
                old.is_active = True
                if _same(o, old):
                    result.offers_same += 1
                    continue
                result.changed.append({"heading": p.heading, "label": o.label,
                                       "old_price": str(old.price), "new_price": str(o.price)})
                old.price, old.mrp, old.units_per_box = o.price, o.mrp, o.units_per_box
                old.extra = list(o.extra)
                db.add(_history(old, actor, "import"))
                result.offers_changed += 1

    all_offers = [o for rows in by_product.values() for o in rows]
    result.not_in_paste = sum(1 for o in all_offers
                              if o.offer_id not in touched_offers and o.is_active)
    await db.flush()
    await audit.record(
        db, user_id=actor.user_id, supplier_user_id=actor.supplier_user_id,
        action="catalog.import", entity="supplier", entity_id=supplier_id,
        after={k: v for k, v in result.__dict__.items() if k != "changed"},
    )
    return result


# ---------------------------------------------------------------- edits from the app
async def set_template(db, supplier_id, *, header: str, footer: str, actor: Actor) -> None:
    supplier = await _supplier(db, supplier_id)
    supplier.price_list_header = header.strip("\n")
    supplier.price_list_footer = footer.strip("\n")
    await audit.record(db, user_id=actor.user_id, supplier_user_id=actor.supplier_user_id,
                       action="catalog.template", entity="supplier", entity_id=supplier_id)


async def add_section(db, supplier_id, *, title: str, actor: Actor) -> SupplierSection:
    await _lock(db, supplier_id)
    await _supplier(db, supplier_id)
    title = title.strip()
    taken = await db.scalar(select(SupplierSection).where(
        SupplierSection.supplier_id == supplier_id,
        func.lower(SupplierSection.title) == title.lower()))
    if taken:
        raise Conflict("a section with this name already exists")
    last = await db.scalar(select(func.max(SupplierSection.sort_order))
                           .where(SupplierSection.supplier_id == supplier_id))
    section = SupplierSection(supplier_id=supplier_id, title=title,
                              sort_order=(last or 0) + 10)
    db.add(section)
    await db.flush()
    await audit.record(db, user_id=actor.user_id, supplier_user_id=actor.supplier_user_id,
                       action="catalog.section_add", entity="supplier_section",
                       entity_id=section.section_id, after={"title": title})
    return section


async def update_section(db, supplier_id, section_id, *, changes: dict,
                         actor: Actor) -> SupplierSection:
    _no_nulls(changes)
    await _lock(db, supplier_id)
    section = await _section(db, supplier_id, section_id)
    if "title" in changes:
        changes["title"] = changes["title"].strip()
        taken = await db.scalar(select(SupplierSection).where(
            SupplierSection.supplier_id == supplier_id,
            func.lower(SupplierSection.title) == changes["title"].lower(),
            SupplierSection.section_id != section_id))
        if taken:
            raise Conflict("a section with this name already exists")
    for k, v in changes.items():
        setattr(section, k, v)
    await db.flush()
    await audit.record(db, user_id=actor.user_id, supplier_user_id=actor.supplier_user_id,
                       action="catalog.section_update", entity="supplier_section",
                       entity_id=section_id, after=changes)
    return section


async def add_product(db, supplier_id, *, section_id, heading: str, notes_before: list[str],
                      notes_after: list[str], actor: Actor) -> SupplierProduct:
    await _lock(db, supplier_id)
    await _section(db, supplier_id, section_id)
    last = await db.scalar(select(func.max(SupplierProduct.sort_order))
                           .where(SupplierProduct.section_id == section_id))
    product = SupplierProduct(supplier_id=supplier_id, section_id=section_id,
                              heading=heading.strip(), notes_before=notes_before,
                              notes_after=notes_after, sort_order=(last or 0) + 10)
    db.add(product)
    await db.flush()
    await audit.record(db, user_id=actor.user_id, supplier_user_id=actor.supplier_user_id,
                       action="catalog.product_add", entity="supplier_product",
                       entity_id=product.product_id, after={"heading": product.heading})
    return product


async def update_product(db, supplier_id, product_id, *, changes: dict,
                         actor: Actor) -> SupplierProduct:
    _no_nulls(changes)
    await _lock(db, supplier_id)
    product = await _product(db, supplier_id, product_id)
    if "section_id" in changes:
        await _section(db, supplier_id, changes["section_id"])
    if "heading" in changes:
        changes["heading"] = changes["heading"].strip()
    for k, v in changes.items():
        setattr(product, k, v)
    await db.flush()
    await audit.record(db, user_id=actor.user_id, supplier_user_id=actor.supplier_user_id,
                       action="catalog.product_update", entity="supplier_product",
                       entity_id=product_id,
                       after={k: str(v) for k, v in changes.items()})
    return product


async def add_offer(db, supplier_id, product_id, *, label: str, price: Decimal,
                    mrp: Decimal | None, units_per_box: int | None,
                    actor: Actor) -> SupplierOffer:
    await _lock(db, supplier_id)
    await _product(db, supplier_id, product_id)
    _check_money(price, mrp, units_per_box)
    last = await db.scalar(select(func.max(SupplierOffer.sort_order))
                           .where(SupplierOffer.product_id == product_id))
    offer = SupplierOffer(supplier_id=supplier_id, product_id=product_id, label=label.strip(),
                          price=price, mrp=mrp, units_per_box=units_per_box,
                          sort_order=(last or 0) + 10)
    db.add(offer)
    await db.flush()
    db.add(_history(offer, actor, actor.source))
    await audit.record(db, user_id=actor.user_id, supplier_user_id=actor.supplier_user_id,
                       action="catalog.offer_add", entity="supplier_offer",
                       entity_id=offer.offer_id, after={"price": str(price)})
    return offer


async def update_offer(db, supplier_id, offer_id, *, changes: dict,
                       actor: Actor) -> SupplierOffer:
    _no_nulls(changes, nullable=("mrp", "units_per_box"))
    await _lock(db, supplier_id)
    offer = await _offer(db, supplier_id, offer_id)
    price = changes.get("price", offer.price)
    mrp = changes["mrp"] if "mrp" in changes else offer.mrp
    units = changes["units_per_box"] if "units_per_box" in changes else offer.units_per_box
    _check_money(price, mrp, units)
    money_changed = (price, mrp, units) != (offer.price, offer.mrp, offer.units_per_box)
    before = {"price": str(offer.price), "mrp": str(offer.mrp), "units": offer.units_per_box}
    for k, v in changes.items():
        setattr(offer, k, v.strip() if k == "label" else v)
    await db.flush()
    if money_changed:
        db.add(_history(offer, actor, actor.source))
    await audit.record(db, user_id=actor.user_id, supplier_user_id=actor.supplier_user_id,
                       action="catalog.offer_update", entity="supplier_offer",
                       entity_id=offer_id, before=before,
                       after={k: str(v) for k, v in changes.items()})
    return offer


async def offer_history(db, supplier_id, offer_id) -> list[SupplierOfferPrice]:
    await _offer(db, supplier_id, offer_id)
    return list((await db.scalars(
        select(SupplierOfferPrice).where(SupplierOfferPrice.offer_id == offer_id)
        .order_by(SupplierOfferPrice.changed_at.desc(), SupplierOfferPrice.history_id.desc())
    )).all())
