"""QR labels (joint-go-live plan, phase C): numbers, PDF sheets, linking, receiving."""

import re
import uuid
import zlib

import numpy as np
import pytest
import zxingcpp
from sqlalchemy import text

from app.services import label_pdf, labels
from tests.conftest import sign_in
from tests.stock_helpers import approved_order, fresh_supplier, new_product, receive, stocked

PT = 3  # raster pixels per PDF point


def _pages(pdf: bytes) -> list[str]:
    return [zlib.decompress(m.group(1)).decode("latin-1")
            for m in re.finditer(rb"stream\n(.*?)\nendstream", pdf, re.S)]


def _decode_page(content: str) -> list[str]:
    """Paint the page's filled rectangles and read every QR code on it."""
    w, h = int(210 / 25.4 * 72 * PT) + 1, int(297 / 25.4 * 72 * PT) + 1
    img = np.full((h, w), 255, dtype=np.uint8)
    for x, y, rw, rh in re.findall(r"([\d.]+) ([\d.]+) ([\d.]+) ([\d.]+) re", content):
        x0, y0 = round(float(x) * PT), round(float(y) * PT)
        x1, y1 = round((float(x) + float(rw)) * PT), round((float(y) + float(rh)) * PT)
        img[h - y1:h - y0, x0:x1] = 0
    return [r.text for r in zxingcpp.read_barcodes(img)]


def test_numbers_have_a_check_digit():
    n = labels.make_number(100000123)
    assert len(n) == 10 and labels.parse_number(n) == n
    assert labels.parse_number(f"{n[:4]} {n[4:8]}-{n[8:]}") == n  # typed with spaces
    wrong = n[:9] + str((int(n[9]) + 1) % 10)
    with pytest.raises(labels.SupplyError, match="check digit"):
        labels.parse_number(wrong)
    swapped = n[:3] + n[4] + n[3] + n[5:]
    if swapped != n:
        with pytest.raises(labels.SupplyError):
            labels.parse_number(swapped)
    for bad in ("", "12345", "0123456789", "12345678901", "abcdefghij"):
        with pytest.raises(labels.SupplyError):
            labels.parse_number(bad)


def test_default_sheet_fits_96_labels_that_scan():
    w, h, qr = label_pdf.layout(8, 12)
    assert (round(w, 2), round(h, 2)) == (25.0, 23.33) and qr > 17
    with pytest.raises(ValueError):
        label_pdf.sheet_pages(["1000000003"], 20, 30)


async def test_print_a_run_and_every_qr_decodes_back(client, owner):
    r = await client.post("/labels/ranges", headers=owner.headers, json={"count": 101})
    assert r.status_code == 201, r.text
    rng = r.json()
    assert rng["count"] == 101 and rng["sheets"] == 2 and (rng["cols"], rng["rows"]) == (8, 12)
    assert labels.parse_number(rng["first"]) and labels.parse_number(rng["last"])
    pdf = await client.get(f"/labels/ranges/{rng['range_id']}/pdf", headers=owner.headers)
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF-1.4") and b"Rs" not in pdf.content
    pages = _pages(pdf.content)
    assert len(pages) == 3  # 96 + 5 labels, then the list of numbers
    found = _decode_page(pages[0]) + _decode_page(pages[1])
    expected = [labels.make_number(b) for b in range(int(rng["first"][:9]),
                                                      int(rng["last"][:9]) + 1)]
    assert sorted(found) == expected and len(found) == 101
    assert all(f"{n[:4]} {n[4:8]} {n[8:]}" in pages[2] for n in expected)
    # the next run never repeats a number
    r2 = (await client.post("/labels/ranges", headers=owner.headers, json={"count": 3})).json()
    assert int(r2["first"][:9]) == int(rng["last"][:9]) + 1


async def test_link_rules(client, owner, branch):
    rng = (await client.post("/labels/ranges", headers=owner.headers, json={"count": 20})).json()
    nums = [labels.make_number(b) for b in range(int(rng["first"][:9]), int(rng["last"][:9]) + 1)]
    _, batch = await stocked(client, owner, branch, qty="5", name="Soap QR")
    url = f"/stock/batches/{batch['batch_id']}/labels"

    r = await client.post(url, headers=owner.headers, json={"numbers": nums[:3]})
    assert r.status_code == 200 and r.json()["labels_on_batch"] == 3
    # linked twice (same batch, or another batch) is refused
    r = await client.post(url, headers=owner.headers, json={"numbers": [nums[0]]})
    assert r.status_code == 409 and "already linked" in r.json()["detail"]
    _, other = await stocked(client, owner, branch, qty="5", name="Soap QR 2")
    r = await client.post(f"/stock/batches/{other['batch_id']}/labels", headers=owner.headers,
                          json={"numbers": [nums[1]]})
    assert r.status_code == 409 and "another batch" in r.json()["detail"]
    # the same label twice in one scan
    r = await client.post(url, headers=owner.headers, json={"numbers": [nums[5], nums[5]]})
    assert r.status_code == 422 and "twice" in r.json()["detail"]
    # a valid-looking number that was never printed
    never = labels.make_number(999_999_990)
    r = await client.post(url, headers=owner.headers, json={"numbers": [never]})
    assert r.status_code == 422 and "never printed" in r.json()["detail"]
    # a mistyped number (bad check digit)
    bad = nums[6][:9] + str((int(nums[6][9]) + 1) % 10)
    r = await client.post(url, headers=owner.headers, json={"numbers": [bad]})
    assert r.status_code == 422 and "check digit" in r.json()["detail"]
    # no more labels than units
    r = await client.post(url, headers=owner.headers, json={"numbers": nums[10:13]})
    assert r.status_code == 422 and "one label per unit" in r.json()["detail"]
    # "next 2 from this label" skips the ones already used
    r = await client.post(url, headers=owner.headers, json={"start": nums[0], "count": 2})
    assert r.status_code == 200 and r.json()["linked"] == nums[3:5]
    assert r.json()["labels_on_batch"] == 5

    look = (await client.get(f"/labels/{nums[0]}", headers=owner.headers)).json()
    assert look["status"] == "linked" and look["batch"]["batch_id"] == batch["batch_id"]
    assert look["batch"]["product_name"].startswith("Soap QR")
    assert (await client.get(f"/labels/{nums[9]}", headers=owner.headers)).json()["batch"] is None
    rows = (await client.get("/stock/batches", headers=owner.headers,
                             params={"product_id": batch["product_id"]})).json()
    assert next(x for x in rows if x["batch_id"] == batch["batch_id"])["labelled"] == 5


