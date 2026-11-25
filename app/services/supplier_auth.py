"""Supplier-app sign-in: phone number + SMS one-time code (PLAN.md decision 3).

  POST /supplier/auth/request-code  phone         -> always the same answer
  POST /supplier/auth/verify        phone + code  -> tokens
  POST /supplier/auth/refresh       refresh token -> new tokens (rotated)
  POST /supplier/auth/login         email + password -> tokens (+ must_change_password)
  POST /supplier/auth/change-password, /forgot-password, /reset-password (SMS code)

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

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.config import get_settings
from app.core.phone import InvalidPhone, normalise_mobile
from app.models import Supplier, SupplierDevice, SupplierOtp, SupplierSession, SupplierUser
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
    must_change_password: bool = False


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


async def _consume_code(db: AsyncSession, user: SupplierUser, phone: str, code: str,
                        ip: str | None) -> None:
    """Check an SMS code for this registered number and use it up. Raises the one generic
    error when it is wrong, expired or already used."""
    wrong = SignInError("the code is wrong or expired")
    now = _now()
    # The newest few live codes all work, so a stranger asking for codes cannot keep
    # killing the one the supplier is typing (review #3). A wrong guess costs an attempt
    # on every live code, so the guess budget does not grow with the number of codes.
    live = (await db.scalars(
        select(SupplierOtp)
        .where(SupplierOtp.phone == phone, SupplierOtp.code_hash.is_not(None),
               SupplierOtp.used_at.is_(None), SupplierOtp.expires_at > now,
               SupplierOtp.attempts < get_settings().otp_max_attempts)
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


async def verify_code(
    db: AsyncSession, *, phone: str, code: str, ip: str | None, user_agent: str | None
) -> SupplierTokens:
    settings = get_settings()
    phone = _phone(phone)
    code = (code or "").strip()

    user = await _active_user(db, phone)
    if user is None:
        raise SignInError("the code is wrong or expired")

    fixed = settings.dev_fixed_otp
    if fixed and settings.environment != "production" and code == fixed:
        return await _issue(db, user, ip, user_agent)

    await _consume_code(db, user, phone, code, ip)
    return await _issue(db, user, ip, user_agent)


# ---------------------------------------------------------------- email + password
MAX_FAILED_LOGINS = 5
LOCK_MINUTES = 15


def normalise_email(raw: str) -> str:
    return (raw or "").strip().lower()


def check_password(password: str, *, email: str | None = None, phone: str | None = None) -> None:
    """Rules for a supplier password: 8 to 128 characters, not their own email or number."""
    if not 8 <= len(password or "") <= 128:
        raise SupplyError("the password must be 8 to 128 characters")
    low = password.lower()
    if low in {(email or "").lower(), (phone or "").lower()} or password.strip() == "":
        raise SupplyError("the password cannot be your email or phone number")


async def _by_email(db: AsyncSession, email: str) -> SupplierUser | None:
    user = await db.scalar(select(SupplierUser).where(
        func.lower(SupplierUser.email) == normalise_email(email)))
    if user is None or not user.is_active:
        return None
    supplier = await db.get(Supplier, user.supplier_id)
    return user if supplier is not None and supplier.is_active else None


async def set_password(db: AsyncSession, user: SupplierUser, password: str, *,
                       temporary: bool, keep_session: uuid.UUID | None = None) -> None:
    """Store the hash. A temporary (staff-set) password must be changed at the next
    sign-in. Every session ends: the old password's holders are out."""
    check_password(password, email=user.email, phone=user.phone)
    user.password_hash = security.hash_password(password)
    user.must_change_password = temporary
    user.password_changed_at = _now()
    user.failed_logins, user.locked_until = 0, None
    await revoke_all_for_user(db, user.supplier_user_id, keep_session)


