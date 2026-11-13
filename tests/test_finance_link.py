"""Supply <-> Finance PO link (docs/plans/joint-go-live.md, phase A).

Finance's side is stood in for by an httpx MockTransport serving the answers Finance
writes to its outbox; the contract fixture tests/fixtures/contract_order_submitted.json
is what Finance's own tests read (refresh it with SUPPLY_EVENTS_DUMP=1).
"""

import hashlib
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from tests.test_orders import new_order, outbox_events, shop  # noqa: F401  (fixture)
from tests.test_supplier_auth import settings_override

TOKEN = "finance-test-token-" + "x" * 20
TOKEN_SHA = hashlib.sha256(TOKEN.encode()).hexdigest()
CONTRACT = Path(__file__).parent / "fixtures" / "contract_order_submitted.json"


async def _set_online_branch(branch_id: str) -> str:
    from app.core.db import get_sessionmaker
    from app.models import Branch

    online = uuid.uuid4()
    async with get_sessionmaker()() as s, s.begin():
        (await s.get(Branch, uuid.UUID(branch_id))).online_branch_id = online
    return str(online)


async def _submitted_order(client, purchasing, shop, boxes=2):  # noqa: F811
    o = await new_order(client, purchasing, shop)
    oid = o["order_id"]
    r = await client.post(f"/orders/{oid}/lines", headers=purchasing.headers,
                          json={"offer_id": shop["soap"], "boxes": boxes})
    assert r.status_code == 200, r.text
    r = await client.post(f"/orders/{oid}/lines", headers=purchasing.headers,
                          json={"offer_id": shop["unmatched"], "boxes": 1})
    assert r.status_code == 200, r.text
    r = await client.post(f"/orders/{oid}/submit", headers=purchasing.headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "submitted"  # waits for Finance, even for the owner
    return oid


def _feed(events: list[dict]):
    """A Finance stand-in serving `events` after the cursor it is asked for."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/internal/supply/events"
        assert request.headers["authorization"] == "Bearer finance-feed-token"
        after = int(request.url.params["after"])
        seen.append(after)
        return httpx.Response(200, json={"events": [e for e in events if e["seq"] > after]})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


def _ev(seq: int, kind: str, order_id: str, **payload) -> dict:
    return {"seq": seq, "event_id": str(uuid.uuid4()), "type": kind, "aggregate_id": order_id,
            "occurred_at": datetime.now(UTC).isoformat(),
            "payload": {"order_id": order_id, **payload}}


async def _cursor_reset() -> int:
    from app.core.db import get_sessionmaker
    from app.models import FeedState

    async with get_sessionmaker()() as s, s.begin():
        st = await s.get(FeedState, "finance")
        st.last_seq, st.gaps = 0, {}
    return 0


async def _run(events, now=None, mode="finance"):
    from app.core.db import get_sessionmaker
    from app.services import finance_link

    client, seen = _feed(events)
    async with client:
        with settings_override(finance_api_url="http://finance.test", order_approval=mode,
                               finance_feed_token="finance-feed-token"):
            counts = await finance_link.run_once(get_sessionmaker(), client, now=now)
    return counts, seen


async def test_internal_events_locks(client, owner, purchasing, shop):  # noqa: F811
    with settings_override(finance_service_token_sha256=""):
        assert (await client.get("/internal/finance/events")).status_code == 404
    with settings_override(finance_service_token_sha256=TOKEN_SHA):
        r = await client.get("/internal/finance/events")
        assert r.status_code == 401
        r = await client.get("/internal/finance/events",
                             headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401
        r = await client.get("/internal/finance/events",
                             headers={"Authorization": f"Bearer {TOKEN}",
                                      "X-Forwarded-For": "203.0.113.9"})
        assert r.status_code == 403
        with settings_override(finance_allowed_ips="10.9.9.0/24"):
            r = await client.get("/internal/finance/events",
                                 headers={"Authorization": f"Bearer {TOKEN}"})
            assert r.status_code == 403
        r = await client.get("/internal/finance/events",
                             headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 200


async def test_submit_needs_online_branch(client, owner, purchasing, shop):  # noqa: F811
    with settings_override(order_approval="finance"):
        o = await new_order(client, purchasing, shop)
        await client.post(f"/orders/{o['order_id']}/lines", headers=purchasing.headers,
                          json={"offer_id": shop["soap"], "boxes": 1})
        r = await client.post(f"/orders/{o['order_id']}/submit", headers=purchasing.headers)
        assert r.status_code == 409 and "branch id" in r.json()["detail"]


async def _only_variant(product_id) -> str:
    """Give the mirrored product one pack size (Online's default variant)."""
    from sqlalchemy.dialects.postgresql import insert

    from app.core.db import get_sessionmaker
    from app.models import OnlineVariant

    vid = uuid.uuid5(uuid.NAMESPACE_URL, f"variant-of-{product_id}")
    async with get_sessionmaker()() as s, s.begin():
        await s.execute(insert(OnlineVariant).values(
            online_variant_id=vid, online_product_id=product_id, name="Default",
            is_active=True).on_conflict_do_nothing())
    return str(vid)


async def test_finance_sees_the_order(client, owner, purchasing, shop):  # noqa: F811
    from tests.test_matching import CHERAMY_75

    online_branch = await _set_online_branch(shop["branch"])
    soap_variant = await _only_variant(CHERAMY_75)
    with settings_override(order_approval="finance"):
        oid = await _submitted_order(client, owner, shop)
        # the owner cannot approve here: Finance decides
        r = await client.post(f"/orders/{oid}/approve", headers=owner.headers)
        assert r.status_code == 409
    with settings_override(finance_service_token_sha256=TOKEN_SHA):
        r = await client.get("/internal/finance/events",
                             headers={"Authorization": f"Bearer {TOKEN}"}, params={"after": 0})
    evs = [e for e in r.json()["events"] if e["aggregate_id"] == oid]
    assert [e["type"] for e in evs] == ["order.submitted"]
    ev = evs[0]
    uuid.UUID(ev["event_id"])
    p = ev["payload"]
    assert p["branch"]["online_branch_id"] == online_branch
    assert p["submitted_by_role"] == "owner" and p["approval_mode"] == "finance"
    assert p["supplier"]["supplier_id"] == shop["supplier_id"]
    soap = next(ln for ln in p["lines"] if ln["online_product_id"])
    assert (soap["units"], soap["unit_price"], soap["line_total"]) == (300, "125.00", "37500.00")
    assert soap["finance_product_id"] == soap_variant  # matched to the product, one pack size
    unmatched = next(ln for ln in p["lines"] if not ln["online_product_id"])
    assert unmatched["description"]  # Finance shows it by name until it is matched
    assert unmatched["finance_product_id"] is None
    assert Decimal(p["goods_total"]) == sum(Decimal(ln["line_total"]) for ln in p["lines"])
    # paging: after the event's own seq it is gone
    with settings_override(finance_service_token_sha256=TOKEN_SHA):
        r = await client.get("/internal/finance/events",
                             headers={"Authorization": f"Bearer {TOKEN}"},
                             params={"after": ev["seq"]})
    assert all(e["seq"] > ev["seq"] for e in r.json()["events"])
    if os.environ.get("SUPPLY_EVENTS_DUMP"):
        CONTRACT.write_text(json.dumps(ev, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")


async def test_finance_answers(client, owner, purchasing, shop):  # noqa: F811
    await _set_online_branch(shop["branch"])
    with settings_override(order_approval="finance"):
        ok = await _submitted_order(client, purchasing, shop)
        no = await _submitted_order(client, purchasing, shop)
        gone = await _submitted_order(client, purchasing, shop)
    r = await client.post(f"/orders/{gone}/cancel", headers=purchasing.headers,
                          json={"reason": "not needed"})
    assert r.status_code == 200

    # the supplier sees nothing while Finance has not answered
    assert all(o["order_id"] not in (ok, no) for o in
               (await client.get("/supplier/orders", headers=shop["sh"])).json())

    await _cursor_reset()
    ok_total = (await client.get(f"/orders/{ok}", headers=purchasing.headers)).json()["goods_total"]
    events = [
        _ev(1, "po.created", ok, po_no="PO-000101", status="pending_approval"),
        _ev(2, "po.approved", ok, po_no="PO-000101", approved_by_name="Owner Name",
            total=ok_total),
        _ev(3, "po.rejected", no, po_no="PO-000102", reason="too much stock already",
            rejected_by_name="Owner Name"),
        _ev(4, "po.approved", gone, po_no="PO-000103"),
        _ev(5, "po.approved", str(uuid.uuid4()), po_no="PO-000999"),
        _ev(6, "something.else", ok),
    ]
    counts, _ = await _run(events)
    assert counts == {"applied": 3, "stale": 1, "unknown": 1, "ignored": 1}

    o = (await client.get(f"/orders/{ok}", headers=purchasing.headers)).json()
    assert o["status"] == "approved" and o["finance_po_no"] == "PO-000101"
    assert o["approved_by_name"] == "Owner Name"
    assert any(x["order_id"] == ok for x in
               (await client.get("/supplier/orders", headers=shop["sh"])).json())
    assert await outbox_events(ok) == ["order.submitted", "order.approved", "supplier.notify"]

    o = (await client.get(f"/orders/{no}", headers=purchasing.headers)).json()
    assert o["status"] == "rejected" and o["reject_reason"] == "too much stock already"
    assert (await client.get(f"/supplier/orders/{no}", headers=shop["sh"])).status_code == 404
    o = (await client.get(f"/orders/{gone}", headers=purchasing.headers)).json()
    assert o["status"] == "cancelled"

    # the same events again (Finance re-served them): nothing changes, nothing doubles
    await _cursor_reset()
    counts, _ = await _run(events)
    assert counts == {"duplicate": 6}
    assert await outbox_events(ok) == ["order.submitted", "order.approved", "supplier.notify"]


async def test_gap_waits_then_moves_on(client, owner, purchasing, shop):  # noqa: F811
    from app.core.db import get_sessionmaker
    from app.models import FeedState

    await _set_online_branch(shop["branch"])
    with settings_override(order_approval="finance"):
        oid = await _submitted_order(client, purchasing, shop)
    await _cursor_reset()
    events = [_ev(1, "po.created", oid, po_no="PO-000201"),
              _ev(3, "po.approved", oid, po_no="PO-000201", approved_by_name="X")]  # no total
    now = datetime.now(UTC)
    counts, _ = await _run(events, now=now)
    assert counts == {"applied": 1, "waiting_on_gap": 1}
    o = (await client.get(f"/orders/{oid}", headers=purchasing.headers)).json()
    assert o["status"] == "submitted"
    counts, _ = await _run(events, now=now + timedelta(seconds=30))
    assert counts == {"waiting_on_gap": 1}
    counts, _ = await _run(events, now=now + timedelta(seconds=200))
    assert counts == {"applied": 1}
    o = (await client.get(f"/orders/{oid}", headers=purchasing.headers)).json()
    assert o["status"] == "approved"
    async with get_sessionmaker()() as s:
        st = await s.scalar(select(FeedState).where(FeedState.feed == "finance"))
        assert st.last_seq == 3 and st.gaps == {}


@pytest.mark.parametrize("approval", ["local"])
async def test_local_mode_unchanged(client, owner, purchasing, shop, approval):  # noqa: F811
    """Until the switch, the owner still approves in Supply."""
    with settings_override(order_approval=approval):
        o = await new_order(client, purchasing, shop)
        await client.post(f"/orders/{o['order_id']}/lines", headers=purchasing.headers,
                          json={"offer_id": shop["soap"], "boxes": 1})
        await client.post(f"/orders/{o['order_id']}/submit", headers=purchasing.headers)
        r = await client.post(f"/orders/{o['order_id']}/approve", headers=owner.headers)
        assert r.status_code == 200 and r.json()["status"] == "approved"


async def test_refused_mismatch_and_local_mode(client, owner, purchasing, shop):  # noqa: F811
    await _set_online_branch(shop["branch"])
    with settings_override(order_approval="finance"):
        refused = await _submitted_order(client, purchasing, shop)
        wrong = await _submitted_order(client, purchasing, shop)
        local = await _submitted_order(client, purchasing, shop)
    await _cursor_reset()
    counts, _ = await _run([
        _ev(1, "po.refused", refused, reason="branch X is not in Finance"),
        _ev(2, "po.approved", wrong, po_no="PO-9", total="1.00"),
    ])
    assert counts == {"applied": 1, "mismatch": 1}
    o = (await client.get(f"/orders/{refused}", headers=purchasing.headers)).json()
    assert o["status"] == "rejected" and "could not take" in o["reject_reason"]
    o = (await client.get(f"/orders/{wrong}", headers=purchasing.headers)).json()
    assert o["status"] == "submitted"  # never shown to the supplier
    # while Supply approves locally, Finance's answers change nothing
    await _cursor_reset()
    counts, _ = await _run([_ev(1, "po.approved", local, po_no="PO-10")], mode="local")
    assert counts == {"ignored": 1}
    o = (await client.get(f"/orders/{local}", headers=purchasing.headers)).json()
    assert o["status"] == "submitted"
