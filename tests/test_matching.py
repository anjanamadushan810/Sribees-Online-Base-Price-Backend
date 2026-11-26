"""Matching supplier offers (Sinhala) to Online products (English / name_si)."""

import uuid
from decimal import Decimal

import pytest

from app.matching import scorer
from app.matching.text import parse_size, romanize, sound_key
from tests.test_catalog import FIXTURE, RESOLUTIONS, bearer
from tests.test_supplier_auth import signed_in_supplier


# ---------------------------------------------------------------- pure helpers
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


def cand(name, name_si=None, size=None):
    return scorer.Candidate(online_product_id=uuid.uuid4(), name=name, name_si=name_si,
                            size=parse_size(size or name))


def test_size_must_agree_and_brand_must_agree():
    right = cand("Baby Cheramy Soap 75g")
    wrong_size = cand("Baby Cheramy Soap 100g")
    other_brand = cand("Pears Baby Soap 75g")
    ranked = scorer.best("බේබි ශෙරමි සබන් 75g", "", [wrong_size, other_brand, right])
    assert [c for c, _ in ranked] == [right]


def test_size_from_offer_label():
    one_l, half = cand("Coca Cola 1L"), cand("Coca Cola 1.5L")
    ranked = scorer.best("කොකාකෝලා,ස්ප්‍රයිට්", "1.5L", [one_l, half])
    assert [c for c, _ in ranked] == [half]


def test_sinhala_name_on_online_side():
    c = cand("Kaleen Soap 70g", name_si="කැලීන් සබන් 70g")
    assert scorer.best("කැලීන් සබන් 70g", "", [c])


# ---------------------------------------------------------------- flow
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


@pytest.fixture
async def matched_setup(client, owner):
    from app.core.db import get_sessionmaker
    from app.services import online_catalog

    supplier, _, tokens = await signed_in_supplier(client, owner)
    r = await client.post("/supplier/catalog/import", headers=bearer(tokens),
                          json={"text": FIXTURE, "resolutions": RESOLUTIONS})
    assert r.status_code == 200, r.text
    async with get_sessionmaker()() as s, s.begin():
        await online_catalog.apply(s, PAYLOAD, user_id=None)
    return supplier["supplier_id"]


def find(queue, heading, label=""):
    return next(i for i in queue if i["heading"] == heading and i["label"] == label)


async def test_suggest_confirm_reject_unmatch(client, purchasing, branch_manager,
                                              matched_setup):
    sid = matched_setup
    r = await client.post(f"/suppliers/{sid}/matching/suggest", headers=purchasing.headers)
    assert r.status_code == 200, r.text
    q = (await client.get(f"/suppliers/{sid}/matching", headers=branch_manager.headers)).json()

    soap = find(q, "බේබි ශෙරමි සබන් 75g")
    assert [s["online_product_id"] for s in soap["suggestions"]] == [str(CHERAMY_75)]
    clogard = find(q, "ක්ලෝගාර්ඩ් ටූත්  120g")
    assert [s["online_product_id"] for s in clogard["suggestions"]] == [str(CLOGARD_120)]
    signal = find(q, "සිග්නල් 120g")
    assert [s["online_product_id"] for s in signal["suggestions"]] == [str(SIGNAL_120)]
    coke = find(q, "කොකාකෝලා,ස්ප්‍රයිට්", "1.5L")
    assert [s["online_product_id"] for s in coke["suggestions"]] == [str(COKE_15)]
    assert find(q, "කොකාකෝලා,ස්ප්‍රයිට්", "1L")["suggestions"] == []  # no 1L on Online

    # branch manager may look, not decide
    r = await client.post(f"/matching/offers/{soap['offer_id']}/confirm",
                          headers=branch_manager.headers,
                          json={"online_product_id": str(CHERAMY_75)})
    assert r.status_code == 403

    r = await client.post(f"/matching/offers/{soap['offer_id']}/confirm",
                          headers=purchasing.headers,
                          json={"online_product_id": str(CHERAMY_75)})
    assert r.status_code == 204
    r = await client.post(f"/matching/offers/{clogard['offer_id']}/reject",
                          headers=purchasing.headers,
                          json={"online_product_id": str(CLOGARD_120)})
    assert r.status_code == 204

    # a new suggestion run keeps the confirmation and never brings back the rejection
    await client.post(f"/suppliers/{sid}/matching/suggest", headers=purchasing.headers)
    matched = (await client.get(f"/suppliers/{sid}/matching?state=matched",
                                headers=purchasing.headers)).json()
    assert [(m["heading"], m["confirmed"]["online_product_id"]) for m in matched] == [
        ("බේබි ශෙරමි සබන් 75g", str(CHERAMY_75))]
    todo = (await client.get(f"/suppliers/{sid}/matching", headers=purchasing.headers)).json()
    assert find(todo, "ක්ලෝගාර්ඩ් ටූත්  120g")["suggestions"] == []
    assert all(i["heading"] != "බේබි ශෙරමි සබන් 75g" or i["label"] for i in todo)

    # changing the match keeps one confirmed row; unmatch puts it back in the queue
    r = await client.post(f"/matching/offers/{soap['offer_id']}/confirm",
                          headers=purchasing.headers,
                          json={"online_product_id": str(CHERAMY_100)})
    assert r.status_code == 204
    matched = (await client.get(f"/suppliers/{sid}/matching?state=matched",
                                headers=purchasing.headers)).json()
    assert matched[0]["confirmed"]["online_product_id"] == str(CHERAMY_100)
    r = await client.delete(f"/matching/offers/{soap['offer_id']}", headers=purchasing.headers)
    assert r.status_code == 204
    matched = (await client.get(f"/suppliers/{sid}/matching?state=matched",
                                headers=purchasing.headers)).json()
    assert matched == []


