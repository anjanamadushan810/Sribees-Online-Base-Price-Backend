"""Fulfilment partner P4 (owner MCQ 2026-11-28): handover of complete bags, one partner
order (Finance PO) per day, goods straight out to the customer, and back to our shelf."""

import uuid
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
)


async def _setup(client, owner, *, items=(("A", "120.00", 2), ("B", "50.00", 1))):
    """A branch with a partner, its manager, and one handed-over-ready request."""
    branch = await _stock_branch("Books branch")
    manager = await sign_in(client, "branch_manager", [branch])
    sid, ph = await _partner(client, owner, branch, signed_in=True)
    products = []
    for name, price, _qty in items:
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


async def _hand(client, manager, fr):
    r = await client.post(f"/partners/requests/{fr.request_id}/handover", headers=manager.headers,
                          json={"lines": [{"line_id": str(ln.line_id), "qty": ln.qty}
                                          for ln in fr.lines]})
    assert r.status_code == 200, r.text
    return r.json()


async def _close(branch, day=None):
    from app.core.db import get_sessionmaker
    from app.services import partner_books

    async with get_sessionmaker()() as s, s.begin():
        return await partner_books.close_day(s, branch, day or _today())


def _today():
    from app.services import partner_books

    return partner_books.local_day(datetime.now(UTC))


async def _journals(prefix):
    from app.core.db import get_sessionmaker
    from app.models import OutboxEvent

    async with get_sessionmaker()() as s:
        rows = await s.scalars(select(OutboxEvent).where(OutboxEvent.event == "journal.requested"))
        return [r.payload for r in rows if (r.payload.get("source_ref") or "").startswith(prefix)]


async def _batch(batch_id):
    from app.core.db import get_sessionmaker
    from app.models import StockBatch

    async with get_sessionmaker()() as s:
        return await s.get(StockBatch, batch_id)


# ---------------------------------------------------------------- handover (F20)
async def test_only_a_complete_bag_is_taken(client, owner):
    branch, manager, sid, ph, (a, b) = await _setup(client, owner)
    order_id, fr = await _order(branch, [_line(a, 2), _line(b, 1)], number="SB-HAND-1")
    la = next(ln for ln in fr.lines if ln.online_product_id == a)

    found = (await client.get("/partners/requests/lookup", headers=manager.headers,
                              params={"code": "SB-HAND-1"})).json()
    assert found["request_no"] == fr.request_no and found["customer_name"] is None
    r = await client.post(f"/partners/requests/{fr.request_id}/tick", headers=manager.headers,
                          json={"lines": [{"line_id": str(la.line_id), "qty": 2}]})
    assert r.status_code == 200
    r = await client.post(f"/partners/requests/{fr.request_id}/handover",
                          headers=manager.headers, json={"lines": []})
    assert r.status_code == 409 and "0/1" in r.json()["detail"]  # B has not come yet
    r = await client.post(f"/partners/requests/{fr.request_id}/tick", headers=manager.headers,
                          json={"lines": [{"line_id": str(la.line_id), "qty": 3}]})
    assert r.status_code == 422  # more than ordered
    done = await _hand(client, manager, (await _request(order_id)))
    assert done["status"] == "handed"

    other = await sign_in(client, "branch_manager", [await _stock_branch("Elsewhere")])
    assert (await client.get("/partners/requests/lookup", headers=other.headers,
                             params={"code": "SB-HAND-1"})).status_code == 404
    assert (await client.post(f"/partners/requests/{fr.request_id}/handover",
                              headers=other.headers, json={"lines": []})).status_code == 404


async def test_a_cancelled_request_cannot_be_handed_over(client, owner):
    branch, manager, sid, ph, (a, _b) = await _setup(client, owner)
    order_id, fr = await _order(branch, [_line(a, 1)])
    await _event(branch, order_id, "cancelled", [_line(a, 1)])
    r = await client.post(f"/partners/requests/{fr.request_id}/handover",
                          headers=manager.headers,
                          json={"lines": [{"line_id": str(fr.lines[0].line_id), "qty": 1}]})
    assert r.status_code == 409


