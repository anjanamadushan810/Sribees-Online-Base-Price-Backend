"""Supplier-app sign-in with email + password: staff set a temporary password when they
create the supplier, the supplier changes it, a forgotten one is reset with an SMS code."""

import secrets

from tests.conftest import ip_headers, unique_mobile
from tests.test_supplier_auth import code_in, last_sms

PASSWORD = "Temp-pass-2026"
NEW = "My-own-pass-2026"


def _email() -> str:
    return f"supplier.{secrets.token_hex(4)}@example.com"


async def new_login_supplier(client, staff, password=PASSWORD):
    email, phone = _email(), unique_mobile()
    r = await client.post("/suppliers", headers=staff.headers, json={
        "name": f"Login Supplier {secrets.token_hex(3)}", "email": email, "phone": phone,
        "contact_name": "Nishantha", "app_password": password, "app_language": "en"})
    assert r.status_code == 201, r.text
    return r.json(), email, phone


async def login(client, email, password, **kw):
    return await client.post("/supplier/auth/login", headers=ip_headers(),
                             json={"email": email, "password": password}, **kw)


async def test_the_form_creates_the_login(client, owner):
    s, email, phone = await new_login_supplier(client, owner)
    assert len(s["users"]) == 1
    u = s["users"][0]
    assert u["email"] == email and u["must_change_password"] is True
    assert PASSWORD not in str(s) and "password_hash" not in str(s)
    r = await login(client, email.upper(), PASSWORD)  # email is not case sensitive
    assert r.status_code == 200, r.text
    assert r.json()["must_change_password"] is True


async def test_login_needs_phone_and_email(client, owner):
    r = await client.post("/suppliers", headers=owner.headers, json={
        "name": f"No Phone {secrets.token_hex(3)}", "app_password": PASSWORD})
    assert r.status_code == 422 and "phone and email" in r.text
    r = await client.post("/suppliers", headers=owner.headers, json={
        "name": f"Weak {secrets.token_hex(3)}", "email": _email(), "phone": unique_mobile(),
        "app_password": "short"})
    assert r.status_code == 422


async def test_temporary_password_only_allows_changing_it(client, owner):
    _, email, _ = await new_login_supplier(client, owner)
    tok = (await login(client, email, PASSWORD)).json()
    h = {"Authorization": f"Bearer {tok['access_token']}"}
    assert (await client.get("/supplier/catalog", headers=h)).status_code == 403
    me = await client.get("/supplier/me", headers=h)
    assert me.status_code == 200 and me.json()["must_change_password"] is True
    r = await client.post("/supplier/auth/change-password", headers=h, json={
        "current_password": "wrong-one-12", "new_password": NEW})
    assert r.status_code == 401
    r = await client.post("/supplier/auth/change-password", headers=h, json={
        "current_password": PASSWORD, "new_password": NEW})
    assert r.status_code == 204, r.text
    assert (await client.get("/supplier/catalog", headers=h)).status_code == 200  # same session
    assert (await login(client, email, PASSWORD)).status_code == 401  # old one is dead
    again = await login(client, email, NEW)
    assert again.status_code == 200 and again.json()["must_change_password"] is False


async def test_change_ends_other_sessions_only(client, owner):
    _, email, _ = await new_login_supplier(client, owner)
    a = (await login(client, email, PASSWORD)).json()
    b = (await login(client, email, PASSWORD)).json()
    ha = {"Authorization": f"Bearer {a['access_token']}"}
    hb = {"Authorization": f"Bearer {b['access_token']}"}
    r = await client.post("/supplier/auth/change-password", headers=ha, json={
        "current_password": PASSWORD, "new_password": NEW})
    assert r.status_code == 204
    assert (await client.get("/supplier/me", headers=ha)).status_code == 200
    assert (await client.get("/supplier/me", headers=hb)).status_code == 401


