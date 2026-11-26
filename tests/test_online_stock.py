"""Sending received stock to SRIBEESonline: the request, the answers, the failures."""

import json
import uuid
from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.services import online_stock
from app.services.errors import SupplyError
from tests.test_supplier_auth import settings_override

ON = {"online_api_url": "http://online.test", "online_service_token": "t" * 32}


def arrived(**extra) -> online_stock.Arrived:
    product = extra.pop("product", uuid.uuid4())
    values = dict(line_id=uuid.uuid4(), online_product_id=product,
                  online_variant_id=uuid.uuid4(), units=60, free_units=0,
                  value=Decimal("12600.00"))
    return online_stock.Arrived(**{**values, **extra})


def client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_free_units_are_stock_and_share_the_cost():
    line = online_stock.payload_line(arrived(units=10, free_units=2, value=Decimal("1000.00"),
                                             expiry_date=date(2027, 1, 31)))
    assert (line["units"], line["purchase_cost"], line["expires_on"]) == (12, "83.33",
                                                                          "2027-01-31")
    assert "listed_price" not in line and "grams" not in line  # Online keeps its price


def test_a_default_pack_size_is_sent_without_a_variant():
    pid = uuid.uuid4()
    assert online_stock.payload_line(arrived(product=pid, online_variant_id=pid))[
        "variant_id"] is None
    assert online_stock.payload_line(arrived())["variant_id"] is not None


def test_weighed_goods_go_in_grams_with_typed_prices():
    line = online_stock.payload_line(arrived(units=1, weight_kg=Decimal("24.750"),
                                             listed_price=Decimal("300.00"),
                                             selling_price=Decimal("280.00")))
    assert (line["grams"], line["listed_price"], line["selling_price"]) == (24750, "300.00",
                                                                           "280.00")


async def test_send_reads_each_answer():
    lines = [arrived(), arrived(), arrived()]
    seen = {}

    def online(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        states = ["added", "already", "refused"]
        return httpx.Response(200, json={"results": [
            {"line_id": ln["line_id"], "status": s, "reason": "kept per pack size"}
            for ln, s in zip(seen["body"]["lines"], states, strict=True)]})

    branch = uuid.uuid4()
    with settings_override(**ON):
        async with client(online) as c:
            got = await online_stock.send(c, branch, "RCV-000009", lines)
    assert [(a.sent, a.reason) for a in got] == [(True, None), (True, None),
                                                 (False, "kept per pack size")]
    assert seen["auth"] == f"Bearer {'t' * 32}"
    assert seen["body"]["branch_id"] == str(branch)
    assert seen["body"]["receipt_no"] == "RCV-000009"


@pytest.mark.parametrize("response,words", [
    (httpx.Response(404, json={}), "switched off"),
    (httpx.Response(409, json={}), "409"),
    (httpx.Response(200, json={"results": []}), "every line"),
])
async def test_send_fails_loudly_so_it_is_retried(response, words):
    with settings_override(**ON):
        async with client(lambda _: response) as c:
            with pytest.raises(SupplyError, match=words):
                await online_stock.send(c, uuid.uuid4(), "R", [arrived()])


async def test_unreachable_or_not_set_up():
    def down(request):
        raise httpx.ConnectError("down", request=request)

    async with client(down) as c:
        with settings_override(**ON):
            with pytest.raises(SupplyError, match="cannot be reached"):
                await online_stock.send(c, uuid.uuid4(), "R", [arrived()])
        with settings_override(online_api_url="", online_service_token=""):
            with pytest.raises(SupplyError, match="not set up"):
                await online_stock.send(c, uuid.uuid4(), "R", [arrived()])
