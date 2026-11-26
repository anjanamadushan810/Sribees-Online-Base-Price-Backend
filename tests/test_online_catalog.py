"""The Online product mirror: catalogue sync, default pack sizes, photos, and the size and
sound rules of the text helpers. (Matching supplier names was removed 2026-11-26: orders
name our own products.)"""

import uuid
from decimal import Decimal

import pytest

from app.matching.text import parse_size, romanize, sound_key


@pytest.mark.parametrize(("si", "en"), [
    ("බේබි ශෙරමි", "Baby Cheramy"), ("සන්ලයිට්", "Sunlight"), ("ලයිෆ්බෝයි", "Lifebuoy"),
    ("සිග්නල්", "Signal"), ("ක්ලෝගාර්ඩ්", "Clogard"), ("ඩෙටෝල්", "Dettol"),
    ("නෙස්ටමෝල්ට්", "Nestomalt"), ("මැලිබන්", "Maliban"), ("කොකාකෝලා", "Coca Cola"),
    ("පැනඩෝල්", "Panadol"), ("වෙල්වට්", "Velvet"), ("ලක්ස්", "Lux"), ("රත්තී", "Raththi"),
    ("හයිලන්ඩ්", "Highland"), ("සමපෝෂ", "Samaposha"), ("ප්‍රීමා", "Prima"), ("මයිලෝ", "Milo"),
])
def test_sinhala_and_english_brands_sound_alike(si, en):
    assert sound_key(si) == sound_key(en)


def test_romanize_keeps_joiners_meaning():
    assert romanize("ප්‍රීමා") == "priimaa"
    assert romanize("බ්‍රෂ්") == "brash"


@pytest.mark.parametrize(("text", "size"), [
    ("75g", (Decimal(75), "mass")), ("1.5L", (Decimal(1500), "volume")),
    ("250ml", (Decimal(250), "volume")), ("1kg", (Decimal(1000), "mass")),
    ("Fems 10s", (Decimal(10), "count")), ("16pcs", (Decimal(16), "count")),
    ("ඩයමන්ඩ් පිටි Sch", None), ("no size", None),
])
def test_sizes(text, size):
    assert parse_size(text) == size


def online_payload(*products):
    return {"products": [
        {"product_id": str(pid), "sku": sku, "name": name, "name_si": name_si,
         "display_size": size, "display_size_unit": unit, "is_active": True, "variants": []}
        for pid, sku, name, name_si, size, unit in products]}


CHERAMY_75, CHERAMY_100, CLOGARD_120, SIGNAL_120, COKE_15 = (uuid.uuid4() for _ in range(5))
PAYLOAD = online_payload(
    (CHERAMY_75, "4792037100145", "Baby Cheramy Soap", None, "75", "g"),
    (CHERAMY_100, "4792037100152", "Baby Cheramy Soap", None, "100", "g"),
    (CLOGARD_120, "4792037400122", "Clogard Toothpaste 120g", None, None, None),
    (SIGNAL_120, "8901030700012", "Signal Toothpaste 120g", "සිග්නල් ටූත් පේස්ට් 120g",
     None, None),
    (COKE_15, "5449000000439", "Coca Cola 1.5L", None, None, None),
)


async def test_sync_marks_missing_products_inactive(client, purchasing):
    from app.core.db import get_sessionmaker
    from app.models import OnlineProduct
    from app.services import online_catalog

    async with get_sessionmaker()() as s, s.begin():
        await online_catalog.apply(s, PAYLOAD, user_id=None)
    short = {"products": [p for p in PAYLOAD["products"]
                          if p["product_id"] != str(CHERAMY_75)]}
    async with get_sessionmaker()() as s, s.begin():
        await online_catalog.apply(s, short, user_id=None)
    async with get_sessionmaker()() as s:
        assert (await s.get(OnlineProduct, CHERAMY_75)).is_active is False
    r = await client.get("/online-products?q=Cheramy", headers=purchasing.headers)
    assert [p["online_product_id"] for p in r.json()] == [str(CHERAMY_100)]
    async with get_sessionmaker()() as s, s.begin():
        await online_catalog.apply(s, PAYLOAD, user_id=None)  # back for other tests


async def test_sync_gives_plain_products_a_default_pack_size(client, owner):
    """Owner 2026-11-26: most Online products have no pack sizes; Supply still stocks them."""
    from app.core.db import get_sessionmaker
    from app.models import OnlineVariant
    from app.services import online_catalog

    plain, real = uuid.uuid4(), uuid.uuid4()
    payload = online_payload((plain, "P-1", "Plain Soap 70g", None, None, None))
    async with get_sessionmaker()() as s, s.begin():
        await online_catalog.apply(s, {"products": PAYLOAD["products"] + payload["products"]},
                                   user_id=None)
    async with get_sessionmaker()() as s:
        v = await s.get(OnlineVariant, plain)
        assert (v.online_product_id, v.is_active, v.weight_grams, v.name) == (plain, True, 70, "")
    payload["products"][0]["variants"] = [{"variant_id": str(real), "sku": "P-1a",
                                           "name": "Rose", "weight_grams": 70,
                                           "is_active": True}]
    async with get_sessionmaker()() as s, s.begin():
        await online_catalog.apply(s, {"products": PAYLOAD["products"] + payload["products"]},
                                   user_id=None)
    async with get_sessionmaker()() as s:
        assert (await s.get(OnlineVariant, plain)).is_active is False
        assert (await s.get(OnlineVariant, real)).is_active is True
    async with get_sessionmaker()() as s, s.begin():
        await online_catalog.apply(s, PAYLOAD, user_id=None)


async def test_sync_needs_the_online_link(client, owner):
    r = await client.post("/online-catalog/sync", headers=owner.headers)
    assert r.status_code == 422
    assert "not set up" in r.json()["detail"]


async def test_photo_comes_with_the_product(client, purchasing, owner):
    """Owner 2026-11-26: the matching screen shows the Online product's photo."""
    from app.core.db import get_sessionmaker
    from app.models import OnlineProduct
    from app.services import online_catalog

    pid = uuid.uuid4()
    payload = online_payload((pid, "IMG-1", "Photo Soap 70g", None, None, None))
    payload["products"][0]["image_url"] = "https://api.example.lk/uploads/products/soap.webp"
    bad = uuid.uuid4()
    payload["products"] += online_payload(
        (bad, "IMG-2", "Bad Photo 70g", None, None, None))["products"]
    payload["products"][1]["image_url"] = "javascript:alert(1)"
    async with get_sessionmaker()() as s, s.begin():
        await online_catalog.apply(s, {"products": PAYLOAD["products"] + payload["products"]},
                                   user_id=None)
    async with get_sessionmaker()() as s:
        assert (await s.get(OnlineProduct, pid)).image_url.endswith("/soap.webp")
        assert (await s.get(OnlineProduct, bad)).image_url is None
    r = await client.get("/online-products?q=Photo Soap", headers=purchasing.headers)
    assert r.json()[0]["image_url"].endswith("/soap.webp")
    async with get_sessionmaker()() as s, s.begin():
        await online_catalog.apply(s, PAYLOAD, user_id=None)
