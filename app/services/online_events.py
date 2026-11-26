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
from app.models import OnlineEventFailure, SyncState
from app.services import picks

log = logging.getLogger("supply.online_events")
KEY = "online_events"


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


async def run_once(sessionmaker: async_sessionmaker, client: httpx.AsyncClient,
                   limit: int = 200) -> dict:
    s = get_settings()
    after = await _position(sessionmaker)
    r = await client.get(f"{s.online_api_url.rstrip('/')}/internal/supply/events",
                         params={"after": after, "limit": limit},
                         headers={"Authorization": f"Bearer {s.online_service_token}"},
                         timeout=20)
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
