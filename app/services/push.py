"""Supplier app alerts over FCM HTTP v1 (Firebase project sribeesonline).

The push round reads `supplier.notify` events from supply_outbox after its cursor
(feed_state 'push'; a sequence gap waits `push_gap_grace_seconds`, like the feeds) and
sends one alert to every phone signed in for that supplier. A token FCM no longer
knows is dropped. Sending is best effort: an alert that cannot be sent is logged and
the round moves on; the supplier still sees the order on the Orders tab.

Auth: a Google service account (FCM_CREDENTIALS_JSON, the same account Online uses)
signs a JWT (RS256) that is swapped for a one-hour OAuth token. No new dependency.
Never SMS (owner, 2026-11-12).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import jwt
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.models import (
    FeedState,
    OutboxEvent,
    SupplierDevice,
    SupplierSession,
    SupplierUser,
)

log = logging.getLogger("supply.push")
FEED = "push"
SCOPE = "https://www.googleapis.com/auth/firebase.messaging"

# What the supplier reads (Sinhala first: the app's default language).
TEXT = {
    "new_order": ("නව ඇණවුමක් · New order",
                  "{order_no}: Sribees ඇණවුමක් එවා ඇත. / Sribees sent an order."),
    "order_cancelled": ("ඇණවුම අවලංගුයි · Order cancelled",
                        "{order_no}: Sribees ඇණවුම අවලංගු කළා. / Sribees cancelled this order."),
}

_token: dict = {"value": None, "expires": 0.0}


def _credentials() -> dict | None:
    raw = get_settings().fcm_credentials_json.strip()
    if not raw:
        return None
    try:
        info = json.loads(raw)
    except ValueError:
        log.error("FCM_CREDENTIALS_JSON is not JSON; alerts are off")
        return None
    if not all(info.get(k) for k in ("client_email", "private_key", "project_id")):
        log.error("FCM_CREDENTIALS_JSON lacks client_email/private_key/project_id")
        return None
    return info


def enabled() -> bool:
    return _credentials() is not None


async def _access_token(client: httpx.AsyncClient, info: dict) -> str:
    if _token["value"] and _token["expires"] > time.time() + 60:
        return _token["value"]
    now = int(time.time())
    assertion = jwt.encode(
        {"iss": info["client_email"], "scope": SCOPE, "aud": info.get("token_uri") or
         "https://oauth2.googleapis.com/token", "iat": now, "exp": now + 3600},
        info["private_key"], algorithm="RS256")
    r = await client.post(info.get("token_uri") or "https://oauth2.googleapis.com/token",
                          data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                "assertion": assertion}, timeout=20)
    r.raise_for_status()
    body = r.json()
    _token.update(value=body["access_token"], expires=now + int(body.get("expires_in", 3600)))
    return _token["value"]


async def send(client: httpx.AsyncClient, info: dict, token: str, title: str, body: str,
               data: dict[str, str]) -> str:
    """'sent', 'dead' (FCM does not know the token any more) or 'failed'."""
    access = await _access_token(client, info)
    r = await client.post(
        f"https://fcm.googleapis.com/v1/projects/{info['project_id']}/messages:send",
        headers={"Authorization": f"Bearer {access}"},
        json={"message": {
            "token": token,
            "notification": {"title": title, "body": body},
            "data": data,
            "android": {"priority": "high",
                        "notification": {"tag": data.get("order_id", "")}},
        }}, timeout=20)
    if r.status_code == 200:
        return "sent"
    detail = r.text[:300]
    if r.status_code == 404 or "UNREGISTERED" in detail:
        return "dead"
    if r.status_code == 401:
        _token["value"] = None
    log.warning("FCM refused an alert (%s): %s", r.status_code, detail)
    return "failed"


async def devices_for(db: AsyncSession, supplier_id: uuid.UUID) -> list[str]:
    now = datetime.now(UTC)
    rows = await db.scalars(
        select(SupplierDevice.token)
        .join(SupplierSession, SupplierSession.session_id == SupplierDevice.session_id)
        .join(SupplierUser, SupplierUser.supplier_user_id == SupplierDevice.supplier_user_id)
        .where(SupplierUser.supplier_id == supplier_id, SupplierUser.is_active.is_(True),
               SupplierSession.revoked_at.is_(None), SupplierSession.expires_at > now))
    return list(rows)


async def run_once(sessionmaker: async_sessionmaker, client: httpx.AsyncClient,
                   now: datetime | None = None) -> dict:
    info = _credentials()
    if info is None:
        return {}
    now = now or datetime.now(UTC)
    grace = timedelta(seconds=get_settings().push_gap_grace_seconds)
    counts: dict[str, int] = {}
    async with sessionmaker() as db, db.begin():
        state = await db.get(FeedState, FEED, with_for_update=True)
        cursor, gaps = state.last_seq, dict(state.gaps or {})
        rows = list(await db.scalars(select(OutboxEvent).where(OutboxEvent.seq > cursor)
                                     .order_by(OutboxEvent.seq).limit(200)))
    for row in rows:
        missing = list(range(cursor + 1, row.seq))
        if missing:
            for n in missing:
                gaps.setdefault(str(n), now.isoformat())
            if any(now - datetime.fromisoformat(gaps[str(n)]) < grace for n in missing):
                counts["waiting_on_gap"] = len(missing)
                break
        for n in missing:
            gaps.pop(str(n), None)
        cursor = row.seq
        p = row.payload or {}
        if row.event != "supplier.notify" or p.get("kind") not in TEXT:
            continue
        title, body = TEXT[p["kind"]]
        body = body.format(order_no=p.get("order_no") or "")
        data = {"kind": p["kind"], "order_id": str(p.get("order_id") or ""),
                "order_no": str(p.get("order_no") or "")}
        async with sessionmaker() as db, db.begin():
            tokens = await devices_for(db, uuid.UUID(p["supplier_id"]))
        for token in tokens:
            try:
                outcome = await send(client, info, token, title, body, data)
            except httpx.HTTPError as exc:
                log.warning("alert not sent: %s", exc)
                outcome = "failed"
            counts[outcome] = counts.get(outcome, 0) + 1
            if outcome == "dead":
                async with sessionmaker() as db, db.begin():
                    await db.execute(delete(SupplierDevice).where(SupplierDevice.token == token))
    async with sessionmaker() as db, db.begin():
        state = await db.get(FeedState, FEED, with_for_update=True)
        state.last_seq, state.gaps, state.updated_at = cursor, gaps, now
    return counts
