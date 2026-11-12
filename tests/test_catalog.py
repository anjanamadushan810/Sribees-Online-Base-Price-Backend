"""Supplier catalogue: import of the real Nishi Palace list, edits, price history, the
WhatsApp message, and the wall between suppliers."""

import uuid
from pathlib import Path

import pytest

from app.pricelist import parse
from tests.test_supplier_auth import signed_in_supplier

FIXTURE = (Path(__file__).parent / "fixtures" / "nishi_palace_2026-11-12.txt").read_text(
    encoding="utf-8")

# The supplier's answers for the six lines the parser will not guess, by the line as
# written (line numbers move when a paste changes). Skip the matchbox line; fill the others.
ANSWERS = {
    "(660)(100)": {"mrp": "800", "units_per_box": 100},  # Amins henna
    "(775/1360)": {"units_per_box": 12},  # pampers 16
    "(1460/2550)": {"units_per_box": 6},  # pampers 30
    "(8490)(14400)(720)": {"skip": True},  # matches
    "(29/100)(12×26)": {"units_per_box": 312},  # super glue
    "(1120)(1)": {"mrp": "1500", "units_per_box": 1},  # laminating pouch
}


def answers_for(text: str) -> list[dict]:
    return [{"line_no": o.line_no, **ANSWERS[o.raw]}
            for o in parse(text).offers() if o.needs_review]


RESOLUTIONS = answers_for(FIXTURE)


def bearer(tokens):
    return {"Authorization": f"Bearer {tokens['access_token']}"}


@pytest.fixture
async def nishi(client, owner):
    supplier, su, tokens = await signed_in_supplier(client, owner)
    return supplier, bearer(tokens)


async def imported(client, h):
    r = await client.post("/supplier/catalog/import", headers=h,
                          json={"text": FIXTURE, "resolutions": RESOLUTIONS})
    assert r.status_code == 200, r.text
    return r.json()


def all_offers(catalog):
    return [(p, o) for s in catalog["sections"] for p in s["products"] for o in p["offers"]]


# ---------------------------------------------------------------- import
async def test_flagged_lines_match_fixture_line_numbers():
    lines = FIXTURE.split("\n")
    flagged = [o for o in parse(FIXTURE).offers() if o.needs_review]
    assert {o.raw for o in flagged} == set(ANSWERS)
    assert [o.line_no for o in flagged] == [162, 184, 187, 335, 371, 381]
    for o in flagged:
        assert lines[o.line_no - 1].strip() == o.raw


async def test_preview_on_empty_catalogue(client, nishi):
    _, h = nishi
    r = await client.post("/supplier/catalog/import/preview", headers=h, json={"text": FIXTURE})
    assert r.status_code == 200, r.text
    p = r.json()
    assert (p["sections"], p["products"], p["offers"]) == (11, 144, 178)
    assert (p["new_sections"], p["new_products"], p["new_offers"]) == (11, 144, 172)
    assert len(p["flagged"]) == 6 and p["changed_offers"] == []
    assert p["has_header"] and p["has_footer"]
    # preview wrote nothing
    cat = (await client.get("/supplier/catalog", headers=h)).json()
    assert cat["sections"] == []


async def test_import_refuses_unanswered_flags(client, nishi):
    _, h = nishi
    r = await client.post("/supplier/catalog/import", headers=h,
                          json={"text": FIXTURE, "resolutions": RESOLUTIONS[:5]})
    assert r.status_code == 422
    assert "line 381" in r.json()["detail"]
    assert (await client.get("/supplier/catalog", headers=h)).json()["sections"] == []


async def test_import_then_message_has_no_flags(client, nishi):
    _, h = nishi
    result = await imported(client, h)
    assert result["offers_new"] == 177 and result["offers_skipped"] == 1
    assert result["products_new"] == 144 and result["sections_new"] == 11

    cat = (await client.get("/supplier/catalog", headers=h)).json()
    assert cat["header"].startswith("#PRICE LIST (Wholesale )")
    assert "NISHI SUPER PALACE" in cat["footer"]
    soap = next(p for s in cat["sections"] for p in s["products"]
                if p["heading"] == "බේබි ශෙරමි සබන් 75g")
    assert [(o["price"], o["mrp"], o["units_per_box"], o["label"]) for o in soap["offers"]] == [
        ("125.00", "165.00", 150, ""),
        ("424.00", "530.00", 36, "4+1 free pack"),
        ("560.00", "690.00", 36, "5+1 free pack"),
    ]

    msg = (await client.get("/supplier/catalog/message", headers=h)).json()["text"]
    again = parse(msg)
    assert len(again.offers()) == 177
    assert not [o for o in again.offers() if o.needs_review]
    assert "(29/100)(312)" in msg  # the supplier's answer, not the original typo
    assert "(8490)" not in msg  # skipped line
    assert "(125/165)(150)" in msg


