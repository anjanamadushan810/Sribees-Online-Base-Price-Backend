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
