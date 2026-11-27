"""Fulfilment partner P4 + P5 (owner MCQ 2026-11-28): complete, priced bags only; one partner
order (Finance PO) per day; goods straight out to the customer; returns go back to the
PARTNER (staff send, partner accepts, booked goods become a supplier credit); the weekly
statement."""

import uuid
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from tests.conftest import _stock_branch, sign_in
from tests.test_partners import (
    _event,
    _line,
    _list,
    _partner,
    _product,
    _request,
    _set_item,
    _stock_of,
)
from tests.test_supplier_auth import settings_override


async def _setup(client, owner, *, items=(("A", "120.00"), ("B", "50.00"))):
    """A branch with a partner, its manager, and priced Other Products."""
    branch = await _stock_branch("Books branch")
    manager = await sign_in(client, "branch_manager", [branch])
    sid, ph = await _partner(client, owner, branch, signed_in=True)
    products = []
    for name, price in items:
        pid, v = await _product(name)
        await _list(branch, pid, "external_daily")
        await _set_item(client, ph, branch, v[0], price=price, stock=50)
        products.append(pid)
    return branch, manager, sid, ph, products


async def _order(branch, lines, number=None):
    order_id = uuid.uuid4()
    number = number or f"SB-{uuid.uuid4().hex[:6]}"
    assert await _event(branch, order_id, "confirmed", lines, number=number) == "partner:created"
    return order_id, await _request(order_id)


async def _hand(client, manager, fr, expect=200):
    r = await client.post(f"/partners/requests/{fr.request_id}/handover", headers=manager.headers,
                          json={"lines": [{"line_id": str(ln.line_id), "qty": ln.qty}
                                          for ln in fr.lines]})
    assert r.status_code == expect, r.text
    return r.json()


def _today():
    from app.services import partner_books

    return partner_books.local_day(datetime.now(UTC))


async def _close(branch, day=None):
    from app.core.db import get_sessionmaker
    from app.services import partner_books

    async with get_sessionmaker()() as s, s.begin():
        return await partner_books.close_day(s, branch, day or _today())


async def _journals(prefix):
    from app.core.db import get_sessionmaker
    from app.models import OutboxEvent

    async with get_sessionmaker()() as s:
        rows = await s.scalars(select(OutboxEvent).where(OutboxEvent.event == "journal.requested"))
        return [r.payload for r in rows if (r.payload.get("source_ref") or "").startswith(prefix)]


async def _outbox(event):
    from app.core.db import get_sessionmaker
    from app.models import OutboxEvent

    async with get_sessionmaker()() as s:
        return [r.payload for r in await s.scalars(
            select(OutboxEvent).where(OutboxEvent.event == event))]


async def _batch(batch_id):
    from app.core.db import get_sessionmaker
    from app.models import StockBatch

    async with get_sessionmaker()() as s:
        return await s.get(StockBatch, batch_id)


async def _receipt_of(order_no):
    from app.core.db import get_sessionmaker
    from app.models import Receipt, ReceiptLine, SupplyOrder

    async with get_sessionmaker()() as s:
        order = await s.scalar(select(SupplyOrder).where(SupplyOrder.order_no == order_no))
        receipt = await s.scalar(select(Receipt).where(Receipt.order_id == order.order_id))
        lines = list(await s.scalars(select(ReceiptLine)
                                     .where(ReceiptLine.receipt_id == receipt.receipt_id)))
        return order, receipt, lines


async def _shift_handed(request_id, days):
    from app.core.db import get_sessionmaker
    from app.models import FulfilmentRequest

    async with get_sessionmaker()() as s, s.begin():
        row = await s.get(FulfilmentRequest, request_id)
        row.handed_at = row.handed_at - timedelta(days=days)
        return row.handed_at


async def _returns(client, who, ph=None):
    if ph is not None:
        return (await client.get("/supplier/partner/returns", headers=ph)).json()
    return (await client.get("/partners/returns", headers=who.headers)).json()


