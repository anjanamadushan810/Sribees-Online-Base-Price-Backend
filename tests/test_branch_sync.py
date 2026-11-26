"""Branches follow SRIBEESonline's: created, linked to a hand-made one, updated; never
deleted; an empty product list never switches the catalogue off."""

import uuid

import pytest
from sqlalchemy import select

from app.services.errors import SupplyError
from tests.test_online_catalog import PAYLOAD

pytestmark = pytest.mark.usefixtures("database")


async def apply(payload):
    from app.core.db import get_sessionmaker
    from app.services import online_catalog

    async with get_sessionmaker()() as s, s.begin():
        return await online_catalog.apply(s, payload, user_id=None)


async def branch_by_online(oid):
    from app.core.db import get_sessionmaker
    from app.models import Branch

    async with get_sessionmaker()() as s:
        return await s.scalar(select(Branch).where(Branch.online_branch_id == oid))


async def test_new_branch_is_created_and_then_updated():
    oid = uuid.uuid4()
    code = f"B{uuid.uuid4().hex[:5].upper()}"
    r = await apply({**PAYLOAD, "branches": [
        {"branch_id": str(oid), "code": code, "name": "Matugama", "is_active": True}]})
    assert r["branches"]["created"] == 1
    b = await branch_by_online(oid)
    assert (b.code, b.name, b.is_active) == (code, "Matugama", True)
    r = await apply({**PAYLOAD, "branches": [
        {"branch_id": str(oid), "code": code, "name": "Matugama Town", "is_active": False}]})
    assert r["branches"] == {"created": 0, "linked": 0, "updated": 1, "skipped": 0}
    b = await branch_by_online(oid)
    assert (b.name, b.is_active) == ("Matugama Town", False)
    # a repeat changes nothing
    r = await apply({**PAYLOAD, "branches": [
        {"branch_id": str(oid), "code": code, "name": "Matugama Town", "is_active": False}]})
    assert r["branches"]["updated"] == 0 and r["branches"]["created"] == 0


async def test_a_hand_made_branch_with_the_same_code_is_linked_not_duplicated():
    from app.core.db import get_sessionmaker
    from app.models import Branch

    code = f"H{uuid.uuid4().hex[:5].upper()}"
    async with get_sessionmaker()() as s, s.begin():
        s.add(Branch(code=code, name="Typed by hand"))
    oid = uuid.uuid4()
    r = await apply({**PAYLOAD, "branches": [
        {"branch_id": str(oid), "code": code, "name": "From Online", "is_active": True}]})
    assert r["branches"]["linked"] == 1 and r["branches"]["created"] == 0
    b = await branch_by_online(oid)
    assert b.code == code and b.name == "Typed by hand"  # ours is kept, only linked
    async with get_sessionmaker()() as s:
        assert len((await s.scalars(select(Branch).where(Branch.code == code))).all()) == 1


async def test_a_code_taken_by_another_online_branch_is_skipped_and_bad_rows_too():
    code = f"T{uuid.uuid4().hex[:5].upper()}"
    first = uuid.uuid4()
    await apply({**PAYLOAD, "branches": [
        {"branch_id": str(first), "code": code, "name": "One", "is_active": True}]})
    r = await apply({**PAYLOAD, "branches": [
        {"branch_id": str(uuid.uuid4()), "code": code, "name": "Two", "is_active": True},
        {"branch_id": "not-a-uuid", "code": "X", "name": "Bad"},
        {"branch_id": str(uuid.uuid4()), "code": "", "name": "No code"}]})
    assert r["branches"]["skipped"] == 3 and r["branches"]["created"] == 0
    assert (await branch_by_online(first)).name == "One"


async def test_a_branch_missing_from_a_sync_is_left_alone():
    oid = uuid.uuid4()
    code = f"M{uuid.uuid4().hex[:5].upper()}"
    await apply({**PAYLOAD, "branches": [
        {"branch_id": str(oid), "code": code, "name": "Stays", "is_active": True}]})
    await apply({**PAYLOAD, "branches": []})
    await apply(PAYLOAD)  # an older Online that sends no branches at all
    b = await branch_by_online(oid)
    assert b is not None and b.is_active is True


async def test_the_staff_branch_list_shows_synced_branches(client, owner):
    oid = uuid.uuid4()
    code = f"L{uuid.uuid4().hex[:5].upper()}"
    await apply({**PAYLOAD, "branches": [
        {"branch_id": str(oid), "code": code, "name": "Listed", "is_active": True}]})
    rows = (await client.get("/branches", headers=owner.headers)).json()
    mine = next(r for r in rows if r["code"] == code)
    assert mine["online_branch_id"] == str(oid)


async def test_an_empty_product_list_changes_nothing():
    await apply(PAYLOAD)  # make sure we hold products
    with pytest.raises(SupplyError, match="empty product list"):
        await apply({"products": []})
