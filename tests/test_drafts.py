"""Barcode-first receiving drafts (plan 2026-11-27, P2): saved product by product, QR labels
held at once, a QR never counts twice, finish = the normal receipt."""

import asyncio
from datetime import date, timedelta

from sqlalchemy import func, select

from app.services import labels
from tests.stock_helpers import approved_order, fresh_supplier, new_product

TODAY = date.today()
EXP = str(TODAY + timedelta(days=200))


async def _labels(client, owner, n):
    rng = (await client.post("/labels/ranges", headers=owner.headers, json={"count": n})).json()
    first = int(rng["first"][:9])
    return [labels.make_number(b) for b in range(first, first + n)]


async def _order(client, owner, branch, items):
    supplier = await fresh_supplier(client, owner)
    prods = [await new_product(client, owner, name=name) for name, _, _ in items]
    rows = [(p, u, price) for p, (_, u, price) in zip(prods, items, strict=True)]
    oid, lines = await approved_order(owner, supplier, branch, rows)
    return oid, lines, prods


async def _draft(client, who, oid):
    r = await client.post(f"/orders/{oid}/receipt-draft", headers=who.headers)
    assert r.status_code == 200, r.text
    return r.json()


async def _line(client, who, did, **body):
    body.setdefault("expiry_date", EXP)
    return await client.post(f"/receipt-drafts/{did}/lines", headers=who.headers, json=body)


async def _finish(client, who, did, **extra):
    return await client.post(f"/receipt-drafts/{did}/finish", headers=who.headers,
                             json={"received_date": str(TODAY), **extra})


async def _linked(batch_id):
    from app.core.db import get_sessionmaker
    from app.models import Label
    async with get_sessionmaker()() as s:
        return await s.scalar(select(func.count()).select_from(Label).where(
            Label.batch_id == batch_id, Label.status == "linked"))


async def test_saved_product_by_product_then_finished(client, owner, branch):
    oid, (l1, l2), _ = await _order(client, owner, branch,
                                    [("Soap DR", 3, "100.00"), ("Tea DR", 2, "50.00")])
    qr = await _labels(client, owner, 5)
    d = await _draft(client, owner, oid)
    assert [x["due"] for x in d["order_lines"]] == [3, 2]
    # scanned one at a time: each scan is held at once
    for n in qr[:3]:
        r = await client.post(f"/receipt-drafts/{d['draft_id']}/labels", headers=owner.headers,
                              json={"number": n})
        assert r.status_code == 200, r.text
    r = await _line(client, owner, d["draft_id"], order_line_id=l1, units=3, mrp="150.00",
                    labels=qr[:3])
    assert r.status_code == 201, r.text
    r = await _line(client, owner, d["draft_id"], order_line_id=l2, units=2, labels=qr[3:5])
    assert r.status_code == 201, r.text

    # the phone died: opening the order again gives the same draft, nothing lost
    again = await _draft(client, owner, oid)
    assert again["draft_id"] == d["draft_id"]
    assert [len(x["labels"]) for x in again["lines"]] == [3, 2]
    assert [x["due"] for x in again["order_lines"]] == [0, 0]

    r = await _finish(client, owner, d["draft_id"], invoice_no="B-1", invoice_total="400.00")
    assert r.status_code == 201, r.text
    rec = r.json()
    assert [ln["units"] for ln in rec["lines"]] == [3, 2]
    assert [ln["expiry_date"] for ln in rec["lines"]] == [EXP, EXP]
    for ln in rec["lines"]:
        assert await _linked(ln["batch_id"]) == ln["units"]
    done = (await client.get(f"/receipt-drafts/{d['draft_id']}", headers=owner.headers)).json()
    assert done["status"] == "finished" and done["receipt_id"] == rec["receipt_id"]
    # a finished draft takes nothing more
    r = await _line(client, owner, d["draft_id"], order_line_id=l1, units=1, labels=[])
    assert r.status_code == 409


async def test_exactly_one_qr_per_unit(client, owner, branch):
    oid, (l1,), _ = await _order(client, owner, branch, [("Biscuit DR", 48, "40.00")])
    qr = await _labels(client, owner, 49)
    d = await _draft(client, owner, oid)
    r = await _line(client, owner, d["draft_id"], order_line_id=l1, units=48, labels=qr[:47])
    assert r.status_code == 422 and "48 units need 48 QR labels, 47" in r.text
    r = await _line(client, owner, d["draft_id"], order_line_id=l1, units=48, labels=qr[:49])
    assert r.status_code == 422
    r = await _line(client, owner, d["draft_id"], order_line_id=l1, units=48,
                    labels=qr[:47] + [qr[0]])
    assert r.status_code == 422 and "twice" in r.text
    r = await _line(client, owner, d["draft_id"], order_line_id=l1, units=48, labels=qr[:48])
    assert r.status_code == 201, r.text
    # more than is due
    r = await _line(client, owner, d["draft_id"], order_line_id=l1, units=1, labels=[qr[48]])
    assert r.status_code == 422 and "still due" in r.text