# ---------------------------------------------------------------- the day's order (F18)
async def test_a_day_becomes_one_order_and_the_goods_go_straight_out(client, owner):
    from app.core.db import get_sessionmaker
    from app.models import BasePrice, OnlineStockSend, Receipt, ReceiptLine, SupplyOrder

    branch, manager, sid, ph, (a, b) = await _setup(client, owner)
    o1, fr1 = await _order(branch, [_line(a, 2), _line(b, 1)])
    o2, fr2 = await _order(branch, [_line(a, 1)])
    await _hand(client, manager, fr1)
    await _hand(client, manager, fr2)

    out = await _close(branch)
    assert out["booked"] == 2 and len(out["orders"]) == 1
    assert (await _close(branch))["booked"] == 0  # never twice

    async with get_sessionmaker()() as s:
        order = await s.scalar(select(SupplyOrder).where(SupplyOrder.order_no == out["orders"][0]))
        assert order.is_quick_buy and order.status == "received"
        assert str(order.supplier_id) == sid and order.branch_id == branch
        receipt = await s.scalar(select(Receipt).where(Receipt.order_id == order.order_id))
        assert receipt.total == Decimal("410.00")  # 3 x 120 + 1 x 50
        rows = list(await s.scalars(select(ReceiptLine)
                                    .where(ReceiptLine.receipt_id == receipt.receipt_id)))
        assert all(r.hold for r in rows) and sorted(r.units for r in rows) == [1, 3]
        sends = await s.scalar(select(OnlineStockSend)
                               .where(OnlineStockSend.receipt_id == receipt.receipt_id))
        assert sends is None  # never added to Online's stock
        prices = {bp.online_product_id: bp.unit_price for bp in await s.scalars(
            select(BasePrice).where(BasePrice.order_id == order.order_id))}
        assert prices == {a: Decimal("120.00"), b: Decimal("50.00")}

    for fr in (await _request(o1), await _request(o2)):
        assert fr.booked_at is not None
        for ln in fr.lines:
            assert (await _batch(ln.batch_id)).qty_on_hand == 0  # straight out
    grn = await _journals(f"receipt:{receipt.receipt_id}")
    assert grn[0]["total"] == "410.00"
    out1 = await _journals(f"partner:{fr1.request_id}:out")
    out2 = await _journals(f"partner:{fr2.request_id}:out")
    assert (out1[0]["total"], out2[0]["total"]) == ("290.00", "120.00")
    assert {ln["account_code"] for ln in out1[0]["lines"]} == {"5000", "1300"}

    held = (await client.get("/stock/held", headers=owner.headers,
                             params={"branch_id": str(branch)})).json()
    assert held["lines"] == []  # nothing stays on our shelf


async def test_a_mid_day_price_change_is_paid_at_each_price(client, owner):
    from app.core.db import get_sessionmaker
    from app.models import Receipt, ReceiptLine, SupplyOrder

    branch, manager, sid, ph, (a,) = await _setup(client, owner, items=(("A", "120.00", 1),))
    _o1, fr1 = await _order(branch, [_line(a, 2)])
    variant = fr1.lines[0].online_variant_id
    await _set_item(client, ph, branch, variant, price="130.00", stock=50)
    _o2, fr2 = await _order(branch, [_line(a, 1)])
    await _hand(client, manager, fr1)
    await _hand(client, manager, fr2)
    out = await _close(branch)
    async with get_sessionmaker()() as s:
        order = await s.scalar(select(SupplyOrder).where(SupplyOrder.order_no == out["orders"][0]))
        assert len(order.lines) == 1  # one line per pack size on the PO
        receipt = await s.scalar(select(Receipt).where(Receipt.order_id == order.order_id))
        rows = sorted((r.unit_price, r.units) for r in await s.scalars(
            select(ReceiptLine).where(ReceiptLine.receipt_id == receipt.receipt_id)))
        assert rows == [(Decimal("120.00"), 2), (Decimal("130.00"), 1)]
        assert receipt.total == Decimal("370.00")


async def test_a_day_without_a_price_waits_and_is_booked_after_midnight(client, owner):
    from app.core.db import get_sessionmaker
    from app.models import FulfilmentRequest, PartnerItem
    from app.services import partner_books
    from app.services.errors import SupplyError

    branch, manager, sid, ph, (a,) = await _setup(client, owner, items=(("A", "120.00", 1),))
    _o, fr = await _order(branch, [_line(a, 1)])
    await _hand(client, manager, fr)
    yesterday = datetime.now(UTC) - timedelta(days=1)
    async with get_sessionmaker()() as s, s.begin():
        row = await s.get(FulfilmentRequest, fr.request_id)
        row.handed_at = yesterday
        for ln in row.lines:
            ln.price_to_us = None
        item = await s.scalar(select(PartnerItem).where(PartnerItem.branch_id == branch))
        item.price_to_us = None
    with pytest.raises(SupplyError):
        await _close(branch, partner_books.local_day(yesterday))
    got = await partner_books.run_once(get_sessionmaker())
    assert any(str(branch) in k for k in got["problems"])
    assert any(str(branch) in k for k in (await client.get(
        "/partners/books", headers=owner.headers)).json()["problems"])

    await _set_item(client, ph, branch, fr.lines[0].online_variant_id, price="125.00")
    got = await partner_books.run_once(get_sessionmaker())
    assert any(str(branch) in k for k in got["booked"])
    assert (await _request(fr.online_order_id)).booked_at is not None