async def test_database_keeps_a_label_on_its_batch(client, owner, branch, db):
    rng = (await client.post("/labels/ranges", headers=owner.headers, json={"count": 2})).json()
    _, batch = await stocked(client, owner, branch, qty="2", name="Tea QR")
    _, other = await stocked(client, owner, branch, qty="2", name="Tea QR 2")
    await client.post(f"/stock/batches/{batch['batch_id']}/labels", headers=owner.headers,
                      json={"numbers": [rng["first"]]})
    for sql, why in (("UPDATE labels SET batch_id = CAST(:b AS uuid) WHERE number = :n",
                      "already on another batch"),
                     ("UPDATE labels SET status = 'printed', batch_id = NULL WHERE number = :n",
                      "already on another batch"),
                     ("UPDATE labels SET status = 'printed' WHERE number = :n",
                      "cannot be unlinked"),
                     ("DELETE FROM labels WHERE number = :n", "never deleted")):
        with pytest.raises(Exception, match=why):
            async with db.begin_nested():
                await db.execute(text(sql), {"n": rng["first"], "b": other["batch_id"]})
    with pytest.raises(Exception, match="never changed"):
        async with db.begin_nested():
            await db.execute(text("DELETE FROM label_ranges WHERE range_id = :r"),
                             {"r": rng["range_id"]})


async def test_receiving_with_labels_and_the_go_live_switch(client, owner, branch):
    rng = (await client.post("/labels/ranges", headers=owner.headers, json={"count": 10})).json()
    nums = [labels.make_number(b) for b in range(int(rng["first"][:9]), int(rng["last"][:9]) + 1)]
    supplier = await fresh_supplier(client, owner)
    rice = await new_product(client, owner, name="Biscuit QR")
    oid, (line,) = await approved_order(owner, supplier, branch, [(rice, 6, "80.00")])

    r = await client.patch("/settings/labels_required", headers=owner.headers,
                           json={"value": "1"})
    assert r.status_code == 200, r.text
    try:
        r = await receive(client, owner, oid, [{"order_line_id": line, "units": 3,
                                                "free_units": 1, "labels": nums[:3]}])
        assert r.status_code == 422 and "4 units need 4 QR labels" in r.json()["detail"]
        r = await receive(client, owner, oid, [{"order_line_id": line, "units": 3,
                                                "free_units": 1, "labels": nums[:4]}])
        assert r.status_code == 201, r.text
        batch_id = r.json()["lines"][0]["batch_id"]
        look = (await client.get(f"/labels/{nums[3]}", headers=owner.headers)).json()
        assert look["batch"]["batch_id"] == batch_id
        # a label already on a batch cannot arrive again (the whole receipt is refused)
        supplier = await fresh_supplier(client, owner)
        oid, (line,) = await approved_order(owner, supplier, branch, [(rice, 6, "80.00")])
        r = await receive(client, owner, oid, [{"order_line_id": line, "units": 1,
                                                "labels": [nums[0]]}])
        assert r.status_code == 409
    finally:
        await client.patch("/settings/labels_required", headers=owner.headers,
                           json={"value": "0"})
    # switch off: receiving without labels works as before
    supplier = await fresh_supplier(client, owner)
    oid, (line,) = await approved_order(owner, supplier, branch, [(rice, 6, "80.00")])
    r = await receive(client, owner, oid, [{"order_line_id": line, "units": 1}])
    assert r.status_code == 201, r.text


async def test_who_may_print_and_see(client, owner, branch, other_branch):
    acc = await sign_in(client, "accountant")
    r = await client.post("/labels/ranges", headers=acc.headers, json={"count": 1})
    assert r.status_code == 403
    rng = (await client.post("/labels/ranges", headers=owner.headers, json={"count": 1})).json()
    _, batch = await stocked(client, owner, branch, qty="1", name="Salt QR")
    await client.post(f"/stock/batches/{batch['batch_id']}/labels", headers=owner.headers,
                      json={"numbers": [rng["first"]]})
    elsewhere = await sign_in(client, "branch_manager", branch_ids=[other_branch])
    r = await client.get(f"/labels/{rng['first']}", headers=elsewhere.headers)
    assert r.status_code == 404
    r = await client.post(f"/stock/batches/{batch['batch_id']}/labels",
                          headers=elsewhere.headers, json={"numbers": [rng["first"]]})
    assert r.status_code == 404
    assert (await client.post("/labels/ranges", headers=owner.headers,
                              json={"count": 10001})).status_code == 422
    assert uuid.UUID(rng["range_id"])
