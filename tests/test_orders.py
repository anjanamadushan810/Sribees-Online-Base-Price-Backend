"""Supplier orders end to end, base prices, and who may do what."""

import uuid

import pytest
from sqlalchemy import select, text

from tests.conftest import make_branch, sign_in
from tests.stock_helpers import D
from tests.test_catalog import bearer
from tests.test_online_catalog import CHERAMY_75, COKE_15, PAYLOAD
from tests.test_supplier_auth import signed_in_supplier


async def outbox_events(order_id: str) -> list[str]:
    from app.core.db import get_sessionmaker
    from app.models import OutboxEvent

    async with get_sessionmaker()() as s:
        rows = (await s.scalars(select(OutboxEvent).order_by(OutboxEvent.seq))).all()
    return [r.event for r in rows if r.payload.get("order_id") == order_id]


@pytest.fixture
async def shop(client, owner, purchasing):
    """A supplier on the app, our catalogue synced, and a branch. Orders name OUR pack
    sizes (owner 2026-11-26); a plain product's pack size has the product's own id."""
    from app.core.db import get_sessionmaker
    from app.services import online_catalog

    supplier, _, tokens = await signed_in_supplier(client, owner)
    async with get_sessionmaker()() as s, s.begin():
        await online_catalog.apply(s, PAYLOAD, user_id=None)
    branch = await make_branch("Matugama-test")
    async with get_sessionmaker()() as s, s.begin():  # receiving needs Online's branch id
        from app.models import Branch
        b = await s.get(Branch, uuid.UUID(branch))
        b.online_branch_id = uuid.uuid4()
    return {"supplier_id": supplier["supplier_id"], "sh": bearer(tokens),
            "soap": str(CHERAMY_75), "coke": str(COKE_15), "branch": branch}


def line(variant: str, units: int, price: str) -> dict:
    return {"online_variant_id": variant, "units": units, "unit_price": price}


async def new_order(client, staff, shop):
    r = await client.post("/orders", headers=staff.headers,
                          json={"supplier_id": shop["supplier_id"], "branch_id": shop["branch"]})
    assert r.status_code == 201, r.text
    return r.json()


async def test_order_flow_and_base_price(client, owner, purchasing, shop):
    """Owner 2026-11-26: we order in OUR product names; the base price is the bill price
    staff enter at receiving, not the order's price."""
    o = await new_order(client, purchasing, shop)
    oid = o["order_id"]
    assert o["order_no"].startswith("SPO-") and o["status"] == "draft"

    r = await client.post(f"/orders/{oid}/lines", headers=purchasing.headers,
                          json=line(shop["soap"], 300, "125"))
    assert r.status_code == 200, r.text
    ln = r.json()["lines"][0]
    assert (ln["units"], ln["unit_price"], ln["line_total"]) == (300, "125.00", "37500.00")
    assert ln["heading"] == "Baby Cheramy Soap" and ln["offer_id"] is None
    r = await client.post(f"/orders/{oid}/lines", headers=purchasing.headers,
                          json=line(shop["soap"], 10, "125"))
    assert r.status_code == 409  # the same item twice
    r = await client.patch(f"/orders/{oid}/lines/{ln['line_id']}",
                           headers=purchasing.headers, json={"units": 450})
    assert r.json()["goods_total"] == "56250.00"
    await client.post(f"/orders/{oid}/lines", headers=purchasing.headers,
                      json=line(shop["coke"], 12, "369"))

    # the supplier sees nothing before approval
    assert (await client.get("/supplier/orders", headers=shop["sh"])).json() == []
    r = await client.post(f"/orders/{oid}/submit", headers=purchasing.headers)
    assert r.json()["status"] == "submitted"
    r = await client.post(f"/orders/{oid}/lines", headers=purchasing.headers,
                          json=line(shop["coke"], 1, "369"))
    assert r.status_code == 409  # not a draft any more
    assert (await client.post(f"/orders/{oid}/approve",
                              headers=purchasing.headers)).status_code == 403
    r = await client.post(f"/orders/{oid}/approve", headers=owner.headers)
    assert r.json()["status"] == "approved"

    mine = (await client.get(f"/supplier/orders/{oid}", headers=shop["sh"])).json()
    names = sorted(x["heading"] for x in mine["lines"])
    assert names == ["Baby Cheramy Soap", "Coca Cola 1.5L"]  # our names, not theirs
    assert all("online_product_id" not in x for x in mine["lines"])
    r = await client.post(f"/supplier/orders/{oid}/answer", headers=shop["sh"],
                          json={"accept": True, "delivery_charge": "300", "note": "හෙට"})
    assert r.status_code == 200, r.text
    acc = r.json()
    assert acc["status"] == "accepted" and acc["total"] == "60978.00"
    bp = (await client.get(f"/base-prices/{CHERAMY_75}", headers=purchasing.headers)).json()
    assert all(h["order_id"] != oid for h in bp["history"])  # not from the order's price
    assert await outbox_events(oid) == ["order.submitted", "order.approved", "supplier.notify",
                                        "order.accepted"]

    # receiving: the bill said 120, not 125
    staff = (await client.get(f"/orders/{oid}", headers=purchasing.headers)).json()
    soap_line = next(x for x in staff["lines"] if x["online_variant_id"] == shop["soap"])
    from app.core.db import get_sessionmaker
    from app.models import OnlineProduct

    async with get_sessionmaker()() as s, s.begin():  # the supplier's Rs 300 goes by weight
        (await s.get(OnlineProduct, CHERAMY_75)).weight_grams = 80
    r = await client.post(f"/orders/{oid}/receipts", headers=owner.headers, json={
        "received_date": D("2026-11-26"), "invoice_no": "N-1",
        "lines": [{"order_line_id": soap_line["line_id"], "units": 450, "unit_price": "120"}]})
    assert r.status_code == 201, r.text
    assert r.json()["lines"][0]["unit_price"] == "120.00"
    # only the soap came: it carries the whole charge, 120 + 300/450 a piece
    assert r.json()["transport_charge"] == "300.00"
    bp = (await client.get(f"/base-prices/{CHERAMY_75}", headers=purchasing.headers)).json()
    assert [(h["unit_price"], h["bill_price"]) for h in bp["history"]
            if h["order_id"] == oid] == [("120.67", "120.00")]
    current = (await client.get("/base-prices", headers=purchasing.headers)).json()
    row = next(c for c in current if c["online_product_id"] == str(CHERAMY_75))
    assert row["order_id"] == oid and row["unit_price"] == "120.67"
    # the next order is offered the BILL price: transport never creeps into it
    found = (await client.get("/online-items", headers=purchasing.headers,
                              params={"q": "Cheramy"})).json()
    assert next(x for x in found if x["online_variant_id"] == shop["soap"])[
        "last_price"] == "120.00"