async def test_reimport_shows_and_records_changes(client, nishi):
    _, h = nishi
    await imported(client, h)
    changed = FIXTURE.replace("(125/165)(150)", "(128/170)(150)")
    p = (await client.post("/supplier/catalog/import/preview", headers=h,
                           json={"text": changed})).json()
    # the skipped matchbox line is still flagged (not "new") until answered again
    assert p["new_products"] == 0 and p["new_offers"] == 0 and len(p["flagged"]) == 6
    assert [(c["heading"], c["old_price"], c["new_price"]) for c in p["changed_offers"]] == [
        ("බේබි ශෙරමි සබන් 75g", "125.00", "128.00")]
    r = await client.post("/supplier/catalog/import", headers=h,
                          json={"text": changed, "resolutions": RESOLUTIONS})
    assert r.json()["offers_changed"] == 1 and r.json()["offers_new"] == 0

    cat = (await client.get("/supplier/catalog", headers=h)).json()
    offer = next(o for p, o in all_offers(cat)
                 if p["heading"] == "බේබි ශෙරමි සබන් 75g" and o["label"] == "")
    hist = (await client.get(f"/supplier/catalog/offers/{offer['offer_id']}/history",
                             headers=h)).json()
    assert [x["price"] for x in hist] == ["128.00", "125.00"]
    assert {x["source"] for x in hist} == {"import"}


async def test_items_missing_from_a_paste_are_left_alone(client, nishi):
    _, h = nishi
    await imported(client, h)
    shorter = FIXTURE.replace("🟡බේබි ශෙරමි සබන් 75g\n(125/165)(150)\n", "🟡බේබි ශෙරමි සබන් 75g\n")
    r = await client.post("/supplier/catalog/import", headers=h,
                          json={"text": shorter, "resolutions": answers_for(shorter)})
    assert r.status_code == 200, r.text
    assert r.json()["not_in_paste"] >= 1
    cat = (await client.get("/supplier/catalog", headers=h)).json()
    assert ("125.00", True) in [(o["price"], o["is_active"]) for _, o in all_offers(cat)]


# ---------------------------------------------------------------- edits in the app
async def test_app_edits_price_history_and_inactive(client, nishi):
    _, h = nishi
    r = await client.post("/supplier/catalog/sections", headers=h, json={"title": "සබන්"})
    assert r.status_code == 201
    section_id = r.json()["section_id"]
    assert (await client.post("/supplier/catalog/sections", headers=h,
                              json={"title": "සබන්"})).status_code == 409
    r = await client.post("/supplier/catalog/products", headers=h,
                          json={"section_id": section_id, "heading": "සන්ලයිට් සබන්"})
    product_id = r.json()["product_id"]
    r = await client.post(f"/supplier/catalog/products/{product_id}/offers", headers=h,
                          json={"price": "134", "mrp": "160", "units_per_box": 120})
    assert r.status_code == 201
    offer_id = r.json()["offer_id"]

    r = await client.patch(f"/supplier/catalog/offers/{offer_id}", headers=h,
                           json={"price": "136.50"})
    assert r.json()["price"] == "136.50"
    r = await client.patch(f"/supplier/catalog/offers/{offer_id}", headers=h,
                           json={"label": "Promo"})  # no money change -> no history row
    hist = (await client.get(f"/supplier/catalog/offers/{offer_id}/history", headers=h)).json()
    assert [x["price"] for x in hist] == ["136.50", "134.00"]
    assert {x["source"] for x in hist} == {"app"}

    msg = (await client.get("/supplier/catalog/message", headers=h)).json()["text"]
    assert "🟡සන්ලයිට් සබන්\n(136.50/160)(120) Promo" in msg
    r = await client.patch(f"/supplier/catalog/products/{product_id}", headers=h,
                           json={"is_active": False})
    assert r.json()["is_active"] is False
    msg = (await client.get("/supplier/catalog/message", headers=h)).json()["text"]
    assert "සන්ලයිට්" not in msg and "සබන්" not in msg  # empty section dropped too