async def password_login(db: AsyncSession, *, email: str, password: str, ip: str | None,
                         user_agent: str | None) -> SupplierTokens:
    bad = SignInError("the email or password is wrong")
    email = normalise_email(email)
    await _serialise(db, f"pw:{email}")
    user = await _by_email(db, email)
    if user is None or not user.password_hash:
        security.verify_password(None, password)  # same work as a real check
        raise bad
    now = _now()
    if user.locked_until and user.locked_until > now:
        raise TooMany("too many wrong passwords; try again in a few minutes")
    if not security.verify_password(user.password_hash, password):
        user.failed_logins += 1
        if user.failed_logins >= MAX_FAILED_LOGINS:
            user.failed_logins, user.locked_until = 0, now + timedelta(minutes=LOCK_MINUTES)
        await audit.record(db, supplier_user_id=user.supplier_user_id,
                           action="supplier_auth.password_failed", entity="supplier_user",
                           entity_id=user.supplier_user_id, ip_address=ip)
        raise bad
    user.failed_logins, user.locked_until = 0, None
    return await _issue(db, user, ip, user_agent, via_password=True)


async def change_password(db: AsyncSession, user: SupplierUser, *, current: str, new: str,
                          keep_session: uuid.UUID) -> None:
    if not security.verify_password(user.password_hash, current):
        raise SignInError("the current password is wrong")
    if current == new:
        raise SupplyError("choose a different password")
    await set_password(db, user, new, temporary=False, keep_session=keep_session)
    await audit.record(db, supplier_user_id=user.supplier_user_id,
                       action="supplier_auth.password_changed", entity="supplier_user",
                       entity_id=user.supplier_user_id)


async def forgot_password(db: AsyncSession, *, email: str, ip: str | None) -> None:
    """Send the sign-in code by SMS to the number registered for this email. The answer
    never says whether the email exists."""
    user = await _by_email(db, email)
    if user is not None:
        await request_code(db, phone=user.phone, ip=ip)


async def reset_password(db: AsyncSession, *, email: str, code: str, new: str,
                         ip: str | None) -> None:
    user = await _by_email(db, email)
    if user is None:
        raise SignInError("the code is wrong or expired")
    await _consume_code(db, user, user.phone, (code or "").strip(), ip)
    await set_password(db, user, new, temporary=False)
    await audit.record(db, supplier_user_id=user.supplier_user_id,
                       action="supplier_auth.password_reset", entity="supplier_user",
                       entity_id=user.supplier_user_id, ip_address=ip)


async def _issue(
    db: AsyncSession, user: SupplierUser, ip: str | None, user_agent: str | None,
    via_password: bool = False,
) -> SupplierTokens:
    settings = get_settings()
    refresh, refresh_hash = security.new_refresh_token()
    session = SupplierSession(
        supplier_user_id=user.supplier_user_id,
        refresh_hash=refresh_hash,
        ip_address=ip,
        user_agent=(user_agent or "")[:300] or None,
        expires_at=_now() + timedelta(days=settings.supplier_refresh_days),
        via_password=via_password,
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
        must_change_password=via_password and user.must_change_password,
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
    # Signed out: this phone gets no more order alerts.
    await db.execute(delete(SupplierDevice).where(SupplierDevice.session_id == session_id))


async def revoke_all_for_user(db: AsyncSession, supplier_user_id: uuid.UUID,
                              except_session: uuid.UUID | None = None) -> None:
    """When staff deactivate a phone (or a password changes), its sessions end at once.
    `except_session` keeps the one the person is using right now."""
    keep = [] if except_session is None else [SupplierSession.session_id != except_session]
    await db.execute(
        update(SupplierSession)
        .where(SupplierSession.supplier_user_id == supplier_user_id,
               SupplierSession.revoked_at.is_(None), *keep)
        .values(revoked_at=_now())
    )
    dev_keep = [] if except_session is None else [SupplierDevice.session_id != except_session]
    await db.execute(delete(SupplierDevice).where(
        SupplierDevice.supplier_user_id == supplier_user_id, *dev_keep))