async def test_cancel_voids_nothing_received(client, owner, purchasing, shop):
    o = await new_order(client, purchasing, shop)
    oid = o["order_id"]
    await client.post(f"/orders/{oid}/lines", headers=purchasing.headers,
                      json=line(shop["soap"], 150, "125"))
    await client.post(f"/orders/{oid}/submit", headers=owner.headers)
    r = await client.post(f"/orders/{oid}/cancel", headers=purchasing.headers,
                          json={"reason": "supplier cannot deliver this week"})
    assert r.json()["status"] == "cancelled"
    mine = (await client.get(f"/supplier/orders/{oid}", headers=shop["sh"])).json()
    assert mine["status"] == "cancelled" and "deliver" in mine["cancel_reason"]


async def test_an_item_no_longer_sold_cannot_be_submitted(client, owner, shop):
    from app.core.db import get_sessionmaker
    from app.models import OnlineVariant

    o = await new_order(client, owner, shop)
    await client.post(f"/orders/{o['order_id']}/lines", headers=owner.headers,
                      json=line(shop["coke"], 12, "369"))
    async with get_sessionmaker()() as s, s.begin():
        (await s.get(OnlineVariant, COKE_15)).is_active = False
    r = await client.post(f"/orders/{o['order_id']}/submit", headers=owner.headers)
    assert r.status_code == 409 and "no longer sold" in r.json()["detail"]
    async with get_sessionmaker()() as s, s.begin():
        (await s.get(OnlineVariant, COKE_15)).is_active = True


async def test_owner_submit_counts_as_approval(client, owner, shop):
    o = await new_order(client, owner, shop)
    await client.post(f"/orders/{o['order_id']}/lines", headers=owner.headers,
                      json=line(shop["soap"], 150, "125"))
    r = await client.post(f"/orders/{o['order_id']}/submit", headers=owner.headers)
    assert r.json()["status"] == "approved"


async def test_decline_writes_no_base_price(client, owner, shop):
    o = await new_order(client, owner, shop)
    oid = o["order_id"]
    await client.post(f"/orders/{oid}/lines", headers=owner.headers,
                      json=line(shop["coke"], 24, "369"))
    await client.post(f"/orders/{oid}/submit", headers=owner.headers)
    r = await client.post(f"/supplier/orders/{oid}/answer", headers=shop["sh"],
                          json={"accept": False, "note": "stock නෑ"})
    assert r.json()["status"] == "declined"
    bp = (await client.get(f"/base-prices/{COKE_15}", headers=owner.headers)).json()
    assert all(h["order_id"] != oid for h in bp["history"])


async def test_empty_order_cannot_be_submitted(client, purchasing, shop):
    o = await new_order(client, purchasing, shop)
    r = await client.post(f"/orders/{o['order_id']}/submit", headers=purchasing.headers)
    assert r.status_code == 422


