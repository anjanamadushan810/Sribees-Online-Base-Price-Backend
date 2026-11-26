"""Quick buy (owner 2026-11-26): a market cash buy ordered and received in one step."""

import uuid
from decimal import Decimal

from sqlalchemy import select

from app.services import labels as label_svc
from tests.conftest import sign_in
from tests.stock_helpers import D, fresh_supplier
from tests.test_supplier_auth import settings_override


async def online_item(name="Coconut QB", grams=None):
    from app.core.db import get_sessionmaker
    from app.models import OnlineProduct, OnlineVariant

    async with get_sessionmaker()() as s, s.begin():
        p = OnlineProduct(online_product_id=uuid.uuid4(), name=f"{name} {uuid.uuid4().hex[:4]}")
        s.add(p)
        await s.flush()
        v = OnlineVariant(online_variant_id=uuid.uuid4(), online_product_id=p.online_product_id,
                          name="1 pc", weight_grams=grams)
        s.add(v)
        await s.flush()
        return str(v.online_variant_id), p.name


async def test_a_market_buy_is_received_in_one_step(client, owner, branch):
    from app.core.db import get_sessionmaker
    from app.models import OutboxEvent

    supplier = await fresh_supplier(client, owner)
    coconut, cname = await online_item()
    onion, _ = await online_item("Onion QB")
    rng = (await client.post("/labels/ranges", headers=owner.headers, json={"count": 3})).json()
    first, last = int(rng["first"][:9]), int(rng["last"][:9])
    nums = [label_svc.make_number(b) for b in range(first, last + 1)]
    found = (await client.get("/quick-buys/items", headers=owner.headers,
                              params={"q": cname[:8]})).json()
    assert any(x["online_variant_id"] == coconut for x in found)

    body = {"supplier_id": supplier["supplier_id"], "branch_id": str(branch),
            "received_date": D("2026-11-25"), "bill_no": "M-12", "bill_total": "530.00",
            "lines": [{"online_variant_id": coconut, "units": 3, "unit_price": "110.00",
                       "labels": nums},
                      {"online_variant_id": onion, "units": 1, "unit_price": "200.00"}]}
    with settings_override(order_approval="finance"):
        r = await client.post("/quick-buys", headers=owner.headers, json=body)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["order_status"] == "received"
    assert Decimal(out["receipt"]["total"]) == Decimal("530.00")
    look = (await client.get(f"/labels/{nums[0]}", headers=owner.headers)).json()
    assert look["batch"]["batch_id"] == out["receipt"]["lines"][0]["batch_id"]
    async with get_sessionmaker()() as s:
        kinds = [e.event for e in await s.scalars(select(OutboxEvent).where(
            OutboxEvent.payload["order_id"].astext == out["order_id"]).order_by(OutboxEvent.seq))]
        rcv = await s.scalar(select(OutboxEvent).where(
            OutboxEvent.event == "receipt.posted",
            OutboxEvent.payload["order_id"].astext == out["order_id"]))
    assert "order.submitted" in kinds and "receipt.posted" in kinds  # Finance hears both
    assert "supplier.notify" not in kinds  # paid at their counter: no alert to their app
    assert rcv.payload["invoice_no"] == "M-12" and rcv.payload["invoice_total"] == "530.00"

    # the same item twice, or nothing, is refused
    body2 = {**body, "lines": [body["lines"][1], body["lines"][1]]}
    assert (await client.post("/quick-buys", headers=owner.headers, json=body2)).status_code == 422


async def test_who_and_which_supplier(client, owner, branch):
    from app.core.db import get_sessionmaker
    from app.models import SupplierUser

    item, _ = await online_item("Lime QB")
    supplier = await fresh_supplier(client, owner)
    body = {"supplier_id": supplier["supplier_id"], "branch_id": str(branch),
            "received_date": D("2026-11-25"),
            "lines": [{"online_variant_id": item, "units": 1, "unit_price": "50.00"}]}
    acc = await sign_in(client, "accountant")
    assert (await client.post("/quick-buys", headers=acc.headers, json=body)).status_code == 403
    async with get_sessionmaker()() as s, s.begin():
        s.add(SupplierUser(supplier_id=uuid.UUID(supplier["supplier_id"]), phone="+94770000999",
                           full_name="Shop app user"))
    # owner 2026-11-26: a supplier on the app (Nishi Palace) may be paid at the counter too
    r = await client.post("/quick-buys", headers=owner.headers, json=body)
    assert r.status_code == 201, r.text
    # the price paid is the item's base price
    from app.models import BasePrice
    async with get_sessionmaker()() as s:
        bp = await s.scalar(select(BasePrice).where(
            BasePrice.online_variant_id == uuid.UUID(item)))
    assert bp.unit_price == Decimal("50.00")
    assert str(bp.supplier_id) == supplier["supplier_id"]


async def test_a_bill_from_last_week_keeps_its_date(client, owner, branch):
    """Owner 2026-11-26: bills of the last days are written up now; the bill's date stands."""
    item, _ = await online_item("Bean QB")
    supplier = await fresh_supplier(client, owner)
    body = {"supplier_id": supplier["supplier_id"], "branch_id": str(branch),
            "received_date": D("2026-11-18"), "bill_no": "OLD-1",
            "lines": [{"online_variant_id": item, "units": 2, "unit_price": "40.00"}]}
    r = await client.post("/quick-buys", headers=owner.headers, json=body)
    assert r.status_code == 201, r.text
    assert r.json()["receipt"]["received_date"] == D("2026-11-18")
