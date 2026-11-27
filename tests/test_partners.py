"""Fulfilment partner, phase P1 (docs/plans/fulfilment-partner-2026-11-27.md)."""

import secrets
import uuid
from datetime import date
from decimal import Decimal

from tests.conftest import _stock_branch, sign_in
from tests.test_supplier_auth import new_supplier, signed_in_supplier


async def _product(name="Item", variants=0):
    """An Online product in the catalogue mirror, with its default pack size (or real ones)."""
    from app.core.db import get_sessionmaker
    from app.models import OnlineProduct, OnlineVariant

    pid = uuid.uuid4()
    vids = []
    async with get_sessionmaker()() as s, s.begin():
        s.add(OnlineProduct(online_product_id=pid, name=f"{name} {secrets.token_hex(2)}",
                            is_active=True))
        await s.flush()
        s.add(OnlineVariant(online_variant_id=pid, online_product_id=pid, name="",
                            is_active=variants == 0))
        for n in range(variants):
            vid = uuid.uuid4()
            vids.append(vid)
            s.add(OnlineVariant(online_variant_id=vid, online_product_id=pid,
                                name=f"Shade {n + 1}", is_active=True))
    return pid, (vids or [pid])


async def _list(branch_id, *pids):
    from app.core.db import get_sessionmaker
    from app.models import OnlineBranchListing

    async with get_sessionmaker()() as s, s.begin():
        for pid in pids:
            s.add(OnlineBranchListing(branch_id=branch_id, online_product_id=pid))


async def _carry(branch_id, variant_id):
    """The branch once held stock of this pack size."""
    from app.core.db import get_sessionmaker
    from app.models import Product, StockBatch

    async with get_sessionmaker()() as s, s.begin():
        if await s.get(Product, variant_id) is None:
            s.add(Product(product_id=variant_id, name="carried", unit="each",
                          stock_mode="unit", kind="sellable"))
            await s.flush()
        s.add(StockBatch(batch_no=f"T-{secrets.token_hex(3)}", branch_id=branch_id,
                         product_id=variant_id, received_date=date.today(),
                         unit_cost=Decimal("10"), qty_received=Decimal("1"),
                         qty_on_hand=Decimal("0"), value_on_hand=Decimal("0")))


async def _partner(client, owner, branch_id, signed_in=False):
    if signed_in:
        supplier, su, tokens = await signed_in_supplier(client, owner)
    else:
        supplier, su = await new_supplier(client, owner)
        tokens = None
    sid = supplier["supplier_id"]
    r = await client.put(f"/partners/{sid}/kind", headers=owner.headers,
                         json={"kind": "fulfilment_partner"})
    assert r.status_code == 200, r.text
    r = await client.post(f"/partners/{sid}/branches", headers=owner.headers,
                          json={"branch_id": str(branch_id)})
    assert r.status_code == 201, r.text
    headers = {"Authorization": f"Bearer {tokens['access_token']}"} if tokens else None
    return sid, headers


# ---------------------------------------------------------------- set-up
async def test_only_a_partner_can_serve_a_branch(client, owner, branch_manager):
    branch = await _stock_branch("P branch")
    supplier, _ = await new_supplier(client, owner)
    sid = supplier["supplier_id"]
    r = await client.post(f"/partners/{sid}/branches", headers=owner.headers,
                          json={"branch_id": str(branch)})
    assert r.status_code == 422  # still a wholesale supplier
    r = await client.put(f"/partners/{sid}/kind", headers=branch_manager.headers,
                         json={"kind": "fulfilment_partner"})
    assert r.status_code == 403
    await _partner(client, owner, branch)


async def test_seed_takes_listed_products_the_branch_does_not_carry(client, owner):
    branch = await _stock_branch("Seed branch")
    sid, _ = await _partner(client, owner, branch)
    carried, cv = await _product("Carried")
    missing, _ = await _product("Missing")
    decided, _ = await _product("Decided own")
    not_listed, _ = await _product("Not listed")
    await _list(branch, carried, missing, decided)
    await _carry(branch, cv[0])
    r = await client.put("/partners/sourcing", headers=owner.headers, json={
        "branch_id": str(branch), "online_product_id": str(decided), "mode": "own"})
    assert r.status_code == 200, r.text

    r = await client.post(f"/partners/{sid}/seed", headers=owner.headers,
                          json={"branch_id": str(branch)})
    assert r.status_code == 200, r.text
    assert r.json() == {"added": 1, "skipped_carried": 1, "listed": 3}
    again = await client.post(f"/partners/{sid}/seed", headers=owner.headers,
                              json={"branch_id": str(branch)})
    assert again.json()["added"] == 0

    rows = (await client.get("/partners/sourcing", headers=owner.headers,
                             params={"branch_id": str(branch)})).json()
    modes = {i["online_product_id"]: i["mode"] for i in rows["items"]}
    assert modes == {str(carried): "own", str(missing): "partner", str(decided): "own"}
    assert str(not_listed) not in modes
    share = (await client.get("/partners/share", headers=owner.headers,
                              params={"branch_id": str(branch)})).json()
    assert share == {"listed": 3, "partner": 1, "own": 2, "partner_pct": 33.3}


