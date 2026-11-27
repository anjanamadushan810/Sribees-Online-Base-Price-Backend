"""Receiving against a supply order (phase B; replaces Finance's GRN): short deliveries,
the transport charge, the journal Finance is asked to post, and who may receive."""

import json
import os
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from tests.conftest import sign_in
from tests.stock_helpers import (
    D,
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


async def test_a_short_delivery_closes_the_order(client, owner, branch):
    supplier = await fresh_supplier(client, owner)
    jam = await new_product(client, owner, name="Jam")
    tea = await new_product(client, owner, track_expiry=True, name="Tea")
    oid, (jam_line, tea_line) = await approved_order(
        owner, supplier, branch, [(jam, 10, "100.00"), (tea, 5, "450.00")])
    inventory = await balance(client, owner, "1300", branch)
    grni = await balance(client, owner, "2020", branch)

    # more than was ordered is refused; tea needs its expiry
    r = await receive(client, owner, oid, [{"order_line_id": jam_line, "units": 11}])
    assert r.status_code == 422 and "only 10 of 10" in r.json()["detail"]
    r = await receive(client, owner, oid, [{"order_line_id": tea_line, "units": 5,
                                            "expiry_date": None}])
    assert r.status_code == 422 and "expiry" in r.json()["detail"]

    # 8 of 10 jars and 2 free, no tea; the invoice comes with the goods
    r = await receive(client, owner, oid, [
        {"order_line_id": jam_line, "units": 8, "free_units": 2}],
        invoice_no="INV-77", invoice_total="800.00")
    assert r.status_code == 201, r.text
    first = r.json()
    assert first["receipt_no"].startswith("RCV-") and first["total"] == "800.00"
    line = first["lines"][0]
    assert (line["units"], line["free_units"], line["qty"], line["line_value"]) == (
        8, 2, "10", "800.00")
    # owner 2026-11-26: what did not come is ordered again; this order is finished
    order = (await client.get(f"/orders/{oid}", headers=owner.headers)).json()
    assert order["status"] == "received"
    posted = (await _events(oid, "receipt.posted"))[0]
    assert (posted["order_status"], posted["outstanding_units"]) == ("received", 7)

    # landed cost: Rs 800 over 10 jars (8 paid + 2 free)
    batches = (await client.get("/stock/batches", headers=owner.headers,
                                params={"product_id": jam["product_id"]})).json()
    assert [(b["qty_on_hand"], b["unit_cost"]) for b in batches] == [("10", "80.0000")]
    assert await balance(client, owner, "1300", branch) - inventory == Decimal("800.00")
    assert await balance(client, owner, "2020", branch) - grni == Decimal("-800.00")

    r = await receive(client, owner, oid, [{"order_line_id": tea_line, "units": 5,
                                            "expiry_date": "2027-06-30"}])
    assert r.status_code == 409


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


async def test_nothing_to_close_or_cancel_after_goods_arrived(client, owner, branch):
    supplier = await fresh_supplier(client, owner)
    rice = await new_product(client, owner, name="Rice pack")
    oid, (line,) = await approved_order(owner, supplier, branch, [(rice, 10, "200.00")])
    r = await client.post(f"/orders/{oid}/close-rest", headers=owner.headers,
                          json={"reason": "supplier out of stock"})
    assert r.status_code == 409 and "cancel" in r.json()["detail"]
    assert (await receive(client, owner, oid, [{"order_line_id": line, "units": 6}])
            ).status_code == 201
    for path in ("cancel", "close-rest"):
        r = await client.post(f"/orders/{oid}/{path}", headers=owner.headers,
                              json={"reason": "changed my mind"})
        assert r.status_code == 409, path


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
    r = await receive(client, here, oid, body, day=D("2026-09-01"))
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
    import calendar
    from datetime import date, timedelta

    from app.core.db import get_sessionmaker
    from app.services import finance_link

    period = str(uuid.uuid4())
    # A month that ended recently, so its days stay inside the 90-day window.
    ref = date.today() - timedelta(days=35)
    first = ref.replace(day=1)
    last = ref.replace(day=calendar.monthrange(ref.year, ref.month)[1])
    order_day, receive_day = str(first + timedelta(days=1)), str(first + timedelta(days=5))

    async def tell(status):
        async with get_sessionmaker()() as s, s.begin():
            return await finance_link.apply(s, {
                "type": f"period.{'closed' if status == 'closed' else 'reopened'}",
                "aggregate_id": period,
                "payload": {"start_date": str(first), "end_date": str(last),
                            "status": status}})

    supplier = await fresh_supplier(client, owner)
    sugar = await new_product(client, owner, name="Sugar pack")
    oid, (line,) = await approved_order(owner, supplier, branch, [(sugar, 4, "210.00")],
                                        day=order_day)
    assert await tell("closed") == "applied"
    r = await receive(client, owner, oid, [{"order_line_id": line, "units": 4}],
                      day=receive_day)
    assert r.status_code == 409 and "closed the month" in r.json()["detail"]
    assert await tell("open") == "applied"  # the owner reopened it
    r = await receive(client, owner, oid, [{"order_line_id": line, "units": 4}],
                      day=receive_day)
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



async def _weigh(product: dict, grams: int | None) -> None:
    """The weight staff entered on the Online product."""
    from app.core.db import get_sessionmaker
    from app.models import OnlineProduct, OnlineVariant

    async with get_sessionmaker()() as s, s.begin():
        variant = await s.get(OnlineVariant, uuid.UUID(product["product_id"]))
        (await s.get(OnlineProduct, variant.online_product_id)).weight_grams = grams


async def _example(client, owner, branch):
    """The owner's example (2026-11-26): A 100 x 100 g at 200, B 60 x 500 g at 400."""
    supplier = await fresh_supplier(client, owner)
    a = await new_product(client, owner, name="Soap A")
    b = await new_product(client, owner, name="Powder B")
    oid, (a_line, b_line) = await approved_order(
        owner, supplier, branch, [(a, 100, "200.00"), (b, 60, "400.00")])
    await _weigh(a, 100)
    await _weigh(b, 500)
    return oid, a, b, [{"order_line_id": a_line, "units": 100},
                       {"order_line_id": b_line, "units": 60}]


async def test_the_transport_charge_goes_by_weight_into_the_base_price(client, owner, branch):
    from app.core.db import get_sessionmaker
    from app.models import BasePrice, OutboxEvent

    oid, a, b, lines = await _example(client, owner, branch)
    r = await client.post(f"/orders/{oid}/receipts/preview", headers=owner.headers,
                          json={"transport_charge": "4000", "lines": lines})
    assert r.status_code == 200, r.text
    seen = r.json()
    assert [x["delivered_unit_price"] for x in seen["lines"]] == ["210.00", "450.00"]
    assert seen["total"] == "48000.00" and seen["missing_weight"] == []

    r = await receive(client, owner, oid, lines, transport_charge="4000",
                      invoice_no="T-1", invoice_total="48000.00")
    assert r.status_code == 201, r.text
    got = r.json()
    assert (got["transport_charge"], got["total"]) == ("4000.00", "48000.00")
    assert [(x["unit_price"], x["landed_cost"]) for x in got["lines"]] == [
        ("200.00", "1000.00"), ("400.00", "3000.00")]
    async with get_sessionmaker()() as s:
        prices = {p.online_variant_id: (p.unit_price, p.bill_price) for p in await s.scalars(
            select(BasePrice).where(BasePrice.order_id == uuid.UUID(oid)))}
        ev = await s.scalar(select(OutboxEvent).where(
            OutboxEvent.event == "receipt.posted",
            OutboxEvent.payload["receipt_id"].astext == got["receipt_id"]))
    assert prices[uuid.UUID(a["product_id"])] == (Decimal("210.00"), Decimal("200.00"))
    assert prices[uuid.UUID(b["product_id"])] == (Decimal("450.00"), Decimal("400.00"))
    assert ev.payload["total"] == "48000.00"  # what Finance matches the bill against


async def test_the_suppliers_figure_is_offered_and_staff_override_it(client, owner, branch):
    from app.core.db import get_sessionmaker
    from app.models import SupplyOrder

    oid, _, _, lines = await _example(client, owner, branch)
    async with get_sessionmaker()() as s, s.begin():
        (await s.get(SupplyOrder, uuid.UUID(oid))).delivery_charge = Decimal("100.00")
    page = (await client.get(f"/orders/{oid}/receiving", headers=owner.headers)).json()
    assert page["transport_charge"] == "100.00" and page["online_stock_in"] is False
    assert sorted((x["due_units"], x["unit_weight_grams"]) for x in page["lines"]) == [
        (60, 500), (100, 100)]
    r = await receive(client, owner, oid, lines, transport_charge="0")
    assert r.status_code == 201, r.text
    assert (r.json()["transport_charge"], r.json()["total"]) == ("0.00", "44000.00")


async def test_a_missing_weight_saves_nothing(client, owner, branch):
    oid, a, b, lines = await _example(client, owner, branch)
    await _weigh(b, None)
    seen = (await client.post(f"/orders/{oid}/receipts/preview", headers=owner.headers,
                              json={"transport_charge": "4000", "lines": lines})).json()
    assert len(seen["missing_weight"]) == 1 and "Powder B" in seen["missing_weight"][0]
    r = await receive(client, owner, oid, lines, transport_charge="4000")
    assert r.status_code == 422 and "Powder B" in r.json()["detail"]
    assert (await client.get(f"/orders/{oid}", headers=owner.headers)).json()[
        "status"] == "approved"
    assert await batch_qty(client, owner, a) == {}
    # without a charge no weight is needed
    assert (await receive(client, owner, oid, lines)).status_code == 201


async def test_a_large_charge_is_the_owners_to_save(client, owner, branch):
    oid, _, _, lines = await _example(client, owner, branch)
    staff = await sign_in(client, "purchasing")
    r = await receive(client, staff, oid, lines, transport_charge="11000.01")  # above 25%
    assert r.status_code == 403 and "owner" in r.json()["detail"]
    assert (await receive(client, owner, oid, lines, transport_charge="11000.01")
            ).status_code == 201


async def test_a_product_that_came_without_being_ordered(client, owner, branch):
    """Owner 2026-11-26: staff may add a product we already sell that came unordered; a
    product that is not on Online is refused (only the super admin adds new ones)."""
    from app.core.db import get_sessionmaker
    from app.models import OnlineProduct, OnlineVariant

    supplier = await fresh_supplier(client, owner)
    jam = await new_product(client, owner, name="Jam")
    oid, (jam_line,) = await approved_order(owner, supplier, branch, [(jam, 10, "100.00")])
    async with get_sessionmaker()() as s, s.begin():
        op = OnlineProduct(online_product_id=uuid.uuid4(), name="Honey 500g", is_active=True)
        s.add(op)
        await s.flush()
        honey = OnlineVariant(online_variant_id=op.online_product_id,
                              online_product_id=op.online_product_id, name="")
        s.add(honey)
        gone = OnlineProduct(online_product_id=uuid.uuid4(), name="Old Honey", is_active=False)
        s.add(gone)
        await s.flush()
        s.add(OnlineVariant(online_variant_id=gone.online_product_id,
                            online_product_id=gone.online_product_id, name="",
                            is_active=False))
    honey_id, gone_id = str(honey.online_variant_id), str(gone.online_product_id)

    r = await receive(client, owner, oid, [{"order_line_id": None, "online_variant_id": gone_id,
                                            "units": 2, "unit_price": "300"}])
    assert r.status_code == 422 and "super admin" in r.json()["detail"]
    r = await receive(client, owner, oid, [{"order_line_id": None, "online_variant_id": honey_id,
                                            "units": 2}])
    assert r.status_code == 422 and "bill price" in r.json()["detail"]
    r = await receive(client, owner, oid, [
        {"order_line_id": jam_line, "units": 10},
        {"order_line_id": None, "online_variant_id": honey_id, "units": 3,
         "unit_price": "450.00"}])
    assert r.status_code == 201, r.text
    assert r.json()["total"] == "2350.00"  # 10 x 100 + 3 x 450
    order = (await client.get(f"/orders/{oid}", headers=owner.headers)).json()
    added = [ln for ln in order["lines"] if ln["online_variant_id"] == honey_id]
    assert [(ln["units"], ln["unit_price"]) for ln in added] == [(3, "450.00")]
    assert order["status"] == "received"
    bp = (await client.get(f"/base-prices/{honey_id}", headers=owner.headers)).json()
    assert [h["unit_price"] for h in bp["history"]] == ["450.00"]


async def test_the_bill_photo_is_kept_with_the_receipt(client, owner, accountant, branch):
    supplier = await fresh_supplier(client, owner)
    jam = await new_product(client, owner, name="Jam")
    oid, (jam_line,) = await approved_order(owner, supplier, branch, [(jam, 2, "100.00")])
    r = await receive(client, owner, oid, [{"order_line_id": jam_line, "units": 2}])
    rid = r.json()["receipt_id"]
    jpeg = bytes.fromhex("ffd8ffe000104a464946") + b"x" * 100
    url = f"/receipts/{rid}/photo"
    assert (await client.get(url, headers=owner.headers)).status_code == 404
    r = await client.put(url, headers={**owner.headers, "Content-Type": "text/plain"},
                         content=jpeg)
    assert r.status_code == 415
    r = await client.put(url, headers={**owner.headers, "Content-Type": "image/png"},
                         content=jpeg)
    assert r.status_code == 415  # the bytes are not a PNG
    r = await client.put(url, headers={**accountant.headers, "Content-Type": "image/jpeg"},
                         content=jpeg)
    assert r.status_code == 403  # only who receives goods sends the bill
    r = await client.put(url, headers={**owner.headers, "Content-Type": "image/jpeg"},
                         content=jpeg)
    assert r.status_code == 204, r.text
    got = await client.get(url, headers=accountant.headers)
    assert got.status_code == 200 and got.content == jpeg
    assert got.headers["content-type"] == "image/jpeg"
