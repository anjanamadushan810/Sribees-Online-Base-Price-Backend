"""Delivered batches with a new shop price wait for the branch manager (plan 2026-11-27, P6)."""

import json
import uuid
from decimal import Decimal

import httpx

from app.services import online_stock
from tests.stock_helpers import EXP, approved_order, fresh_supplier, new_product, receive
from tests.test_supplier_auth import settings_override

ON = {"online_stock_in": True, "online_api_url": "http://online.test",
      "online_service_token": "t" * 32}


async def _round(handler) -> dict:
    from app.core.db import get_sessionmaker

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        return await online_stock.run_once(get_sessionmaker(), c)


def _online(listed, selling, old_listed, old_selling, sent):
    batch = str(uuid.uuid4())

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        return httpx.Response(200, json={"results": [
            {"line_id": ln["line_id"], "status": "added", "batch_id": batch,
             "listed_price": listed, "selling_price": selling,
             "old_listed_price": old_listed, "old_selling_price": old_selling}
            for ln in body["lines"]]})
    return handler, batch


async def _linked_branch(branch):
    from app.core.db import get_sessionmaker
    from app.models import Branch

    async with get_sessionmaker()() as s, s.begin():
        b = await s.get(Branch, uuid.UUID(str(branch)))
        if b.online_branch_id is None:
            b.online_branch_id = uuid.uuid4()


async def _drain():
    with settings_override(**ON):
        await _round(_online("1.00", "1.00", "1.00", "1.00", [])[0])


async def _delivered(client, owner, branch, mrp="220.00", price="150.00"):
    await _linked_branch(branch)
    await _drain()
    supplier = await fresh_supplier(client, owner)
    soap = await new_product(client, owner, name="Soap PR")
    oid, (line,) = await approved_order(owner, supplier, branch, [(soap, 4, price)])
    with settings_override(**ON):
        r = await receive(client, owner, oid, [{"order_line_id": line, "units": 4,
                                                "mrp": mrp, "expiry_date": EXP}])
    assert r.status_code == 201, r.text
    return r.json()["lines"][0]["receipt_line_id"]


async def test_a_new_mrp_waits_for_review_and_the_manager_changes_it(
        client, owner, branch_manager, branch):
    rid = await _delivered(client, owner, branch)
    sent: list = []
    handler, batch = _online("220.00", "198.00", "200.00", "180.00", sent)
    with settings_override(**ON):
        await _round(handler)
    assert sent[0]["lines"][0]["mrp"] == "220.00"  # the MRP goes to Online
    rows = (await client.get("/price-reviews", headers=owner.headers)).json()
    row = next(r for r in rows if r["receipt_line_id"] == rid)
    assert (row["old_selling_price"], row["selling_price"], row["base_price"]) == (
        "180.00", "198.00", "150.00")

    calls: list = []

    def online(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"selling_price": "195.00"})

    real = httpx.AsyncClient

    class Fake(real):
        def __init__(self, *a, **k):
            super().__init__(transport=httpx.MockTransport(online))

    import app.api.price_reviews as api_mod
    api_mod.httpx.AsyncClient = Fake
    try:
        with settings_override(**ON):
            above = await client.post(f"/price-reviews/{rid}", headers=owner.headers,
                                      json={"selling_price": "230.00"})
            assert above.status_code == 422 and "MRP" in above.text
            r = await client.post(f"/price-reviews/{rid}", headers=owner.headers,
                                  json={"selling_price": "195.00"})
    finally:
        api_mod.httpx.AsyncClient = real
    assert r.status_code == 200 and r.json()["status"] == "changed", r.text
    assert calls[0][0] == f"/internal/supply/batches/{batch}/price"
    assert Decimal(calls[0][1]["selling_price"]) == Decimal("195.00")
    rows = (await client.get("/price-reviews", headers=owner.headers)).json()
    assert rid not in {r["receipt_line_id"] for r in rows}
    again = await client.post(f"/price-reviews/{rid}", headers=owner.headers,
                              json={"selling_price": "195.00"})
    assert again.status_code == 409


async def test_unchanged_price_needs_no_review_and_kept_needs_no_online_call(
        client, owner, branch):
    rid = await _delivered(client, owner, branch, mrp="200.00")
    with settings_override(**ON):
        await _round(_online("200.00", "180.00", "200.00", "180.00", [])[0])
    rows = (await client.get("/price-reviews", headers=owner.headers)).json()
    assert rid not in {r["receipt_line_id"] for r in rows}

    rid2 = await _delivered(client, owner, branch)
    with settings_override(**ON):
        await _round(_online("220.00", "198.00", "200.00", "180.00", [])[0])
    r = await client.post(f"/price-reviews/{rid2}", headers=owner.headers,
                          json={"selling_price": "198.00"})  # as it is: Online untouched
    assert r.status_code == 200 and r.json()["status"] == "kept", r.text


async def test_below_cost_only_the_owner(client, owner, branch_manager, branch):
    from tests.conftest import sign_in

    rid = await _delivered(client, owner, branch, price="210.00")
    with settings_override(**ON):
        await _round(_online("220.00", "198.00", "200.00", "180.00", [])[0])
    rows = (await client.get("/price-reviews", headers=owner.headers)).json()
    assert next(r for r in rows if r["receipt_line_id"] == rid)["below_cost"] is True
    manager = await sign_in(client, "branch_manager", [branch])
    r = await client.post(f"/price-reviews/{rid}", headers=manager.headers,
                          json={"selling_price": "198.00"})
    assert r.status_code == 403 and "below what we paid" in r.text