# ---------------------------------------------------------------- handover (F20, price)
async def test_only_a_complete_priced_bag_is_taken(client, owner):
    branch, manager, sid, ph, (a, b) = await _setup(client, owner)
    order_id, fr = await _order(branch, [_line(a, 2), _line(b, 1)], number="SB-HAND-1")
    la = next(ln for ln in fr.lines if ln.online_product_id == a)

    found = (await client.get("/partners/requests/lookup", headers=manager.headers,
                              params={"code": "SB-HAND-1"})).json()
    assert found["request_no"] == fr.request_no
    r = await client.post(f"/partners/requests/{fr.request_id}/tick", headers=manager.headers,
                          json={"lines": [{"line_id": str(la.line_id), "qty": 2}]})
    assert r.status_code == 200
    r = await client.post(f"/partners/requests/{fr.request_id}/handover",
                          headers=manager.headers, json={"lines": []})
    assert r.status_code == 409 and "0/1" in r.json()["detail"]  # B has not come yet
    r = await client.post(f"/partners/requests/{fr.request_id}/tick", headers=manager.headers,
                          json={"lines": [{"line_id": str(la.line_id), "qty": 3}]})
    assert r.status_code == 422  # more than ordered
    assert (await _hand(client, manager, await _request(order_id)))["status"] == "handed"

    other = await sign_in(client, "branch_manager", [await _stock_branch("Elsewhere")])
    assert (await client.get("/partners/requests/lookup", headers=other.headers,
                             params={"code": "SB-HAND-1"})).status_code == 404
    assert (await client.post(f"/partners/requests/{fr.request_id}/handover",
                              headers=other.headers, json={"lines": []})).status_code == 404


async def test_a_price_is_mandatory(client, owner):
    branch = await _stock_branch("Price rule branch")
    manager = await sign_in(client, "branch_manager", [branch])
    sid, ph = await _partner(client, owner, branch, signed_in=True)
    pid, v = await _product("Unpriced")
    await _list(branch, pid, "external_daily")
    r = await client.put("/supplier/partner/items", headers=ph, json={
        "branch_id": str(branch), "online_variant_id": str(v[0]), "stock_qty": 5})
    assert r.status_code == 422  # no stock without a price
    items = (await client.get("/supplier/partner/items", headers=ph)).json()["items"]
    assert items[0]["price_missing"] is True
    _o, fr = await _order(branch, [_line(pid, 1)])
    body = await _hand(client, manager, fr, expect=409)
    assert "price" in body["detail"]
    await _set_item(client, ph, branch, v[0], price="40.00", stock=3)
    assert (await _hand(client, manager, fr))["lines"][0]["price_to_us"] == "40.00"


async def test_an_edit_after_ticking_keeps_the_ticks_within_the_order(client, owner):
    branch, manager, sid, ph, (a,) = await _setup(client, owner, items=(("A", "10.00"),))
    item = uuid.uuid4()
    order_id, fr = await _order(branch, [_line(a, 3, item)])
    await client.post(f"/partners/requests/{fr.request_id}/tick", headers=manager.headers,
                      json={"lines": [{"line_id": str(fr.lines[0].line_id), "qty": 3}]})
    assert await _event(branch, order_id, "processing", [_line(a, 2, item)]) == "partner:changed"
    line = (await _request(order_id)).lines[0]
    assert (line.qty, line.ticked_qty) == (2, 2)


async def test_a_bag_that_went_out_unscanned_is_flagged(client, owner):
    branch, manager, sid, ph, (a,) = await _setup(client, owner, items=(("A", "10.00"),))
    order_id, fr = await _order(branch, [_line(a, 1)])
    assert await _event(branch, order_id, "handed_to_courier", []) == "partner:unscanned"
    books = (await client.get("/partners/books", headers=manager.headers)).json()
    assert [u["request_no"] for u in books["unscanned"]] == [fr.request_no]


