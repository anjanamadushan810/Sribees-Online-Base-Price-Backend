"""Automatic draft orders (owner MCQ 2026-11-26): below the minimum, up to the maximum, in
units, for the supplier we last bought from (or the one staff chose); drafts only."""

import uuid
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select

from app.services import reorder
from tests.conftest import make_branch, sign_in
from tests.stock_helpers import D, fresh_supplier
from tests.test_supplier_auth import settings_override

ON = {"online_api_url": "http://online.test", "online_service_token": "t" * 32}


def level(**over) -> reorder.Level:
    values = dict(product_id=uuid.uuid4(), stock_mode="unit", available=30, minimum=40,
                  maximum=100, visible=True, product_active=True)
    return reorder.Level(**{**values, **over})


@pytest.mark.parametrize("over,coming,need", [
    ({}, 0, 70),                       # the owner's example: max 100, stock 30
    ({}, 50, 20),                      # already on its way
    ({}, 90, 0),
    ({"available": 40}, 0, 0),         # at the minimum is not below it
    ({"maximum": None}, 0, 0),         # no maximum: never ordered
    ({"available": -3}, 0, 100),       # reserved more than on hand
])
def test_units_needed(over, coming, need):
    assert reorder.units_needed(level(**over), coming) == need


def test_parse_levels_uses_available():
    (lv,) = reorder.parse_levels({"items": [{
        "product_id": str(uuid.uuid4()), "stock_quantity": 30, "reserved_quantity": 5,
        "low_stock_threshold": 40, "max_stock_level": 100, "visible": True}]})
    assert (lv.available, lv.minimum, lv.maximum) == (25, 40, 100)


@pytest.mark.parametrize("hour_now,last,due", [
    (6, None, False), (7, None, True), (9, "2026-11-27", False), (9, "2026-11-26", True)])
def test_the_daily_run_is_once_after_seven(hour_now, last, due):
    now = datetime(2026, 11, 27, hour_now, 30, tzinfo=reorder.orders.COLOMBO).astimezone(UTC)
    assert reorder.due_today(now, last, 7) is due
    assert reorder.due_today(now, None, -1) is False


async def _item(name: str):
    """An Online product with its default pack size (id = product id)."""
    from app.core.db import get_sessionmaker
    from app.models import OnlineProduct, OnlineVariant

    pid = uuid.uuid4()
    async with get_sessionmaker()() as s, s.begin():
        s.add(OnlineProduct(online_product_id=pid, name=f"{name} {pid.hex[:4]}"))
        await s.flush()
        s.add(OnlineVariant(online_variant_id=pid, online_product_id=pid, name=""))
    return pid


async def _bought(client, owner, branch, supplier, item, price):
    r = await client.post("/quick-buys", headers=owner.headers, json={
        "supplier_id": supplier["supplier_id"], "branch_id": str(branch),
        "received_date": D("2026-11-25"),
        "lines": [{"online_variant_id": str(item), "units": 1, "unit_price": price}]})
    assert r.status_code == 201, r.text


def online(levels: dict, seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"items": [
            {"product_id": str(pid), "stock_quantity": stock, "reserved_quantity": 0,
             "low_stock_threshold": mn, "max_stock_level": mx, "visible": True}
            for pid, (stock, mn, mx) in levels.items()]})
    return handler


async def _run(branch_id, handler, user_id=None):
    from app.core.db import get_sessionmaker
    from app.models import Branch

    async with get_sessionmaker()() as s, s.begin():
        branch = await s.get(Branch, uuid.UUID(str(branch_id)))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            with settings_override(**ON):
                return await reorder.run_branch(s, c, branch, user_id=user_id)


