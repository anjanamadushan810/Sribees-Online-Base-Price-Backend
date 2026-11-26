"""Repack (joint-go-live plan B2): goods kept by weight (a supplier's rice or flour bag) are
packed into Online pack sizes (1 kg, 500 g ...). The kilograms leave the bag's batch and
each pack size becomes a new batch at its share of the cost, by weight, with the bag's
expiry; a QR label goes on each pack. Value only moves inside inventory (1300 -> 1300,
same branch), so no journal is needed.

Waste (spilled, sweepings) up to repack_normal_waste_pct of the kilograms stays in the
packs' cost; more than that is refused here (write it off instead, so it is approved).
"""

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OnlineVariant, Product, StockBatch
from app.services import audit, books, labels, settings, stock
from app.services.actors import Actor
from app.services.errors import Conflict, NotFound, SupplyError
from app.services.receiving import ensure_product

ZERO = Decimal("0")
KG = Decimal("1000")


@dataclass(frozen=True)
class RepackOut:
    online_variant_id: uuid.UUID
    packs: int
    labels: tuple[str, ...] = field(default_factory=tuple)


async def repack(db: AsyncSession, actor: Actor, *, batch_id: uuid.UUID, kg_used: Decimal,
                 outputs: list[RepackOut], repack_date: date, notes: str | None = None,
                 scope: set[uuid.UUID] | None = None) -> dict:
    source = await db.get(StockBatch, batch_id, with_for_update=True)
    if source is None or (scope is not None and source.branch_id not in scope):
        raise NotFound("batch not found")
    bulk = await db.get(Product, source.product_id)
    if bulk.unit != "kg":
        raise SupplyError(f"{bulk.name} is counted in pieces; only goods kept by weight "
                          "are repacked")
    if source.status != "available":
        raise Conflict(f"batch {source.batch_no} is in {source.status}")
    if repack_date > date.today() + timedelta(days=1):
        raise SupplyError("the repack date is in the future")
    await books.open_period(db, repack_date)
    if source.expiry_date is not None and source.expiry_date <= repack_date:
        raise SupplyError(f"batch {source.batch_no} expired on {source.expiry_date}; "
                          "it cannot be packed for sale")
    kg_used = stock.quantity(kg_used, bulk)
    if kg_used <= 0:
        raise SupplyError("enter the kilograms taken from the bag")
    if not outputs or len({o.online_variant_id for o in outputs}) != len(outputs):
        raise SupplyError("list each pack size once")
    packed_g = ZERO
    plan = []
    for o in outputs:
        variant = await db.get(OnlineVariant, o.online_variant_id)
        if variant is None or not variant.is_active or not variant.weight_grams:
            raise SupplyError("a pack size needs its weight in the Online catalogue")
        if o.packs <= 0:
            raise SupplyError("packs must be above zero")
        if o.labels and len(o.labels) != o.packs:
            raise SupplyError(f"{variant.name}: {o.packs} packs need {o.packs} labels, "
                              f"{len(o.labels)} scanned")
        grams = Decimal(variant.weight_grams * o.packs)
        packed_g += grams
        plan.append((o, variant, grams))
    used_g = kg_used * KG
    if packed_g > used_g:
        raise SupplyError(f"{packed_g / KG} kg of packs from {kg_used} kg: weigh again")
    waste = (used_g - packed_g) / KG
    normal = await settings.get_value(db, "repack_normal_waste_pct")
    if waste > kg_used * normal / 100:
        raise SupplyError(f"{waste} kg waste is more than the normal {normal}%; write the "
                          "extra off instead, so it is approved")
    if await labels.required(db) and any(not o.labels for o, _, _ in plan):
        raise SupplyError("scan a QR label on each pack")

    value_out = await stock.issue_value(db, source, kg_used)  # negative, at the bag's cost
    doc_id = uuid.uuid4()
    no = await settings.next_doc_no(db, "RPK")
    await stock.move(db, batch=source, move_type="repack_out", qty=-kg_used, value=value_out,
                     move_date=repack_date, doc_type="repack", doc_id=doc_id,
                     user_id=actor.user_id)
    total = -value_out
    made = []
    left = total
    for n, (o, variant, grams) in enumerate(plan, start=1):
        product = await ensure_product(db, variant.online_variant_id)
        value = left if n == len(plan) else stock.money(total * grams / packed_g)
        left -= value
        qty = stock.quantity(Decimal(o.packs), product)
        batch = await stock.create_batch(
            db, batch_no=f"{no}-{n}", branch_id=source.branch_id, product=product,
            supplier_id=source.supplier_id, received_date=repack_date,
            expiry_date=source.expiry_date, qty=qty, value=value)
        await stock.move(db, batch=batch, move_type="repack_in", qty=qty, value=value,
                         move_date=repack_date, doc_type="repack", doc_id=doc_id,
                         user_id=actor.user_id)
        if o.labels:
            await labels.link(db, actor, batch=batch, numbers=list(o.labels))
        made.append({"batch_id": batch.batch_id, "batch_no": batch.batch_no,
                     "product_id": product.product_id, "packs": o.packs, "value": value})
    await audit.record(db, user_id=actor.user_id, action="repack.post", entity="stock_batch",
                       entity_id=source.batch_id,
                       after={"repack_no": no, "kg_used": str(kg_used), "waste_kg": str(waste),
                              "value": str(total), "outputs": len(made)},
                       ip_address=actor.ip_address)
    return {"repack_no": no, "kg_used": kg_used, "waste_kg": waste, "value": total,
            "outputs": made}
