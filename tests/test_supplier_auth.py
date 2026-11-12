"""Supplier-app sign-in (phone + SMS code) and the wall between supplier and staff tokens."""

import re
from contextlib import contextmanager

import pytest
from sqlalchemy import update

from app.core.phone import InvalidPhone, normalise_mobile
from tests.conftest import ip_headers, unique_mobile


@contextmanager
def settings_override(**values):
    from app.core.config import get_settings

    s = get_settings()
    old = {k: getattr(s, k) for k in values}
    for k, v in values.items():
        setattr(s, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(s, k, v)


def last_sms(phone_e164: str):
    from app.services.sms import fake_outbox

    msgs = [m for m in fake_outbox().outbox if m.phone == phone_e164]
    return msgs[-1] if msgs else None


def sms_count(phone_e164: str) -> int:
    from app.services.sms import fake_outbox

    return sum(1 for m in fake_outbox().outbox if m.phone == phone_e164)


def code_in(text: str) -> str:
    return re.search(r"\b(\d{6})\b", text).group(1)


async def new_supplier(client, staff, name=None, language="si"):
    import secrets

    r = await client.post("/suppliers", headers=staff.headers,
                          json={"name": name or f"Supplier {secrets.token_hex(3)}"})
    assert r.status_code == 201, r.text
    supplier = r.json()
    phone = unique_mobile()
    r = await client.post(f"/suppliers/{supplier['supplier_id']}/users", headers=staff.headers,
                          json={"phone": phone, "full_name": "Nishi", "language": language})
    assert r.status_code == 201, r.text
    return supplier, r.json()


async def signed_in_supplier(client, staff, **kw):
    supplier, su = await new_supplier(client, staff, **kw)
    h = ip_headers()
    r = await client.post("/supplier/auth/request-code", json={"phone": su["phone"]}, headers=h)
    assert r.status_code == 200, r.text
    code = code_in(last_sms(su["phone"]).text)
    r = await client.post("/supplier/auth/verify", headers=h,
                          json={"phone": su["phone"], "code": code})
    assert r.status_code == 200, r.text
    return supplier, su, r.json()


# ---------------------------------------------------------------- phone numbers
@pytest.mark.parametrize("raw", ["0715810938", "715810938", "94715810938", "+94 71 581 0938",
                                 "071-581-0938"])
def test_phone_forms(raw):
    assert normalise_mobile(raw) == "+94715810938"


@pytest.mark.parametrize("raw", ["0112345678", "07158109", "+44715810938", "", "abc"])
def test_phone_rejected(raw):
    with pytest.raises(InvalidPhone):
        normalise_mobile(raw)


# ---------------------------------------------------------------- staff side
async def test_staff_registers_supplier_phone(client, purchasing):
    supplier, su = await new_supplier(client, purchasing)
    assert su["phone"].startswith("+947") and su["language"] == "si"
    # same phone again, written differently -> refused
    local = "0" + su["phone"][3:]
    r = await client.post(f"/suppliers/{supplier['supplier_id']}/users",
                          headers=purchasing.headers, json={"phone": local, "full_name": "Xy"})
    assert r.status_code == 409
    r = await client.post(f"/suppliers/{supplier['supplier_id']}/users",
                          headers=purchasing.headers, json={"phone": "0112345678",
                                                            "full_name": "Xy"})
    assert r.status_code == 422


async def test_branch_manager_cannot_create_supplier(client, branch_manager):
    r = await client.post("/suppliers", headers=branch_manager.headers, json={"name": "Nope"})
    assert r.status_code == 403


# ---------------------------------------------------------------- sign-in
async def test_sign_in_with_sms_code(client, owner):
    supplier, su, tokens = await signed_in_supplier(client, owner)
    assert "ඇතුල් වීමේ කේතය" in last_sms(su["phone"]).text  # Sinhala by default
    me = await client.get("/supplier/me",
                          headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert me.status_code == 200
    assert me.json()["supplier_name"] == supplier["name"]


async def test_english_sms_for_english_user(client, owner):
    _, su = await new_supplier(client, owner, language="en")
    await client.post("/supplier/auth/request-code", json={"phone": su["phone"]},
                      headers=ip_headers())
    assert last_sms(su["phone"]).text.startswith("Sribees Supply sign-in code:")


async def test_unknown_number_same_answer_and_no_sms(client):
    phone = unique_mobile()
    r = await client.post("/supplier/auth/request-code", json={"phone": phone},
                          headers=ip_headers())
    assert r.status_code == 200
    assert r.json()["status"] == "sent_if_registered"
    assert sms_count(normalise_mobile(phone)) == 0
    r = await client.post("/supplier/auth/verify", json={"phone": phone, "code": "123456"},
                          headers=ip_headers())
    assert r.status_code == 401


async def test_code_works_once(client, owner):
    _, su = await new_supplier(client, owner)
    h = ip_headers()
    await client.post("/supplier/auth/request-code", json={"phone": su["phone"]}, headers=h)
    code = code_in(last_sms(su["phone"]).text)
    body = {"phone": su["phone"], "code": code}
    assert (await client.post("/supplier/auth/verify", json=body, headers=h)).status_code == 200
    assert (await client.post("/supplier/auth/verify", json=body, headers=h)).status_code == 401


async def test_wrong_codes_kill_the_code(client, owner):
    _, su = await new_supplier(client, owner)
    h = ip_headers()
    await client.post("/supplier/auth/request-code", json={"phone": su["phone"]}, headers=h)
    code = code_in(last_sms(su["phone"]).text)
    wrong = f"{(int(code) + 1) % 1_000_000:06d}"
    for _ in range(5):
        r = await client.post("/supplier/auth/verify", headers=h,
                              json={"phone": su["phone"], "code": wrong})
        assert r.status_code == 401
    # the right code no longer works: attempts were saved although each request failed
    r = await client.post("/supplier/auth/verify", headers=h,
                          json={"phone": su["phone"], "code": code})
    assert r.status_code == 401


async def test_expired_code_refused(client, owner):
    from app.core.db import get_sessionmaker
    from app.models import SupplierOtp

    _, su = await new_supplier(client, owner)
    h = ip_headers()
    await client.post("/supplier/auth/request-code", json={"phone": su["phone"]}, headers=h)
    code = code_in(last_sms(su["phone"]).text)
    async with get_sessionmaker()() as s, s.begin():
        await s.execute(update(SupplierOtp).where(SupplierOtp.phone == su["phone"])
                        .values(expires_at=SupplierOtp.created_at))
    r = await client.post("/supplier/auth/verify", headers=h,
                          json={"phone": su["phone"], "code": code})
    assert r.status_code == 401


async def test_recent_codes_all_work_until_one_is_used(client, owner):
    """A stranger asking for codes must not kill the one the supplier is typing."""
    _, su = await new_supplier(client, owner)
    h = ip_headers()
    with settings_override(otp_resend_seconds=0):
        await client.post("/supplier/auth/request-code", json={"phone": su["phone"]}, headers=h)
        first = code_in(last_sms(su["phone"]).text)
        await client.post("/supplier/auth/request-code", json={"phone": su["phone"]},
                          headers=ip_headers())
        second = code_in(last_sms(su["phone"]).text)
    r = await client.post("/supplier/auth/verify", headers=h,
                          json={"phone": su["phone"], "code": first})
    assert r.status_code == 200
    if first != second:  # one success ends every live code
        r = await client.post("/supplier/auth/verify", headers=h,
                              json={"phone": su["phone"], "code": second})
        assert r.status_code == 401


async def test_concurrent_requests_send_one_sms(client, owner):
    import asyncio

    _, su = await new_supplier(client, owner)
    h = ip_headers()
    results = await asyncio.gather(*[
        client.post("/supplier/auth/request-code", json={"phone": su["phone"]}, headers=h)
        for _ in range(5)])
    assert sorted(r.status_code for r in results) == [200, 429, 429, 429, 429]
    assert sms_count(su["phone"]) == 1


# ---------------------------------------------------------------- limits
async def test_resend_wait(client, owner):
    _, su = await new_supplier(client, owner)
    h = ip_headers()
    assert (await client.post("/supplier/auth/request-code", json={"phone": su["phone"]},
                              headers=h)).status_code == 200
    r = await client.post("/supplier/auth/request-code", json={"phone": su["phone"]}, headers=h)
    assert r.status_code == 429
    assert sms_count(su["phone"]) == 1


async def test_hourly_limit_per_phone(client, owner):
    _, su = await new_supplier(client, owner)
    here = ip_headers()
    with settings_override(otp_resend_seconds=0):
        for _ in range(5):
            r = await client.post("/supplier/auth/request-code", json={"phone": su["phone"]},
                                  headers=here)
            assert r.status_code == 200
        r = await client.post("/supplier/auth/request-code", json={"phone": su["phone"]},
                              headers=here)
        assert r.status_code == 429  # 5 per number from one address
        for _ in range(5):  # others can still ask, up to 10 for the number in all
            r = await client.post("/supplier/auth/request-code", json={"phone": su["phone"]},
                                  headers=ip_headers())
            assert r.status_code == 200
        r = await client.post("/supplier/auth/request-code", json={"phone": su["phone"]},
                              headers=ip_headers())
        assert r.status_code == 429
    assert sms_count(su["phone"]) == 10


async def test_hourly_limit_per_ip_counts_unknown_numbers(client):
    h = ip_headers()
    for _ in range(20):
        r = await client.post("/supplier/auth/request-code", json={"phone": unique_mobile()},
                              headers=h)
        assert r.status_code == 200
    r = await client.post("/supplier/auth/request-code", json={"phone": unique_mobile()},
                          headers=h)
    assert r.status_code == 429


async def test_forged_forwarded_for_does_not_dodge_ip_limit(client):
    real = ip_headers()["X-Forwarded-For"]
    for i in range(20):
        await client.post("/supplier/auth/request-code", json={"phone": unique_mobile()},
                          headers={"X-Forwarded-For": f"1.2.3.{i}, {real}"})
    r = await client.post("/supplier/auth/request-code", json={"phone": unique_mobile()},
                          headers={"X-Forwarded-For": f"9.9.9.9, {real}"})
    assert r.status_code == 429


# ---------------------------------------------------------------- sessions and the wall
async def test_supplier_token_is_not_a_staff_token(client, owner):
    _, _, tokens = await signed_in_supplier(client, owner)
    h = {"Authorization": f"Bearer {tokens['access_token']}"}
    assert (await client.get("/suppliers", headers=h)).status_code == 401
    assert (await client.get("/auth/me", headers=h)).status_code == 401
    assert (await client.get("/supplier/me", headers=owner.headers)).status_code == 401


async def test_refresh_rotates(client, owner):
    _, _, tokens = await signed_in_supplier(client, owner)
    r = await client.post("/supplier/auth/refresh",
                          json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 200
    again = await client.post("/supplier/auth/refresh",
                              json={"refresh_token": tokens["refresh_token"]})
    assert again.status_code == 401


async def test_deactivated_phone_is_signed_out_at_once(client, owner):
    supplier, su, tokens = await signed_in_supplier(client, owner)
    h = {"Authorization": f"Bearer {tokens['access_token']}"}
    r = await client.patch(
        f"/suppliers/{supplier['supplier_id']}/users/{su['supplier_user_id']}",
        headers=owner.headers, json={"is_active": False})
    assert r.status_code == 200
    assert (await client.get("/supplier/me", headers=h)).status_code == 401
    r = await client.post("/supplier/auth/refresh",
                          json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 401
    with settings_override(otp_resend_seconds=0):
        r = await client.post("/supplier/auth/request-code", json={"phone": su["phone"]},
                              headers=ip_headers())
    assert r.status_code == 200  # same answer as a stranger ...
    assert sms_count(su["phone"]) == 1  # ... and no new SMS


async def test_deactivated_supplier_signs_out_all_its_phones(client, owner):
    supplier, _, tokens = await signed_in_supplier(client, owner)
    h = {"Authorization": f"Bearer {tokens['access_token']}"}
    r = await client.patch(f"/suppliers/{supplier['supplier_id']}", headers=owner.headers,
                           json={"is_active": False})
    assert r.status_code == 200
    assert (await client.get("/supplier/me", headers=h)).status_code == 401


async def test_logout(client, owner):
    _, _, tokens = await signed_in_supplier(client, owner)
    h = {"Authorization": f"Bearer {tokens['access_token']}"}
    assert (await client.post("/supplier/auth/logout", headers=h)).status_code == 204
    assert (await client.get("/supplier/me", headers=h)).status_code == 401


async def test_audit_log_is_append_only(client, owner):
    from sqlalchemy import text

    from app.core.db import get_sessionmaker

    with pytest.raises(Exception, match="cannot be changed"):
        async with get_sessionmaker()() as s, s.begin():
            await s.execute(text("DELETE FROM audit_log"))


def test_production_refuses_fake_sms_and_fixed_code():
    from app.core.config import Settings

    base = dict(environment="production", database_url="postgresql+asyncpg://x/y",
                jwt_secret="x" * 32, totp_encryption_key="k", otp_secret="y" * 32)
    with pytest.raises(ValueError, match="SMS_PROVIDER=fake"):
        Settings(**base, sms_provider="fake")
    with pytest.raises(ValueError, match="DEV_FIXED_OTP"):
        Settings(**base, sms_provider="textlk", dev_fixed_otp="123456")



async def test_sms_not_set_up_fails_the_same_for_everyone(client, owner):
    _, su = await new_supplier(client, owner)
    with settings_override(sms_provider="textlk", textlk_api_token="", textlk_sender_id=""):
        known = await client.post("/supplier/auth/request-code", json={"phone": su["phone"]},
                                  headers=ip_headers())
        stranger = await client.post("/supplier/auth/request-code",
                                     json={"phone": unique_mobile()}, headers=ip_headers())
    assert known.status_code == stranger.status_code == 503
