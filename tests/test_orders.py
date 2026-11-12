"""Supplier orders end to end, base prices, and who may do what."""

import pytest
from sqlalchemy import select, text

from tests.conftest import make_branch, sign_in
from tests.test_catalog import FIXTURE, RESOLUTIONS, bearer
from tests.test_matching import CHERAMY_75, COKE_15, PAYLOAD
from tests.test_supplier_auth import signed_in_supplier


async def outbox_events(order_id: str) -> list[str]:
    from app.core.db import get_sessionmaker
    from app.models import OutboxEvent

    async with get_sessionmaker()() as s:
        rows = (await s.scalars(select(OutboxEvent).order_by(OutboxEvent.seq))).all()
    return [r.event for r in rows if r.payload.get("order_id") == order_id]


@pytest.fixture
async def shop(client, owner, purchasing):
    """A supplier with the real list imported, two offers matched, and a branch."""
    from app.core.db import get_sessionmaker
    from app.services import online_catalog

    supplier, _, tokens = await signed_in_supplier(client, owner)
    sh = bearer(tokens)
    r = await client.post("/supplier/catalog/import", headers=sh,
                          json={"text": FIXTURE, "resolutions": RESOLUTIONS})
    assert r.status_code == 200, r.text
    async with get_sessionmaker()() as s, s.begin():
        await online_catalog.apply(s, PAYLOAD, user_id=None)
    cat = (await client.get("/supplier/catalog", headers=sh)).json()
    offers = {(p["heading"], o["label"]): o["offer_id"]
              for sec in cat["sections"] for p in sec["products"] for o in p["offers"]}
    soap = offers[("බේබි ශෙරමි සබන් 75g", "")]
    coke = offers[("කොකාකෝලා,ස්ප්‍රයිට්", "1.5L")]
    for offer_id, pid in ((soap, CHERAMY_75), (coke, COKE_15)):
        r = await client.post(f"/matching/offers/{offer_id}/confirm", headers=purchasing.headers,
                              json={"online_product_id": str(pid)})
        assert r.status_code == 204
    branch = await make_branch("Matugama-test")
    return {"supplier_id": supplier["supplier_id"], "sh": sh, "soap": soap, "coke": coke,
            "unmatched": offers[("ලයිෆ්බෝයි සබන්", "95g")], "branch": branch}


async def new_order(client, staff, shop):
    r = await client.post("/orders", headers=staff.headers,
                          json={"supplier_id": shop["supplier_id"], "branch_id": shop["branch"]})
    assert r.status_code == 201, r.text
    return r.json()


