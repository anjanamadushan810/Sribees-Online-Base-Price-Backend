"""Supplier master data: auto code, details Finance needs, bank details for owner and
accountant only, and a supplier.updated event so Finance never needs them typed again."""

import json
import os
import secrets
from pathlib import Path

import pytest
from sqlalchemy import select

from tests.conftest import sign_in
from tests.test_finance_link import TOKEN, TOKEN_SHA
from tests.test_supplier_auth import settings_override

BANK = {"bank_name": "Commercial Bank", "bank_branch": "Matugama",
        "bank_account_name": "Nishi Palace (Pvt) Ltd", "bank_account_no": "8001234567"}
FULL = {"contact_name": "Mr Nishantha", "phone": "0712345678", "email": "nishi@example.com",
        "address": "12 Main St, Matugama", "credit_days": 14, "is_related_party": False}


@pytest.fixture(scope="module")
async def accountant(client):
    return await sign_in(client, "accountant")


def _name() -> str:
    return f"Supplier {secrets.token_hex(4)}"


async def _feed(client, after=0):
    with settings_override(finance_service_token_sha256=TOKEN_SHA):
        r = await client.get("/internal/finance/events",
                             headers={"Authorization": f"Bearer {TOKEN}"},
                             params={"after": after, "limit": 1000})
    assert r.status_code == 200, r.text
    return r.json()["events"]


async def test_codes_are_numbered_by_the_system_and_never_repeat(client, owner):
    a = (await client.post("/suppliers", headers=owner.headers, json={"name": _name()})).json()
    b = (await client.post("/suppliers", headers=owner.headers, json={"name": _name()})).json()
    assert a["code"].startswith("SUP-") and b["code"].startswith("SUP-")
    assert int(b["code"][4:]) == int(a["code"][4:]) + 1
    # a client-supplied code is ignored
    c = (await client.post("/suppliers", headers=owner.headers,
                           json={"name": _name(), "code": "HACK"})).json()
    assert c["code"] != "HACK"


async def test_owner_creates_with_every_field(client, owner):
    r = await client.post("/suppliers", headers=owner.headers,
                          json={"name": _name(), "notes": "n", **FULL, **BANK})
    assert r.status_code == 201, r.text
    s = r.json()
    for k, v in {**FULL, **BANK}.items():
        assert s[k] == v


async def test_purchasing_never_gets_bank_keys(client, owner, purchasing, branch_manager):
    sid = (await client.post("/suppliers", headers=owner.headers,
                             json={"name": _name(), **BANK})).json()["supplier_id"]
    for who in (purchasing, branch_manager):
        one = (await client.get(f"/suppliers/{sid}", headers=who.headers)).json()
        assert not set(BANK) & set(one)
        many = (await client.get("/suppliers", headers=who.headers)).json()
        assert many and all(not set(BANK) & set(row) for row in many)
    # purchasing creating or patching cannot leak them back either
    r = await client.post("/suppliers", headers=purchasing.headers,
                          json={"name": _name(), "credit_days": 7})
    assert r.status_code == 201 and not set(BANK) & set(r.json())
    r = await client.patch(f"/suppliers/{sid}", headers=purchasing.headers,
                           json={"credit_days": 30})
    assert r.status_code == 200 and not set(BANK) & set(r.json())


async def test_owner_and_accountant_see_bank(client, owner, accountant):
    sid = (await client.post("/suppliers", headers=owner.headers,
                             json={"name": _name(), **BANK})).json()["supplier_id"]
    for who in (owner, accountant):
        one = (await client.get(f"/suppliers/{sid}", headers=who.headers)).json()
        assert one["bank_account_no"] == BANK["bank_account_no"]


async def test_purchasing_cannot_write_bank(client, owner, purchasing, branch_manager):
    r = await client.post("/suppliers", headers=purchasing.headers,
                          json={"name": _name(), "bank_account_no": "1"})
    assert r.status_code == 403
    sid = (await client.post("/suppliers", headers=owner.headers,
                             json={"name": _name()})).json()["supplier_id"]
    for who in (purchasing, branch_manager):
        r = await client.patch(f"/suppliers/{sid}/bank", headers=who.headers,
                               json={"bank_name": "X"})
        assert r.status_code == 403
    # the ordinary PATCH ignores bank keys (they are not part of its schema)
    await client.patch(f"/suppliers/{sid}", headers=purchasing.headers,
                       json={"bank_account_no": "999"})
    one = (await client.get(f"/suppliers/{sid}", headers=owner.headers)).json()
    assert one["bank_account_no"] is None


async def test_accountant_edits_bank_only(client, owner, accountant):
    sid = (await client.post("/suppliers", headers=owner.headers,
                             json={"name": _name()})).json()["supplier_id"]
    r = await client.patch(f"/suppliers/{sid}/bank", headers=accountant.headers, json=BANK)
    assert r.status_code == 200 and r.json()["bank_account_no"] == BANK["bank_account_no"]
    r = await client.patch(f"/suppliers/{sid}", headers=accountant.headers,
                           json={"credit_days": 5})
    assert r.status_code == 403  # accountant has no other write


async def test_bank_audit_keeps_only_last_four(client, owner, accountant):
    from app.core.db import get_sessionmaker
    from app.models import AuditLog

    sid = (await client.post("/suppliers", headers=owner.headers,
                             json={"name": _name(), **BANK})).json()["supplier_id"]
    await client.patch(f"/suppliers/{sid}/bank", headers=accountant.headers,
                       json={"bank_account_no": "5550001234"})
    async with get_sessionmaker()() as s:
        rows = (await s.scalars(select(AuditLog).where(AuditLog.entity_id == sid))).all()
    text = " ".join(str(r.before) + str(r.after) for r in rows)
    assert "8001234567" not in text and "5550001234" not in text
    assert "1234" in text
    assert any(r.action == "supplier.bank_update" for r in rows)


async def test_validation(client, owner):
    bad = [{"credit_days": -1}, {"credit_days": 400}, {"email": "not-an-email"}]
    for extra in bad:
        r = await client.post("/suppliers", headers=owner.headers,
                              json={"name": _name(), **extra})
        assert r.status_code == 422, extra
    sid = (await client.post("/suppliers", headers=owner.headers,
                             json={"name": _name(), "contact_name": "  "})).json()
    assert sid["contact_name"] is None  # blank means not set


async def test_finance_gets_supplier_updated_with_everything(client, owner, accountant):
    before = max([e["seq"] for e in await _feed(client)] or [0])
    s = (await client.post("/suppliers", headers=owner.headers,
                           json={"name": _name(), **FULL, **BANK})).json()
    await client.patch(f"/suppliers/{s['supplier_id']}", headers=owner.headers,
                       json={"credit_days": 30})
    await client.patch(f"/suppliers/{s['supplier_id']}/bank", headers=accountant.headers,
                       json={"bank_branch": "Colombo"})
    evs = [e for e in await _feed(client, before) if e["aggregate_id"] == s["supplier_id"]]
    assert [e["type"] for e in evs] == ["supplier.updated"] * 3
    first, second, third = (e["payload"]["supplier"] for e in evs)
    assert first["code"] == s["code"] and first["bank_account_no"] == BANK["bank_account_no"]
    assert first["credit_days"] == 14 and second["credit_days"] == 30
    assert third["bank_branch"] == "Colombo" and third["credit_days"] == 30
    if os.environ.get("SUPPLY_EVENTS_DUMP"):  # the file Finance's tests read
        out = Path(__file__).parent / "fixtures" / "contract_supplier_updated.json"
        out.write_text(json.dumps(evs[0], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
