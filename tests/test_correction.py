"""Product correction (owner 2026-11-26): stock received on the wrong product moves to the
right one at the same value; the received batch itself is never edited."""

from decimal import Decimal

from sqlalchemy import select

from tests.conftest import sign_in
from tests.stock_helpers import D, fresh_supplier
from tests.test_quick_buy import online_item


async def _bought(client, owner, branch, item, units=3, price="100.00"):
    supplier = await fresh_supplier(client, owner)
    r = await client.post("/quick-buys", headers=owner.headers, json={
        "supplier_id": supplier["supplier_id"], "branch_id": str(branch),
        "received_date": D("2026-11-18"),
        "lines": [{"online_variant_id": item, "units": units, "unit_price": price}]})
    assert r.status_code == 201, r.text
    return r.json()["receipt"]["lines"][0]["batch_id"]


async def test_units_move_to_the_right_product(client, owner, branch):
    from app.core.db import get_sessionmaker
    from app.models import AuditLog, StockBatch, StockMove

    wrong, _ = await online_item("Dup Wash")
    right, _ = await online_item("Real Wash")
    batch_id = await _bought(client, owner, branch, wrong)
    r = await client.post("/stock/corrections", headers=owner.headers, json={
        "batch_id": batch_id, "qty": "2", "to_product_id": right,
        "reason": "duplicate product made from a bill"})
    assert r.status_code == 201, r.text
    out = r.json()
    assert Decimal(out["value"]) == Decimal("200.00")
    async with get_sessionmaker()() as s:
        src = await s.get(StockBatch, __import__("uuid").UUID(batch_id))
        new = await s.get(StockBatch, __import__("uuid").UUID(out["batch_id"]))
        assert (src.qty_on_hand, src.value_on_hand) == (Decimal("1"), Decimal("100.00"))
        assert str(new.product_id) == right and new.qty_on_hand == Decimal("2")
        assert new.received_date == src.received_date and new.supplier_id == src.supplier_id
        kinds = sorted(m.move_type for m in await s.scalars(
            select(StockMove).where(StockMove.doc_type == "correction")))
        assert kinds == ["correct_in", "correct_out"]
        assert await s.scalar(select(AuditLog).where(AuditLog.action == "stock.correct_product"))


async def test_only_the_owner_and_never_more_than_is_there(client, owner, branch):
    wrong, _ = await online_item("Dup Lotion")
    right, _ = await online_item("Real Lotion")
    batch_id = await _bought(client, owner, branch, wrong, units=1)
    body = {"batch_id": batch_id, "qty": "2", "to_product_id": right, "reason": "wrong kind"}
    r = await client.post("/stock/corrections", headers=owner.headers, json=body)
    assert r.status_code == 422 and "only" in r.json()["detail"]
    acc = await sign_in(client, "purchasing")
    r = await client.post("/stock/corrections", headers=acc.headers, json={**body, "qty": "1"})
    assert r.status_code == 403
    r = await client.post("/stock/corrections", headers=owner.headers,
                          json={**body, "qty": "1", "to_product_id": wrong})
    assert r.status_code == 422