@pytest.mark.parametrize("body", [{"price": "0"}, {"price": "-5"}, {"units_per_box": 0},
                                  {"price": None}, {"price": "1.234"}])
async def test_bad_prices_refused(client, nishi, body):
    _, h = nishi
    sid = (await client.post("/supplier/catalog/sections", headers=h,
                             json={"title": "X1"})).json()["section_id"]
    pid = (await client.post("/supplier/catalog/products", headers=h,
                             json={"section_id": sid, "heading": "Y"})).json()["product_id"]
    oid = (await client.post(f"/supplier/catalog/products/{pid}/offers", headers=h,
                             json={"price": "10"})).json()["offer_id"]
    r = await client.patch(f"/supplier/catalog/offers/{oid}", headers=h, json=body)
    assert r.status_code == 422


async def test_template(client, nishi):
    _, h = nishi
    r = await client.put("/supplier/catalog/template", headers=h,
                         json={"header": "මිල ලැයිස්තුව", "footer": "☎️ 0715810938"})
    assert r.status_code == 204
    sid = (await client.post("/supplier/catalog/sections", headers=h,
                             json={"title": "Z"})).json()["section_id"]
    pid = (await client.post("/supplier/catalog/products", headers=h,
                             json={"section_id": sid, "heading": "Item"})).json()["product_id"]
    await client.post(f"/supplier/catalog/products/{pid}/offers", headers=h,
                      json={"price": "10", "mrp": "12", "units_per_box": 5})
    msg = (await client.get("/supplier/catalog/message", headers=h)).json()["text"]
    assert msg.startswith("මිල ලැයිස්තුව\n") and msg.rstrip().endswith("☎️ 0715810938")


# ---------------------------------------------------------------- the wall between suppliers
async def test_supplier_cannot_touch_another_suppliers_catalogue(client, owner):
    _, _, ta = await signed_in_supplier(client, owner)
    _, _, tb = await signed_in_supplier(client, owner)
    a, b = bearer(ta), bearer(tb)
    sid = (await client.post("/supplier/catalog/sections", headers=a,
                             json={"title": "A only"})).json()["section_id"]
    pid = (await client.post("/supplier/catalog/products", headers=a,
                             json={"section_id": sid, "heading": "A item"})).json()["product_id"]
    oid = (await client.post(f"/supplier/catalog/products/{pid}/offers", headers=a,
                             json={"price": "10", "mrp": "12",
                                   "units_per_box": 5})).json()["offer_id"]

    attempts = [
        ("PATCH", f"/supplier/catalog/sections/{sid}", {"title": "hacked"}),
        ("POST", "/supplier/catalog/products", {"section_id": sid, "heading": "B into A"}),
        ("PATCH", f"/supplier/catalog/products/{pid}", {"heading": "hacked"}),
        ("POST", f"/supplier/catalog/products/{pid}/offers", {"price": "1"}),
        ("PATCH", f"/supplier/catalog/offers/{oid}", {"price": "1"}),
        ("GET", f"/supplier/catalog/offers/{oid}/history", None),
    ]
    for method, url, body in attempts:
        r = await client.request(method, url, headers=b, json=body)
        assert r.status_code == 404, (method, url, r.status_code, r.text)

    # B moving its own product into A's section is refused too
    bsid = (await client.post("/supplier/catalog/sections", headers=b,
                              json={"title": "B"})).json()["section_id"]
    bpid = (await client.post("/supplier/catalog/products", headers=b,
                              json={"section_id": bsid, "heading": "B item"})).json()["product_id"]
    r = await client.patch(f"/supplier/catalog/products/{bpid}", headers=b,
                           json={"section_id": sid})
    assert r.status_code == 404

    # nothing of A changed, and B sees none of A
    cat_a = (await client.get("/supplier/catalog", headers=a)).json()
    assert [s["title"] for s in cat_a["sections"]] == ["A only"]
    assert all_offers(cat_a)[0][1]["price"] == "10.00"
    cat_b = (await client.get("/supplier/catalog", headers=b)).json()
    assert "A only" not in [s["title"] for s in cat_b["sections"]]


