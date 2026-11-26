"""Worker round: read SRIBEESonline's order events (``GET /internal/supply/events``) and
settle pack-scan picks (app/services/picks.py). Each event is applied in its own
transaction together with the new read position, so a crash never applies one twice and
never skips one."""

import logging

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import get_settings
from app.models import SyncState
from app.services import picks

log = logging.getLogger("supply.online_events")
KEY = "online_events"


def enabled() -> bool:
    s = get_settings()
    return bool(s.online_api_url and s.online_service_token)


async def run_once(sessionmaker: async_sessionmaker, client: httpx.AsyncClient,
                   limit: int = 200) -> dict:
    s = get_settings()
    async with sessionmaker() as db:
        state = await db.get(SyncState, KEY)
        after = int((state.value if state else {}).get("seq", 0))
    r = await client.get(f"{s.online_api_url.rstrip('/')}/internal/supply/events",
                         params={"after": after, "limit": limit},
                         headers={"Authorization": f"Bearer {s.online_service_token}"},
                         timeout=20)
    r.raise_for_status()
    body = r.json()
    counts: dict[str, int] = {}
    for ev in body.get("events") or []:
        async with sessionmaker() as db, db.begin():
            state = await db.get(SyncState, KEY, with_for_update=True)
            if state is None:
                state = SyncState(key=KEY, value={"seq": 0})
                db.add(state)
            if int(state.value.get("seq", 0)) >= int(ev["seq"]):
                continue  # another round got here first
            what = await picks.apply_event(db, ev)
            state.value = {"seq": int(ev["seq"])}
        kind = what.split(":")[0]
        counts[kind] = counts.get(kind, 0) + 1
    return {k: v for k, v in counts.items() if k != "ignored"}
