"""Received goods reach SRIBEESonline: only while switched on, retried until answered."""

import json
import uuid

import httpx
from sqlalchemy import select

from app.services import online_stock
from tests.stock_helpers import approved_order, fresh_supplier, new_product, receive
from tests.test_supplier_auth import settings_override

ON = {"online_stock_in": True, "online_api_url": "http://online.test",
      "online_service_token": "t" * 32}


async def _received(client, owner, branch, **extra):
    supplier = await fresh_supplier(client, owner)
    jam = await new_product(client, owner, name="Jam online")
    oid, (line,) = await approved_order(owner, supplier, branch, [(jam, 10, "100.00")])
    r = await receive(client, owner, oid, [{"order_line_id": line, "units": 6,
                                            "free_units": 1, **extra}])
    assert r.status_code == 201, r.text
    return r.json()


async def _state(client, owner, receipt):
    r = await client.get(f"/receipts/{receipt['receipt_id']}/online-stock", headers=owner.headers)
    assert r.status_code == 200, r.text
    return r.json()


async def _round(handler) -> dict:
    from app.core.db import get_sessionmaker

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        return await online_stock.run_once(get_sessionmaker(), c)


def _answer(status: str, sent: list):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        return httpx.Response(200, json={"results": [
            {"line_id": ln["line_id"], "status": status, "reason": "choose which size"}
            for ln in body["lines"]]})
    return handler


async def _drain() -> None:
    """Earlier tests may have left rows waiting."""
    with settings_override(**ON):
        await _round(_answer("added", []))


async def test_nothing_is_sent_for_receipts_saved_while_off(client, owner, branch):
    await _drain()
    receipt = await _received(client, owner, branch)
    assert await _state(client, owner, receipt) == []
    sent: list = []
    with settings_override(**ON):
        assert await _round(_answer("added", sent)) == {}
    assert sent == []


async def test_sent_once_and_retried_until_online_answers(client, owner, branch):
    from app.core.db import get_sessionmaker
    from app.models import Branch

    await _drain()
    with settings_override(**ON):
        receipt = await _received(client, owner, branch, listed_price="150.00",
                                  selling_price="140.00")
        assert [x["status"] for x in await _state(client, owner, receipt)] == ["pending"]

        assert await _round(lambda _: httpx.Response(500)) == {"waiting": 1}
        row = (await _state(client, owner, receipt))[0]
        assert (row["status"], row["attempts"]) == ("pending", 1) and "500" in row["reason"]

        sent: list = []
        assert await _round(_answer("added", sent)) == {"sent": 1}
        assert await _round(_answer("added", sent)) == {}  # never twice
    async with get_sessionmaker()() as s:
        online = (await s.get(Branch, uuid.UUID(str(branch)))).online_branch_id
    (body,) = sent
    assert body["branch_id"] == str(online) and body["receipt_no"] == receipt["receipt_no"]
    (line,) = body["lines"]
    assert line["line_id"] == receipt["lines"][0]["receipt_line_id"]
    assert (line["units"], line["listed_price"], line["selling_price"]) == (7, "150.00",
                                                                           "140.00")
    assert line["purchase_cost"] == "85.71"  # 600 over 7 pieces
    assert (await _state(client, owner, receipt))[0]["status"] == "sent"


async def test_a_refused_line_waits_for_staff(client, owner, accountant, branch):
    from app.core.db import get_sessionmaker
    from app.models import OnlineStockSend

    await _drain()
    with settings_override(**ON):
        receipt = await _received(client, owner, branch)
        assert await _round(_answer("refused", [])) == {"refused": 1}
        assert await _round(_answer("added", [])) == {}  # not retried by itself
        row = (await _state(client, owner, receipt))[0]
        assert (row["status"], row["reason"]) == ("refused", "choose which size")
        url = f"/receipts/{receipt['receipt_id']}/online-stock/retry"
        assert (await client.post(url, headers=accountant.headers)).status_code == 403
        assert (await client.post(url, headers=owner.headers)).json() == {"queued": 1}
        assert (await client.post(url, headers=owner.headers)).status_code == 409
        assert await _round(_answer("already", [])) == {"sent": 1}
    async with get_sessionmaker()() as s:
        got = await s.scalar(select(OnlineStockSend).where(
            OnlineStockSend.receipt_id == uuid.UUID(receipt["receipt_id"])))
    assert got.status == "sent"


async def test_a_selling_price_above_the_listed_one_is_refused(client, owner, branch):
    supplier = await fresh_supplier(client, owner)
    jam = await new_product(client, owner, name="Jam price")
    oid, (line,) = await approved_order(owner, supplier, branch, [(jam, 1, "100.00")])
    r = await receive(client, owner, oid, [{"order_line_id": line, "units": 1,
                                            "listed_price": "150.00",
                                            "selling_price": "160.00"}])
    assert r.status_code == 422 and "above the listed" in r.json()["detail"]