async def test_seed_needs_the_branch_list_from_online(client, owner):
    branch = await _stock_branch("Unlisted branch")
    sid, _ = await _partner(client, owner, branch)
    r = await client.post(f"/partners/{sid}/seed", headers=owner.headers,
                          json={"branch_id": str(branch)})
    assert r.status_code == 422


async def test_a_partner_fills_only_the_branches_it_serves(client, owner):
    branch = await _stock_branch("Served")
    other = await _stock_branch("Not served")
    sid, _ = await _partner(client, owner, branch)
    pid, _ = await _product()
    r = await client.put("/partners/sourcing", headers=owner.headers, json={
        "branch_id": str(other), "online_product_id": str(pid), "mode": "partner",
        "partner_id": sid})
    assert r.status_code == 422
    wholesale, _ = await new_supplier(client, owner)
    r = await client.put("/partners/sourcing", headers=owner.headers, json={
        "branch_id": str(branch), "online_product_id": str(pid), "mode": "partner",
        "partner_id": wholesale["supplier_id"]})
    assert r.status_code == 422


# ---------------------------------------------------------------- partner app
async def test_partner_sets_price_and_stock_and_staff_see_the_change(client, owner):
    branch = await _stock_branch("App branch")
    sid, ph = await _partner(client, owner, branch, signed_in=True)
    pid, vids = await _product("Shampoo", variants=2)
    r = await client.put("/partners/sourcing", headers=owner.headers, json={
        "branch_id": str(branch), "online_product_id": str(pid), "mode": "partner",
        "partner_id": sid})
    assert r.status_code == 200, r.text

    items = (await client.get("/supplier/partner/items", headers=ph)).json()
    assert items["total"] == 2  # one row per shade, not the hidden default pack size
    assert {i["online_variant_id"] for i in items["items"]} == {str(v) for v in vids}
    assert all(i["price_to_us"] is None and i["stock_qty"] == 0 for i in items["items"])

    body = {"branch_id": str(branch), "online_variant_id": str(vids[0])}
    r = await client.put("/supplier/partner/items", headers=ph,
                         json={**body, "price_to_us": "120.00", "stock_qty": 5})
    assert r.status_code == 200, r.text
    r = await client.put("/supplier/partner/items", headers=ph, json={**body, "stock_qty": 3})
    assert r.json()["price_to_us"] == "120.00" and r.json()["stock_qty"] == 3  # price kept
    r = await client.put("/supplier/partner/items", headers=ph,
                         json={**body, "price_to_us": "130.00"})
    assert r.status_code == 200

    changes = (await client.get("/partners/price-changes", headers=owner.headers,
                                params={"branch_id": str(branch)})).json()
    assert [(c["old_price"], c["new_price"], c["rise"]) for c in changes] == [
        ("120.00", "130.00", True), (None, "120.00", False)]
    for c in changes:
        r = await client.post(f"/partners/price-changes/{c['change_id']}/seen",
                              headers=owner.headers)
        assert r.status_code == 204
    assert (await client.get("/partners/price-changes", headers=owner.headers,
                             params={"branch_id": str(branch)})).json() == []


async def test_partner_cannot_touch_own_products_or_other_branches(client, owner):
    branch = await _stock_branch("Guard branch")
    other = await _stock_branch("Guard other")
    sid, ph = await _partner(client, owner, branch, signed_in=True)
    own, own_v = await _product("Own")
    mine, mine_v = await _product("Mine")
    await client.put("/partners/sourcing", headers=owner.headers, json={
        "branch_id": str(branch), "online_product_id": str(mine), "mode": "partner",
        "partner_id": sid})
    r = await client.put("/supplier/partner/items", headers=ph, json={
        "branch_id": str(branch), "online_variant_id": str(own_v[0]), "stock_qty": 1})
    assert r.status_code == 403  # the branch carries it itself
    r = await client.put("/supplier/partner/items", headers=ph, json={
        "branch_id": str(other), "online_variant_id": str(mine_v[0]), "stock_qty": 1})
    assert r.status_code == 403  # not a branch it serves
    r = await client.put("/supplier/partner/items", headers=ph, json={
        "branch_id": str(branch), "online_variant_id": str(mine_v[0]), "price_to_us": "0"})
    assert r.status_code == 422