async def test_database_refuses_cross_supplier_rows(client, owner):
    """Even if the application had a bug, the composite foreign keys hold the wall."""
    from sqlalchemy import text

    from app.core.db import get_sessionmaker

    a, _, ta = await signed_in_supplier(client, owner)
    b, _, _ = await signed_in_supplier(client, owner)
    sid = (await client.post("/supplier/catalog/sections", headers=bearer(ta),
                             json={"title": "A sec"})).json()["section_id"]
    with pytest.raises(Exception, match="foreign key"):
        async with get_sessionmaker()() as s, s.begin():
            await s.execute(text(
                "INSERT INTO supplier_products (supplier_id, section_id, heading) "
                "VALUES (:b, :sid, 'sneaky')"), {"b": b["supplier_id"], "sid": sid})


async def test_staff_onboard_supplier_and_read(client, purchasing, branch_manager, owner):
    supplier, _, tokens = await signed_in_supplier(client, owner)
    sid = supplier["supplier_id"]
    r = await client.post(f"/suppliers/{sid}/catalog/import/preview",
                          headers=branch_manager.headers, json={"text": FIXTURE})
    assert r.status_code == 403  # read-only role
    r = await client.post(f"/suppliers/{sid}/catalog/import", headers=purchasing.headers,
                          json={"text": FIXTURE, "resolutions": RESOLUTIONS})
    assert r.status_code == 200, r.text
    # the supplier sees what staff imported for them
    cat = (await client.get("/supplier/catalog", headers=bearer(tokens))).json()
    assert len(all_offers(cat)) == 177
    offer_id = all_offers(cat)[0][1]["offer_id"]
    hist = (await client.get(f"/suppliers/{sid}/catalog/offers/{offer_id}/history",
                             headers=branch_manager.headers)).json()
    assert hist[0]["source"] == "import"
    # an offer id under the wrong supplier id is not found
    other, _, _ = await signed_in_supplier(client, owner)
    r = await client.get(f"/suppliers/{other['supplier_id']}/catalog/offers/{offer_id}/history",
                         headers=purchasing.headers)
    assert r.status_code == 404
    r = await client.get(f"/suppliers/{uuid.uuid4()}/catalog", headers=purchasing.headers)
    assert r.status_code == 404


async def test_price_history_is_append_only(client, nishi):
    from sqlalchemy import text

    from app.core.db import get_sessionmaker

    _, h = nishi
    await imported(client, h)
    with pytest.raises(Exception, match="cannot be changed"):
        async with get_sessionmaker()() as s, s.begin():
            await s.execute(text("UPDATE supplier_offer_prices SET price = 1"))


# ---------------------------------------------------------------- review 2026-11-12
@pytest.mark.parametrize(("url_part", "body"), [
    ("offers", {"label": None}), ("offers", {"is_active": None}),
    ("offers", {"price": "200", "mrp": "150"}), ("offers", {"units_per_box": 1_000_001}),
    ("products", {"heading": None}), ("products", {"is_active": None}),
])
async def test_bad_patches_are_422_not_500(client, nishi, url_part, body):
    _, h = nishi
    sid = (await client.post("/supplier/catalog/sections", headers=h,
                             json={"title": "Nulls"})).json()["section_id"]
    pid = (await client.post("/supplier/catalog/products", headers=h,
                             json={"section_id": sid, "heading": "N"})).json()["product_id"]
    oid = (await client.post(f"/supplier/catalog/products/{pid}/offers", headers=h,
                             json={"price": "10", "mrp": "12"})).json()["offer_id"]
    target = oid if url_part == "offers" else pid
    r = await client.patch(f"/supplier/catalog/{url_part}/{target}", headers=h, json=body)
    assert r.status_code == 422, r.text


async def test_preview_warns_when_a_heading_gains_or_loses_lines(client, nishi):
    _, h = nishi
    await imported(client, h)
    # Signal 40g has two unlabelled lines; dropping one could shift the other's price.
    shorter = FIXTURE.replace("(107/130)(72)\n", "")
    p = (await client.post("/supplier/catalog/import/preview", headers=h,
                           json={"text": shorter})).json()
    assert [(x["heading"], x["label"], x["before"], x["after"]) for x in p["reshaped"]] == [
        ("සිග්නල් 40g", "", 2, 1)]
    # a heading with distinct notes losing one line is safe: no warning
    safe = FIXTURE.replace("(424/530)(36) 4+1 free pack\n", "")
    p = (await client.post("/supplier/catalog/import/preview", headers=h,
                           json={"text": safe})).json()
    assert p["reshaped"] == []
