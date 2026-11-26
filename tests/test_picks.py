"""Pack scan (joint-go-live plan, phase E): Online's packers scan QR labels; Online's order
events settle the picks (delivered / cancelled / returned)."""

import hashlib
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.services import labels as label_svc
from tests.stock_helpers import balance, batch_qty, new_product, stocked
from tests.test_supplier_auth import settings_override

TOKEN = "online-pick-test-token"  # noqa: S105
SHA = hashlib.sha256(TOKEN.encode()).hexdigest()
H = {"Authorization": f"Bearer {TOKEN}"}
BACK = f"{date.today()}T05:00:00+00:00"  # return_received_at: the goods are back


@pytest.fixture(autouse=True)
def _token():
    with settings_override(online_pick_token_sha256=SHA):
        yield


async def _online_branch(branch) -> uuid.UUID:
    from app.core.db import get_sessionmaker
    from app.models import Branch

    async with get_sessionmaker()() as s:
        return (await s.get(Branch, uuid.UUID(str(branch)))).online_branch_id


async def labelled(client, owner, branch, qty=3, **kw):
    """Stock received at the branch with one QR label per unit."""
    product, batch = await stocked(client, owner, branch, qty=str(qty), **kw)
    rng = (await client.post("/labels/ranges", headers=owner.headers,
                             json={"count": qty})).json()
    nums = [label_svc.make_number(b) for b in range(int(rng["first"][:9]),
                                                     int(rng["last"][:9]) + 1)]
    r = await client.post(f"/stock/batches/{batch['batch_id']}/labels", headers=owner.headers,
                          json={"numbers": nums})
    assert r.status_code == 200, r.text
    return product, batch, nums


def body(order, item, online_branch, product, label, qty=2, **kw):
    return {"online_order_id": str(order), "order_number": "SB-T1", "order_item_id": str(item),
            "online_branch_id": str(online_branch), "variant_id": product["product_id"],
            "quantity": qty, "label": label, "picked_by": "packer@test", **kw}


async def event(to_status, order, number="SB-T1", **extra):
    from app.core.db import get_sessionmaker
    from app.services import picks

    payload = {"order_id": str(order), "order_number": number, "to_status": to_status,
               "lines": extra.pop("lines", []), **extra}
    async with get_sessionmaker()() as s, s.begin():
        return await picks.apply_event(s, {"type": extra.pop("type", "order.status"),
                                           "payload": payload})


async def test_locks(client):
    with settings_override(online_pick_token_sha256=""):
        assert (await client.get(f"/internal/online/orders/{uuid.uuid4()}/picks",
                                 headers=H)).status_code == 404
    assert (await client.get(f"/internal/online/orders/{uuid.uuid4()}/picks",
                             headers={"Authorization": "Bearer nope"})).status_code == 401
    assert (await client.get(f"/internal/online/orders/{uuid.uuid4()}/picks",
                             headers={**H, "X-Forwarded-For": "1.2.3.4"})).status_code == 403


async def test_scan_rules(client, owner, branch, other_branch):
    product, batch, nums = await labelled(client, owner, branch, qty=3, name="Soap PK")
    other, _, other_nums = await labelled(client, owner, branch, qty=1, name="Tea PK")
    ob = await _online_branch(branch)
    order, item = uuid.uuid4(), uuid.uuid4()

    r = await client.post("/internal/online/pick", headers=H,
                          json=body(order, item, ob, product, nums[0]))
    assert r.status_code == 200, r.text and r.json()["scanned_units"] == 1
    r = await client.post("/internal/online/pick", headers=H,
                          json=body(order, item, ob, product, nums[0]))
    assert r.status_code == 409 and "already scanned for this order" in r.json()["detail"]
    r = await client.post("/internal/online/pick", headers=H,
                          json=body(order, item, ob, product, other_nums[0]))
    assert r.status_code == 422 and "not on this item" in r.json()["detail"]
    r = await client.post("/internal/online/pick", headers=H,
                          json=body(order, item, await _online_branch(other_branch), product,
                                    nums[1]))
    assert r.status_code == 422 and "another branch" in r.json()["detail"]
    loose = (await client.post("/labels/ranges", headers=owner.headers,
                               json={"count": 1})).json()["first"]
    r = await client.post("/internal/online/pick", headers=H,
                          json=body(order, item, ob, product, loose))
    assert r.status_code == 422 and "not on any stock" in r.json()["detail"]
    bad = nums[1][:9] + str((int(nums[1][9]) + 1) % 10)
    r = await client.post("/internal/online/pick", headers=H,
                          json=body(order, item, ob, product, bad))
    assert r.status_code == 422 and "check digit" in r.json()["detail"]
    r = await client.post("/internal/online/pick", headers=H,
                          json=body(order, item, ob, product, nums[1]))
    assert r.status_code == 200 and r.json()["scanned_units"] == 2
    r = await client.post("/internal/online/pick", headers=H,
                          json=body(order, item, ob, product, nums[2]))
    assert r.status_code == 422 and "already scanned" in r.json()["detail"]
    # another order cannot take a label held for this one
    r = await client.post("/internal/online/pick", headers=H,
                          json=body(uuid.uuid4(), uuid.uuid4(), ob, product, nums[1], qty=1))
    assert r.status_code == 409 and "SB-T1" in r.json()["detail"]
    # a mistaken scan is taken back, then the label is free for another order
    r = await client.post("/internal/online/unpick", headers=H,
                          json={"online_order_id": str(order), "label": nums[1]})
    assert r.status_code == 200 and r.json()["items"][0]["scanned_units"] == 1
    r = await client.post("/internal/online/pick", headers=H,
                          json=body(uuid.uuid4(), uuid.uuid4(), ob, product, nums[1], qty=1))
    assert r.status_code == 200
    assert other  # (used above)


