"""Supplier offer -> Online product matches (PLAN.md decision 6).

- suggest(): for every active offer without a confirmed match, keep the top suggestions
  (never one a person already rejected for that offer).
- confirm(): a person says "this offer is that Online product" (one per offer).
- reject(): "not this one" -- never suggested again for that offer.
- unmatch(): undo a confirmation (kept as rejected, so it is not suggested back).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.matching import scorer
from app.models import (
    OfferMatch,
    OnlineProduct,
    OnlineVariant,
    SupplierOffer,
    SupplierProduct,
)
from app.services import audit
from app.services.errors import NotFound, SupplyError


def _now() -> datetime:
    return datetime.now(UTC)


def _candidate(p: OnlineProduct) -> scorer.Candidate:
    size = (Decimal(p.size_amount), p.size_kind) if p.size_amount is not None else None
    return scorer.Candidate(online_product_id=p.online_product_id, name=p.name,
                            name_si=p.name_si, size=size)


async def suggest(db: AsyncSession, supplier_id: uuid.UUID | None = None) -> dict:
    products = (await db.scalars(
        select(OnlineProduct).where(OnlineProduct.is_active.is_(True)))).all()
    candidates = [_candidate(p) for p in products]

    q = (select(SupplierOffer, SupplierProduct.heading)
         .join(SupplierProduct, SupplierProduct.product_id == SupplierOffer.product_id)
         .where(SupplierOffer.is_active.is_(True), SupplierProduct.is_active.is_(True)))
    if supplier_id:
        q = q.where(SupplierOffer.supplier_id == supplier_id)
    offers = (await db.execute(q)).all()

    existing = (await db.scalars(select(OfferMatch).where(
        OfferMatch.offer_id.in_([o.offer_id for o, _ in offers])))).all() if offers else []
    confirmed = {m.offer_id for m in existing if m.status == "confirmed"}
    rejected: dict[uuid.UUID, set] = defaultdict(set)
    for m in existing:
        if m.status == "rejected":
            rejected[m.offer_id].add(m.online_product_id)

    todo = [(o, h) for o, h in offers if o.offer_id not in confirmed]
    if todo:
        await db.execute(delete(OfferMatch).where(
            OfferMatch.status == "suggested",
            OfferMatch.offer_id.in_([o.offer_id for o, _ in todo])))
    made = with_any = 0
    for offer, heading in todo:
        ranked = scorer.best(heading, offer.label, candidates, exclude=rejected[offer.offer_id])
        if ranked:
            with_any += 1
        for cand, s in ranked:
            db.add(OfferMatch(offer_id=offer.offer_id, supplier_id=offer.supplier_id,
                              online_product_id=cand.online_product_id, status="suggested",
                              score=Decimal(str(s))))
            made += 1
    await db.flush()
    return {"offers": len(offers), "already_matched": len(confirmed),
            "with_suggestions": with_any, "suggestions": made,
            "online_products": len(candidates)}


async def _offer(db, offer_id: uuid.UUID, supplier_id: uuid.UUID | None) -> SupplierOffer:
    offer = await db.get(SupplierOffer, offer_id)
    if offer is None or (supplier_id and offer.supplier_id != supplier_id):
        raise NotFound("offer not found")
    return offer


async def confirm(db: AsyncSession, offer_id: uuid.UUID, *, online_product_id: uuid.UUID,
                  online_variant_id: uuid.UUID | None, user_id: uuid.UUID) -> OfferMatch:
    offer = await _offer(db, offer_id, None)
    product = await db.get(OnlineProduct, online_product_id)
    if product is None or not product.is_active:
        raise NotFound("Online product not found")
    if online_variant_id is not None:
        variant = await db.get(OnlineVariant, online_variant_id)
        if variant is None or variant.online_product_id != online_product_id:
            raise SupplyError("that pack size belongs to another product")
    now = _now()
    rows = (await db.scalars(select(OfferMatch).where(OfferMatch.offer_id == offer_id)
                             .with_for_update())).all()
    previous = None
    for m in rows:
        if m.status == "confirmed":
            previous = m.online_product_id
            m.status, m.decided_by, m.decided_at = "rejected", user_id, now
    await db.flush()
    target = next((m for m in rows if m.online_product_id == online_product_id
                   and m.online_variant_id == online_variant_id), None)
    other_suggestions = delete(OfferMatch).where(OfferMatch.offer_id == offer_id,
                                                 OfferMatch.status == "suggested")
    if target is not None:
        other_suggestions = other_suggestions.where(OfferMatch.match_id != target.match_id)
    await db.execute(other_suggestions)
    if target is None:
        target = OfferMatch(offer_id=offer_id, supplier_id=offer.supplier_id,
                            online_product_id=online_product_id,
                            online_variant_id=online_variant_id)
        db.add(target)
    target.status, target.decided_by, target.decided_at = "confirmed", user_id, now
    await db.flush()
    await audit.record(db, user_id=user_id, action="match.confirm", entity="supplier_offer",
                       entity_id=offer_id,
                       before={"online_product_id": str(previous)} if previous else None,
                       after={"online_product_id": str(online_product_id),
                              "online_variant_id": str(online_variant_id)
                              if online_variant_id else None})
    return target


async def reject(db: AsyncSession, offer_id: uuid.UUID, *, online_product_id: uuid.UUID,
                 user_id: uuid.UUID) -> None:
    await _offer(db, offer_id, None)
    rows = (await db.scalars(select(OfferMatch).where(
        OfferMatch.offer_id == offer_id,
        OfferMatch.online_product_id == online_product_id).with_for_update())).all()
    if not rows:
        offer = await db.get(SupplierOffer, offer_id)
        if await db.get(OnlineProduct, online_product_id) is None:
            raise NotFound("Online product not found")
        rows = [OfferMatch(offer_id=offer_id, supplier_id=offer.supplier_id,
                           online_product_id=online_product_id)]
        db.add(rows[0])
    for m in rows:
        m.status, m.decided_by, m.decided_at = "rejected", user_id, _now()
    await db.flush()
    await audit.record(db, user_id=user_id, action="match.reject", entity="supplier_offer",
                       entity_id=offer_id, after={"online_product_id": str(online_product_id)})


async def unmatch(db: AsyncSession, offer_id: uuid.UUID, *, user_id: uuid.UUID) -> None:
    await _offer(db, offer_id, None)
    m = await db.scalar(select(OfferMatch).where(OfferMatch.offer_id == offer_id,
                                                 OfferMatch.status == "confirmed"))
    if m is None:
        raise NotFound("this offer has no confirmed match")
    m.status, m.decided_by, m.decided_at = "rejected", user_id, _now()
    await db.flush()
    await audit.record(db, user_id=user_id, action="match.unmatch", entity="supplier_offer",
                       entity_id=offer_id, before={"online_product_id": str(m.online_product_id)})


async def queue(db: AsyncSession, supplier_id: uuid.UUID, *, state: str) -> list[dict]:
    """state: 'todo' (no confirmed match), 'matched', or 'all'."""
    rows = (await db.execute(
        select(SupplierOffer, SupplierProduct.heading)
        .join(SupplierProduct, SupplierProduct.product_id == SupplierOffer.product_id)
        .where(SupplierOffer.supplier_id == supplier_id, SupplierOffer.is_active.is_(True),
               SupplierProduct.is_active.is_(True))
        .order_by(SupplierProduct.sort_order, SupplierOffer.sort_order))).all()
    if not rows:
        return []
    matches = (await db.execute(
        select(OfferMatch, OnlineProduct)
        .join(OnlineProduct, OnlineProduct.online_product_id == OfferMatch.online_product_id)
        .where(OfferMatch.offer_id.in_([o.offer_id for o, _ in rows]),
               OfferMatch.status.in_(["suggested", "confirmed"]))
        .order_by(OfferMatch.score.desc().nullslast()))).all()
    by_offer: dict[uuid.UUID, list] = defaultdict(list)
    for m, p in matches:
        by_offer[m.offer_id].append((m, p))
    out = []
    for offer, heading in rows:
        found = by_offer[offer.offer_id]
        confirmed = next(((m, p) for m, p in found if m.status == "confirmed"), None)
        if state == "todo" and confirmed:
            continue
        if state == "matched" and not confirmed:
            continue
        out.append({
            "offer_id": offer.offer_id, "heading": heading, "label": offer.label,
            "price": offer.price, "mrp": offer.mrp, "units_per_box": offer.units_per_box,
            "confirmed": _product_out(*confirmed) if confirmed else None,
            "suggestions": [_product_out(m, p) for m, p in found if m.status == "suggested"],
        })
    return out


def _product_out(m: OfferMatch, p: OnlineProduct) -> dict:
    return {"online_product_id": p.online_product_id, "online_variant_id": m.online_variant_id,
            "name": p.name, "name_si": p.name_si, "sku": p.sku,
            "size": f"{p.display_size or ''} {p.display_size_unit or ''}".strip() or None,
            "score": m.score}