async def test_order_flow_and_base_price(client, owner, purchasing, shop):
    o = await new_order(client, purchasing, shop)
    oid = o["order_id"]
    assert o["order_no"].startswith("SPO-") and o["status"] == "draft"

    r = await client.post(f"/orders/{oid}/lines", headers=purchasing.headers,
                          json={"offer_id": shop["unmatched"], "boxes": 1})
    assert r.status_code == 409 and "match" in r.json()["detail"]
    r = await client.post(f"/orders/{oid}/lines", headers=purchasing.headers,
                          json={"offer_id": shop["soap"], "boxes": 2})
    assert r.status_code == 200, r.text
    line = r.json()["lines"][0]
    assert (line["units"], line["unit_price"], line["line_total"]) == (300, "125.00", "37500.00")
    r = await client.post(f"/orders/{oid}/lines", headers=purchasing.headers,
                          json={"offer_id": shop["soap"], "boxes": 1})
    assert r.status_code == 409
    r = await client.patch(f"/orders/{oid}/lines/{line['line_id']}",
                           headers=purchasing.headers, json={"boxes": 3})
    assert r.json()["goods_total"] == "56250.00"
    await client.post(f"/orders/{oid}/lines", headers=purchasing.headers,
                      json={"offer_id": shop["coke"], "boxes": 1})

    # the supplier sees nothing before approval
    assert (await client.get("/supplier/orders", headers=shop["sh"])).json() == []
    assert (await client.get(f"/supplier/orders/{oid}", headers=shop["sh"])).status_code == 404

    r = await client.post(f"/orders/{oid}/submit", headers=purchasing.headers)
    assert r.json()["status"] == "submitted"
    r = await client.post(f"/orders/{oid}/lines", headers=purchasing.headers,
                          json={"offer_id": shop["unmatched"], "boxes": 1})
    assert r.status_code == 409  # not a draft any more
    assert (await client.post(f"/orders/{oid}/approve",
                              headers=purchasing.headers)).status_code == 403
    r = await client.post(f"/orders/{oid}/approve", headers=owner.headers)
    assert r.json()["status"] == "approved"

    # a later price change by the supplier does not touch the order
    r = await client.patch(f"/supplier/catalog/offers/{shop['soap']}", headers=shop["sh"],
                           json={"price": "128"})
    assert r.status_code == 200
    mine = (await client.get(f"/supplier/orders/{oid}", headers=shop["sh"])).json()
    soap_line = next(ln for ln in mine["lines"] if ln["offer_id"] == shop["soap"])
    assert soap_line["unit_price"] == "125.00"
    assert "online_product_id" not in soap_line  # the supplier never sees our side
    staff = (await client.get(f"/orders/{oid}", headers=purchasing.headers)).json()
    assert staff["supplier_seen_at"] is not None

    r = await client.post(f"/supplier/orders/{oid}/answer", headers=shop["sh"],
                          json={"accept": True, "delivery_charge": "300", "note": "හෙට"})
    assert r.status_code == 200, r.text
    acc = r.json()
    assert acc["status"] == "accepted" and acc["delivery_charge"] == "300.00"
    # soap 3 boxes x 150 x 125 + coke 1.5L 1 box x 12 x 369 + delivery 300
    assert acc["goods_total"] == "60678.00" and acc["total"] == "60978.00"
    r = await client.post(f"/supplier/orders/{oid}/answer", headers=shop["sh"],
                          json={"accept": False})
    assert r.status_code == 409

    bp = (await client.get(f"/base-prices/{CHERAMY_75}", headers=purchasing.headers)).json()
    assert [h["unit_price"] for h in bp["history"] if h["order_id"] == oid] == ["125.00"]
    assert [o["price"] for o in bp["offers"]] == ["128.00"]  # what we could pay today
    current = (await client.get("/base-prices", headers=purchasing.headers)).json()
    row = next(c for c in current if c["online_product_id"] == str(CHERAMY_75))
    assert row["order_id"] == oid and row["unit_price"] == "125.00"

    assert await outbox_events(oid) == ["order.submitted", "order.approved", "supplier.notify",
                                        "order.accepted"]

    r = await client.post(f"/orders/{oid}/cancel", headers=purchasing.headers,
                          json={"reason": "supplier cannot deliver this week"})
    assert r.json()["status"] == "cancelled"
    current = (await client.get("/base-prices", headers=purchasing.headers)).json()
    assert all(c["order_id"] != oid for c in current)
    mine = (await client.get(f"/supplier/orders/{oid}", headers=shop["sh"])).json()
    assert mine["status"] == "cancelled" and "deliver" in mine["cancel_reason"]


async def test_owner_submit_counts_as_approval(client, owner, shop):
    o = await new_order(client, owner, shop)
    await client.post(f"/orders/{o['order_id']}/lines", headers=owner.headers,
                      json={"offer_id": shop["soap"], "boxes": 1})
    r = await client.post(f"/orders/{o['order_id']}/submit", headers=owner.headers)
    assert r.json()["status"] == "approved"


async def test_decline_writes_no_base_price(client, owner, shop):
    o = await new_order(client, owner, shop)
    oid = o["order_id"]
    await client.post(f"/orders/{oid}/lines", headers=owner.headers,
                      json={"offer_id": shop["coke"], "boxes": 2})
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
                      json={"offer_id": shop["soap"], "boxes": 1})
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
                          json={"offer_id": shop["soap"], "boxes": 1})
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
                      json={"offer_id": shop["soap"], "boxes": 1})
    await client.post(f"/orders/{o['order_id']}/submit", headers=owner.headers)
    with pytest.raises(Exception, match="only while the order is a draft"):
        async with get_sessionmaker()() as s, s.begin():
            await s.execute(text("UPDATE supply_order_lines SET boxes = 99 "
                                 "WHERE order_id = :o"), {"o": o["order_id"]})