async def test_today_is_booked_only_after_midnight(client, owner):
    from app.core.db import get_sessionmaker
    from app.services import partner_books

    branch, manager, sid, ph, (a,) = await _setup(client, owner, items=(("A", "80.00", 1),))
    _o, fr = await _order(branch, [_line(a, 1)])
    await _hand(client, manager, fr)
    now = datetime.now(UTC)
    first = await partner_books.run_once(get_sessionmaker(), now=now)
    assert not any(str(branch) in k for k in (first or {}).get("booked", {}))
    later = await partner_books.run_once(get_sessionmaker(), now=now + timedelta(days=1))
    assert any(str(branch) in k for k in later["booked"])


# ---------------------------------------------------------------- back to our shelf
async def test_a_cancel_after_booking_puts_the_goods_on_the_held_list(client, owner):
    branch, manager, sid, ph, (a, b) = await _setup(client, owner)
    order_id, fr = await _order(branch, [_line(a, 2), _line(b, 1)])
    await _hand(client, manager, fr)
    await _close(branch)
    assert await _event(branch, order_id, "cancelled", []) == "partner:back:3"
    assert await _event(branch, order_id, "cancelled", []) == "ignored"  # once
    fr = await _request(order_id)
    assert fr.order_closed == "cancelled" and all(ln.returned_qty == ln.qty for ln in fr.lines)
    back = await _journals(f"partner:{fr.request_id}:cancelled")
    assert back[0]["total"] == "290.00"
    debit = next(ln for ln in back[0]["lines"] if ln["debit"] != "0.00")
    assert debit["account_code"] == "1300"
    held = (await client.get("/stock/held", headers=owner.headers,
                             params={"branch_id": str(branch)})).json()
    assert sorted(x["left"] for x in held["lines"]) == [1, 2]  # staff label them and sell


async def test_a_cancel_before_booking_is_bought_but_never_dispatched(client, owner):
    branch, manager, sid, ph, (a,) = await _setup(client, owner, items=(("A", "100.00", 1),))
    order_id, fr = await _order(branch, [_line(a, 2)])
    await _hand(client, manager, fr)
    assert await _event(branch, order_id, "cancelled", []) == "partner:back:2"
    await _close(branch)
    fr = await _request(order_id)
    assert (await _batch(fr.lines[0].batch_id)).qty_on_hand == 2  # bought, still with us
    assert await _journals(f"partner:{fr.request_id}:out") == []  # nothing went out


async def test_refused_items_come_back(client, owner):
    branch, manager, sid, ph, (a, b) = await _setup(client, owner)
    item_a = uuid.uuid4()
    order_id, fr = await _order(branch, [_line(a, 2, item_a), _line(b, 1)])
    await _hand(client, manager, fr)
    await _close(branch)
    from app.core.db import get_sessionmaker
    from app.models import Branch
    from app.services import partner_requests

    async with get_sessionmaker()() as s, s.begin():
        online = (await s.get(Branch, branch)).online_branch_id
        got = await partner_requests.apply_event(s, {
            "seq": 1, "event_id": str(uuid.uuid4()), "type": "order.undelivered",
            "payload": {"order_id": str(order_id), "branch": {"branch_id": str(online)},
                        "to_status": "delivered",
                        "undelivered": [{"order_item_id": str(item_a), "quantity": 1}]}})
    assert got == "partner:back:1"
    line_a = next(ln for ln in (await _request(order_id)).lines if ln.order_item_id == item_a)
    assert line_a.returned_qty == 1 and (await _batch(line_a.batch_id)).qty_on_hand == 1


def test_days_are_sri_lanka_days():
    from app.services import partner_books

    # 2026-11-27 20:00 UTC is already 2026-11-28 01:30 in Sri Lanka
    assert partner_books.local_day(datetime(2026, 11, 27, 20, 0, tzinfo=UTC)) == date(2026, 11, 28)


async def test_an_expiry_typed_at_handover_goes_on_the_batch(client, owner):
    branch, manager, sid, ph, (a,) = await _setup(client, owner, items=(("A", "90.00", 1),))
    order_id, fr = await _order(branch, [_line(a, 1)])
    soon = _today() + timedelta(days=40)
    line = fr.lines[0]
    r = await client.post(f"/partners/requests/{fr.request_id}/handover", headers=manager.headers,
                          json={"lines": [{"line_id": str(line.line_id), "qty": 1,
                                           "expiry_date": str(_today())}]})
    assert r.status_code == 422  # expired goods are refused
    r = await client.post(f"/partners/requests/{fr.request_id}/handover", headers=manager.headers,
                          json={"lines": [{"line_id": str(line.line_id), "qty": 1,
                                           "expiry_date": str(soon)}]})
    assert r.status_code == 200, r.text
    await _close(branch)
    batch = await _batch((await _request(order_id)).lines[0].batch_id)
    assert batch.expiry_date == soon
