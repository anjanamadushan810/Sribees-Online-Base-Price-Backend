"""Repack (B2): a weighed bag packed into Online pack sizes, cost shared by weight."""

import uuid
from decimal import Decimal

from app.services import labels as label_svc
from tests.stock_helpers import D, approved_order, fresh_supplier, new_product, receive


async def _bag(client, owner, branch, kg="25", price="5000.00", expiry=None):
    rice = await new_product(client, owner, unit="kg", name="Rice RP")
    supplier = await fresh_supplier(client, owner)
    oid, (line,) = await approved_order(owner, supplier, branch, [(rice, 1, price)])
    body = {"order_line_id": line, "units": 1, "weight_kg": kg}
    if expiry:
        body["expiry_date"] = expiry
    r = await receive(client, owner, oid, [body])
    assert r.status_code == 201, r.text
    return rice, r.json()["lines"][0]["batch_id"]


async def _pack_size(grams):
    from app.core.db import get_sessionmaker
    from app.models import OnlineProduct, OnlineVariant

    async with get_sessionmaker()() as s, s.begin():
        p = OnlineProduct(online_product_id=uuid.uuid4(), name=f"Rice pack {uuid.uuid4().hex[:4]}")
        s.add(p)
        await s.flush()
        v = OnlineVariant(online_variant_id=uuid.uuid4(), online_product_id=p.online_product_id,
                          name=f"{grams} g", weight_grams=grams)
        s.add(v)
        return str(v.online_variant_id)


async def test_a_bag_becomes_packs_at_its_cost(client, owner, branch):
    _, bag = await _bag(client, owner, branch)
    one, half = await _pack_size(1000), await _pack_size(500)
    rng = (await client.post("/labels/ranges", headers=owner.headers, json={"count": 10})).json()
    first = int(rng["first"][:9])
    nums = [label_svc.make_number(b) for b in range(first, first + 10)]
    body = {"batch_id": bag, "kg_used": "20.5", "repack_date": D("2026-11-25"),
            "outputs": [{"online_variant_id": one, "packs": 10, "labels": nums},
                        {"online_variant_id": half, "packs": 20}]}
    r = await client.post("/stock/repacks", headers=owner.headers, json=body)
    assert r.status_code == 201, r.text
    out = r.json()
    assert Decimal(out["waste_kg"]) == Decimal("0.5")
    assert Decimal(out["value"]) == Decimal("4100.00")  # 20.5 of 25 kg at Rs 200/kg
    v1, v2 = (Decimal(o["value"]) for o in out["outputs"])
    assert v1 == v2 == Decimal("2050.00") and v1 + v2 == Decimal(out["value"])
    look = (await client.get(f"/labels/{nums[0]}", headers=owner.headers)).json()
    assert look["batch"]["batch_id"] == out["outputs"][0]["batch_id"]
    rows = (await client.get("/stock/batches", headers=owner.headers)).json()
    left = next(b for b in rows if b["batch_id"] == bag)
    assert Decimal(left["qty_on_hand"]) == Decimal("4.5")

    # more packs than rice, or too much waste, is refused
    bad = {**body, "kg_used": "1", "outputs": [{"online_variant_id": one, "packs": 2}]}
    r = await client.post("/stock/repacks", headers=owner.headers, json=bad)
    assert r.status_code == 422 and "weigh again" in r.json()["detail"]
    bad = {**body, "kg_used": "4", "outputs": [{"online_variant_id": one, "packs": 3}]}
    r = await client.post("/stock/repacks", headers=owner.headers, json=bad)
    assert r.status_code == 422 and "waste" in r.json()["detail"]


async def test_pieces_and_expired_bags_are_not_repacked(client, owner, branch):
    from tests.stock_helpers import stocked

    _, batch = await stocked(client, owner, branch, qty="5", name="Soap RP")
    one = await _pack_size(1000)
    r = await client.post("/stock/repacks", headers=owner.headers, json={
        "batch_id": batch["batch_id"], "kg_used": "1", "repack_date": D("2026-11-25"),
        "outputs": [{"online_variant_id": one, "packs": 1}]})
    assert r.status_code == 422 and "pieces" in r.json()["detail"]
    _, bag = await _bag(client, owner, branch, expiry=D("2026-11-20"))
    r = await client.post("/stock/repacks", headers=owner.headers, json={
        "batch_id": bag, "kg_used": "1", "repack_date": D("2026-11-25"),
        "outputs": [{"online_variant_id": one, "packs": 1}]})
    assert r.status_code == 422 and "expired" in r.json()["detail"]
