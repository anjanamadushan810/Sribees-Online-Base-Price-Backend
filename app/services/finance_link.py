"""Supply <-> Finance (docs/plans/joint-go-live.md, phase A).

Both directions are pull feeds over an append-only outbox, the same pattern as
SRIBEESonline -> Finance:

* Finance reads Supply's events (``GET /internal/finance/events``, served from
  ``supply_outbox``): an order submitted, cancelled, accepted or declined.
* Supply reads Finance's answers (``GET <finance>/internal/supply/events``):
  ``po.created`` (Finance's PO number), ``po.approved`` and ``po.rejected``.

Each Finance event is applied in one transaction with its ``processed_events`` row and
the cursor, so it is applied once or not at all, and a replay changes nothing.
A sequence number missing from Finance's feed is waited for (a late commit) for
``finance_feed_gap_grace_seconds`` and then skipped (a rolled-back transaction).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.models import FeedState, OutboxEvent, ProcessedEvent, SupplyOrder
from app.services import audit, orders

log = logging.getLogger("supply.finance_feed")
FEED = "finance"
LOCK_KEY = 0x5B1EED  # pg advisory lock: one consumer at a time
MAX_LIMIT = 1000


# ---------------------------------------------------------------- our outbox, served
def envelope(row: OutboxEvent) -> dict:
    payload = row.payload or {}
    return {
        "seq": row.seq,
        "event_id": str(row.event_id),
        "type": row.event,
        "aggregate_id": payload.get("order_id"),
        "occurred_at": row.created_at.isoformat(),
        "payload": payload,
    }


async def events_after(db: AsyncSession, after: int, limit: int) -> list[dict]:
    limit = max(1, min(limit, MAX_LIMIT))
    rows = await db.scalars(select(OutboxEvent).where(OutboxEvent.seq > after)
                            .order_by(OutboxEvent.seq).limit(limit))
    return [envelope(r) for r in rows]


# ---------------------------------------------------------------- Finance's answers
async def _order(db: AsyncSession, p: dict) -> SupplyOrder | None:
    try:
        order_id = uuid.UUID(str(p.get("order_id")))
    except ValueError:
        return None
    return await db.scalar(select(SupplyOrder).where(SupplyOrder.order_id == order_id)
                           .with_for_update())


async def apply(db: AsyncSession, ev: dict) -> str:
    """Apply one Finance event. Returns the outcome stored with it."""
    kind, p = ev["type"], ev.get("payload") or {}
    if kind not in ("po.created", "po.approved", "po.rejected"):
        return "ignored"
    order = await _order(db, p)
    if order is None:
        log.warning("Finance event %s names an unknown order %s", ev.get("seq"), p.get("order_id"))
        return "unknown"
    if p.get("po_no"):
        order.finance_po_no = str(p["po_no"])[:20]
    if kind == "po.created":
        return "applied"
    if order.status != "submitted":
        # Cancelled here before Finance answered (Finance hears of it next round), or a
        # replayed answer: nothing more to do.
        return "stale"
    if kind == "po.approved":
        await db.refresh(order, ["lines"])
        order.approved_by_name = (p.get("approved_by_name") or "Finance")[:150]
        await orders._approve(db, order, None)
        return "applied"
    reason = (p.get("reason") or "").strip() or "rejected in Finance"
    order.status, order.reject_reason, order.rejected_at = "rejected", reason, datetime.now(UTC)
    await audit.record(db, action="order.reject", entity="order", entity_id=order.order_id,
                       after={"reason": reason, "by": p.get("rejected_by_name"),
                              "po_no": order.finance_po_no})
    return "applied"


# ---------------------------------------------------------------- the consumer
async def fetch(client: httpx.AsyncClient, after: int, limit: int = 500) -> list[dict]:
    s = get_settings()
    r = await client.get(
        f"{s.finance_api_url.rstrip('/')}/internal/supply/events",
        params={"after": after, "limit": limit},
        headers={"Authorization": f"Bearer {s.finance_feed_token}"},
        timeout=20,
    )
    r.raise_for_status()
    return sorted(r.json()["events"], key=lambda e: e["seq"])


async def book(sessionmaker: async_sessionmaker, ev: dict) -> str:
    event_id = uuid.UUID(ev["event_id"])
    async with sessionmaker() as db, db.begin():
        state = await db.get(FeedState, FEED, with_for_update=True)
        if await db.get(ProcessedEvent, event_id) is not None:
            state.last_seq = max(state.last_seq, ev["seq"])
            return "duplicate"
        outcome = await apply(db, ev)
        db.add(ProcessedEvent(event_id=event_id, feed=FEED, seq=ev["seq"],
                              event_type=ev["type"][:40], outcome=outcome))
        state.last_seq = max(state.last_seq, ev["seq"])
        state.last_event_at = datetime.fromisoformat(ev["occurred_at"])
        return outcome


async def run_once(sessionmaker: async_sessionmaker, client: httpx.AsyncClient,
                   now: datetime | None = None) -> dict:
    """One round: fetch after the cursor, apply in order, stop at a fresh gap."""
    s = get_settings()
    now = now or datetime.now(UTC)
    grace = timedelta(seconds=s.finance_feed_gap_grace_seconds)
    counts: dict[str, int] = {}
    async with sessionmaker() as db, db.begin():
        state = await db.get(FeedState, FEED, with_for_update=True)
        cursor, gaps = state.last_seq, dict(state.gaps or {})
        state.last_polled_at = now
    try:
        events = await fetch(client, cursor)
    except httpx.HTTPError as exc:
        await _set_error(sessionmaker, f"cannot read the Finance feed: {exc}")
        raise
    for ev in events:
        if ev["seq"] <= cursor:
            continue
        missing = list(range(cursor + 1, ev["seq"]))
        if missing:
            for n in missing:
                gaps.setdefault(str(n), now.isoformat())
            if any(now - datetime.fromisoformat(gaps[str(n)]) < grace for n in missing):
                counts["waiting_on_gap"] = len(missing)
                break
            log.info("Finance sequence numbers %s never committed; moving past them", missing)
        for n in missing:
            gaps.pop(str(n), None)
        gaps.pop(str(ev["seq"]), None)
        outcome = await book(sessionmaker, ev)
        counts[outcome] = counts.get(outcome, 0) + 1
        cursor = ev["seq"]
    async with sessionmaker() as db, db.begin():
        state = await db.get(FeedState, FEED, with_for_update=True)
        state.gaps = gaps
        state.last_error = None
        state.updated_at = now
    return counts


async def _set_error(sessionmaker: async_sessionmaker, message: str) -> None:
    async with sessionmaker() as db, db.begin():
        state = await db.get(FeedState, FEED, with_for_update=True)
        state.last_error = message[:2000]
        state.updated_at = datetime.now(UTC)


async def try_lock(db: AsyncSession) -> bool:
    return bool(await db.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}))
