"""Tell SRIBEESonline what arrived, so the shop's stock goes up (owner, 2026-11-26).

Online adds the received quantity to the branch (never sets a figure) and applies each
receipt line once, keyed by the line id, so sending a line again is always safe.

Receiving writes an ``online_stock_sends`` row per line only while ``ONLINE_STOCK_IN`` is
on, so receipts from before the switch are never sent. The worker sends the pending rows
(`run_once`) until Online answers; a refused line waits, with Online's reason, until staff
fix the cause and press "send again".

Contract: Online `app/api/supply_internal.py`, ``POST /internal/supply/receipts``.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.models import Branch, OnlineStockSend, OnlineVariant, Product, Receipt, ReceiptLine
from app.services import audit
from app.services.errors import Conflict, NotFound, SupplyError

CENT = Decimal("0.01")
MAX_LINES = 500  # Online's limit for one call
ROUND_RECEIPTS = 20
TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class Arrived:
    """One received line, in Supply's own terms."""

    line_id: uuid.UUID  # the receipt line: Online's idempotency key
    online_product_id: uuid.UUID
    online_variant_id: uuid.UUID  # = online_product_id for a plain product's default size
    units: int  # paid units
    free_units: int
    value: Decimal  # goods value plus the line's transport share
    weight_kg: Decimal | None = None  # goods kept by weight
    listed_price: Decimal | None = None  # typed at receiving; None = keep Online's
    selling_price: Decimal | None = None
    expiry_date: date | None = None


def payload_line(a: Arrived) -> dict:
    arrived = a.units + a.free_units
    if arrived <= 0:
        raise SupplyError("a received line needs at least one unit")
    out: dict = {
        "line_id": str(a.line_id),
        "product_id": str(a.online_product_id),
        # Supply gives a product without pack sizes a default one carrying its own id;
        # Online knows no such variant.
        "variant_id": (None if a.online_variant_id == a.online_product_id
                       else str(a.online_variant_id)),
        "units": arrived,
        "purchase_cost": str((a.value / arrived).quantize(CENT, rounding=ROUND_HALF_UP)),
    }
    if a.weight_kg is not None:
        out["grams"] = int((a.weight_kg * 1000).to_integral_value(rounding=ROUND_HALF_UP))
    if a.listed_price is not None:
        out["listed_price"] = str(a.listed_price)
    if a.selling_price is not None:
        out["selling_price"] = str(a.selling_price)
    if a.expiry_date is not None:
        out["expires_on"] = a.expiry_date.isoformat()
    return out


@dataclass(frozen=True)
class Answer:
    line_id: uuid.UUID
    sent: bool  # Online holds this line's stock (added now, or already before)
    reason: str | None = None  # why Online refused it


async def _post(client: httpx.AsyncClient, path: str, body: dict) -> dict:
    s = get_settings()
    if not s.online_api_url or not s.online_service_token:
        raise SupplyError("the link to SRIBEESonline is not set up yet")
    try:
        r = await client.post(f"{s.online_api_url.rstrip('/')}{path}", json=body,
                              timeout=TIMEOUT_SECONDS,
                              headers={"Authorization": f"Bearer {s.online_service_token}"})
    except httpx.HTTPError as exc:
        raise SupplyError(f"SRIBEESonline cannot be reached ({type(exc).__name__})") from None
    if r.status_code == 404 and path.endswith("/receipts"):
        raise SupplyError("SRIBEESonline does not take received stock yet (switched off)")
    if r.status_code != 200:
        raise SupplyError(f"SRIBEESonline answered {r.status_code}")
    return r.json()


async def send(client: httpx.AsyncClient, online_branch_id: uuid.UUID, receipt_no: str,
               lines: Sequence[Arrived]) -> list[Answer]:
    """Send one receipt's lines. Raises SupplyError when nothing is known to have been
    taken (retry the same lines later)."""
    answers: list[Answer] = []
    for start in range(0, len(lines), MAX_LINES):
        chunk = lines[start:start + MAX_LINES]
        body = await _post(client, "/internal/supply/receipts", {
            "branch_id": str(online_branch_id), "receipt_no": receipt_no,
            "lines": [payload_line(a) for a in chunk]})
        results = {r.get("line_id"): r for r in body.get("results") or []}
        for a in chunk:
            r = results.get(str(a.line_id))
            if r is None:
                raise SupplyError("SRIBEESonline did not answer for every line")
            ok = r.get("status") in ("added", "already")
            answers.append(Answer(a.line_id, ok, None if ok else
                                  str(r.get("reason") or "refused")[:300]))
    return answers