async def test_a_wholesale_supplier_has_no_partner_list(client, owner):
    _, _, tokens = await signed_in_supplier(client, owner)
    r = await client.get("/supplier/partner/items",
                         headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert r.status_code == 403


async def test_a_staff_token_is_not_a_partner_token(client, owner):
    r = await client.get("/supplier/partner/items", headers=owner.headers)
    assert r.status_code == 401


# ---------------------------------------------------------------- branch scope
async def test_another_branchs_manager_sees_nothing(client, owner):
    branch = await _stock_branch("Scoped branch")
    other = await _stock_branch("Manager's branch")
    manager = await sign_in(client, "branch_manager", [other])
    sid, ph = await _partner(client, owner, branch, signed_in=True)
    pid, vids = await _product()
    await client.put("/partners/sourcing", headers=owner.headers, json={
        "branch_id": str(branch), "online_product_id": str(pid), "mode": "partner",
        "partner_id": sid})
    await client.put("/supplier/partner/items", headers=ph, json={
        "branch_id": str(branch), "online_variant_id": str(vids[0]), "price_to_us": "50.00"})

    for path in ("/partners/sourcing", "/partners/share", "/partners/price-changes"):
        r = await client.get(path, headers=manager.headers, params={"branch_id": str(branch)})
        assert r.status_code == 403, path
    mine = (await client.get("/partners/price-changes", headers=manager.headers)).json()
    assert all(c["branch_id"] != str(branch) for c in mine)
    change = (await client.get("/partners/price-changes", headers=owner.headers,
                               params={"branch_id": str(branch)})).json()[0]
    r = await client.post(f"/partners/price-changes/{change['change_id']}/seen",
                          headers=manager.headers)
    assert r.status_code == 404
    r = await client.put("/partners/sourcing", headers=manager.headers, json={
        "branch_id": str(other), "online_product_id": str(pid), "mode": "own"})
    assert r.status_code == 403  # managers read; owner/purchasing decide

    own_manager = await sign_in(client, "branch_manager", [branch])
    r = await client.get("/partners/price-changes", headers=own_manager.headers,
                         params={"branch_id": str(branch)})
    assert r.status_code == 200 and len(r.json()) == 1


# ---------------------------------------------------------------- clean exit (G6)
async def test_unlinking_the_partner_puts_every_product_back_to_own(client, owner):
    branch = await _stock_branch("Exit branch")
    sid, ph = await _partner(client, owner, branch, signed_in=True)
    a, _ = await _product("A")
    b, _ = await _product("B")
    await _list(branch, a, b)
    seeded = await client.post(f"/partners/{sid}/seed", headers=owner.headers,
                               json={"branch_id": str(branch)})
    assert seeded.json()["added"] == 2
    assert (await client.get("/supplier/partner/items", headers=ph)).json()["total"] == 2

    r = await client.delete(f"/partners/{sid}/branches/{branch}", headers=owner.headers)
    assert r.status_code == 200, r.text
    assert r.json()["products_back_to_own"] == 2
    assert (await client.get("/supplier/partner/items", headers=ph)).json()["total"] == 0
    share = (await client.get("/partners/share", headers=owner.headers,
                              params={"branch_id": str(branch)})).json()
    assert share["partner"] == 0 and share["partner_pct"] == 0.0
    r = await client.put(f"/partners/{sid}/kind", headers=owner.headers,
                         json={"kind": "wholesale"})
    assert r.status_code == 200


async def test_kind_cannot_go_back_while_serving_a_branch(client, owner):
    branch = await _stock_branch("Kind branch")
    sid, _ = await _partner(client, owner, branch)
    r = await client.put(f"/partners/{sid}/kind", headers=owner.headers,
                         json={"kind": "wholesale"})
    assert r.status_code == 409


# ---------------------------------------------------------------- catalogue listings
async def test_catalogue_listings_follow_online(db):
    from sqlalchemy import select

    from app.models import Branch, OnlineBranchListing
    from app.services.online_catalog import apply_listings

    branch_id = await _stock_branch("Listing branch")
    online_id = (await db.get(Branch, branch_id)).online_branch_id
    pid, _ = await _product()
    assert await apply_listings(db, [{"product_id": str(pid)}]) is None  # older Online
    n = await apply_listings(db, [{"product_id": str(pid),
                                   "branch_ids": [str(online_id), str(uuid.uuid4())]}])
    assert n == 1  # the unknown Online branch is ignored
    rows = (await db.scalars(select(OnlineBranchListing.branch_id)
                             .where(OnlineBranchListing.online_product_id == pid))).all()
    assert rows == [branch_id]
    assert await apply_listings(db, [{"product_id": str(pid), "branch_ids": []}]) == 0
