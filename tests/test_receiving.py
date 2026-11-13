"""Receiving against a supply order (phase B; replaces Finance's GRN): back-orders,
landed cost, the journal Finance is asked to post, and who may receive."""

import json
import os
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from tests.conftest import sign_in
from tests.stock_helpers import (
    approved_order,
    balance,
    batch_qty,
    fresh_supplier,
    journal_requests,
    new_product,
    receive,
)

JOURNAL_CONTRACT = Path(__file__).parent / "fixtures" / "contract_journal_requested.json"


async def _events(order_id: str, kind: str) -> list[dict]:
    from app.core.db import get_sessionmaker
    from app.models import OutboxEvent

    async with get_sessionmaker()() as s:
        rows = await s.scalars(select(OutboxEvent).where(OutboxEvent.event == kind)
                               .order_by(OutboxEvent.seq))
        return [r.payload for r in rows if r.payload.get("order_id") == order_id]


async def test_short_delivery_is_a_back_order(client, owner, branch):
    supplier = await fresh_supplier(client, owner)
    jam = await new_product(client, owner, name="Jam")
    tea = await new_product(client, owner, track_expiry=True, name="Tea")
    oid, (jam_line, tea_line) = await approved_order(
        owner, supplier, branch, [(jam, 10, "100.00"), (tea, 5, "450.00")])
    inventory = await balance(client, owner, "1300", branch)
    grni = await balance(client, owner, "2020", branch)

    # 8 of 10 jars and 2 free, no tea yet; the invoice comes with the goods
    r = await receive(client, owner, oid, [
        {"order_line_id": jam_line, "units": 8, "free_units": 2}],
        invoice_no="INV-77", invoice_total="800.00")
    assert r.status_code == 201, r.text
    first = r.json()
    assert first["receipt_no"].startswith("RCV-") and first["total"] == "800.00"
    line = first["lines"][0]
    assert (line["units"], line["free_units"], line["qty"], line["line_value"]) == (
        8, 2, "10", "800.00")
    order = (await client.get(f"/orders/{oid}", headers=owner.headers)).json()
    assert order["status"] == "approved"  # still open for the rest

    # landed cost: Rs 800 over 10 jars (8 paid + 2 free)
    batches = (await client.get("/stock/batches", headers=owner.headers,
                                params={"product_id": jam["product_id"]})).json()
    assert [(b["qty_on_hand"], b["unit_cost"]) for b in batches] == [("10", "80.0000")]
    assert await balance(client, owner, "1300", branch) - inventory == Decimal("800.00")
    assert await balance(client, owner, "2020", branch) - grni == Decimal("-800.00")

    # more than is still due is refused; tea needs its expiry
    r = await receive(client, owner, oid, [{"order_line_id": jam_line, "units": 3}])
    assert r.status_code == 422 and "only 2 of 10" in r.json()["detail"]
    r = await receive(client, owner, oid, [{"order_line_id": tea_line, "units": 5}])
    assert r.status_code == 422 and "expiry" in r.json()["detail"]

    r = await receive(client, owner, oid, [
        {"order_line_id": jam_line, "units": 2},
        {"order_line_id": tea_line, "units": 5, "expiry_date": "2027-06-30",
         "mfg_date": "2026-09-01", "mrp": "520.00"}], day="2026-10-04")
    assert r.status_code == 201, r.text
    assert (await client.get(f"/orders/{oid}", headers=owner.headers)).json()["status"] == \
        "received"
    r = await receive(client, owner, oid, [{"order_line_id": jam_line, "units": 1}])
    assert r.status_code == 409

    posted = await _events(oid, "receipt.posted")
    assert [(e["outstanding_units"], e["order_status"]) for e in posted] == [
        (7, "approved"), (0, "received")]
    assert posted[0]["invoice_no"] == "INV-77" and posted[0]["invoice_total"] == "800.00"
    assert posted[1]["total"] == "2450.00"  # 2 x 100 + 5 x 450
    notes = await _events(oid, "supplier.notify")
    assert [(n["kind"], n["received_units"], n["ordered_units"]) for n in notes] == [
        ("received", 8, 15), ("received", 15, 15)]
    receipts = (await client.get(f"/orders/{oid}/receipts", headers=owner.headers)).json()
    assert [r["total"] for r in receipts] == ["800.00", "2450.00"]