async def stock_info(client: httpx.AsyncClient, online_branch_id: uuid.UUID,
                     items: Sequence[tuple[uuid.UUID, uuid.UUID]]) -> dict:
    """Online's current stock and selling price for (product id, variant id) pairs."""
    return await _post(client, "/internal/supply/stock-info", {
        "branch_id": str(online_branch_id),
        "items": [{"product_id": str(p), "variant_id": None if v == p else str(v)}
                  for p, v in items]})


# ---------------------------------------------------------------- the worker's side
def enabled() -> bool:
    s = get_settings()
    return bool(s.online_stock_in and s.online_api_url and s.online_service_token)


async def _send_receipt(db: AsyncSession, client: httpx.AsyncClient,
                        receipt_id: uuid.UUID) -> dict[str, int]:
    rows = {r.receipt_line_id: r for r in await db.scalars(
        select(OnlineStockSend).where(OnlineStockSend.receipt_id == receipt_id,
                                      OnlineStockSend.status == "pending")
        .with_for_update(skip_locked=True))}
    if not rows:
        return {}
    receipt = await db.get(Receipt, receipt_id)
    branch = await db.get(Branch, receipt.branch_id)

    def settle(line_id: uuid.UUID, status: str, reason: str | None) -> None:
        row = rows[line_id]
        row.status, row.reason, row.attempts = status, reason, row.attempts + 1

    counts: dict[str, int] = {}
    lines: list[Arrived] = []
    found = await db.execute(
        select(ReceiptLine, Product, OnlineVariant)
        .join(Product, Product.product_id == ReceiptLine.product_id)
        .outerjoin(OnlineVariant, OnlineVariant.online_variant_id == ReceiptLine.product_id)
        .where(ReceiptLine.receipt_line_id.in_(rows)).order_by(ReceiptLine.line_no))
    for line, product, variant in found:
        why = None
        if branch.online_branch_id is None:
            why = f"branch {branch.name} is not linked to a SRIBEESonline branch"
        elif variant is None:
            why = f"{product.name} is not an Online pack size"
        if why:
            settle(line.receipt_line_id, "refused", why[:300])
            counts["refused"] = counts.get("refused", 0) + 1
            continue
        lines.append(Arrived(
            line_id=line.receipt_line_id, online_product_id=variant.online_product_id,
            online_variant_id=variant.online_variant_id, units=line.units,
            free_units=line.free_units, value=line.line_value + line.landed_cost,
            weight_kg=line.qty if product.unit == "kg" else None,
            listed_price=line.listed_price, selling_price=line.selling_price,
            expiry_date=line.expiry_date))
    if not lines:
        return counts
    try:
        answers = await send(client, branch.online_branch_id, receipt.receipt_no, lines)
    except SupplyError as exc:  # nothing is known to be taken: the same lines go again
        for a in lines:
            settle(a.line_id, "pending", str(exc)[:300])
        counts["waiting"] = len(lines)
        return counts
    for a in answers:
        status = "sent" if a.sent else "refused"
        settle(a.line_id, status, a.reason)
        counts[status] = counts.get(status, 0) + 1
    return counts


async def run_once(sessionmaker: async_sessionmaker, client: httpx.AsyncClient) -> dict:
    """Send what is waiting, a receipt at a time (each in its own transaction), the least
    tried first so one that keeps failing never starves the others."""
    if not enabled():
        return {}
    async with sessionmaker() as db:
        ids = list(await db.scalars(
            select(OnlineStockSend.receipt_id).where(OnlineStockSend.status == "pending")
            .group_by(OnlineStockSend.receipt_id)
            .order_by(func.min(OnlineStockSend.attempts), func.min(OnlineStockSend.created_at))
            .limit(ROUND_RECEIPTS)))
    totals: dict[str, int] = {}
    for receipt_id in ids:
        async with sessionmaker() as db, db.begin():
            for key, n in (await _send_receipt(db, client, receipt_id)).items():
                totals[key] = totals.get(key, 0) + n
    return totals


async def retry(db: AsyncSession, receipt_id: uuid.UUID, *, user_id: uuid.UUID,
                scope: set[uuid.UUID] | None = None) -> int:
    """Staff fixed what Online refused: queue those lines again."""
    receipt = await db.get(Receipt, receipt_id)
    if receipt is None or (scope is not None and receipt.branch_id not in scope):
        raise NotFound("receipt not found")
    done = await db.execute(update(OnlineStockSend).where(
        OnlineStockSend.receipt_id == receipt_id, OnlineStockSend.status == "refused")
        .values(status="pending", reason=None))
    if not done.rowcount:
        raise Conflict("nothing on this receipt was refused")
    await audit.record(db, user_id=user_id, action="online_stock.retry", entity="receipt",
                       entity_id=receipt_id, after={"lines": done.rowcount})
    return done.rowcount
