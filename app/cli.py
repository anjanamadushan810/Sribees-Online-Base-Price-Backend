"""Server-side commands.

    python -m app.cli create-owner --email you@example.com --name "Full Name"
    ... create-owner ... --password-stdin   (scripted: password as one line on stdin)
    python -m app.cli finance-feed          # read Finance's PO answers, forever
    python -m app.cli finance-feed --once   # one round, then exit

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


async def finance_feed(once: bool) -> None:
    """Read Finance's answers (PO approved / rejected). A second copy waits on the lock."""
    import logging

    import httpx

    from app.core.config import get_settings
    from app.services import finance_link

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    log = logging.getLogger("supply.finance_feed")
    settings = get_settings()
    if not settings.finance_api_url or not settings.finance_feed_token:
        sys.exit("FINANCE_API_URL and FINANCE_FEED_TOKEN are not set")
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as lock_db:
        while not await finance_link.try_lock(lock_db):
            if once:
                sys.exit("another finance feed is running")
            await asyncio.sleep(30)
        async with httpx.AsyncClient() as client:
            while True:
                try:
                    counts = await finance_link.run_once(sessionmaker, client)
                    if counts:
                        log.info("round: %s", counts)
                except Exception:
                    log.exception("finance feed round failed")
                if once:
                    return
                await asyncio.sleep(settings.finance_feed_poll_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    owner = sub.add_parser("create-owner")
    owner.add_argument("--email", required=True)
    owner.add_argument("--name", required=True)
    owner.add_argument("--password-stdin", action="store_true")
    ff = sub.add_parser("finance-feed")
    ff.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.command == "create-owner":
        asyncio.run(create_owner(args.email, args.name, args.password_stdin))
    elif args.command == "finance-feed":
        asyncio.run(finance_feed(args.once))


if __name__ == "__main__":
    main()
