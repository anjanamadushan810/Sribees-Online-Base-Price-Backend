"""Supplier-app sign-in: phone number + SMS one-time code (PLAN.md decision 3).

  POST /supplier/auth/request-code  phone         -> always the same answer
  POST /supplier/auth/verify        phone + code  -> tokens
  POST /supplier/auth/refresh       refresh token -> new tokens (rotated)

Rules
- Only numbers we registered get an SMS; the answer never says whether a number is
  registered (no enumeration, and no SMS cost for strangers).
- Limits: one code per OTP_RESEND_SECONDS, OTP_MAX_PER_PHONE_HOUR per number,
  OTP_MAX_PER_IP_HOUR per address. A code dies after OTP_MAX_ATTEMPTS wrong tries or
  OTP_MINUTES, and works once. The newest LIVE_CODES live codes are valid.
- Codes are stored as an HMAC only.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.config import get_settings
from app.core.phone import InvalidPhone, normalise_mobile
from app.models import Supplier, SupplierOtp, SupplierSession, SupplierUser
from app.services import audit, sms
from app.services.errors import SupplyError

LIVE_CODES = 3


class SignInError(SupplyError):
    status_code = 401


class TooMany(SupplyError):
    status_code = 429


@dataclass
class SupplierTokens:
    access_token: str
    refresh_token: str
    expires_in: int


def _now() -> datetime:
    return datetime.now(UTC)


def _phone(raw: str) -> str:
    try:
        return normalise_mobile(raw)
    except InvalidPhone as exc:
        raise SupplyError(str(exc)) from None


async def _active_user(db: AsyncSession, phone: str) -> SupplierUser | None:
    user = await db.scalar(select(SupplierUser).where(SupplierUser.phone == phone))
    if user is None or not user.is_active:
        return None
    supplier = await db.get(Supplier, user.supplier_id)
    return user if supplier is not None and supplier.is_active else None


async def _serialise(db: AsyncSession, *keys: str) -> None:
    """Concurrent requests for the same phone / address wait for each other, so the
    counts below cannot be raced past (review 2026-11-12 #2)."""
    for key in sorted(set(keys)):
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key})


async def request_code(db: AsyncSession, *, phone: str, ip: str | None) -> None:
    settings = get_settings()
    phone = _phone(phone)
    # Fail the same way for every number when SMS is not available, so a broken sender
    # cannot tell registered numbers from strangers (review #9).
    sender = sms.get_sender()
    now = _now()
    hour_ago = now - timedelta(hours=1)
    await _serialise(db, f"otp:phone:{phone}", f"otp:ip:{ip or '-'}")

    if ip:
        per_ip = await db.scalar(
            select(func.count()).select_from(SupplierOtp)
            .where(SupplierOtp.ip_address == ip, SupplierOtp.created_at > hour_ago)
        )
        if per_ip >= settings.otp_max_per_ip_hour:
            raise TooMany("too many requests; try again later")

    recent = (await db.execute(
        select(SupplierOtp.created_at, SupplierOtp.ip_address)
        .where(SupplierOtp.phone == phone, SupplierOtp.created_at > hour_ago)
        .order_by(SupplierOtp.created_at.desc())
    )).all()
    if recent and (now - recent[0].created_at).total_seconds() < settings.otp_resend_seconds:
        raise TooMany("wait a minute before asking for a new code")
    # Per number AND address, plus a higher cap for the number from everywhere: one
    # stranger cannot use up the supplier's own requests (review #3).
    from_here = sum(1 for r in recent if r.ip_address == ip)
    if (from_here >= settings.otp_max_per_phone_hour
            or len(recent) >= settings.otp_max_per_phone_hour_total):
        raise TooMany("too many codes for this number; try again in an hour")

    user = await _active_user(db, phone)
    # The row is written for every request (rate limits count strangers too), but only a
    # registered number gets a code and an SMS.
    code = security.new_otp_code() if user else None
    db.add(SupplierOtp(
        phone=phone,
        supplier_user_id=user.supplier_user_id if user else None,
        code_hash=security.hash_otp(phone, code) if code else None,
        ip_address=ip,
        expires_at=now + timedelta(minutes=settings.otp_minutes),
    ))
    await db.flush()
    if user and code:
        await sender.send(phone, sms.otp_text(code, user.language, settings.otp_minutes))


