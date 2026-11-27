"""Goods close to expiry (plan 2026-11-27, P7): alert days from Online, else 30."""

import uuid
from datetime import date, timedelta

from tests.stock_helpers import approved_order, fresh_supplier, new_product, receive

TODAY = date.today()


async def _received(client, owner, branch, name, days_ahead):
    supplier = await fresh_supplier(client, owner)
    item = await new_product(client, owner, name=name)
    oid, (line,) = await approved_order(owner, supplier, branch, [(item, 2, "10.00")])
    exp = str(TODAY + timedelta(days=days_ahead))
    r = await receive(client, owner, oid, [{"order_line_id": line, "units": 2,
                                            "expiry_date": exp}], day=str(TODAY))
    assert r.status_code == 201, r.text
    return item, r.json()["lines"][0]["batch_id"]


async def _online_days(product_id, days):
    from app.core.db import get_sessionmaker
    from app.models import OnlineProduct, OnlineVariant

    async with get_sessionmaker()() as s, s.begin():
        v = await s.get(OnlineVariant, uuid.UUID(product_id))
        (await s.get(OnlineProduct, v.online_product_id)).expiry_alert_days = days


async def test_near_follows_alert_days_and_expired_shows(client, owner, branch):
    soon, b1 = await _received(client, owner, branch, "Bread EX", 2)
    far, b2 = await _received(client, owner, branch, "Soap EX", 50)
    body = (await client.get("/stock/expiring", headers=owner.headers)).json()
    got = {i["batch_id"]: i for i in body["items"]}
    assert got[b1]["state"] == "near" and got[b1]["days_left"] == 2
    assert got[b1]["labels_on_shelf"] == 0 and got[b1]["alert_days"] == 30
    assert b2 not in got  # 50 days away, 30 by default
    await _online_days(far["product_id"], 60)
    got = {i["batch_id"]: i for i in (await client.get(
        "/stock/expiring", headers=owner.headers)).json()["items"]}
    assert got[b2]["alert_days"] == 60

    from app.core.db import get_sessionmaker
    from app.services import expiry
    async with get_sessionmaker()() as s:
        later = await expiry.expiring(s, None, today=TODAY + timedelta(days=3))
    mine = next(i for i in later["items"] if str(i["batch_id"]) == b1)
    assert mine["state"] == "expired" and mine["days_left"] == -1


async def test_catalog_sync_keeps_alert_days():
    from app.services.online_catalog import _days

    assert [_days(x) for x in (3, "60", 0, 400, None, "x")] == [3, 60, None, None, None, None]
