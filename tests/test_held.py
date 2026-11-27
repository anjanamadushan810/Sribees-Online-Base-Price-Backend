"""Held stock (owner 2026-11-27): part of a delivery kept aside without QR labels, in stock
and Finance at once, not on Online; later taken in by sticking labels on (a release)."""

import json
from decimal import Decimal

import httpx
from sqlalchemy import func, select

from app.services import held
from tests.stock_helpers import approved_order, fresh_supplier, new_product, receive
from tests.test_drafts import EXP, _draft, _finish, _labels, _line, _order
from tests.test_supplier_auth import settings_override

ON = {"online_stock_in": True, "online_api_url": "http://online.test",
      "online_service_token": "t" * 32}


async def _held(client, who):
    r = await client.get("/stock/held", headers=who.headers)
    assert r.status_code == 200, r.text
    return r.json()


async def _mine(client, who, batch_no_prefix):
    return [x for x in (await _held(client, who))["lines"]
            if x["batch_no"].startswith(batch_no_prefix)]


async def test_a_part_held_without_qr_then_taken_in(client, owner, branch):
    oid, (l1,), _ = await _order(client, owner, branch, [("Ramen HS", 48, "450.00")])
    qr = await _labels(client, owner, 30)
    d = await _draft(client, owner, oid)
    # a held part takes no QR labels; the shop part takes one per unit as before
    r = await _line(client, owner, d["draft_id"], order_line_id=l1, units=36, hold=True,
                    labels=[qr[0]])
    assert r.status_code == 422 and "no labels" in r.text
    r = await _line(client, owner, d["draft_id"], order_line_id=l1, units=12, labels=qr[:12])
    assert r.status_code == 201, r.text
    r = await _line(client, owner, d["draft_id"], order_line_id=l1, units=36, hold=True,
                    labels=[], mrp="520.00")
    assert r.status_code == 201, r.text
    assert [x["hold"] for x in r.json()["lines"]] == [False, True]
    rec = (await _finish(client, owner, d["draft_id"], invoice_total="21600.00")).json()
    assert rec["total"] == "21600.00"  # all 48 are in stock and Finance at once
    assert [(x["units"], x["hold"]) for x in rec["lines"]] == [(12, False), (36, True)]
    no = rec["receipt_no"]

    rows = await _mine(client, owner, no)
    assert [(x["held"], x["released"], x["left"], x["expiry_date"]) for x in rows] == [
        (36, 0, 36, str(EXP))]
    line_id, batch_id = rows[0]["receipt_line_id"], rows[0]["batch_id"]

    # labels go on a held batch only through a release (so Online hears of them)
    r = await client.post(f"/stock/batches/{batch_id}/labels", headers=owner.headers,
                          json={"numbers": [qr[12]]})
    assert r.status_code == 409 and "Held stock" in r.text
    # a label held by a delivery being received elsewhere is refused
    d2 = await _draft(client, owner, (await _order(client, owner, branch,
                                                   [("Other HS", 1, "10.00")]))[0])
    assert (await client.post(f"/receipt-drafts/{d2['draft_id']}/labels", headers=owner.headers,
                              json={"number": qr[29]})).status_code in (200, 201)
    r = await client.post(f"/stock/held/{line_id}/release", headers=owner.headers,
                          json={"numbers": [qr[29]]})
    assert r.status_code == 409

    r = await client.post(f"/stock/held/{line_id}/release", headers=owner.headers,
                          json={"numbers": qr[12:22]})
    assert r.status_code == 200, r.text
    assert (r.json()["units"], r.json()["left"], r.json()["online"]) == (10, 26, "off")
    rows = await _mine(client, owner, no)
    assert [(x["released"], x["left"]) for x in rows] == [(10, 26)]
    # more than is left, or the same label twice, is refused
    r = await client.post(f"/stock/held/{line_id}/release", headers=owner.headers,
                          json={"numbers": qr[12:14]})
    assert r.status_code == 409
    many = await _labels(client, owner, 27)
    r = await client.post(f"/stock/held/{line_id}/release", headers=owner.headers,
                          json={"numbers": many})
    assert r.status_code == 422 and "only 26" in r.text
    summary = (await client.get("/stock/held/summary", headers=owner.headers)).json()
    assert summary["units"] >= 26 and summary["lines"] >= 1


async def test_weighed_goods_cannot_be_held(client, owner, branch):
    supplier = await fresh_supplier(client, owner)
    rice = await new_product(client, owner, unit="kg", name="Rice HS")
    oid, (line,) = await approved_order(owner, supplier, branch, [(rice, 10, "200.00")])
    r = await receive(client, owner, oid, [{"order_line_id": line, "units": 10,
                                            "weight_kg": "10", "hold": True}])
    assert r.status_code == 422 and "weight" in r.text


async def test_held_goods_reach_online_only_when_taken_in(client, owner, branch):
    from app.core.db import get_sessionmaker
    from app.models import OnlineStockSend, ReceiptLine, StockRelease

    supplier = await fresh_supplier(client, owner)
    jam = await new_product(client, owner, name="Jam HS")
    oid, (line,) = await approved_order(owner, supplier, branch, [(jam, 10, "100.00")])
    with settings_override(**ON):
        r = await receive(client, owner, oid, [
            {"order_line_id": line, "units": 4},
            {"order_line_id": line, "units": 6, "hold": True, "mrp": "130.00"}])
        assert r.status_code == 201, r.text
        rec = r.json()
        async with get_sessionmaker()() as s:
            sends = await s.scalar(select(func.count()).select_from(OnlineStockSend).where(
                OnlineStockSend.receipt_id == rec["receipt_id"]))
            assert sends == 1  # only the shop part; the held part waits for its labels
            held_line = await s.scalar(select(ReceiptLine).where(
                ReceiptLine.receipt_id == rec["receipt_id"], ReceiptLine.hold.is_(True)))
        qr = await _labels(client, owner, 3)
        r = await client.post(f"/stock/held/{held_line.receipt_line_id}/release",
                              headers=owner.headers, json={"numbers": qr})
        assert r.status_code == 200 and r.json()["online"] == "pending"

        sent: list = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            sent.append(body)
            return httpx.Response(200, json={"results": [
                {"line_id": ln["line_id"], "status": "added"} for ln in body["lines"]]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            counts = await held.run_once(get_sessionmaker(), c)
        assert counts.get("sent", 0) >= 1
        mine = [ln for b in sent for ln in b["lines"]
                if ln["line_id"] == r.json()["release_id"] or str(ln["line_id"]) == str(
                    r.json()["release_id"])]
        assert len(mine) == 1
        assert (mine[0]["units"], mine[0]["purchase_cost"], mine[0]["mrp"]) == (
            3, "100.00", "130.00")
        async with get_sessionmaker()() as s:
            rel = await s.get(StockRelease, r.json()["release_id"])
            assert (rel.send_status, rel.units) == ("sent", 3)
        # sending again sends nothing more
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            await held.run_once(get_sessionmaker(), c)
        assert sum(1 for b in sent for ln in b["lines"]
                   if str(ln["line_id"]) == str(r.json()["release_id"])) == 1
    assert Decimal(rec["total"]) == Decimal("1000.00")