# ---------------------------------------------------------------- the day's order (F18)
async def test_a_day_becomes_one_order_and_the_goods_go_straight_out(client, owner):
    from app.core.db import get_sessionmaker
    from app.models import BasePrice, OnlineStockSend

    branch, manager, sid, ph, (a, b) = await _setup(client, owner)
    o1, fr1 = await _order(branch, [_line(a, 2), _line(b, 1)])
    o2, fr2 = await _order(branch, [_line(a, 1)])
    await _hand(client, manager, fr1)
    await _hand(client, manager, fr2)

    with settings_override(online_stock_in=True):
        out = await _close(branch)
    assert out["booked"] == 2 and len(out["orders"]) == 1
    assert (await _close(branch))["booked"] == 0  # never twice

    order, receipt, rows = await _receipt_of(out["orders"][0])
    assert order.is_quick_buy and order.status == "received" and str(order.supplier_id) == sid
    assert receipt.total == Decimal("410.00")  # 3 x 120 + 1 x 50
    assert sum(ln.boxes * ln.unit_price for ln in order.lines) == receipt.total  # PO = receipt
    assert all(r.hold for r in rows) and sorted(r.units for r in rows) == [1, 3]
    async with get_sessionmaker()() as s:
        assert await s.scalar(select(OnlineStockSend).where(
            OnlineStockSend.receipt_id == receipt.receipt_id)) is None  # never on Online
        prices = {bp.online_product_id: bp.unit_price for bp in await s.scalars(
            select(BasePrice).where(BasePrice.order_id == order.order_id))}
    assert prices == {a: Decimal("120.00"), b: Decimal("50.00")}
    for fr in (await _request(o1), await _request(o2)):
        for ln in fr.lines:
            assert (await _batch(ln.batch_id)).qty_on_hand == 0  # straight out
    assert (await _journals(f"receipt:{receipt.receipt_id}"))[0]["total"] == "410.00"
    out1 = await _journals(f"partner:{fr1.request_id}:out")
    out2 = await _journals(f"partner:{fr2.request_id}:out")
    assert (out1[0]["total"], out2[0]["total"]) == ("290.00", "120.00")
    assert {ln["account_code"] for ln in out1[0]["lines"]} == {"5000", "1300"}
    held = (await client.get("/stock/held", headers=owner.headers,
                             params={"branch_id": str(branch)})).json()
    assert held["lines"] == []


async def test_a_mid_day_price_change_keeps_the_po_equal_to_the_receipt(client, owner):
    branch, manager, sid, ph, (a,) = await _setup(client, owner, items=(("A", "120.00"),))
    _o1, fr1 = await _order(branch, [_line(a, 2)])
    await _set_item(client, ph, branch, fr1.lines[0].online_variant_id, price="130.00", stock=50)
    _o2, fr2 = await _order(branch, [_line(a, 1)])
    await _hand(client, manager, fr1)
    await _hand(client, manager, fr2)
    out = await _close(branch)
    first, second = out["orders"][0].split(", ")  # the second price: a second PO that day
    for no, price, units in ((first, "120.00", 2), (second, "130.00", 1)):
        order, receipt, rows = await _receipt_of(no)
        assert [(ln.unit_price, ln.boxes) for ln in order.lines] == [(Decimal(price), units)]
        assert receipt.total == Decimal(price) * units  # each PO equals its receipt
    assert receipt.invoice_no.endswith("-2")


async def test_the_day_order_in_finance_approval_mode(client, owner):
    branch, manager, sid, ph, (a,) = await _setup(client, owner, items=(("A", "75.00"),))
    _o, fr = await _order(branch, [_line(a, 2)])
    await _hand(client, manager, fr)
    with settings_override(order_approval="finance"):
        out = await _close(branch)
    order, receipt, _rows = await _receipt_of(out["orders"][0])
    assert receipt.total == Decimal("150.00") and order.status == "received"


async def test_weighed_goods_from_the_partner_are_booked_by_weight(client, owner):
    from app.core.db import get_sessionmaker
    from app.models import OnlineVariant, Product

    branch, manager, sid, ph, (rice,) = await _setup(client, owner, items=(("Rice", "60.00"),))
    variant_id = rice  # no pack sizes: its default pack size carries the product's id
    async with get_sessionmaker()() as s, s.begin():
        (await s.get(OnlineVariant, variant_id)).weight_grams = 500
        s.add(Product(product_id=variant_id, name="Rice loose", unit="kg", stock_mode="loose",
                      kind="sellable"))
    order_id, fr = await _order(branch, [_line(rice, 3)])  # 3 x 500 g
    await _hand(client, manager, fr)
    out = await _close(branch)
    _order_row, receipt, rows = await _receipt_of(out["orders"][0])
    assert receipt.total == Decimal("180.00")
    line = (await _request(order_id)).lines[0]
    assert line.stock_per_unit == Decimal("0.500")
    batch = await _batch(line.batch_id)
    assert batch.qty_received == Decimal("1.500") and batch.qty_on_hand == 0


