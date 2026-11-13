"""Server-side commands.

    python -m app.cli create-owner --email you@example.com --name "Full Name"
    ... create-owner ... --password-stdin   (scripted: password as one line on stdin)
    python -m app.cli worker          # Finance's PO answers + supplier alerts, forever
    python -m app.cli worker --once   # one round, then exit

The owner's password is read from the terminal, never from the command line.
"""

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import func, select

from app.core.db import get_sessionmaker
from app.core.security import MIN_PASSWORD_LENGTH, hash_password
from app.models import User
from app.services import audit


async def create_owner(email: str, name: str, password_stdin: bool = False) -> None:
    if password_stdin:  # for scripted set-up: one line on stdin, never on the command line
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("Owner password: ")
        if password != getpass.getpass("Repeat: "):
            sys.exit("passwords differ")
    if len(password) < MIN_PASSWORD_LENGTH:
        sys.exit(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    async with get_sessionmaker()() as db, db.begin():
        if await db.scalar(select(User).where(func.lower(User.email) == email.lower())):
            sys.exit("a user with this email exists")
        user = User(email=email.lower(), full_name=name, role="owner",
                    password_hash=hash_password(password))
        db.add(user)
        await db.flush()
        await audit.record(db, action="user.create", entity="user", entity_id=user.user_id,
                           after={"email": user.email, "role": "owner", "via": "cli"})
    print("owner created; two-factor set-up starts at first sign-in")


async def worker(once: bool) -> None:
    """Background rounds: Finance's answers (when FINANCE_API_URL is set) and supplier
    app alerts (when FCM_CREDENTIALS_JSON is set). A second copy waits on the lock."""
    import logging

    import httpx

    from app.core.config import get_settings
    from app.services import finance_link, push

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    log = logging.getLogger("supply.worker")
    settings = get_settings()
    finance_on = bool(settings.finance_api_url and settings.finance_feed_token)
    if not finance_on and not push.enabled():
        sys.exit("nothing to do: set FINANCE_API_URL/FINANCE_FEED_TOKEN or FCM_CREDENTIALS_JSON")
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as lock_db:
        while not await finance_link.try_lock(lock_db):
            if once:
                sys.exit("another worker is running")
            await asyncio.sleep(30)
        async with httpx.AsyncClient() as client:
            while True:
                if finance_on:
                    try:
                        counts = await finance_link.run_once(sessionmaker, client)
                        if counts:
                            log.info("finance round: %s", counts)
                    except Exception:  # noqa: BLE001 -- logged, retried next round
                        log.exception("finance feed round failed")
                try:
                    counts = await push.run_once(sessionmaker, client)
                    if counts:
                        log.info("push round: %s", counts)
                except Exception:  # noqa: BLE001 -- logged, retried next round
                    log.exception("push round failed")
                if once:
                    return
                await asyncio.sleep(min(settings.finance_feed_poll_seconds,
                                        settings.push_poll_seconds))


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    owner = sub.add_parser("create-owner")
    owner.add_argument("--email", required=True)
    owner.add_argument("--name", required=True)
    owner.add_argument("--password-stdin", action="store_true")
    for name in ("worker", "finance-feed"):  # finance-feed: the first name, kept
        w = sub.add_parser(name)
        w.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.command == "create-owner":
        asyncio.run(create_owner(args.email, args.name, args.password_stdin))
    elif args.command in ("worker", "finance-feed"):
        asyncio.run(worker(args.once))


if __name__ == "__main__":
    main()