async def test_journal_request_is_balanced_and_names_the_online_branch(client, owner, branch):
    from app.core.db import get_sessionmaker
    from app.models import Branch

    supplier = await fresh_supplier(client, owner)
    oil = await new_product(client, owner, name="Oil")
    oid, (line,) = await approved_order(owner, supplier, branch, [(oil, 4, "612.50")])
    rcv = (await receive(client, owner, oid, [{"order_line_id": line, "units": 4}])).json()
    async with get_sessionmaker()() as s:
        online = str((await s.get(Branch, branch)).online_branch_id)
    ref = f"receipt:{rcv['receipt_id']}"
    j = next(j for j in await journal_requests() if j["source_ref"] == ref)
    assert j["source"] == "purchasing" and j["total"] == "2450.00"
    assert [(ln["account_code"], ln["debit"], ln["credit"], ln["online_branch_id"])
            for ln in j["lines"]] == [("1300", "2450.00", "0.00", online),
                                      ("2020", "0.00", "2450.00", online)]
    # asking again for the same document gives the first request back
    from app.services import ledger

    async with get_sessionmaker()() as s, s.begin():
        again = await ledger.post_journal(
            s, entry_date=date(2026, 10, 2), source="purchasing", source_ref=ref, memo="again",
            lines=[ledger.LineIn("1300", branch, debit=Decimal("1")),
                   ledger.LineIn("2020", branch, credit=Decimal("1"))], user_id=None)
    assert str(again.journal_id) == j["event_id"]
    # the contract Finance's tests read (refresh with SUPPLY_EVENTS_DUMP=1)
    if os.environ.get("SUPPLY_EVENTS_DUMP"):
        from app.models import OutboxEvent
        from app.services.finance_link import envelope

        async with get_sessionmaker()() as s:
            row = await s.scalar(select(OutboxEvent).where(
                OutboxEvent.event_id == uuid.UUID(j["event_id"])))
        JOURNAL_CONTRACT.write_text(json.dumps(envelope(row), ensure_ascii=False, indent=2)
                                    + "\n", encoding="utf-8")


async def test_close_the_rest_and_no_cancel_after_goods_arrived(client, owner, branch):
    supplier = await fresh_supplier(client, owner)
    rice = await new_product(client, owner, name="Rice pack")
    oid, (line,) = await approved_order(owner, supplier, branch, [(rice, 10, "200.00")])
    r = await client.post(f"/orders/{oid}/close-rest", headers=owner.headers,
                          json={"reason": "supplier out of stock"})
    assert r.status_code == 409 and "cancel" in r.json()["detail"]
    assert (await receive(client, owner, oid, [{"order_line_id": line, "units": 6}])
            ).status_code == 201
    r = await client.post(f"/orders/{oid}/cancel", headers=owner.headers,
                          json={"reason": "changed my mind"})
    assert r.status_code == 409 and "close the rest" in r.json()["detail"]
    r = await client.post(f"/orders/{oid}/close-rest", headers=owner.headers,
                          json={"reason": "supplier out of stock"})
    assert r.status_code == 204
    order = (await client.get(f"/orders/{oid}", headers=owner.headers)).json()
    assert order["status"] == "received"
    closed = await _events(oid, "order.rest_closed")
    assert closed[0]["outstanding_units"] == 4 and closed[0]["reason"] == "supplier out of stock"


async def test_weighed_goods_take_their_kilograms(client, owner, branch):
    supplier = await fresh_supplier(client, owner)
    dhal = await new_product(client, owner, unit="kg", name="Dhal loose")
    oid, (line,) = await approved_order(owner, supplier, branch, [(dhal, 2, "1250.00")])
    r = await receive(client, owner, oid, [{"order_line_id": line, "units": 2}])
    assert r.status_code == 422 and "kilograms" in r.json()["detail"]
    r = await receive(client, owner, oid, [{"order_line_id": line, "units": 2,
                                            "weight_kg": "50.250"}])
    assert r.status_code == 201, r.text
    assert r.json()["lines"][0]["qty"] == "50.25" and r.json()["total"] == "2500.00"
    assert list((await batch_qty(client, owner, dhal)).values()) == ["50.25"]


async def test_who_may_receive_and_where(client, owner, accountant, branch, other_branch):
    supplier = await fresh_supplier(client, owner)
    soap = await new_product(client, owner, name="Soap")
    oid, (line,) = await approved_order(owner, supplier, branch, [(soap, 5, "90.00")])
    body = [{"order_line_id": line, "units": 5}]
    assert (await receive(client, accountant, oid, body)).status_code == 403
    elsewhere = await sign_in(client, "branch_manager", branch_ids=[other_branch])
    assert (await receive(client, elsewhere, oid, body)).status_code == 404
    here = await sign_in(client, "branch_manager", branch_ids=[branch])
    r = await receive(client, here, oid, body, day="2026-09-01")
    assert r.status_code == 422 and "before they were ordered" in r.json()["detail"]
    assert (await receive(client, here, oid, body)).status_code == 201