async def test_a_day_without_a_price_waits_and_one_bad_day_never_blocks_another(
        client, owner, monkeypatch):
    from app.core.db import get_sessionmaker
    from app.models import FulfilmentRequest, PartnerItem
    from app.services import partner_books

    branch, manager, sid, ph, (a,) = await _setup(client, owner, items=(("A", "120.00"),))
    _o, fr = await _order(branch, [_line(a, 1)])
    await _hand(client, manager, fr)
    await _shift_handed(fr.request_id, 1)
    async with get_sessionmaker()() as s, s.begin():
        row = await s.get(FulfilmentRequest, fr.request_id)
        for ln in row.lines:
            ln.price_to_us = None  # an old request from before prices were mandatory
        item = await s.scalar(select(PartnerItem).where(PartnerItem.branch_id == branch))
        item.price_to_us = None
    other_branch, m2, _s2, _p2, (b,) = await _setup(client, owner, items=(("B", "9.00"),))
    _o2, fr2 = await _order(other_branch, [_line(b, 1)])
    await _hand(client, m2, fr2)
    await _shift_handed(fr2.request_id, 1)

    real = partner_books.close_day

    async def picky(db, branch_id, day):
        if branch_id == branch:
            raise RuntimeError("something unexpected")
        return await real(db, branch_id, day)
    monkeypatch.setattr(partner_books, "close_day", picky)
    got = await partner_books.run_once(get_sessionmaker())
    assert any(str(branch) in k for k in got["problems"])
    assert any(str(other_branch) in k for k in got["booked"])  # not held up
    monkeypatch.setattr(partner_books, "close_day", real)
    got = await partner_books.run_once(get_sessionmaker())
    assert "no price" in next(v for k, v in got["problems"].items() if str(branch) in k)
    assert (await client.get("/partners/books", headers=m2.headers)).json()["problems"] == {}


async def test_yesterday_is_booked_after_a_short_grace(client, owner):
    from app.core.db import get_sessionmaker
    from app.services import partner_books

    branch, manager, sid, ph, (a,) = await _setup(client, owner, items=(("A", "80.00"),))
    _o, fr = await _order(branch, [_line(a, 1)])
    await _hand(client, manager, fr)
    tomorrow = datetime.combine(_today() + timedelta(days=1), datetime.min.time(),
                                tzinfo=partner_books.COLOMBO)
    early = await partner_books.run_once(get_sessionmaker(), now=tomorrow + timedelta(minutes=5))
    assert not any(str(branch) in k for k in (early or {}).get("booked", {}))
    later = await partner_books.run_once(get_sessionmaker(), now=tomorrow + timedelta(minutes=20))
    assert any(str(branch) in k for k in later["booked"])


async def test_the_owner_can_take_a_request_out_of_the_day_order(client, owner):
    branch, manager, sid, ph, (a,) = await _setup(client, owner, items=(("A", "5.00"),))
    _o, fr = await _order(branch, [_line(a, 1)])
    await _hand(client, manager, fr)
    body = {"reason": "booked by hand"}
    r = await client.post(f"/partners/requests/{fr.request_id}/exclude",
                          headers=manager.headers, json=body)
    assert r.status_code == 403
    r = await client.post(f"/partners/requests/{fr.request_id}/exclude",
                          headers=owner.headers, json=body)
    assert r.status_code == 200, r.text
    assert (await _close(branch))["booked"] == 0