async def verify_code(
    db: AsyncSession, *, phone: str, code: str, ip: str | None, user_agent: str | None
) -> SupplierTokens:
    settings = get_settings()
    phone = _phone(phone)
    code = (code or "").strip()
    wrong = SignInError("the code is wrong or expired")

    user = await _active_user(db, phone)
    if user is None:
        raise wrong

    fixed = settings.dev_fixed_otp
    if fixed and settings.environment != "production" and code == fixed:
        return await _issue(db, user, ip, user_agent)

    now = _now()
    # The newest few live codes all work, so a stranger asking for codes cannot keep
    # killing the one the supplier is typing (review #3). A wrong guess costs an attempt
    # on every live code, so the guess budget does not grow with the number of codes.
    live = (await db.scalars(
        select(SupplierOtp)
        .where(SupplierOtp.phone == phone, SupplierOtp.code_hash.is_not(None),
               SupplierOtp.used_at.is_(None), SupplierOtp.expires_at > now,
               SupplierOtp.attempts < settings.otp_max_attempts)
        .order_by(SupplierOtp.created_at.desc(), SupplierOtp.otp_id.desc())
        .limit(LIVE_CODES)
        .with_for_update()
    )).all()
    if not live:
        raise wrong
    if not any(security.otp_matches(o.code_hash, phone, code) for o in live):
        for o in live:
            o.attempts += 1
        await audit.record(db, supplier_user_id=user.supplier_user_id,
                           action="supplier_auth.code_failed", entity="supplier_user",
                           entity_id=user.supplier_user_id, ip_address=ip)
        raise wrong
    for o in live:  # one success ends every live code
        o.used_at = now
    return await _issue(db, user, ip, user_agent)


async def _issue(
    db: AsyncSession, user: SupplierUser, ip: str | None, user_agent: str | None
) -> SupplierTokens:
    settings = get_settings()
    refresh, refresh_hash = security.new_refresh_token()
    session = SupplierSession(
        supplier_user_id=user.supplier_user_id,
        refresh_hash=refresh_hash,
        ip_address=ip,
        user_agent=(user_agent or "")[:300] or None,
        expires_at=_now() + timedelta(days=settings.supplier_refresh_days),
    )
    db.add(session)
    user.last_login_at = _now()
    await db.flush()
    await audit.record(db, supplier_user_id=user.supplier_user_id, action="supplier_auth.login",
                       entity="supplier_user", entity_id=user.supplier_user_id, ip_address=ip)
    return SupplierTokens(
        access_token=security.make_supplier_token(
            user.supplier_user_id, user.supplier_id, session.session_id,
            settings.access_token_minutes,
        ),
        refresh_token=refresh,
        expires_in=settings.access_token_minutes * 60,
    )


async def refresh(db: AsyncSession, *, refresh_token: str) -> SupplierTokens:
    settings = get_settings()
    expired = SignInError("session expired; sign in again")
    session = await db.scalar(
        select(SupplierSession)
        .where(SupplierSession.refresh_hash == security.hash_refresh_token(refresh_token))
        .with_for_update()
    )
    if session is None or session.revoked_at is not None or session.expires_at <= _now():
        raise expired
    user = await db.get(SupplierUser, session.supplier_user_id)
    if user is None or await _active_user(db, user.phone) is None:
        raise expired
    new_token, new_hash = security.new_refresh_token()
    session.refresh_hash = new_hash
    session.last_used_at = _now()
    await db.flush()
    return SupplierTokens(
        access_token=security.make_supplier_token(
            user.supplier_user_id, user.supplier_id, session.session_id,
            settings.access_token_minutes,
        ),
        refresh_token=new_token,
        expires_in=settings.access_token_minutes * 60,
    )


async def revoke_session(db: AsyncSession, session_id: uuid.UUID) -> None:
    await db.execute(
        update(SupplierSession)
        .where(SupplierSession.session_id == session_id, SupplierSession.revoked_at.is_(None))
        .values(revoked_at=_now())
    )


async def revoke_all_for_user(db: AsyncSession, supplier_user_id: uuid.UUID) -> None:
    """When staff deactivate a phone, its sessions end at once."""
    await db.execute(
        update(SupplierSession)
        .where(SupplierSession.supplier_user_id == supplier_user_id,
               SupplierSession.revoked_at.is_(None))
        .values(revoked_at=_now())
    )
