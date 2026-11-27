"""Product barcodes (plan 2026-11-27, P1): linked once on first scan, then found by scanning."""

import uuid

from app.services.barcodes import gtin_check_ok


async def _item(name="Soap BC"):
    from app.core.db import get_sessionmaker
    from app.models import OnlineProduct, OnlineVariant

    async with get_sessionmaker()() as s, s.begin():
        p = OnlineProduct(online_product_id=uuid.uuid4(), name=f"{name} {uuid.uuid4().hex[:4]}")
        s.add(p)
        await s.flush()
        v = OnlineVariant(online_variant_id=uuid.uuid4(), online_product_id=p.online_product_id,
                          name="75 g", weight_grams=80)
        s.add(v)
        return str(v.online_variant_id)


def _ean13() -> str:
    body = str(uuid.uuid4().int)[:12]
    for d in "0123456789":
        if gtin_check_ok(body + d):
            return body + d
    raise AssertionError


def test_check_digit():
    assert gtin_check_ok("4006381333931")       # EAN-13
    assert not gtin_check_ok("4006381333932")
    assert gtin_check_ok("96385074")            # EAN-8
    assert gtin_check_ok("036000291452")        # UPC-A


async def test_link_once_then_found(client, branch_manager):
    code, vid = _ean13(), await _item()
    r = await client.get(f"/barcodes/{code}", headers=branch_manager.headers)
    assert r.status_code == 200 and r.json()["item"] is None, r.text

    r = await client.post("/barcodes", headers=branch_manager.headers,
                          json={"barcode": code, "online_variant_id": vid})
    assert r.status_code == 200 and r.json()["changed"] is True, r.text

    r = await client.get(f"/barcodes/{code}", headers=branch_manager.headers)
    assert r.json()["item"]["online_variant_id"] == vid and r.json()["item"]["variant"] == "75 g"
    # linking it again to the same item changes nothing
    r = await client.post("/barcodes", headers=branch_manager.headers,
                          json={"barcode": code, "online_variant_id": vid})
    assert r.json()["changed"] is False


async def test_wrong_check_digit_is_refused(client, branch_manager):
    vid = await _item()
    bad = "4006381333932"
    r = await client.get(f"/barcodes/{bad}", headers=branch_manager.headers)
    assert r.status_code == 422 and "check digit" in r.text
    r = await client.post("/barcodes", headers=branch_manager.headers,
                          json={"barcode": bad, "online_variant_id": vid})
    assert r.status_code == 422
    r = await client.get("/barcodes/ABC123456789", headers=branch_manager.headers)
    assert r.status_code == 422


async def test_moving_a_barcode_needs_purchasing(client, branch_manager, purchasing):
    code, a, b = _ean13(), await _item("Soap A"), await _item("Soap B")
    await client.post("/barcodes", headers=branch_manager.headers,
                      json={"barcode": code, "online_variant_id": a})
    r = await client.post("/barcodes", headers=branch_manager.headers,
                          json={"barcode": code, "online_variant_id": b})
    assert r.status_code == 409 and "Soap A" in r.text
    r = await client.post("/barcodes", headers=purchasing.headers,
                          json={"barcode": code, "online_variant_id": b})
    assert r.status_code == 200 and r.json()["changed"] is True
    r = await client.get(f"/barcodes/{code}", headers=branch_manager.headers)
    assert r.json()["item"]["online_variant_id"] == b

    assert (await client.delete(f"/barcodes/{code}", headers=branch_manager.headers)
            ).status_code == 403
    assert (await client.delete(f"/barcodes/{code}", headers=purchasing.headers)
            ).status_code == 204
    r = await client.get(f"/barcodes/{code}", headers=branch_manager.headers)
    assert r.json()["item"] is None


async def test_one_item_many_barcodes_and_audit(client, owner, branch_manager):
    vid = await _item()
    c1, c2 = _ean13(), _ean13()
    for c in (c1, c2):
        await client.post("/barcodes", headers=branch_manager.headers,
                          json={"barcode": c, "online_variant_id": vid})
    r = await client.get(f"/online-items/{vid}/barcodes", headers=branch_manager.headers)
    assert r.json() == sorted([c1, c2])

    from sqlalchemy import select

    from app.core.db import get_sessionmaker
    from app.models import AuditLog
    async with get_sessionmaker()() as s:
        acts = (await s.scalars(select(AuditLog.action).where(
            AuditLog.entity == "product_barcode", AuditLog.entity_id.in_([c1, c2])))).all()
    assert sorted(acts) == ["barcode.link", "barcode.link"]


async def test_accountant_cannot_link(client, accountant):
    vid = await _item()
    r = await client.post("/barcodes", headers=accountant.headers,
                          json={"barcode": _ean13(), "online_variant_id": vid})
    assert r.status_code == 403