async def test_sync_marks_missing_products_inactive(client, purchasing, matched_setup):
    from app.core.db import get_sessionmaker
    from app.models import OnlineProduct
    from app.services import online_catalog

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


async def test_confirm_unknown_product_404(client, purchasing, matched_setup):
    q = (await client.get(f"/suppliers/{matched_setup}/matching",
                          headers=purchasing.headers)).json()
    r = await client.post(f"/matching/offers/{q[0]['offer_id']}/confirm",
                          headers=purchasing.headers,
                          json={"online_product_id": str(uuid.uuid4())})
    assert r.status_code == 404


# ---------------------------------------------------------------- real-catalogue cases
# Found by running Nishi's list against SRIBEESonline's 889 products (2026-11-13).
@pytest.mark.parametrize(("heading", "label", "good", "bad"), [
    ("පෙයාස් බේබි සබන්", "70g", "Pears Pure & Gentle Baby Soap 70g", "Wijaya Pappadam 70g"),
    ("වෙල්වට් සබන්", "70g", "Velvet Milk And Almond 70g", "Philips 14w Bulbs"),
    ("යහපෝෂ", "", None, "Pepsi 1.5l"),
    ("ක්ලෝගාර්ඩ්", "70g", None, "Clogard Chooty Junior Toothbrush"),
    ("ඇට්ලස්/මැන්ගෝ පෑන්", "", None, "Atlas Colour Pencils (12pcs pack)"),
    ("කොකාකෝලා,ස්ප්‍රයිට්", "250ml", "Coca Cola Orginal Taste 250ml", "Pepsi 250ml"),
    ("මන්චි ක්‍රීම් ක්‍රැකර්", "", "Munchee Super Cream Cracker 125g", None),
    ("සිග්නල් ටූත් බ්‍රෂ්", "", "Signal Triple Clean Adult Toothbrush",
     "Signal Triple Protection 123 Toothpaste 120g"),
])
def test_real_catalogue_cases(heading, label, good, bad):
    cands = [cand(n) for n in (good, bad) if n]
    picked = [c.name for c, _ in scorer.best(heading, label, cands)]
    if good:
        assert good in picked
    if bad:
        assert bad not in picked


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


async def test_a_matched_item_is_matched_again_by_itself(client, owner, purchasing):
    """Owner 2026-11-26: once matched, the same item from that supplier (the next bill's
    line with the same name) gets the same product by itself."""
    from app.core.db import get_sessionmaker
    from app.models import SupplierOffer, SupplierProduct, SupplierSection
    from app.services import online_catalog
    from tests.stock_helpers import fresh_supplier

    async with get_sessionmaker()() as s, s.begin():
        await online_catalog.apply(s, PAYLOAD, user_id=None)
    sup = await fresh_supplier(client, owner)
    sid = uuid.UUID(sup["supplier_id"])
    async with get_sessionmaker()() as s, s.begin():
        sec = SupplierSection(supplier_id=sid, title="Bills to match")
        s.add(sec)
        await s.flush()
        prod = SupplierProduct(supplier_id=sid, section_id=sec.section_id, heading="Signal 120g")
        other = SupplierProduct(supplier_id=sid, section_id=sec.section_id, heading="Signal 40g")
        s.add_all([prod, other])
        await s.flush()
        first = SupplierOffer(supplier_id=sid, product_id=prod.product_id, label="A-1 · 48 pcs",
                              price=Decimal("218"), units_per_box=1)
        second = SupplierOffer(supplier_id=sid, product_id=prod.product_id, label="B-7 · 24 pcs",
                               price=Decimal("220"), units_per_box=1)
        third = SupplierOffer(supplier_id=sid, product_id=other.product_id, label="B-7 · 72 pcs",
                              price=Decimal("111"), units_per_box=1)
        s.add_all([first, second, third])
        await s.flush()
        ids = [str(first.offer_id), str(second.offer_id), str(third.offer_id)]
    r = await client.post(f"/matching/offers/{ids[0]}/confirm", headers=purchasing.headers,
                          json={"online_product_id": str(SIGNAL_120),
                                "online_variant_id": str(SIGNAL_120)})
    assert r.status_code == 204, r.text
    matched = (await client.get(f"/suppliers/{sid}/matching?state=matched",
                                headers=purchasing.headers)).json()
    got = {m["offer_id"]: m["confirmed"]["online_product_id"] for m in matched}
    assert got == {ids[0]: str(SIGNAL_120), ids[1]: str(SIGNAL_120)}  # Signal 40g is not taught