async def test_other_supplier_cannot_see_or_answer(client, owner, shop):
    o = await new_order(client, owner, shop)
    await client.post(f"/orders/{o['order_id']}/lines", headers=owner.headers,
                      json=line(shop["soap"], 150, "125"))
    await client.post(f"/orders/{o['order_id']}/submit", headers=owner.headers)
    _, _, other = await signed_in_supplier(client, owner)
    h = bearer(other)
    assert (await client.get(f"/supplier/orders/{o['order_id']}", headers=h)).status_code == 404
    r = await client.post(f"/supplier/orders/{o['order_id']}/answer", headers=h,
                          json={"accept": True})
    assert r.status_code == 404
    assert (await client.get("/supplier/orders", headers=h)).json() == []


async def test_branch_manager_requests_only_for_own_branch(client, purchasing, shop):
    other_branch = await make_branch("Other")
    manager = await sign_in(client, "branch_manager", branch_ids=[shop["branch"]])
    r = await client.post("/orders", headers=manager.headers,
                          json={"supplier_id": shop["supplier_id"], "branch_id": other_branch})
    assert r.status_code == 403
    o = await new_order(client, manager, shop)
    r = await client.post(f"/orders/{o['order_id']}/lines", headers=manager.headers,
                          json=line(shop["soap"], 150, "125"))
    assert r.status_code == 200
    assert (await client.post(f"/orders/{o['order_id']}/submit",
                              headers=manager.headers)).status_code == 403
    # an order for another branch is invisible to the manager
    theirs = (await client.post("/orders", headers=purchasing.headers,
                                json={"supplier_id": shop["supplier_id"],
                                      "branch_id": other_branch})).json()
    assert (await client.get(f"/orders/{theirs['order_id']}",
                             headers=manager.headers)).status_code == 404
    listed = (await client.get("/orders", headers=manager.headers)).json()
    assert {x["branch_id"] for x in listed} <= {shop["branch"]}
    # purchasing submits the manager's request
    r = await client.post(f"/orders/{o['order_id']}/submit", headers=purchasing.headers)
    assert r.json()["status"] == "submitted"


async def test_database_refuses_line_changes_after_submit(client, owner, shop):
    from app.core.db import get_sessionmaker

    o = await new_order(client, owner, shop)
    await client.post(f"/orders/{o['order_id']}/lines", headers=owner.headers,
                      json=line(shop["soap"], 150, "125"))
    await client.post(f"/orders/{o['order_id']}/submit", headers=owner.headers)
    with pytest.raises(Exception, match="only while the order is a draft"):
        async with get_sessionmaker()() as s, s.begin():
            await s.execute(text("UPDATE supply_order_lines SET boxes = 99 "
                                 "WHERE order_id = :o"), {"o": o["order_id"]})


async def test_delivery_charge_is_capped(client, owner, shop):
    o = await new_order(client, owner, shop)
    oid = o["order_id"]
    await client.post(f"/orders/{oid}/lines", headers=owner.headers,
                      json=line(shop["soap"], 150, "125"))  # 150 x 125 = 18,750
    await client.post(f"/orders/{oid}/submit", headers=owner.headers)
    r = await client.post(f"/supplier/orders/{oid}/answer", headers=shop["sh"],
                          json={"accept": True, "delivery_charge": "9999999"})
    assert r.status_code == 422 and "4,687.50" in r.json()["detail"]
    r = await client.post(f"/supplier/orders/{oid}/answer", headers=shop["sh"],
                          json={"accept": True, "delivery_charge": "4687.50"})
    assert r.status_code == 200


async def test_branch_manager_cannot_edit_purchasings_draft(client, purchasing, shop):
    manager = await sign_in(client, "branch_manager", branch_ids=[shop["branch"]])
    o = await new_order(client, purchasing, shop)
    r = await client.post(f"/orders/{o['order_id']}/lines", headers=manager.headers,
                          json=line(shop["soap"], 150, "125"))
    assert r.status_code == 403


async def test_cancel_note_is_staff_only(client, owner, shop):
    o = await new_order(client, owner, shop)
    oid = o["order_id"]
    await client.post(f"/orders/{oid}/lines", headers=owner.headers,
                      json=line(shop["soap"], 150, "125"))
    await client.post(f"/orders/{oid}/submit", headers=owner.headers)
    r = await client.post(f"/orders/{oid}/cancel", headers=owner.headers,
                          json={"reason": "branch is full this week",
                                "note": "found it cheaper elsewhere"})
    assert r.json()["cancel_note"] == "found it cheaper elsewhere"
    mine = (await client.get(f"/supplier/orders/{oid}", headers=shop["sh"])).json()
    assert mine["cancel_reason"] == "branch is full this week"
    assert "cancel_note" not in mine and "cheaper" not in str(mine)