async def test_expired_batch_is_refused(client, owner, branch):
    today = date.today()
    product, _, nums = await labelled(
        client, owner, branch, qty=1, name="Milk PK", day=str(today - timedelta(days=10)),
        expiry=str(today - timedelta(days=5)))
    r = await client.post("/internal/online/pick", headers=H, json=body(
        uuid.uuid4(), uuid.uuid4(), await _online_branch(branch), product, nums[0], qty=1))
    assert r.status_code == 422 and "expired" in r.json()["detail"]


async def test_delivered_cancelled_returned(client, owner, branch):
    product, batch, nums = await labelled(client, owner, branch, qty=3, name="Jam PK",
                                          price="100.00")
    ob = await _online_branch(branch)
    order, item = uuid.uuid4(), uuid.uuid4()
    for n in nums[:2]:
        assert (await client.post("/internal/online/pick", headers=H,
                                  json=body(order, item, ob, product, n))).status_code == 200
    cogs_before = await balance(client, owner, "5000", branch)
    assert await event("packed", order) == "ignored"
    assert await event("delivered", order, delivered_at=f"{date.today()}T04:00:00+00:00") == \
        "consumed:2"
    assert await event("delivered", order) == "consumed:0"  # a replay changes nothing
    assert Decimal((await batch_qty(client, owner, product))[batch["batch_no"]]) == 1
    assert await balance(client, owner, "5000", branch) - cogs_before == Decimal("200.00")
    look = (await client.get(f"/labels/{nums[0]}", headers=owner.headers)).json()
    assert look["status"] == "used" and look["used_ref"] == "online:SB-T1"
    # a sold label cannot be packed again
    r = await client.post("/internal/online/pick", headers=H,
                          json=body(uuid.uuid4(), uuid.uuid4(), ob, product, nums[0], qty=1))
    assert r.status_code == 409

    # the customer returns one: it goes back into the same batch at its cost
    lines = [{"order_item_id": str(item), "quantity": 2}]
    # a refund with nothing picked up yet is money only (review #3)
    assert await event("refunded", order, lines=lines,
                       returned=[{"order_item_id": str(item), "quantity": 1}]) == "returned:0"
    assert await event("refunded", order, lines=lines, return_received_at=BACK,
                       returned=[{"order_item_id": str(item), "quantity": 1}]) == "returned:1"
    assert Decimal((await batch_qty(client, owner, product))[batch["batch_no"]]) == 2
    assert await balance(client, owner, "5000", branch) - cogs_before == Decimal("100.00")
    back = [n for n in nums[:2]
            if (await client.get(f"/labels/{n}", headers=owner.headers)).json()["status"]
            == "linked"]
    assert len(back) == 1

    # a cancelled order frees its units; nothing leaves the batch
    order2, item2 = uuid.uuid4(), uuid.uuid4()
    assert (await client.post("/internal/online/pick", headers=H,
                              json=body(order2, item2, ob, product, nums[2],
                                        qty=1))).status_code == 200
    assert await event("cancelled", order2) == "released:1"
    assert Decimal((await batch_qty(client, owner, product))[batch["batch_no"]]) == 2
    assert (await client.post("/internal/online/pick", headers=H,
                              json=body(uuid.uuid4(), uuid.uuid4(), ob, product, nums[2],
                                        qty=1))).status_code == 200