# ---------------------------------------------------------------- returns go to the partner
async def test_a_cancel_after_booking_goes_back_to_the_partner_as_a_credit(client, owner):
    branch, manager, sid, ph, (a, b) = await _setup(client, owner)
    order_id, fr = await _order(branch, [_line(a, 2), _line(b, 1)])
    await _hand(client, manager, fr)
    await _close(branch)
    va = next(ln for ln in fr.lines if ln.online_product_id == a).online_variant_id
    stock_before = await _stock_of(branch, va)
    assert await _event(branch, order_id, "cancelled", []) == "partner:return:3"
    assert await _event(branch, order_id, "cancelled", []) == "ignored"  # once

    ret = (await _returns(client, manager))[0]
    assert ret["status"] == "waiting" and ret["reason"] == "cancelled"
    assert (await client.post(f"/supplier/partner/returns/{ret['return_id']}/accept",
                              headers=ph)).status_code == 409  # not handed to them yet
    r = await client.post(f"/partners/returns/{ret['return_id']}/sent", headers=manager.headers)
    assert r.status_code == 200 and r.json()["status"] == "sent"
    mine = await _returns(client, None, ph)
    assert [x["return_no"] for x in mine] == [ret["return_no"]]
    r = await client.post(f"/supplier/partner/returns/{ret['return_id']}/accept", headers=ph)
    assert r.status_code == 200 and r.json()["credit_total"] == "290.00"

    line_a = next(ln for ln in (await _request(order_id)).lines if ln.online_product_id == a)
    assert await _stock_of(branch, va) == stock_before + 2
    assert (await _batch(line_a.batch_id)).qty_on_hand == 0  # gone back, not on our shelf
    back = await _journals(f"partner:return:{ret['return_id']}:in")
    out = await _journals(f"partner:return:{ret['return_id']}:out")
    assert (back[0]["total"], out[0]["total"]) == ("290.00", "290.00")
    assert {(ln["account_code"], ln["debit"] != "0.00") for ln in out[0]["lines"]} == {
        ("2020", True), ("1300", False)}
    credit = [p for p in await _outbox("supplier_return.posted")
              if p["return_id"] == ret["return_id"]]
    assert credit[0]["total"] == "290.00" and credit[0]["supplier_id"] == sid
    held = (await client.get("/stock/held", headers=owner.headers,
                             params={"branch_id": str(branch)})).json()
    assert held["lines"] == []


async def test_a_cancel_before_booking_is_never_bought(client, owner):
    branch, manager, sid, ph, (a, b) = await _setup(client, owner)
    o1, fr1 = await _order(branch, [_line(a, 2)])
    o2, fr2 = await _order(branch, [_line(b, 1)])
    await _hand(client, manager, fr1)
    await _hand(client, manager, fr2)
    await _event(branch, o1, "cancelled", [])
    out = await _close(branch)
    _order_row, receipt, _rows = await _receipt_of(out["orders"][0])
    assert receipt.total == Decimal("50.00")  # only B was bought
    ret = (await _returns(client, manager))[0]
    await client.post(f"/partners/returns/{ret['return_id']}/sent", headers=manager.headers)
    r = await client.post(f"/supplier/partner/returns/{ret['return_id']}/accept", headers=ph)
    assert r.json()["credit_total"] is None  # nothing was bought, nothing to credit


async def test_refused_items_go_back_to_the_partner(client, owner):
    from app.core.db import get_sessionmaker
    from app.models import Branch
    from app.services import partner_requests

    branch, manager, sid, ph, (a, b) = await _setup(client, owner)
    item_a = uuid.uuid4()
    order_id, fr = await _order(branch, [_line(a, 2, item_a), _line(b, 1)])
    await _hand(client, manager, fr)
    await _close(branch)
    async with get_sessionmaker()() as s, s.begin():
        online = (await s.get(Branch, branch)).online_branch_id
        got = await partner_requests.apply_event(s, {
            "seq": 1, "event_id": str(uuid.uuid4()), "type": "order.undelivered",
            "payload": {"order_id": str(order_id), "branch": {"branch_id": str(online)},
                        "to_status": "delivered",
                        "undelivered": [{"order_item_id": str(item_a), "quantity": 1}]}})
    assert got == "partner:return:1"
    ret = (await _returns(client, manager))[0]
    await client.post(f"/partners/returns/{ret['return_id']}/sent", headers=manager.headers)
    r = await client.post(f"/supplier/partner/returns/{ret['return_id']}/accept", headers=ph)
    assert r.json()["credit_total"] == "120.00"


