"""Price reviews for delivered batches (owner plan 2026-11-27, P6).

SRIBEES Online makes each dated delivery its own batch. When the new batch's price is not
today's (a new MRP: Online keeps the same discount) or it sells below what we paid, the
branch's manager sees it: photo, name, what we paid, old and new MRP and selling price.
They submit it as it is or with their own selling price; Online's batch then sells at it.
The automatic price stands until then: sales never wait for a review.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Branch, OnlineProduct, OnlineVariant, PriceReview, Receipt
from app.services import audit
from app.services.actors import Actor
from app.services.errors import Conflict, Forbidden, NotFound, SupplyError

CENT = Decimal("0.01")


def _d(v) -> Decimal | None:
    return None if v in (None, "") else Decimal(str(v))


async def note(db: AsyncSession, receipt: Receipt, arrived, batch: dict) -> PriceReview | None:
    """Called when Online answered "added" with a new batch for a receipt line."""
    listed, selling = _d(batch.get("listed_price")), _d(batch.get("selling_price"))
    old_listed, old_selling = _d(batch.get("old_listed_price")), _d(batch.get("old_selling_price"))
    if listed is None or selling is None:
        return None
    paid_units = max(arrived.units, 1)
    base = (arrived.value / paid_units).quantize(Decimal("0.0001"))
    changed = (listed, selling) != (old_listed, old_selling)
    if not changed and selling >= base:
        return None
    if await db.get(PriceReview, arrived.line_id) is not None:
        return None
    row = PriceReview(
        receipt_line_id=arrived.line_id, branch_id=receipt.branch_id,
        online_batch_id=uuid.UUID(str(batch["batch_id"])),
        online_product_id=arrived.online_product_id, online_variant_id=arrived.online_variant_id,
        base_price=base, old_listed_price=old_listed, old_selling_price=old_selling,
        listed_price=listed, selling_price=selling)
    db.add(row)
    await db.flush()
    return row


async def waiting(db: AsyncSession, scope: set[uuid.UUID] | None) -> list[dict]:
    q = (select(PriceReview, OnlineProduct, OnlineVariant, Branch)
         .join(OnlineProduct, OnlineProduct.online_product_id == PriceReview.online_product_id)
         .outerjoin(OnlineVariant,
                    OnlineVariant.online_variant_id == PriceReview.online_variant_id)
         .join(Branch, Branch.branch_id == PriceReview.branch_id)
         .where(PriceReview.status.in_(("waiting", "failed")))
         .order_by(PriceReview.created_at))
    if scope is not None:
        q = q.where(PriceReview.branch_id.in_(scope))
    out = []
    for r, p, v, b in (await db.execute(q)).all():
        out.append({
            "receipt_line_id": r.receipt_line_id, "branch_id": r.branch_id,
            "branch_name": b.name, "name": p.name, "name_si": p.name_si,
            "variant": v.name if v else "", "image_url": p.image_url,
            "base_price": f"{r.base_price:.2f}" if r.base_price is not None else None,
            "old_listed_price": _s(r.old_listed_price),
            "old_selling_price": _s(r.old_selling_price),
            "listed_price": _s(r.listed_price), "selling_price": _s(r.selling_price),
            "below_cost": r.base_price is not None and r.selling_price < r.base_price,
            "status": r.status, "reason": r.reason, "created_at": r.created_at,
        })
    return out


def _s(v: Decimal | None) -> str | None:
    return None if v is None else f"{v:.2f}"


@dataclass(frozen=True)
class Decision:
    receipt_line_id: uuid.UUID
    selling_price: Decimal  # the price to keep (the same = keep the automatic one)


async def submit(db: AsyncSession, actor: Actor, decision: Decision, *,
                 scope: set[uuid.UUID] | None, can_sell_below_cost: bool,
                 client: httpx.AsyncClient, post) -> dict:
    """Apply one reviewed price. `post(client, path, body)` reaches Online."""
    row = await db.get(PriceReview, decision.receipt_line_id, with_for_update=True)
    if row is None or (scope is not None and row.branch_id not in scope):
        raise NotFound("price review not found")
    if row.status not in ("waiting", "failed"):
        raise Conflict("this price was already reviewed")
    price = decision.selling_price.quantize(CENT)
    if not (Decimal("0") < price <= row.listed_price):
        raise SupplyError(f"the selling price must be above 0 and not above the MRP "
                          f"(Rs {row.listed_price:.2f})")
    if row.base_price is not None and price < row.base_price and not can_sell_below_cost:
        raise Forbidden(f"Rs {price:.2f} is below what we paid (Rs {row.base_price:.2f}); "
                        "only the owner sells below cost")
    branch = await db.get(Branch, row.branch_id)
    if price != row.selling_price:
        if branch is None or branch.online_branch_id is None:
            raise SupplyError("this branch is not linked to a SRIBEES Online branch")
        # Online first: if it cannot be reached, the review stays waiting (the request
        # rolls back) and the manager sees why
        await post(client, f"/internal/supply/batches/{row.online_batch_id}/price",
                   {"branch_id": str(branch.online_branch_id), "selling_price": str(price)})
    before = {"selling_price": _s(row.selling_price)}
    row.status = "kept" if price == row.selling_price else "changed"
    row.final_price, row.reason = price, None
    row.reviewed_by, row.reviewed_at = actor.user_id, datetime.now(UTC)
    await db.flush()
    await audit.record(db, user_id=actor.user_id, action="price_review." + row.status,
                       entity="price_review", entity_id=row.receipt_line_id, before=before,
                       after={"selling_price": _s(price)}, ip_address=actor.ip_address)
    return {"receipt_line_id": row.receipt_line_id, "status": row.status,
            "selling_price": _s(price)}