async def test_a_branch_without_its_online_id_keeps_no_stock(client, owner):
    from tests.conftest import make_branch

    plain = await make_branch("No online id")
    supplier = await fresh_supplier(client, owner)
    salt = await new_product(client, owner, name="Salt")
    oid, (line,) = await approved_order(owner, supplier, plain, [(salt, 3, "60.00")])
    r = await receive(client, owner, oid, [{"order_line_id": line, "units": 3}])
    assert r.status_code == 422 and "SRIBEESonline branch id" in r.json()["detail"]
    batches = (await client.get("/stock/batches", headers=owner.headers,
                                params={"product_id": salt["product_id"]})).json()
    assert batches == []  # nothing half-done: the whole receipt rolled back


async def test_a_supplier_with_the_app_must_accept_first(client, owner, branch):
    from app.core.db import get_sessionmaker
    from app.models import SupplierUser

    supplier = await fresh_supplier(client, owner)
    async with get_sessionmaker()() as s, s.begin():
        s.add(SupplierUser(supplier_id=uuid.UUID(supplier["supplier_id"]),
                           phone=f"+9477{uuid.uuid4().int % 10**7:07d}", full_name="Rep"))
    milk = await new_product(client, owner, name="Milk")
    oid, (line,) = await approved_order(owner, supplier, branch, [(milk, 2, "300.00")])
    r = await receive(client, owner, oid, [{"order_line_id": line, "units": 2}])
    assert r.status_code == 409 and "not accepted" in r.json()["detail"]


async def test_no_stock_moves_in_a_month_finance_closed(client, owner, branch):
    from app.core.db import get_sessionmaker
    from app.services import finance_link

    period = str(uuid.uuid4())

    async def tell(status):
        async with get_sessionmaker()() as s, s.begin():
            return await finance_link.apply(s, {
                "type": f"period.{'closed' if status == 'closed' else 'reopened'}",
                "aggregate_id": period,
                "payload": {"start_date": "2026-08-01", "end_date": "2026-08-31",
                            "status": status}})

    supplier = await fresh_supplier(client, owner)
    sugar = await new_product(client, owner, name="Sugar pack")
    oid, (line,) = await approved_order(owner, supplier, branch, [(sugar, 4, "210.00")],
                                        day="2026-08-20")
    assert await tell("closed") == "applied"
    r = await receive(client, owner, oid, [{"order_line_id": line, "units": 4}],
                      day="2026-08-25")
    assert r.status_code == 409 and "closed the month" in r.json()["detail"]
    assert await tell("open") == "applied"  # the owner reopened it
    r = await receive(client, owner, oid, [{"order_line_id": line, "units": 4}],
                      day="2026-08-25")
    assert r.status_code == 201, r.text


async def test_review_fixes_product_weight_and_dates(client, owner, branch):
    """Review 2026-11-13: #3 a line is received only as a pack size of its own product;
    #4 a first receipt by kilograms makes a weighed item; #1 no far-off dates."""
    from app.core.db import get_sessionmaker
    from app.models import OnlineProduct, OnlineVariant

    supplier = await fresh_supplier(client, owner)
    jam = await new_product(client, owner, name="Jam")
    other = await new_product(client, owner, name="Unrelated")
    oid, (line, other_line) = await approved_order(
        owner, supplier, branch, [(jam, 5, "100.00"), (other, 1, "10.00")])
    r = await receive(client, owner, oid, [{"order_line_id": line, "units": 5,
                                            "product_id": other["product_id"]}])
    assert r.status_code == 422 and "not a pack size" in r.json()["detail"]

    # an Online item never received before, whose first receipt gives kilograms
    async with get_sessionmaker()() as s, s.begin():
        op = OnlineProduct(online_product_id=uuid.uuid4(), name="Red rice loose")
        s.add(op)
        await s.flush()
        vid = uuid.uuid4()
        s.add(OnlineVariant(online_variant_id=vid, online_product_id=op.online_product_id,
                            name=""))
    rice = {"product_id": str(vid), "name": "Red rice loose"}
    oid2, (rice_line,) = await approved_order(owner, await fresh_supplier(client, owner),
                                              branch, [(rice, 1, "9000.00")])
    r = await receive(client, owner, oid2, [{"order_line_id": rice_line, "units": 1,
                                             "weight_kg": "50"}])
    assert r.status_code == 201, r.text
    p = (await client.get("/products", headers=owner.headers,
                          params={"search": "Red rice loose"})).json()
    assert [(x["unit"], x["stock_mode"]) for x in p] == [("kg", "loose")]

    old_id, (old_line,) = await approved_order(owner, await fresh_supplier(client, owner),
                                               branch, [(jam, 2, "100.00")], day="2025-01-01")
    r = await receive(client, owner, old_id, [{"order_line_id": old_line, "units": 2}],
                      day="2025-01-02")
    assert r.status_code == 422 and "outside the last" in r.json()["detail"]
