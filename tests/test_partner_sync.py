"""Fulfilment partner figures to Online (plan P3): branch flag, stock counts, customer
prices, loss alerts, clean exit."""

import json
import uuid
from contextlib import contextmanager
from decimal import Decimal

import httpx
import pytest

from tests.conftest import _stock_branch
from tests.test_partners import _list, _partner, _product, _set_item
from tests.test_supplier_auth import settings_override

ON = {"online_api_url": "http://online.test", "online_service_token": "t" * 32,
      "online_partner_sync": True}


@contextmanager
def online_on():
    with settings_override(**ON):
        yield


class FakeOnline:
    """Records every call; answers like Online's partner routes."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.prices: dict[str, str] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        path = request.url.path
        self.calls.append((path, body))
        if path.endswith("/partner-stock"):
            return httpx.Response(200, json={"results": [
                {"product_id": i["product_id"], "result": "set"} for i in body["items"]]})
        if path.endswith("/stock-info"):
            return httpx.Response(200, json={"items": [
                {"product_id": i["product_id"], "listed_price": self.prices.get(i["product_id"]),
                 "selling_price": self.prices.get(i["product_id"])} for i in body["items"]]})
        return httpx.Response(200, json={"ok": True})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))

    def for_branch(self, online_branch) -> list[tuple[str, dict]]:
        ob = str(online_branch)
        return [(p, b) for p, b in self.calls if b.get("branch_id") == ob or ob in p]


async def _online_id(branch_id):
    from app.core.db import get_sessionmaker
    from app.models import Branch

    async with get_sessionmaker()() as s:
        return (await s.get(Branch, branch_id)).online_branch_id


async def _round(fake):
    from app.core.db import get_sessionmaker
    from app.services import partner_sync

    async with fake.client() as c:
        return await partner_sync.run_once(get_sessionmaker(), c)


async def test_sync_sends_the_flag_then_counts_and_0_for_unpriced_items(client, owner):
    branch = await _stock_branch("Sync branch")
    online = await _online_id(branch)
    sid, ph = await _partner(client, owner, branch, signed_in=True, shortfall=True)
    entered, ev = await _product("Entered")
    untouched, _ = await _product("Untouched")
    backup, bv = await _product("Store backup")
    await _list(branch, entered, "external_daily")
    await _list(branch, untouched, "external_slow")
    await _list(branch, backup, "store")
    await _set_item(client, ph, branch, ev[0], stock=7)
    await _set_item(client, ph, branch, bv[0], stock=3)

    fake = FakeOnline()
    with online_on():
        await _round(fake)
        mine = fake.for_branch(online)
        assert mine[0] == (f"/internal/supply/branches/{online}/partner", {"active": True})
        stock = [b for p, b in mine if p.endswith("/partner-stock")]
        sent = {i["product_id"]: i["stock"] for b in stock for i in b["items"]}
        # owner 2026-11-28: a price is mandatory, so an item the partner never priced is 0;
        # Our Store stock is ours, never the partner's count
        assert sent == {str(entered): 7, str(untouched): 0}

        fake.calls.clear()
        await _round(fake)
        assert fake.for_branch(online) == []  # nothing changed, nothing sent
        await _set_item(client, ph, branch, ev[0], stock=4)
        await _round(fake)
        assert [b["items"][0]["stock"] for p, b in fake.for_branch(online)
                if p.endswith("/partner-stock")] == [4]


async def test_unlinking_zeroes_the_partner_counts_then_switches_the_flag_off(client, owner):
    branch = await _stock_branch("Sync exit branch")
    online = await _online_id(branch)
    sid, ph = await _partner(client, owner, branch, signed_in=True)
    pid, v = await _product("Leaving")
    await _list(branch, pid, "external_daily")
    await _set_item(client, ph, branch, v[0], stock=6)
    fake = FakeOnline()
    with online_on():
        await _round(fake)
        fake.calls.clear()
        r = await client.delete(f"/partners/{sid}/branches/{branch}", headers=owner.headers)
        assert r.status_code == 200
        await _round(fake)
        assert fake.for_branch(online) == [
            ("/internal/supply/partner-stock",
             {"branch_id": str(online),
              "items": [{"product_id": str(pid), "variant_id": None, "stock": 0}]}),
            (f"/internal/supply/branches/{online}/partner", {"active": False}),
        ]


async def test_nothing_is_sent_while_switched_off(client, owner):
    from app.services import partner_sync

    with settings_override(online_partner_sync=False):
        assert partner_sync.enabled() is False


# ---------------------------------------------------------------- customer price (F15-F17)
async def _customer_price(supplier_id, ph_user, branch, variant, price, fake):
    from app.core.db import get_sessionmaker
    from app.services import partners

    async with get_sessionmaker()() as s, s.begin(), fake.client() as c:
        return await partners.set_customer_price(
            s, c, uuid.UUID(supplier_id), ph_user, branch_id=branch,
            online_variant_id=variant, price=Decimal(price))


async def _supplier_user(supplier_id):
    from sqlalchemy import select

    from app.core.db import get_sessionmaker
    from app.models import SupplierUser

    async with get_sessionmaker()() as s:
        return await s.scalar(select(SupplierUser.supplier_user_id)
                              .where(SupplierUser.supplier_id == uuid.UUID(supplier_id)))


async def test_partner_sets_the_customer_price_of_other_products_only(client, owner):
    from app.services.errors import Conflict, Forbidden

    branch = await _stock_branch("Price branch")
    online = await _online_id(branch)
    sid, ph = await _partner(client, owner, branch, signed_in=True)
    user = await _supplier_user(sid)
    other, ov = await _product("Other")
    store, sv = await _product("Store")
    await _list(branch, other, "external_daily")
    await _list(branch, store, "store")
    await _set_item(client, ph, branch, ov[0], price="120.00")
    fake = FakeOnline()
    fake.prices[str(other)] = "150.00"

    with online_on():
        out = await _customer_price(sid, user, branch, ov[0], "110.00", fake)
        assert out["customer_price"] == "110.00" and out["below_price_to_us"] is True
        sent = [b for p, b in fake.calls if p.endswith("/partner-price")]
        assert sent == [{"branch_id": str(online), "product_id": str(other), "variant_id": None,
                         "price": "110.00"}]
        with pytest.raises(Forbidden):  # Our Store: the branch manager's price
            await _customer_price(sid, user, branch, sv[0], "99.00", fake)
    with pytest.raises(Conflict):  # switched off
        await _customer_price(sid, user, branch, ov[0], "130.00", fake)

    changes = (await client.get("/partners/price-changes", headers=owner.headers,
                                params={"branch_id": str(branch)})).json()
    customer = [c for c in changes if c["kind"] == "customer"]
    assert [(c["old_price"], c["new_price"]) for c in customer] == [("150.00", "110.00")]


async def test_staff_alerts_flag_a_loss(client, owner, monkeypatch):
    from app.services import partner_sync

    branch = await _stock_branch("Loss branch")
    sid, ph = await _partner(client, owner, branch, signed_in=True)
    pid, v = await _product("Thin margin")
    await _list(branch, pid, "external_daily")
    await _set_item(client, ph, branch, v[0], price="200.00")

    async def prices(_client, _branch, refs):
        return {vid: "180.00" for _p, vid in refs}
    monkeypatch.setattr(partner_sync, "current_prices", prices)
    with online_on():
        rows = (await client.get("/partners/price-changes", headers=owner.headers,
                                 params={"branch_id": str(branch)})).json()
        assert [(r["customer_price"], r["loss"]) for r in rows] == [("180.00", True)]
        items = (await client.get("/supplier/partner/items", headers=ph)).json()["items"]
        assert [(i["customer_price"], i["loss"]) for i in items] == [("180.00", True)]


async def test_a_loose_products_packs_go_to_online_as_grams(client, owner):
    from app.core.db import get_sessionmaker
    from app.models import OnlineProduct, OnlineVariant

    branch = await _stock_branch("Loose sync branch")
    online = await _online_id(branch)
    sid, ph = await _partner(client, owner, branch, signed_in=True)
    rice, (half, kilo) = await _product("Rice", variants=2)
    async with get_sessionmaker()() as s, s.begin():
        (await s.get(OnlineProduct, rice)).stock_mode = "loose"
        (await s.get(OnlineVariant, half)).weight_grams = 500
        (await s.get(OnlineVariant, kilo)).weight_grams = 1000
    await _list(branch, rice, "external_daily")
    await _set_item(client, ph, branch, half, price="90.00", stock=5)
    await _set_item(client, ph, branch, kilo, price="175.00", stock=3)
    fake = FakeOnline()
    with online_on():
        await _round(fake)
    items = [i for p, b in fake.for_branch(online) if p.endswith("/partner-stock")
             for i in b["items"]]
    assert items == [{"product_id": str(rice), "variant_id": None, "stock": 0, "grams": 5500}]


async def test_a_count_online_does_not_take_is_sent_again(client, owner):
    branch = await _stock_branch("Refused sync branch")
    online = await _online_id(branch)
    sid, ph = await _partner(client, owner, branch, signed_in=True)
    pid, (vid,) = await _product("Disagreement")
    await _list(branch, pid, "external_daily")
    await _set_item(client, ph, branch, vid, price="20.00", stock=4)

    class Refusing(FakeOnline):
        def __call__(self, request):
            if request.url.path.endswith("/partner-stock"):
                body = json.loads(request.content)
                self.calls.append((request.url.path, body))
                return httpx.Response(200, json={"results": [
                    {"product_id": i["product_id"], "result": "needs_grams"}
                    for i in body["items"]]})
            return super().__call__(request)
    fake = Refusing()
    with online_on():
        await _round(fake)
        await _round(fake)
    sends = [b for p, b in fake.for_branch(online) if p.endswith("/partner-stock")]
    assert len(sends) == 2  # not marked as taken, so tried again