async def test_a_bag_sold_by_weight(client, owner, branch):
    from tests.stock_helpers import approved_order, fresh_supplier, receive

    rice = await new_product(client, owner, unit="kg", name="Rice PK")
    supplier = await fresh_supplier(client, owner)
    oid, (line,) = await approved_order(owner, supplier, branch, [(rice, 1, "2500.00")])
    rng = (await client.post("/labels/ranges", headers=owner.headers, json={"count": 1})).json()
    r = await receive(client, owner, oid, [{"order_line_id": line, "units": 1,
                                            "weight_kg": "25", "labels": [rng["first"]]}])
    assert r.status_code == 201, r.text
    ob = await _online_branch(branch)
    order, item = uuid.uuid4(), uuid.uuid4()
    req = body(order, item, ob, rice, rng["first"], qty=2, weight_grams=500)
    r = await client.post("/internal/online/pick", headers=H, json=req)
    assert r.status_code == 422 and "kilograms" in r.json()["detail"]
    assert (await client.post("/internal/online/pick", headers=H,
                              json={**req, "weight_kg": "0.5"})).status_code == 200
    r = await client.post("/internal/online/pick", headers=H, json={**req, "weight_kg": "0.6"})
    assert r.status_code == 200 and Decimal(r.json()["scanned_kg"]) == Decimal("1.1")
    r = await client.post("/internal/online/pick", headers=H, json={**req, "weight_kg": "0.1"})
    assert r.status_code == 422 and "more than" in r.json()["detail"]
    # the same bag serves another order at the same time
    other = body(uuid.uuid4(), uuid.uuid4(), ob, rice, rng["first"], qty=1, weight_grams=1000,
                 weight_kg="1")
    assert (await client.post("/internal/online/pick", headers=H, json=other)).status_code == 200
    assert await event("delivered", order) == "consumed:2"
    rows = (await client.get("/stock/batches", headers=owner.headers,
                             params={"product_id": rice["product_id"]})).json()
    assert Decimal(rows[0]["qty_on_hand"]) == Decimal("23.9")
    look = (await client.get(f"/labels/{rng['first']}", headers=owner.headers)).json()
    assert look["status"] == "linked"  # the bag is not empty yet


async def test_worker_reads_online_events_once(client, owner, branch):
    import httpx

    from app.core.db import get_sessionmaker
    from app.services import online_events

    product, batch, nums = await labelled(client, owner, branch, qty=1, name="Oil PK")
    ob = await _online_branch(branch)
    order, item = uuid.uuid4(), uuid.uuid4()
    assert (await client.post("/internal/online/pick", headers=H,
                              json=body(order, item, ob, product, nums[0],
                                        qty=1))).status_code == 200
    async with get_sessionmaker()() as s:
        from app.models import SyncState
        start = int((await s.get(SyncState, "online_events")).value["seq"])
    seen = []

    def online(request: httpx.Request) -> httpx.Response:
        after = int(request.url.params["after"])
        seen.append(after)
        evs = [{"seq": start + 1, "type": "order.status",
                "payload": {"order_id": str(order), "order_number": "SB-W1",
                            "to_status": "delivered", "lines": []}},
               {"seq": start + 2, "type": "wallet.movement", "payload": {}}]
        return httpx.Response(200, json={"events": [e for e in evs if e["seq"] > after],
                                         "enabled": True})

    with settings_override(online_api_url="http://online.test", online_service_token="x"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(online)) as http:
            assert await online_events.run_once(get_sessionmaker(), http) == {"consumed": 1}
            assert await online_events.run_once(get_sessionmaker(), http) == {}
    assert seen == [start, start + 2]
    assert batch["batch_no"] not in await batch_qty(client, owner, product)  # emptied