async def test_wrong_passwords_lock_the_login_and_strangers_look_the_same(client, owner):
    _, email, _ = await new_login_supplier(client, owner)
    stranger = await login(client, _email(), "whatever-123")
    wrong = await login(client, email, "not-the-password")
    assert stranger.status_code == wrong.status_code == 401
    assert stranger.json()["detail"] == wrong.json()["detail"]
    for _ in range(4):
        assert (await login(client, email, "not-the-password")).status_code == 401
    assert (await login(client, email, PASSWORD)).status_code == 429  # locked even if right


async def test_staff_can_set_a_new_temporary_password(client, owner, purchasing, branch_manager):
    s, email, _ = await new_login_supplier(client, owner)
    uid = s["users"][0]["supplier_user_id"]
    ok = (await login(client, email, PASSWORD)).json()
    r = await client.post(f"/suppliers/{s['supplier_id']}/users/{uid}/password",
                          headers=branch_manager.headers, json={"password": "Another-1234"})
    assert r.status_code == 403
    r = await client.post(f"/suppliers/{s['supplier_id']}/users/{uid}/password",
                          headers=purchasing.headers, json={"password": "Another-1234"})
    assert r.status_code == 200 and r.json()["must_change_password"] is True
    assert (await client.get("/supplier/me", headers={
        "Authorization": f"Bearer {ok['access_token']}"})).status_code == 401  # signed out
    assert (await login(client, email, "Another-1234")).status_code == 200


async def test_forgot_password_by_sms_code(client, owner):
    _, email, phone = await new_login_supplier(client, owner)
    h = ip_headers()
    unknown = await client.post("/supplier/auth/forgot-password", headers=h,
                                json={"email": _email()})
    known = await client.post("/supplier/auth/forgot-password", headers=h,
                              json={"email": email})
    assert unknown.status_code == known.status_code == 200
    assert unknown.json() == known.json()
    code = code_in(last_sms(phone if phone.startswith("+") else
                            "+94" + phone.lstrip("0")).text)
    bad = await client.post("/supplier/auth/reset-password", headers=h, json={
        "email": email, "code": "000000", "new_password": NEW})
    assert bad.status_code == 401
    r = await client.post("/supplier/auth/reset-password", headers=h, json={
        "email": email, "code": code, "new_password": NEW})
    assert r.status_code == 204, r.text
    # the code works once
    r = await client.post("/supplier/auth/reset-password", headers=h, json={
        "email": email, "code": code, "new_password": "Third-pass-123"})
    assert r.status_code == 401
    ok = await login(client, email, NEW)
    assert ok.status_code == 200 and ok.json()["must_change_password"] is False


async def test_email_and_phone_are_unique(client, owner):
    s, email, phone = await new_login_supplier(client, owner)
    r = await client.post("/suppliers", headers=owner.headers, json={
        "name": f"Dup {secrets.token_hex(3)}", "email": email, "phone": unique_mobile(),
        "app_password": PASSWORD})
    assert r.status_code == 409
    r = await client.post(f"/suppliers/{s['supplier_id']}/users", headers=owner.headers, json={
        "phone": unique_mobile(), "full_name": "Second", "email": email,
        "password": PASSWORD})
    assert r.status_code == 409
    r = await client.post(f"/suppliers/{s['supplier_id']}/users", headers=owner.headers, json={
        "phone": unique_mobile(), "full_name": "Second", "email": _email()})
    assert r.status_code == 422  # an email without a password


async def test_phone_code_sign_in_still_works(client, owner):
    s, email, phone = await new_login_supplier(client, owner)
    h = ip_headers()
    await client.post("/supplier/auth/request-code", json={"phone": phone}, headers=h)
    code = code_in(last_sms("+94" + phone.lstrip("0")).text)
    r = await client.post("/supplier/auth/verify", headers=h, json={"phone": phone, "code": code})
    assert r.status_code == 200 and r.json()["must_change_password"] is False
