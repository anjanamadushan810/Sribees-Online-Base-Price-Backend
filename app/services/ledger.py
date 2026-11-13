"""Journals for Finance (joint-go-live plan, phase B: "how the journals cross over").

The stock code moved from Finance keeps its calls to `ledger.post_journal(...)`. Supply
has no ledger: this module has the same signature and, instead of posting, writes one
`journal.requested` outbox event carrying the exact lines. Finance's supply feed posts
it after its own checks (balance, an account allow-list per source, open period, one
post per source_ref), so Finance stays the only writer of its ledger.

Branches: the stock code names Supply branches; Finance keys branches by
SRIBEESonline's id, so each line carries the branch's `online_branch_id`. A branch
without one cannot keep stock (its journals would have nowhere to go).
"""

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Branch, OutboxEvent
from app.services import books
from app.services.errors import SupplyError

CENT = Decimal("0.01")
EVENT = "journal.requested"
BACKDATE_DAYS = 90


@dataclass(frozen=True)
class LineIn:
    account_code: str
    branch_id: uuid.UUID
    debit: Decimal = Decimal("0")
    credit: Decimal = Decimal("0")
    memo: str | None = None


@dataclass(frozen=True)
class RequestedJournal:
    """What post_journal returns here: journal_id is the event's id (the stand-in the
    stock rows keep; Finance records it as the journal's source event)."""

    journal_id: uuid.UUID
    source: str
    source_ref: str | None


def _money(value: Decimal) -> Decimal:
    value = Decimal(value)
    if value != value.quantize(CENT):
        raise SupplyError(f"amount {value} has more than two decimal places")
    return value.quantize(CENT)


async def post_journal(
    db: AsyncSession,
    *,
    entry_date: date,
    source: str,
    memo: str,
    lines: list[LineIn],
    user_id: uuid.UUID | None,
    source_ref: str | None = None,
    reversal_of: uuid.UUID | None = None,
) -> RequestedJournal:
    """Ask Finance to post one balanced journal. With a source_ref, asking again for the
    same document returns the first request instead of a second one."""
    if reversal_of is not None:
        raise SupplyError("reversals are posted in Finance")
    if source_ref is not None:
        existing = await db.scalar(
            select(OutboxEvent).where(OutboxEvent.event == EVENT,
                                      OutboxEvent.payload["source"].astext == source,
                                      OutboxEvent.payload["source_ref"].astext == source_ref))
        if existing is not None:
            return RequestedJournal(existing.event_id, source, source_ref)

    # Dated stock work stays near today, so a typo never lands in a year Finance has
    # no period for (review 2026-11-13 #1); Finance still checks the period itself.
    today = date.today()
    if not (today - timedelta(days=BACKDATE_DAYS) <= entry_date <= today + timedelta(days=1)):
        raise SupplyError(f"date {entry_date} is outside the last {BACKDATE_DAYS} days")
    await books.open_period(db, entry_date)
    if not memo or not memo.strip():
        raise SupplyError("a journal needs a description")
    if len(lines) < 2:
        raise SupplyError("a journal needs at least two lines")

    branches = {b.branch_id: b for b in await db.scalars(
        select(Branch).where(Branch.branch_id.in_({ln.branch_id for ln in lines})))}
    out = []
    total_dr = total_cr = Decimal("0")
    for n, line in enumerate(lines, start=1):
        debit, credit = _money(line.debit), _money(line.credit)
        if debit < 0 or credit < 0:
            raise SupplyError(f"line {n}: amounts cannot be negative")
        if (debit > 0) == (credit > 0):
            raise SupplyError(f"line {n}: enter either a debit or a credit")
        if not line.account_code or not line.account_code.isdigit():
            raise SupplyError(f"line {n}: account {line.account_code!r} is not an account code")
        branch = branches.get(line.branch_id)
        if branch is None or not branch.is_active:
            raise SupplyError(f"line {n}: branch is unknown or inactive")
        if branch.online_branch_id is None:
            raise SupplyError(f"branch {branch.name} has no SRIBEESonline branch id, so its "
                              "stock cannot reach Finance; set it on the Branches page")
        total_dr += debit
        total_cr += credit
        out.append({"account_code": line.account_code,
                    "online_branch_id": str(branch.online_branch_id),
                    "branch_code": branch.code,
                    "debit": str(debit), "credit": str(credit), "memo": line.memo})
    if total_dr != total_cr:
        raise SupplyError(f"debits {total_dr} and credits {total_cr} do not balance")

    event_id = uuid.uuid4()
    db.add(OutboxEvent(event=EVENT, event_id=event_id, payload={
        "source": source, "source_ref": source_ref, "entry_date": entry_date.isoformat(),
        "memo": memo.strip(), "user_id": str(user_id) if user_id else None,
        "total": str(total_dr), "lines": out}))
    await db.flush()
    return RequestedJournal(event_id, source, source_ref)