# ---------------------------------------------------------------- P5: the weekly statement
async def test_the_weekly_statement_and_the_partners_confirmation(client, owner):
    from app.services import partner_statements

    branch, manager, sid, ph, (a, b) = await _setup(client, owner)
    o1, fr1 = await _order(branch, [_line(a, 2)])
    o2, fr2 = await _order(branch, [_line(b, 1)])
    await _hand(client, manager, fr1)
    await _hand(client, manager, fr2)
    handed = await _shift_handed(fr1.request_id, 7)
    await _shift_handed(fr2.request_id, 7)
    day = handed.astimezone(partner_books_colombo()).date()
    await _close(branch, day)
    await _event(branch, o2, "cancelled", [])
    ret = (await _returns(client, manager))[0]
    await client.post(f"/partners/returns/{ret['return_id']}/sent", headers=manager.headers)
    await client.post(f"/supplier/partner/returns/{ret['return_id']}/accept", headers=ph)

    week = partner_statements.week_of(day)
    s = (await client.get("/partners/statements", headers=manager.headers,
                          params={"branch_id": str(branch), "week": str(week)})).json()
    assert s["purchases"] == "290.00" and s["days"][0]["customer_orders"] == 2
    # the credit lands in the week it was accepted (this week), not the purchase week
    assert s["returns_total"] == "0.00" and s["to_pay"] == "290.00" and s["closed"] is True
    now = (await client.get("/partners/statements", headers=manager.headers,
                            params={"branch_id": str(branch),
                                    "week": str(partner_statements.week_of(_today()))})).json()
    assert now["returns_total"] == "50.00" and now["to_pay"] == "-50.00"

    mine = (await client.get("/supplier/partner/statements", headers=ph,
                             params={"week": str(week)})).json()
    assert mine["to_pay"] == "290.00"
    r = await client.post(f"/supplier/partner/statements/{week}/confirm", headers=ph)
    assert r.status_code == 200 and r.json()["confirmed_total"] == "290.00"
    r = await client.post(f"/supplier/partner/statements/{partner_statements.week_of(_today())}"
                          "/confirm", headers=ph)
    assert r.status_code == 422  # this week has not ended
    weeks = (await client.get("/supplier/partner/statements", headers=ph)).json()["weeks"]
    assert any(w["week_start"] == str(week) and w["confirmed_at"] for w in weeks)

    other = await sign_in(client, "branch_manager", [await _stock_branch("Not theirs")])
    assert (await client.get("/partners/statements", headers=other.headers,
                             params={"branch_id": str(branch)})).status_code == 403


def partner_books_colombo():
    from app.services import partner_books

    return partner_books.COLOMBO


def test_days_are_sri_lanka_days():
    from app.services import partner_books, partner_statements

    # 2026-11-27 20:00 UTC is already 2026-11-28 01:30 in Sri Lanka
    assert partner_books.local_day(datetime(2026, 11, 27, 20, 0, tzinfo=UTC)) == date(2026, 11, 28)
    assert partner_statements.week_of(date(2026, 11, 29)) == date(2026, 11, 23)  # Sun -> Mon


@contextmanager
def _unused():
    yield


__all__ = ["pytest"]


# ---------------------------------------------------------------- review round 2
async def test_one_partner_two_branches_get_their_own_invoice_numbers(client, owner):
    from app.core.db import get_sessionmaker
    from app.models import Branch

    branch, manager, sid, ph, (a,) = await _setup(client, owner, items=(("A", "10.00"),))
    second = await _stock_branch("Second branch")
    manager2 = await sign_in(client, "branch_manager", [second])
    r = await client.post(f"/partners/{sid}/branches", headers=owner.headers,
                          json={"branch_id": str(second)})
    assert r.status_code == 201, r.text
    await _list(second, a, "external_daily")
    await _set_item(client, ph, second, a, price="10.00", stock=5)
    _o1, fr1 = await _order(branch, [_line(a, 1)])
    _o2, fr2 = await _order(second, [_line(a, 1)])
    await _hand(client, manager, fr1)
    await _hand(client, manager2, fr2)
    n1 = (await _receipt_of((await _close(branch))["orders"][0]))[1].invoice_no
    n2 = (await _receipt_of((await _close(second))["orders"][0]))[1].invoice_no
    async with get_sessionmaker()() as s:
        code1, code2 = (await s.get(Branch, branch)).code, (await s.get(Branch, second)).code
    assert n1 != n2 and f"-{code1}-" in n1 and f"-{code2}-" in n2