async def test_a_run_makes_one_draft_per_supplier_and_lists_the_rest(client, owner, branch):
    from app.core.db import get_sessionmaker
    from app.models import SupplyOrder

    nishi, other = await fresh_supplier(client, owner), await fresh_supplier(client, owner)
    soap, tea, salt, rice, new = [await _item(n) for n in ("Soap", "Tea", "Salt", "Rice", "New")]
    await _bought(client, owner, branch, other, soap, "90.00")
    await _bought(client, owner, branch, nishi, soap, "95.00")  # bought from Nishi last
    await _bought(client, owner, branch, nishi, tea, "300.00")
    await _bought(client, owner, branch, other, salt, "60.00")
    levels = {soap: (30, 40, 100), tea: (5, 10, 25), salt: (50, 10, 80),
              rice: (1, 10, None), new: (0, 5, 20)}
    seen: list = []
    result = await _run(branch, online(levels, seen))
    assert seen[0].url.path == "/internal/supply/stock-levels"
    (draft,) = result["drafts"]
    assert draft["supplier_id"] == nishi["supplier_id"] and draft["added"] == 2
    reasons = {x["online_product_id"]: x["reason"] for x in result["left_out"]}
    assert reasons == {str(rice): "no_maximum", str(new): "no_supplier"}
    order = (await client.get(f"/orders/{draft['order_id']}", headers=owner.headers)).json()
    assert order["status"] == "draft"  # nothing is sent by itself
    got = {ln["online_variant_id"]: (ln["units"], ln["unit_price"]) for ln in order["lines"]}
    assert got == {str(soap): (70, "95.00"), str(tea): (20, "300.00")}

    # staff change the soap to 48; the next run adds nothing and keeps their number
    line = next(ln for ln in order["lines"] if ln["online_variant_id"] == str(soap))
    r = await client.patch(f"/orders/{draft['order_id']}/lines/{line['line_id']}",
                           headers=owner.headers, json={"units": 48})
    assert r.status_code == 200, r.text
    again = await _run(branch, online(levels, []))
    assert again["added"] == 0
    async with get_sessionmaker()() as s:
        drafts = list(await s.scalars(select(SupplyOrder).where(
            SupplyOrder.supplier_id == uuid.UUID(nishi["supplier_id"]),
            SupplyOrder.is_auto_draft.is_(True))))
    assert len(drafts) == 1
    order = (await client.get(f"/orders/{draft['order_id']}", headers=owner.headers)).json()
    assert next(ln["units"] for ln in order["lines"] if ln["online_variant_id"] == str(soap)) == 48

    # once that draft is submitted, a further drop does not order the soap twice
    assert (await client.post(f"/orders/{draft['order_id']}/submit",
                              headers=owner.headers)).status_code == 200
    third = await _run(branch, online({soap: (20, 40, 100)}, []))
    (d3,) = third["drafts"]
    assert d3["order_id"] != draft["order_id"] and d3["added"] == 1
    o3 = (await client.get(f"/orders/{d3['order_id']}", headers=owner.headers)).json()
    assert [ln["units"] for ln in o3["lines"]] == [100 - 20 - 48]


async def test_the_chosen_main_supplier_wins(client, owner, branch):
    nishi, other = await fresh_supplier(client, owner), await fresh_supplier(client, owner)
    item = await _item("Milk")
    await _bought(client, owner, branch, nishi, item, "1200.00")
    staff = await sign_in(client, "accountant")
    body = {"online_variant_id": str(item), "supplier_id": other["supplier_id"]}
    assert (await client.put("/reorder/main-supplier", headers=staff.headers,
                             json=body)).status_code == 403
    assert (await client.put("/reorder/main-supplier", headers=owner.headers,
                             json=body)).status_code == 204
    result = await _run(branch, online({item: (0, 5, 12)}, []))
    (draft,) = result["drafts"]
    assert draft["supplier_id"] == other["supplier_id"]
    order = (await client.get(f"/orders/{draft['order_id']}", headers=owner.headers)).json()
    assert [(ln["units"], ln["unit_price"]) for ln in order["lines"]] == [(12, "1200.00")]


async def test_the_api_and_a_branch_without_online(client, owner, branch):
    plain = await make_branch("Not linked")
    r = await client.post("/reorder/run", headers=owner.headers, json={"branch_id": plain})
    assert r.status_code == 404
    rows = (await client.get("/reorder", headers=owner.headers)).json()
    assert all(r["branch_id"] != plain for r in rows)
    with settings_override(**ON):
        r = await client.post("/reorder/run", headers=owner.headers,
                              json={"branch_id": str(branch)})
    assert r.status_code in (422, 502, 503), r.text  # Online is not reachable in tests
