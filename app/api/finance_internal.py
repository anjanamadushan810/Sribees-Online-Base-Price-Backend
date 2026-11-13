"""Internal: Supply's events for Finance (``GET /internal/finance/events``).

Same three locks as SRIBEESonline's ``/internal/*`` routes:

* **Service token.** ``Authorization: Bearer <token>``; only its SHA-256 is in our
  settings (``FINANCE_SERVICE_TOKEN_SHA256``), compared in constant time. No token
  configured = the route answers 404, as if it did not exist.
* **Never through the edge.** A request that came through Caddy/nginx carries
  ``X-Forwarded-For`` / ``Forwarded`` and is refused whatever token it has.
* **IP allow-list** (``FINANCE_ALLOWED_IPS``) on the connecting peer, when set.
"""

import hashlib
import hmac
import ipaddress

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_db
from app.services import finance_link

router = APIRouter(prefix="/internal/finance", include_in_schema=False)


def _allowed(peer: str) -> bool:
    raw = (get_settings().finance_allowed_ips or "").strip()
    if not raw:
        return True
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(ip in ipaddress.ip_network(part.strip(), strict=False)
               for part in raw.split(",") if part.strip())


def require_finance_service(request: Request) -> None:
    expected = (get_settings().finance_service_token_sha256 or "").strip().lower()
    if not expected:
        raise HTTPException(status_code=404, detail="Not Found")
    if request.headers.get("x-forwarded-for") or request.headers.get("forwarded"):
        raise HTTPException(status_code=403, detail="not available from outside")
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    presented = hashlib.sha256(token.encode()).hexdigest() if token else ""
    if not token or not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="service token required")
    peer = request.client.host if request.client else ""
    if not _allowed(peer):
        raise HTTPException(status_code=403, detail="caller not allowed")


@router.get("/events")
async def events(
    after: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=finance_link.MAX_LIMIT),
    _: None = Depends(require_finance_service),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return {"events": await finance_link.events_after(db, after, limit)}
