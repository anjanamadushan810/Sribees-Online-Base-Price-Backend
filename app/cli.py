"""Server-side commands.

    python -m app.cli create-owner --email you@example.com --name "Full Name"
    ... create-owner ... --password-stdin   (scripted: password as one line on stdin)

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


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    owner = sub.add_parser("create-owner")
    owner.add_argument("--email", required=True)
    owner.add_argument("--name", required=True)
    owner.add_argument("--password-stdin", action="store_true")
    args = parser.parse_args()
    if args.command == "create-owner":
        asyncio.run(create_owner(args.email, args.name, args.password_stdin))


if __name__ == "__main__":
    main()