async def test_a_retry_acts_on_the_order_as_it_is_now(client, owner, monkeypatch):
    from app.core.db import get_sessionmaker
    from app.models import Branch, PartnerEventFailure
    from app.services import partner_requests

    branch, manager, sid, ph, (a,) = await _setup(client, owner, items=(("A", "10.00"),))
    order_id = uuid.uuid4()
    async with get_sessionmaker()() as s, s.begin():
        online = (await s.get(Branch, branch)).online_branch_id
        row = PartnerEventFailure(seq=1, event_type="order.status", order_id=order_id,
                                  branch_id=branch, error="boom", payload={
                                      "seq": 1, "type": "order.status",
                                      "payload": {"order_id": str(order_id),
                                                  "branch": {"branch_id": str(online)},
                                                  "to_status": "confirmed",
                                                  "lines": [_line(a, 2)]}})
        s.add(row)
        await s.flush()
        failure_id = row.failure_id

    async def cancelled_now(_order_id):
        return {"status": "cancelled", "lines": [_line(a, 2)]}
    monkeypatch.setattr(partner_requests, "fetch_order", cancelled_now)
    other = await sign_in(client, "branch_manager", [await _stock_branch("Not here")])
    assert (await client.get("/partners/failures", headers=other.headers)).json() == []
    assert any(f["failure_id"] == failure_id for f in (await client.get(
        "/partners/failures", headers=manager.headers)).json())
    r = await client.post(f"/partners/failures/{failure_id}/retry", headers=owner.headers)
    assert r.status_code == 200 and r.json()["result"] == "ignored", r.text
    assert await _request(order_id) is None  # never brought back to life


async def test_a_split_line_returns_only_what_our_shelf_did_not_take_back(client, owner):
    from app.core.db import get_sessionmaker
    from app.models import Branch
    from app.services import partner_requests

    branch, manager, sid, ph, (a,) = await _setup(client, owner, items=(("A", "10.00"),))
    item = uuid.uuid4()
    order_id, fr = await _order(branch, [_line(a, 2, item)])
    await _hand(client, manager, fr)
    await _close(branch)
    async with get_sessionmaker()() as s, s.begin():
        online = (await s.get(Branch, branch)).online_branch_id
        s.info["own_back"] = {f"{order_id}:{item}": 1}  # the pack-scan part took one back
        got = await partner_requests.apply_event(s, {
            "seq": 2, "event_id": str(uuid.uuid4()), "type": "order.undelivered",
            "payload": {"order_id": str(order_id), "branch": {"branch_id": str(online)},
                        "to_status": "delivered",
                        "undelivered": [{"order_item_id": str(item), "quantity": 2}]}})
    assert got == "partner:return:1"


async def test_a_loose_product_without_a_stock_item_is_created_by_weight(client, owner):
    from app.core.db import get_sessionmaker
    from app.models import OnlineProduct, OnlineVariant, Product

    branch, manager, sid, ph, (rice,) = await _setup(client, owner, items=(("Rice", "60.00"),))
    async with get_sessionmaker()() as s, s.begin():
        (await s.get(OnlineProduct, rice)).stock_mode = "loose"
        (await s.get(OnlineVariant, rice)).weight_grams = 500
    order_id, fr = await _order(branch, [_line(rice, 2)])
    await _hand(client, manager, fr)
    await _close(branch)
    async with get_sessionmaker()() as s:
        assert (await s.get(Product, rice)).unit == "kg"
    line = (await _request(order_id)).lines[0]
    assert (await _batch(line.batch_id)).qty_received == Decimal("1.000")