async def test_review_fixes(client, owner, branch, monkeypatch):
    """Review 2026-11-26: RTO frees the units (#4); held units cannot be written off (#2);
    a weighed bag never comes back twice (#5); a retried bag scan is one pick (#8)."""
    from tests.stock_helpers import D, approved_order, fresh_supplier, receive

    product, batch, nums = await labelled(client, owner, branch, qty=2, name="Salt RV")
    ob = await _online_branch(branch)
    order, item = uuid.uuid4(), uuid.uuid4()
    for n in nums:
        assert (await client.post("/internal/online/pick", headers=H,
                                  json=body(order, item, ob, product, n))).status_code == 200
    r = await client.post("/stock/write-offs", headers=owner.headers, json={
        "branch_id": str(branch), "reason": "damaged", "adjustment_date": D("2026-11-20"),
        "lines": [{"batch_id": batch["batch_id"], "qty": "1"}]})
    assert r.status_code == 422 and "held for Online orders" in r.json()["detail"]
    assert await event("rto_initiated", order) == "released:2"
    r = await client.post("/stock/write-offs", headers=owner.headers, json={
        "branch_id": str(branch), "reason": "damaged", "adjustment_date": D("2026-11-20"),
        "lines": [{"batch_id": batch["batch_id"], "qty": "1"}]})
    assert r.status_code == 201, r.text

    rice = await new_product(client, owner, unit="kg", name="Rice RV")
    supplier = await fresh_supplier(client, owner)
    oid, (line,) = await approved_order(owner, supplier, branch, [(rice, 1, "2500.00")])
    rng = (await client.post("/labels/ranges", headers=owner.headers, json={"count": 1})).json()
    assert (await receive(client, owner, oid, [{"order_line_id": line, "units": 1,
                                                "weight_kg": "10", "labels": [rng["first"]]}])
            ).status_code == 201
    order2, item2 = uuid.uuid4(), uuid.uuid4()
    req = body(order2, item2, ob, rice, rng["first"], qty=2, weight_grams=1000, weight_kg="2")
    first = (await client.post("/internal/online/pick", headers=H, json=req)).json()
    again = (await client.post("/internal/online/pick", headers=H, json=req)).json()
    assert first == again and Decimal(again["scanned_kg"]) == 2
    lines = [{"order_item_id": str(item2), "quantity": 2}]
    assert await event("delivered", order2, lines=lines) == "consumed:1"
    got = await event("refunded", order2, lines=lines, type="order.undelivered",
                      undelivered=[{"order_item_id": str(item2), "quantity": 1}])
    assert got == "undelivered:1"
    assert await event("refunded", order2, lines=lines, return_received_at=BACK,
                       returned=[{"order_item_id": str(item2), "quantity": 2}]) == "returned:1"
    assert await event("refunded", order2, lines=lines, return_received_at=BACK,
                       returned=[{"order_item_id": str(item2), "quantity": 2}]) == "returned:0"
    rows = (await client.get("/stock/batches", headers=owner.headers,
                             params={"product_id": rice["product_id"]})).json()
    assert Decimal(rows[0]["qty_on_hand"]) == 10  # all 2 kg back once, never 3


async def test_worker_waits_at_a_hole_and_never_stalls(client, owner, branch, monkeypatch):
    import httpx

    from app.core.db import get_sessionmaker
    from app.models import OnlineEventFailure, SyncState
    from app.services import online_events, picks

    async with get_sessionmaker()() as s:
        start = int((await s.get(SyncState, "online_events")).value["seq"])
    now = __import__("datetime").datetime.now(__import__("datetime").UTC)
    young, old = now.isoformat(), (now - timedelta(minutes=10)).isoformat()
    evs = {"events": []}

    def online(request: httpx.Request) -> httpx.Response:
        after = int(request.url.params["after"])
        return httpx.Response(200, json={"events": [e for e in evs["events"] if e["seq"] > after],
                                         "enabled": True})

    bad = str(uuid.uuid4())
    real = picks.apply_event

    async def flaky(db, ev):
        if ev["payload"].get("order_id") == bad:
            raise picks.SupplyError("the batch is short")
        return await real(db, ev)

    monkeypatch.setattr(picks, "apply_event", flaky)
    def ev(seq, at, oid):
        return {"seq": seq, "event_id": str(uuid.uuid4()), "type": "order.status",
                "occurred_at": at, "payload": {"order_id": oid, "to_status": "cancelled"}}
    with settings_override(online_api_url="http://online.test", online_service_token="x"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(online)) as http:
            evs["events"] = [ev(start + 2, young, str(uuid.uuid4()))]  # start+1 not committed yet
            assert await online_events.run_once(get_sessionmaker(), http) == {"waiting": 1}
            evs["events"] = [ev(start + 2, old, bad), ev(start + 3, old, str(uuid.uuid4()))]
            assert await online_events.run_once(get_sessionmaker(), http) == {
                "failed": 1, "released": 1}
    async with get_sessionmaker()() as s:
        assert int((await s.get(SyncState, "online_events")).value["seq"]) == start + 3
        failure = await s.get(OnlineEventFailure, start + 2)
        assert failure is not None and "short" in failure.error



async def test_worker_is_quiet_while_online_has_no_events_route():
    import httpx

    from app.core.db import get_sessionmaker
    from app.services import online_events

    with settings_override(online_api_url="http://online.test", online_service_token="x"):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda r: httpx.Response(404, json={"detail": "Not Found"}))) as http:
            assert await online_events.run_once(get_sessionmaker(), http) == {}