async def test_same_qr_on_two_products_is_refused(client, owner, branch):
    oid, (l1, l2), _ = await _order(client, owner, branch,
                                    [("Jam DR", 1, "10.00"), ("Salt DR", 1, "10.00")])
    qr = await _labels(client, owner, 2)
    d = await _draft(client, owner, oid)
    assert (await _line(client, owner, d["draft_id"], order_line_id=l1, units=1,
                        labels=[qr[0]])).status_code == 201
    r = await _line(client, owner, d["draft_id"], order_line_id=l2, units=1, labels=[qr[0]])
    assert r.status_code == 409 and "another product of this delivery" in r.text
    r = await client.post(f"/receipt-drafts/{d['draft_id']}/labels", headers=owner.headers,
                          json={"number": qr[0]})
    assert r.status_code == 409
    # removing the product frees its QR
    line_id = (await _draft(client, owner, oid))["lines"][0]["draft_line_id"]
    r = await client.delete(f"/receipt-drafts/{d['draft_id']}/lines/{line_id}",
                            headers=owner.headers)
    assert r.status_code == 200 and r.json()["lines"] == []
    assert (await _line(client, owner, d["draft_id"], order_line_id=l2, units=1,
                        labels=[qr[0]])).status_code == 201


async def test_two_phones_cannot_hold_one_qr(client, owner, branch):
    oid_a, _, _ = await _order(client, owner, branch, [("Rice A", 1, "10.00")])
    oid_b, _, _ = await _order(client, owner, branch, [("Rice B", 1, "10.00")])
    (qr,) = await _labels(client, owner, 1)
    a, b = await _draft(client, owner, oid_a), await _draft(client, owner, oid_b)
    ra, rb = await asyncio.gather(*[
        client.post(f"/receipt-drafts/{x['draft_id']}/labels", headers=owner.headers,
                    json={"number": qr}) for x in (a, b)])
    assert sorted([ra.status_code, rb.status_code]) == [200, 409]


async def test_used_or_held_qr_is_refused_elsewhere(client, owner, branch):
    from tests.stock_helpers import receive

    oid, (l1,), _ = await _order(client, owner, branch, [("Oil DR", 1, "10.00")])
    oid2, (m1,), _ = await _order(client, owner, branch, [("Oil DR2", 1, "10.00")])
    qr = await _labels(client, owner, 2)
    d = await _draft(client, owner, oid)
    assert (await _line(client, owner, d["draft_id"], order_line_id=l1, units=1,
                        labels=[qr[0]])).status_code == 201
    # the web Receive page cannot take a QR a phone holds
    r = await receive(client, owner, oid2, [{"order_line_id": m1, "units": 1,
                                             "expiry_date": EXP, "labels": [qr[0]]}],
                      day=str(TODAY))
    assert r.status_code == 409 and "staff app" in r.text
    assert (await _finish(client, owner, d["draft_id"])).status_code == 201
    # now linked to a batch: no draft can take it
    d2 = await _draft(client, owner, oid2)
    r = await client.post(f"/receipt-drafts/{d2['draft_id']}/labels", headers=owner.headers,
                          json={"number": qr[0]})
    assert r.status_code == 409 and "already linked" in r.text


async def test_expiry_rules(client, owner, branch):
    oid, (l1,), _ = await _order(client, owner, branch, [("Milk DR", 1, "10.00")])
    (qr,) = await _labels(client, owner, 1)
    d = await _draft(client, owner, oid)
    r = await client.post(f"/receipt-drafts/{d['draft_id']}/lines", headers=owner.headers,
                          json={"order_line_id": l1, "units": 1, "labels": [qr]})
    assert r.status_code == 422  # no expiry: refused
    too_far = TODAY.replace(year=TODAY.year + 11)
    for bad in (str(TODAY), str(TODAY - timedelta(days=3)), str(too_far)):
        r = await _line(client, owner, d["draft_id"], order_line_id=l1, units=1, labels=[qr],
                        expiry_date=bad)
        assert r.status_code == 422, bad
    assert (await _line(client, owner, d["draft_id"], order_line_id=l1, units=1,
                        labels=[qr])).status_code == 201


