"""Worker round: read SRIBEESonline's order events (``GET /internal/supply/events``) and
settle pack-scan picks (app/services/picks.py).

- Each event is applied in its own transaction together with the new read position, so a
  crash never applies one twice and never skips one.
- A hole in the numbering is waited for (ONLINE_EVENTS_GAP_GRACE_SECONDS): Online numbers an
  event when it is written, and a slower transaction can commit a lower number later
  (review 2026-11-26 #1). Online lists every event (others without their details), so a
  hole is a real hole.
- An event that cannot be applied is kept in online_event_failures and the position moves
  on: one bad event never holds up the deliveries and cancellations after it (review #2).
"""

import logging
import uuid
from datetime import UTC, datetime

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import get_settings
from app.models import OnlineEventFailure, PartnerEventFailure, SyncState
from app.services import audit, partner_requests, picks

log = logging.getLogger("supply.online_events")
KEY = "online_events"
_last_missing = 0.0  # when we last said Online has no /internal/supply/events yet


def enabled() -> bool:
    s = get_settings()
    return bool(s.online_api_url and s.online_service_token)


def _age(ev: dict) -> float:
    try:
        at = datetime.fromisoformat(ev["occurred_at"])
    except (KeyError, TypeError, ValueError):
        return float("inf")
    return (datetime.now(UTC) - at).total_seconds()


async def _position(sessionmaker) -> int:
    async with sessionmaker() as db:
        state = await db.get(SyncState, KEY)
        return int((state.value if state else {}).get("seq", 0))


async def _advance(db, seq: int) -> bool:
    state = await db.get(SyncState, KEY, with_for_update=True)
    if state is None:
        state = SyncState(key=KEY, value={"seq": 0})
        db.add(state)
    if int(state.value.get("seq", 0)) >= seq:
        return False  # another round got here first
    state.value = {"seq": seq}
    return True


async def _branch_of(db, ev: dict):
    """Our branch for an Online event's branch (to scope its failure for staff)."""
    from sqlalchemy import select  # noqa: PLC0415

    from app.models import Branch  # noqa: PLC0415

    online = ((ev.get("payload") or {}).get("branch") or {}).get("branch_id")
    try:
        return await db.scalar(select(Branch.branch_id).where(
            Branch.online_branch_id == uuid.UUID(str(online)))) if online else None
    except ValueError:
        return None


async def _partner(db, ev: dict) -> str:
    """The fulfilment partner's part of an event (app/services/partner_requests.py), in a
    savepoint: if it fails, the pack-scan work of the same event still commits (plan G7)."""
    try:
        async with db.begin_nested():
            return await partner_requests.apply_event(db, ev)
    except Exception as exc:  # noqa: BLE001 -- kept with its payload to retry; the shop goes on
        log.exception("partner part of online event %s failed", ev.get("seq"))
        error = str(getattr(exc, "message", exc))[:2000]
        try:
            order_id = uuid.UUID(str((ev.get("payload") or {}).get("order_id")))
        except ValueError:
            order_id = None
        kept = {**ev, "_own_back": dict(db.info.get("own_back") or {})}  # for the retry
        db.add(PartnerEventFailure(seq=ev.get("seq"), event_type=str(ev.get("type"))[:40],
                                   order_id=order_id, branch_id=await _branch_of(db, ev),
                                   payload=kept, error=error))
        await audit.record(db, action="partner.event_failed", entity="online_event",
                           entity_id=ev.get("seq"),
                           after={"type": ev.get("type"), "error": error[:500]})
        return "partner:failed"


async def run_once(sessionmaker: async_sessionmaker, client: httpx.AsyncClient,
                   limit: int = 200) -> dict:
    s = get_settings()
    after = await _position(sessionmaker)
    r = await client.get(f"{s.online_api_url.rstrip('/')}/internal/supply/events",
                         params={"after": after, "limit": limit},
                         headers={"Authorization": f"Bearer {s.online_service_token}"},
                         timeout=20)
    if r.status_code == 404:
        # Online does not serve the pack-scan events yet (its release comes later): say so
        # once an hour instead of a traceback every round.
        global _last_missing
        import time

        if time.monotonic() - _last_missing > 3600:
            _last_missing = time.monotonic()
            log.warning("Online has no /internal/supply/events yet; pack-scan holds wait")
        return {}
    r.raise_for_status()
    body = r.json()
    if body.get("enabled") is False:
        log.warning("Online's order events are switched off (FINANCE_OUTBOX_ENABLED); "
                    "pack-scan holds are not settled")
    counts: dict[str, int] = {}
    expected = after + 1
    for ev in body.get("events") or []:
        seq = int(ev["seq"])
        if seq > expected and _age(ev) < s.online_events_gap_grace_seconds:
            counts["waiting"] = 1
            break  # an earlier event may still commit on Online
        try:
            async with sessionmaker() as db, db.begin():
                if not await _advance(db, seq):
                    expected = seq + 1
                    continue
                what = await picks.apply_event(db, ev)
                extra = await _partner(db, ev)
                if extra != "ignored":
                    what = extra if what == "ignored" else f"{what}+{extra}"
        except Exception as exc:  # noqa: BLE001 -- kept for staff, position moves on
            log.exception("online event %s (%s) failed", seq, ev.get("type"))
            p = ev.get("payload") or {}
            async with sessionmaker() as db, db.begin():
                if await _advance(db, seq):
                    try:
                        order_id = uuid.UUID(str(p.get("order_id")))
                    except ValueError:
                        order_id = None
                    db.add(OnlineEventFailure(
                        seq=seq, event_id=ev.get("event_id"), event_type=str(ev.get("type"))[:40],
                        order_id=order_id, error=str(getattr(exc, "message", exc))[:2000],
                        payload=ev))
            what = "failed"
        expected = seq + 1
        kind = what.split(":")[0]
        counts[kind] = counts.get(kind, 0) + 1
    return {k: v for k, v in counts.items() if k != "ignored"}
