"""Stock rules the database keeps on its own, and the value of an issue.

Moved from Finance (tests/test_stock_rules.py) with the stock code."""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.models import Product
from app.services import stock


async def _batch(db, branch, qty="3", value="100.00"):
    product = Product(product_id=uuid.uuid4(), name="Test tea", unit="each")
    db.add(product)
    await db.flush()
    batch = await stock.create_batch(
        db, batch_no=f"T-{uuid.uuid4().hex[:8]}", branch_id=branch, product=product,
        supplier_id=None, received_date=date(2026, 10, 1), expiry_date=None,
        qty=Decimal(qty), value=Decimal(value),
    )
    await stock.move(db, batch=batch, move_type="opening", qty=Decimal(qty),
                     value=Decimal(value), move_date=date(2026, 10, 1), doc_type="test",
                     doc_id=uuid.uuid4(), user_id=None)
    return batch


async def test_issues_take_the_last_cent_when_a_batch_empties(db, branch):
    batch = await _batch(db, branch)  # 3 units worth Rs 100.00
    values = []
    for _ in range(3):
        m = await stock.move(db, batch=batch, move_type="writeoff", qty=Decimal("-1"),
                             move_date=date(2026, 10, 2), doc_type="test",
                             doc_id=uuid.uuid4(), user_id=None)
        values.append(m.value)
    assert values == [Decimal("-33.33"), Decimal("-33.34"), Decimal("-33.33")]
    assert sum(values) == Decimal("-100.00")
    assert batch.qty_on_hand == 0 and batch.value_on_hand == 0


async def test_cannot_issue_more_than_the_batch_holds(db, branch):
    batch = await _batch(db, branch)
    with pytest.raises(Exception, match="holds"):
        await stock.move(db, batch=batch, move_type="writeoff", qty=Decimal("-4"),
                         move_date=date(2026, 10, 2), doc_type="test", doc_id=uuid.uuid4(),
                         user_id=None)


async def test_batch_quantity_cannot_be_edited_directly(db, branch):
    batch = await _batch(db, branch)
    with pytest.raises(DBAPIError, match="only through a stock move"):
        await db.execute(text("UPDATE stock_batches SET qty_on_hand = 99 WHERE batch_id = :b"),
                         {"b": batch.batch_id})


async def test_stock_moves_are_append_only(db, branch):
    batch = await _batch(db, branch)
    with pytest.raises(DBAPIError, match="append-only"):
        await db.execute(text("DELETE FROM stock_moves WHERE batch_id = :b"),
                         {"b": batch.batch_id})


async def test_a_batch_never_goes_negative_even_bypassing_the_service(db, branch):
    batch = await _batch(db, branch)
    with pytest.raises(DBAPIError, match="check"):
        await db.execute(text(
            "INSERT INTO stock_moves (batch_id, move_date, move_type, qty, value, doc_type, doc_id)"
            " VALUES (:b, '2026-10-02', 'writeoff', -5, -10, 'test', gen_random_uuid())"
        ), {"b": batch.batch_id})
