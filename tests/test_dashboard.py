"""Home page numbers: every card equals the list behind it, by role and branch."""

from decimal import Decimal

from tests.conftest import make_branch, sign_in
from tests.stock_helpers import D, fresh_supplier
from tests.test_online_catalog import CHERAMY_75
from tests.test_orders import line, new_order, shop  # noqa: F401  (fixture)


def row(body: dict, key: str):
    return next((r for r in body["rows"] if r["key"] == key), None)


async def today(client, who) -> dict:
    r = await client.get("/dashboard/today", headers=who.headers)
    assert r.status_code == 200, r.text
    return r.json()


async def listed(client, who, status: str) -> int:
    r = await client.get("/orders", headers=who.headers, params={"status": status})
    return len(r.json())


async def add_soap_and_submit(client, staff, shop, boxes=2):  # noqa: F811
    o = await new_order(client, staff, shop)
    await client.post(f"/orders/{o['order_id']}/lines", headers=staff.headers,
                      json=line(shop["soap"], boxes * 150, "125"))
    return o["order_id"]


async def test_today_matches_the_lists(client, owner, purchasing, shop):  # noqa: F811
    oid = await add_soap_and_submit(client, purchasing, shop)
    before = await today(client, owner)
    assert row(before, "drafts")["count"] == await listed(client, owner, "draft")
    await client.post(f"/orders/{oid}/submit", headers=purchasing.headers)
    after = await today(client, owner)
    assert row(after, "approve")["count"] == await listed(client, owner, "submitted")
    assert row(after, "approve")["count"] == row(before, "approve")["count"] + 1
    assert row(after, "drafts")["count"] == row(before, "drafts")["count"] - 1
    assert row(after, "approve")["route"] == "/orders?status=submitted"
    held = await client.get("/held-offers", headers=owner.headers)
    assert row(after, "held")["count"] == len(held.json())


async def test_rows_follow_the_role(client, owner, purchasing, branch_manager, shop):  # noqa: F811
    accountant = await sign_in(client, "accountant")
    owner_keys = {r["key"] for r in (await today(client, owner))["rows"]}
    assert {"approve", "unopened", "declined", "drafts", "held"} <= owner_keys
    purchasing_keys = {r["key"] for r in (await today(client, purchasing))["rows"]}
    assert "approve" not in purchasing_keys and "to_match" not in purchasing_keys
    acc_keys = {r["key"] for r in (await today(client, accountant))["rows"]}
    assert "to_match" not in acc_keys and "drafts" not in acc_keys and "held" in acc_keys
    assert (await client.get("/dashboard/today")).status_code == 401


async def test_a_branch_manager_counts_only_their_branch(client, owner, purchasing, shop):  # noqa: F811
    other = await make_branch("Other branch")
    manager = await sign_in(client, "branch_manager", branch_ids=[other])
    await add_soap_and_submit(client, purchasing, shop)  # in the shop's branch, not theirs
    body = await client.get("/dashboard/orders-summary", headers=manager.headers)
    assert body.status_code == 200
    assert sum(body.json()["stages"].values()) == 0
    assert body.json()["week_orders"] == 0


async def test_orders_summary_stages_and_week_value(client, owner, purchasing, shop):  # noqa: F811
    r = await client.get("/dashboard/orders-summary", headers=owner.headers)
    s0 = r.json()
    oid = await add_soap_and_submit(client, purchasing, shop, boxes=2)  # 2 x 150 x 125
    await client.post(f"/orders/{oid}/submit", headers=purchasing.headers)
    s1 = (await client.get("/dashboard/orders-summary", headers=owner.headers)).json()
    assert s1["stages"]["waiting_approval"] == s0["stages"]["waiting_approval"] + 1
    assert Decimal(s1["week_value"]) - Decimal(s0["week_value"]) == Decimal("37500.00")
    assert s1["week_orders"] == s0["week_orders"] + 1
    await client.post(f"/orders/{oid}/approve", headers=owner.headers)
    s2 = (await client.get("/dashboard/orders-summary", headers=owner.headers)).json()
    assert s2["stages"]["sent"] == s1["stages"]["sent"] + 1
    # the supplier opens it: sent -> opened
    await client.get(f"/supplier/orders/{oid}", headers=shop["sh"])
    s3 = (await client.get("/dashboard/orders-summary", headers=owner.headers)).json()
    assert s3["stages"]["opened"] == s2["stages"]["opened"] + 1
    assert s3["stages"]["sent"] == s2["stages"]["sent"] - 1
    # every stage equals the list behind it
    st = s3["stages"]
    assert st["waiting_approval"] == await listed(client, owner, "submitted")
    assert st["sent"] + st["opened"] == await listed(client, owner, "approved")
    for k in ("draft", "accepted", "declined", "rejected", "received"):
        assert st[k] == await listed(client, owner, k), k


async def test_base_price_insight_finds_a_saving(client, owner, branch, shop):  # noqa: F811
    """Owner 2026-11-26: we compare what suppliers really charged us (bill prices)."""
    dear, cheap = await fresh_supplier(client, owner), await fresh_supplier(client, owner)
    for sup, price in ((cheap, "100.00"), (dear, "125.00")):  # the last buy was the dear one
        r = await client.post("/quick-buys", headers=owner.headers, json={
            "supplier_id": sup["supplier_id"], "branch_id": str(branch),
            "received_date": D("2026-11-25"),
            "lines": [{"online_variant_id": shop["soap"], "units": 10, "unit_price": price}]})
        assert r.status_code == 201, r.text
    body = (await client.get("/dashboard/base-price-insight", headers=owner.headers)).json()
    item = next(i for i in body["can_save"] if i["online_product_id"] == str(CHERAMY_75))
    assert (item["base_price"], item["best_price"], item["saving"]) == ("125.00", "100.00", "25.00")
    assert item["saving_pct"] == 20.0 and item["best_supplier"] == cheap["name"]
