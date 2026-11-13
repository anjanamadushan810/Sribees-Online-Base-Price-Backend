"""Write-offs, transfers and counts, with the one loss-approval rule.

Moved from Finance (tests/test_stock_control_api.py) with the stock code; only the
imports changed: goods now arrive through Supply receiving (tests/stock_helpers.py)."""

from decimal import Decimal

import pytest

from tests.conftest import sign_in
from tests.stock_helpers import balance, batch_qty, stocked


@pytest.fixture(scope="session")
async def manager(client, branch):
    return await sign_in(client, "branch_manager", branch_ids=[branch])


async def test_small_writeoff_is_approved_by_the_accountant(
    client, owner, accountant, manager, branch
):
    product, batch = await stocked(client, owner, branch, qty="10", price="300.00")
    r = await client.post("/stock/write-offs", headers=manager.headers, json={
        "branch_id": str(branch), "reason": "damaged", "adjustment_date": "2026-10-10",
        "lines": [{"batch_id": batch["batch_id"], "qty": "3"}]})
    assert r.status_code == 201, r.text
    adj = r.json()
    assert adj["status"] == "pending_approval" and adj["value"] == "-900.00"
    assert (await batch_qty(client, owner, product))[batch["batch_no"]] == "10"  # not yet

    assert (await client.post(f"/stock/adjustments/{adj['adjustment_id']}/approve",
                              headers=manager.headers)).status_code == 403
    losses_before = await balance(client, owner, "5100", branch)
    r = await client.post(f"/stock/adjustments/{adj['adjustment_id']}/approve",
                          headers=accountant.headers)
    assert r.status_code == 200 and r.json()["status"] == "posted"
    assert await balance(client, owner, "5100", branch) - losses_before == Decimal("900")
    assert (await batch_qty(client, owner, product))[batch["batch_no"]] == "7"


async def test_large_writeoff_needs_the_owner(client, owner, accountant, manager, branch):
    product, batch = await stocked(client, owner, branch, qty="5", price="1500.00",
                                   expiry="2026-10-20")
    adj = (await client.post("/stock/write-offs", headers=manager.headers, json={
        "branch_id": str(branch), "reason": "expired", "adjustment_date": "2026-10-21",
        "lines": [{"batch_id": batch["batch_id"], "qty": "5"}]})).json()
    assert adj["value"] == "-7500.00"  # above the Rs 5,000 limit
    r = await client.post(f"/stock/adjustments/{adj['adjustment_id']}/approve",
                          headers=accountant.headers)
    assert r.status_code == 403 and "owner" in r.json()["detail"]
    r = await client.post(f"/stock/adjustments/{adj['adjustment_id']}/approve",
                          headers=owner.headers)
    assert r.json()["status"] == "posted"
    assert batch["batch_no"] not in await batch_qty(client, owner, product)  # empty now


async def test_rejected_writeoff_moves_nothing(client, owner, accountant, manager, branch):
    product, batch = await stocked(client, owner, branch, qty="4")
    adj = (await client.post("/stock/write-offs", headers=manager.headers, json={
        "branch_id": str(branch), "reason": "theft", "adjustment_date": "2026-10-11",
        "lines": [{"batch_id": batch["batch_id"], "qty": "4"}]})).json()
    r = await client.post(f"/stock/adjustments/{adj['adjustment_id']}/reject",
                          headers=accountant.headers, json={"reason": "Found in the back room"})
    assert r.json()["status"] == "rejected"
    assert (await batch_qty(client, owner, product))[batch["batch_no"]] == "4"


async def test_transfer_with_a_short_receipt(
    client, owner, accountant, manager, branch, other_branch
):
    product, batch = await stocked(client, owner, branch, qty="12", price="250.00",
                                   expiry="2027-01-31")
    r = await client.post("/stock/transfers", headers=manager.headers, json={
        "from_branch_id": str(branch), "to_branch_id": str(other_branch),
        "shipped_date": "2026-10-12", "lines": [{"batch_id": batch["batch_id"], "qty": "10"}]})
    assert r.status_code == 201, r.text
    transfer = r.json()
    assert transfer["value_shipped"] == "2500.00"
    transit = await balance(client, owner, "1310", other_branch)

    # The manager of the sending branch cannot receive at the other branch.
    body = {"received_date": "2026-10-13",
            "lines": [{"transfer_line_id": transfer["lines"][0]["transfer_line_id"],
                       "qty_received": "8"}]}
    assert (await client.post(f"/stock/transfers/{transfer['transfer_id']}/receive",
                              headers=manager.headers, json=body)).status_code == 404
    other = await sign_in(client, "branch_manager", branch_ids=[other_branch])
    r = await client.post(f"/stock/transfers/{transfer['transfer_id']}/receive",
                          headers=other.headers, json=body)
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["status"] == "short_pending" and got["value_received"] == "2000.00"

    new_batch = (await client.get("/stock/batches", headers=other.headers,
                                  params={"product_id": product["product_id"]})).json()
    assert len(new_batch) == 1
    assert new_batch[0]["expiry_date"] == "2027-01-31"
    assert Decimal(new_batch[0]["unit_cost"]) == Decimal("250")

    r = await client.post(f"/stock/adjustments/{got['adjustment_id']}/approve",
                          headers=accountant.headers)
    assert r.status_code == 200 and r.json()["value"] == "-500.00"
    assert await balance(client, owner, "1310", other_branch) - transit == Decimal("-2500")
    listed = (await client.get("/stock/transfers", headers=owner.headers)).json()
    assert next(t for t in listed if t["transfer_id"] == transfer["transfer_id"])["status"] \
        == "closed"


async def test_count_differences_post_after_approval(client, owner, accountant, manager, branch):
    product, batch = await stocked(client, owner, branch, qty="20", price="50.00")
    r = await client.post("/stock/counts", headers=manager.headers, json={
        "branch_id": str(branch), "count_date": "2026-10-15",
        "product_ids": [product["product_id"]]})
    assert r.status_code == 201, r.text
    count = r.json()
    assert [ln["system_qty"] for ln in count["lines"]] == ["20"]

    second = await client.post("/stock/counts", headers=manager.headers, json={
        "branch_id": str(branch), "count_date": "2026-10-15",
        "product_ids": [product["product_id"]]})
    assert second.status_code == 409  # one open count per branch

    early = await client.post(f"/stock/counts/{count['count_id']}/submit",
                              headers=manager.headers)
    assert early.status_code == 422 and "not counted" in early.json()["detail"]
    await client.post(f"/stock/counts/{count['count_id']}/lines", headers=manager.headers,
                      json=[{"batch_id": batch["batch_id"], "counted_qty": "18"}])
    r = await client.post(f"/stock/counts/{count['count_id']}/submit", headers=manager.headers)
    assert r.json()["status"] == "submitted"
    adj_id = r.json()["adjustment_id"]
    r = await client.post(f"/stock/adjustments/{adj_id}/approve", headers=accountant.headers)
    assert r.json()["status"] == "posted" and r.json()["value"] == "-100.00"
    assert (await batch_qty(client, owner, product))[batch["batch_no"]] == "18"
