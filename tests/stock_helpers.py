"""Supply stand-ins for the Finance test helpers the moved stock tests use (joint-go-live
plan, phase B: "the moved Finance stock tests pass in Supply unchanged except imports").

- `new_product`, `fresh_supplier`: same calls and shapes as Finance's.
- `stocked`: Finance ordered and received a PO; here goods arrive the Supply way, through
  POST /orders/{id}/receipts against an approved order of a supplier without the app (the
  order itself is written straight to the database: ordering is tested elsewhere).
- `balance`: Finance read its trial balance; Supply has no ledger, so this sums the
  journal lines Supply asked Finance to post (`journal.requested` events) for an account
  and branch. Same sign convention: debit minus credit.
"""

import secrets
import uuid
from decimal import Decimal

from sqlalchemy import select, text


async def new_product(client, who, unit="each", track_expiry=False, name="Dhal 1kg"):
    r = await client.post("/products", headers=who.headers, json={
        "product_id": str(uuid.uuid4()), "name": f"{name} {secrets.token_hex(2)}",
        "unit": unit, "track_expiry": track_expiry,
    })
    assert r.status_code == 201, r.text
    return r.json()


async def fresh_supplier(client, owner, credit_days=30):
    """A supplier without the app (a market cash buy): its approved orders are received
    directly."""
    from app.core.db import get_sessionmaker
    from app.models import Supplier

    async with get_sessionmaker()() as s, s.begin():
        number = await s.scalar(text("SELECT nextval('supplier_code_seq')"))
        sup = Supplier(name=f"Supplier PS{secrets.token_hex(3).upper()}",
                       code=f"SUP-{number:04d}")
        s.add(sup)
        await s.flush()
        return {"supplier_id": str(sup.supplier_id), "name": sup.name}


async def _user_id(email: str) -> uuid.UUID:
    from app.core.db import get_sessionmaker
    from app.models import User

    async with get_sessionmaker()() as s:
        return await s.scalar(select(User.user_id).where(User.email == email))


async def approved_order(owner, supplier, branch, lines, day="2026-10-02"):
    """An approved order with one line per (product, qty, price); returns (order_id,
    [line_id, ...]). Each stock product gets an Online variant with its own id."""
    from datetime import datetime, time

    from app.core.db import get_sessionmaker
    from app.models import (
        OnlineProduct,
        OnlineVariant,
        SupplierOffer,
        SupplierProduct,
        SupplierSection,
        SupplyOrder,
        SupplyOrderLine,
    )
    from app.services.orders import COLOMBO

    by = await _user_id(owner.email)
    sid = uuid.UUID(supplier["supplier_id"])
    async with get_sessionmaker()() as s, s.begin():
        section = SupplierSection(supplier_id=sid, title="Test")
        s.add(section)
        await s.flush()
        order = SupplyOrder(supplier_id=sid, branch_id=uuid.UUID(str(branch)), created_by=by,
                            status="draft")
        s.add(order)
        await s.flush()
        line_ids = []
        for product, units, price in lines:
            pid = uuid.UUID(product["product_id"])
            if await s.get(OnlineVariant, pid) is None:
                op = OnlineProduct(online_product_id=uuid.uuid4(), name=product["name"])
                s.add(op)
                await s.flush()
                s.add(OnlineVariant(online_variant_id=pid,
                                    online_product_id=op.online_product_id,
                                    name=""))
                await s.flush()
            op_id = (await s.get(OnlineVariant, pid)).online_product_id
            sp = SupplierProduct(supplier_id=sid, section_id=section.section_id,
                                 heading=product["name"])
            s.add(sp)
            await s.flush()
            offer = SupplierOffer(supplier_id=sid, product_id=sp.product_id,
                                  price=Decimal(price), units_per_box=1)
            s.add(offer)
            await s.flush()
            ln = SupplyOrderLine(order_id=order.order_id, supplier_id=sid,
                                 offer_id=offer.offer_id, online_product_id=op_id,
                                 online_variant_id=pid, heading=product["name"],
                                 boxes=int(units), units_per_box=1,
                                 unit_price=Decimal(price))
            s.add(ln)
            await s.flush()
            line_ids.append(str(ln.line_id))
        at = datetime.combine(datetime.fromisoformat(day).date(), time(9), COLOMBO)
        order.status, order.submitted_by, order.submitted_at = "submitted", by, at
        order.status, order.approved_by, order.approved_at = "approved", by, at
        return str(order.order_id), line_ids


async def receive(client, who, order_id, lines, day="2026-10-02", **extra):
    r = await client.post(f"/orders/{order_id}/receipts", headers=who.headers,
                          json={"received_date": day, "lines": lines, **extra})
    return r


async def stocked(client, owner, branch, qty="10", price="100.00", expiry=None, day="2026-10-02",
                  product=None, track_expiry=None, name="Jam"):
    """Receive one product at the branch; returns (product, batch)."""
    supplier = await fresh_supplier(client, owner)
    if product is None:
        product = await new_product(client, owner, track_expiry=expiry is not None, name=name)
    order_id, (line_id,) = await approved_order(owner, supplier, branch,
                                                [(product, int(Decimal(qty)), price)], day=day)
    line = {"order_line_id": line_id, "units": int(Decimal(qty))}
    if expiry:
        line["expiry_date"] = expiry
    r = await receive(client, owner, order_id, [line], day=day)
    assert r.status_code == 201, r.text
    rcv = r.json()
    batches = (await client.get("/stock/batches", headers=owner.headers,
                                params={"product_id": product["product_id"]})).json()
    batch = next(b for b in batches if b["batch_id"] == rcv["lines"][0]["batch_id"])
    return product, batch


async def batch_qty(client, who, product):
    rows = (await client.get("/stock/batches", headers=who.headers,
                             params={"product_id": product["product_id"]})).json()
    return {b["batch_no"]: b["qty_on_hand"] for b in rows}


async def journal_requests() -> list[dict]:
    from app.core.db import get_sessionmaker
    from app.models import OutboxEvent

    async with get_sessionmaker()() as s:
        rows = await s.scalars(select(OutboxEvent).where(OutboxEvent.event == "journal.requested")
                               .order_by(OutboxEvent.seq))
        return [{"event_id": str(r.event_id), **r.payload} for r in rows]


async def balance(client, who, code, branch):
    from app.core.db import get_sessionmaker
    from app.models import Branch

    async with get_sessionmaker()() as s:
        online = (await s.get(Branch, uuid.UUID(str(branch)))).online_branch_id
    total = Decimal("0")
    for j in await journal_requests():
        for ln in j["lines"]:
            if ln["account_code"] == code and ln["online_branch_id"] == str(online):
                total += Decimal(ln["debit"]) - Decimal(ln["credit"])
    return total