def test_expiry_check_on_29_february():
    from app.services.drafts import check_expiry

    check_expiry(date(2032, 6, 1), date(2028, 2, 29))  # no error on a leap day
    import pytest

    from app.services.errors import SupplyError
    with pytest.raises(SupplyError, match="10 years"):
        check_expiry(date(2038, 6, 1), date(2028, 2, 29))


async def test_one_box_two_expiry_dates_two_batches(client, owner, branch):
    oid, (l1,), _ = await _order(client, owner, branch, [("Yoghurt DR", 4, "60.00")])
    qr = await _labels(client, owner, 4)
    d = await _draft(client, owner, oid)
    e1, e2 = str(TODAY + timedelta(days=20)), str(TODAY + timedelta(days=40))
    assert (await _line(client, owner, d["draft_id"], order_line_id=l1, units=1,
                        labels=qr[:1], expiry_date=e1)).status_code == 201
    assert (await _line(client, owner, d["draft_id"], order_line_id=l1, units=3,
                        labels=qr[1:], expiry_date=e2)).status_code == 201
    rec = (await _finish(client, owner, d["draft_id"])).json()
    assert sorted((ln["units"], ln["expiry_date"]) for ln in rec["lines"]) == [(1, e1), (3, e2)]
    assert len({ln["batch_id"] for ln in rec["lines"]}) == 2


async def test_scanned_by_barcode_and_unordered_product(client, owner, branch):
    oid, (l1,), prods = await _order(client, owner, branch, [("Soda DR", 2, "30.00")])
    qr = await _labels(client, owner, 3)
    d = await _draft(client, owner, oid)
    # the pack's own Online id (what a barcode lookup returns) finds its order line
    r = await _line(client, owner, d["draft_id"], online_variant_id=prods[0]["product_id"],
                    units=2, labels=qr[:2])
    assert r.status_code == 201, r.text
    assert r.json()["lines"][0]["order_line_id"] == l1
    # a product we sell that was not ordered needs its bill price
    other = await new_product(client, owner, name="Extra DR")
    _, _ = await approved_order(owner, await fresh_supplier(client, owner), branch,
                                [(other, 1, "5.00")])  # gives it an Online variant
    r = await _line(client, owner, d["draft_id"], online_variant_id=other["product_id"],
                    units=1, labels=qr[2:])
    assert r.status_code == 422 and "bill price" in r.text
    r = await _line(client, owner, d["draft_id"], online_variant_id=other["product_id"],
                    units=1, labels=qr[2:], unit_price="5.00")
    assert r.status_code == 201, r.text
    rec = await _finish(client, owner, d["draft_id"])
    assert rec.status_code == 201, rec.text
    assert sorted(ln["units"] for ln in rec.json()["lines"]) == [1, 2]


async def test_loose_scans_block_finish_and_discard_frees(client, owner, branch):
    oid, (l1,), _ = await _order(client, owner, branch, [("Dhal DR", 1, "10.00")])
    qr = await _labels(client, owner, 2)
    d = await _draft(client, owner, oid)
    assert (await _line(client, owner, d["draft_id"], order_line_id=l1, units=1,
                        labels=qr[:1])).status_code == 201
    await client.post(f"/receipt-drafts/{d['draft_id']}/labels", headers=owner.headers,
                      json={"number": qr[1]})
    r = await _finish(client, owner, d["draft_id"])
    assert r.status_code == 422 and "not on any product" in r.text
    assert (await client.delete(f"/receipt-drafts/{d['draft_id']}/labels/{qr[1]}",
                                headers=owner.headers)).status_code == 204
    assert (await client.delete(f"/receipt-drafts/{d['draft_id']}",
                                headers=owner.headers)).status_code == 204
    # every QR is free again: a new draft takes it
    d2 = await _draft(client, owner, oid)
    assert d2["draft_id"] != d["draft_id"] and d2["lines"] == []
    assert (await _line(client, owner, d2["draft_id"], order_line_id=l1, units=1,
                        labels=qr[:1])).status_code == 201


async def test_other_branch_cannot_see_the_draft(client, owner, branch, other_branch):
    from tests.conftest import sign_in

    oid, _, _ = await _order(client, owner, branch, [("Ghee DR", 1, "10.00")])
    d = await _draft(client, owner, oid)
    stranger = await sign_in(client, "branch_manager", [other_branch])
    assert (await client.get(f"/receipt-drafts/{d['draft_id']}",
                             headers=stranger.headers)).status_code == 404
    assert (await client.post(f"/orders/{oid}/receipt-draft",
                              headers=stranger.headers)).status_code == 404
