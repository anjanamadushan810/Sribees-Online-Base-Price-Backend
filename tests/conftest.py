"""DB tests run against a real, throwaway PostgreSQL database (created, migrated, dropped).

Admin connection: SUPPLY_TEST_ADMIN_URL (postgresql://user:pass@host:port/postgres).
Pure tests (the WhatsApp parser) need no database and run without it.
"""

import os
import secrets
import subprocess
import sys
from pathlib import Path

import psycopg
import pyotp
import pytest
from cryptography.fernet import Fernet

BACKEND = Path(__file__).resolve().parents[1]
ADMIN_URL = os.environ.get("SUPPLY_TEST_ADMIN_URL")

DB_NAME = f"supply_test_{secrets.token_hex(4)}"
_base = (ADMIN_URL or "postgresql://unset/postgres").rsplit("/", 1)[0]
os.environ.update(
    ENVIRONMENT="test",
    DATABASE_URL=f"{_base.replace('postgresql://', 'postgresql+asyncpg://')}/{DB_NAME}",
    JWT_SECRET=secrets.token_hex(32),
    TOTP_ENCRYPTION_KEY=Fernet.generate_key().decode(),
    OTP_SECRET=secrets.token_hex(32),
    SMS_PROVIDER="fake",
    DEV_FIXED_OTP="",
    TWO_FACTOR="true",
)


def _admin(sql: str) -> None:
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(sql)


@pytest.fixture(scope="session")
def database():
    if not ADMIN_URL:
        pytest.skip("set SUPPLY_TEST_ADMIN_URL (use .tools/run_supply_tests.py)")
    _admin(f'CREATE DATABASE "{DB_NAME}"')
    try:
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=BACKEND, check=True, env=os.environ.copy(),
        )
        yield
    finally:
        _admin(f'DROP DATABASE IF EXISTS "{DB_NAME}" WITH (FORCE)')


@pytest.fixture(scope="session")
async def client(database):
    import httpx

    from app.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://supply.test"
    ) as c:
        yield c


class Person:
    def __init__(self, email, password, totp_secret, access_token, refresh_token):
        self.email = email
        self.password = password
        self.totp_secret = totp_secret
        self.refresh_token = refresh_token
        self.headers = {"Authorization": f"Bearer {access_token}"}


async def make_user(role: str):
    from app.core.db import get_sessionmaker
    from app.core.security import hash_password
    from app.models import User

    email = f"{role}.{secrets.token_hex(3)}@example.com"
    password = secrets.token_urlsafe(16)
    async with get_sessionmaker()() as s, s.begin():
        s.add(User(email=email, full_name=f"Test {role}", role=role,
                   password_hash=hash_password(password)))
    return email, password


async def sign_in(client, role: str) -> Person:
    """Create a staff user and walk the real sign-in flow, enrolling 2FA."""
    email, password = await make_user(role)
    r = await client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    body = r.json()
    secret = None
    if body["status"] == "totp_setup_required":
        ticket = body["ticket"]
        setup = (await client.post("/auth/totp/setup", json={"ticket": ticket})).json()
        secret = setup["secret"]
        r = await client.post(
            "/auth/totp/enable", json={"ticket": ticket, "code": pyotp.TOTP(secret).now()}
        )
        assert r.status_code == 200, r.text
        body = r.json()
    assert body["status"] == "ok", body
    return Person(email, password, secret, body["access_token"], body["refresh_token"])


@pytest.fixture(scope="session")
async def owner(client):
    return await sign_in(client, "owner")


@pytest.fixture(scope="session")
async def purchasing(client):
    return await sign_in(client, "purchasing")


@pytest.fixture(scope="session")
async def branch_manager(client):
    return await sign_in(client, "branch_manager")


def unique_mobile() -> str:
    return "07" + f"{secrets.randbelow(10**8):08d}"


def ip_headers() -> dict:
    """A fresh client address per test, so per-IP limits of one test do not hit another."""
    return {"X-Forwarded-For": f"10.{secrets.randbelow(250)}.{secrets.randbelow(250)}."
                               f"{secrets.randbelow(250) + 1}"}
