"""Supplier app alerts (FCM HTTP v1): only the supplier's own signed-in phones, dead tokens
dropped, sign-out stops alerts, each event once."""

import json

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import func, select

from tests.test_catalog import bearer
from tests.test_orders import new_order, shop  # noqa: F401  (fixture)
from tests.test_supplier_auth import settings_override, signed_in_supplier

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
CREDS = json.dumps({
    "project_id": "sribees-test", "client_email": "push@sribees-test.iam.example",
    "token_uri": "https://oauth2.test/token",
    "private_key": _KEY.private_bytes(serialization.Encoding.PEM,
                                      serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()).decode(),
})


def _fcm(dead: set[str]):
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.test":
            form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
            claims = jwt.decode(form["assertion"], _KEY.public_key(), algorithms=["RS256"],
                                audience="https://oauth2.test/token")
            assert claims["scope"].endswith("firebase.messaging")
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3600})
        assert request.url.path == "/v1/projects/sribees-test/messages:send"
        assert request.headers["authorization"] == "Bearer at-1"
        msg = json.loads(request.content)["message"]
        if msg["token"] in dead:
            return httpx.Response(404, json={"error": {"status": "NOT_FOUND",
                                                        "details": ["UNREGISTERED"]}})
        sent.append(msg)
        return httpx.Response(200, json={"name": "projects/x/messages/1"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), sent


async def _skip_old_events():
    from app.core.db import get_sessionmaker
    from app.models import FeedState, OutboxEvent

    async with get_sessionmaker()() as s, s.begin():
        st = await s.get(FeedState, "push")
        st.last_seq = await s.scalar(select(func.coalesce(func.max(OutboxEvent.seq), 0)))
        st.gaps = {}


async def _round():
    from app.core.db import get_sessionmaker
    from app.services import push

    push._token.update(value=None, expires=0)
    client, sent = _fcm(dead={"dead-token-" + "d" * 20})
    async with client:
        with settings_override(fcm_credentials_json=CREDS):
            counts = await push.run_once(get_sessionmaker(), client)
    return counts, sent


async def _devices() -> set[str]:
    from app.core.db import get_sessionmaker
    from app.models import SupplierDevice

    async with get_sessionmaker()() as s:
        return set(await s.scalars(select(SupplierDevice.token)))


async def test_alerts_reach_only_the_suppliers_phones(client, owner, purchasing, shop):  # noqa: F811
    good, dead = "good-token-" + "g" * 20, "dead-token-" + "d" * 20
    for token in (good, dead):
        r = await client.post("/supplier/devices", headers=shop["sh"],
                              json={"token": token, "platform": "android"})
        assert r.status_code == 204, r.text
    # another supplier's phone must never hear about this order
    _, _, other = await signed_in_supplier(client, owner)
    other_token = "other-token-" + "o" * 20
    r = await client.post("/supplier/devices", headers=bearer(other),
                          json={"token": other_token})
    assert r.status_code == 204
    assert (await client.post("/supplier/devices", json={"token": good})).status_code == 401

    await _skip_old_events()
    with settings_override(order_approval="local"):
        o = await new_order(client, purchasing, shop)
        await client.post(f"/orders/{o['order_id']}/lines", headers=purchasing.headers,
                          json={"offer_id": shop["soap"], "boxes": 1})
        await client.post(f"/orders/{o['order_id']}/submit", headers=purchasing.headers)
        r = await client.post(f"/orders/{o['order_id']}/approve", headers=owner.headers)
        assert r.json()["status"] == "approved"

    counts, sent = await _round()
    assert counts == {"sent": 1, "dead": 1}
    assert [m["token"] for m in sent] == [good]
    assert sent[0]["data"] == {"kind": "new_order", "order_id": o["order_id"],
                               "order_no": o["order_no"]}
    assert o["order_no"] in sent[0]["notification"]["body"]
    assert dead not in await _devices() and other_token in await _devices()

    # each event once
    counts, sent = await _round()
    assert counts == {} and sent == []

    # signed out: no more alerts to that phone
    r = await client.post("/supplier/auth/logout", headers=shop["sh"])
    assert r.status_code == 204
    assert good not in await _devices()


async def test_no_credentials_no_alerts(client):
    from app.core.db import get_sessionmaker
    from app.services import push

    with settings_override(fcm_credentials_json=""):
        assert not push.enabled()
        async with httpx.AsyncClient() as c:
            assert await push.run_once(get_sessionmaker(), c) == {}
