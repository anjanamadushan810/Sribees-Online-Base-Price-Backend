"""QR labels (joint-go-live plan, phase C).

A label number is 9 digits (from label_number_seq) + a Luhn check digit. Printing registers
the numbers (a range); linking puts each number on exactly one batch, for ever. The QR holds
only the number. Prices and batch details stay in this system.
"""

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Branch, Label, LabelRange, Product, StockBatch
from app.services import audit, settings
from app.services.actors import Actor
from app.services.errors import Conflict, NotFound, SupplyError

MAX_RANGE = 10_000
MAX_LINK = 5_000
SHEET_COLS = "label_sheet_cols"
SHEET_ROWS = "label_sheet_rows"
REQUIRED = "labels_required"
_NOT_DIGIT = re.compile(r"[\s\-]")


# ---------------------------------------------------------------- numbers
def luhn_digit(body: str) -> int:
    total = 0
    for i, ch in enumerate(reversed(body)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (10 - total % 10) % 10


def make_number(body: int) -> str:
    s = str(body)
    return s + str(luhn_digit(s))


def parse_number(raw: str) -> str:
    """A scanned or typed label number, checked. Spaces and dashes are ignored."""
    s = _NOT_DIGIT.sub("", raw or "")
    if not re.fullmatch(r"[1-9]\d{9}", s):
        raise SupplyError(f"{raw!r} is not a Sribees label number (10 digits)")
    if luhn_digit(s[:9]) != int(s[9]):
        raise SupplyError(f"{raw!r} is not a valid label number (check digit); "
                          "scan it again or type it carefully")
    return s


# ---------------------------------------------------------------- ranges
async def sheet_layout(db: AsyncSession) -> tuple[int, int]:
    cols = int(await settings.get_value(db, SHEET_COLS))
    rows = int(await settings.get_value(db, SHEET_ROWS))
    if not (1 <= cols <= 20 and 1 <= rows <= 30):
        raise SupplyError("the label sheet settings are out of range")
    return cols, rows


async def register_range(db: AsyncSession, actor: Actor, *, count: int,
                         branch_id: uuid.UUID | None = None) -> LabelRange:
    if not (0 < count <= MAX_RANGE):
        raise SupplyError(f"print between 1 and {MAX_RANGE} labels at a time")
    if branch_id is not None:
        branch = await db.get(Branch, branch_id)
        if branch is None or not branch.is_active:
            raise NotFound("branch not found")
    cols, rows = await sheet_layout(db)
    # Numbers come from a sequence; one call takes a contiguous block (the sequence is
    # only used here, under a transaction-level advisory lock).
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext('label_number_seq'))"))
    first = await db.scalar(text("SELECT nextval('label_number_seq')"))
    if first + count - 1 > 999_999_999:
        raise SupplyError("label numbers are used up")
    await db.execute(text("SELECT setval('label_number_seq', :last)"),
                     {"last": first + count - 1})
    rng = LabelRange(range_no=await settings.next_doc_no(db, "LBL"), first_body=first,
                     last_body=first + count - 1, count=count, branch_id=branch_id,
                     cols=cols, rows=rows, printed_by=actor.user_id)
    db.add(rng)
    await db.flush()
    await db.execute(
        Label.__table__.insert(),
        [{"number": make_number(b), "range_id": rng.range_id, "status": "printed"}
         for b in range(first, first + count)])
    await audit.record(db, user_id=actor.user_id, action="labels.print", entity="label_range",
                       entity_id=rng.range_id,
                       after={"range_no": rng.range_no, "count": count,
                              "first": make_number(first),
                              "last": make_number(first + count - 1)},
                       ip_address=actor.ip_address)
    return rng


def range_numbers(rng: LabelRange) -> list[str]:
    return [make_number(b) for b in range(rng.first_body, rng.last_body + 1)]


# ---------------------------------------------------------------- linking
@dataclass(frozen=True)
class LinkResult:
    batch_id: uuid.UUID
    linked: list[str]
    labels_on_batch: int


async def _labels_on(db: AsyncSession, batch_id: uuid.UUID) -> int:
    return await db.scalar(select(func.count()).select_from(Label)
                           .where(Label.batch_id == batch_id)) or 0


def label_limit(batch: StockBatch, product: Product) -> int:
    """How many labels a batch may carry: one per piece; for goods kept by weight, one
    per bag, with a generous cap (a bag is at least 100 g)."""
    if product.unit == "kg":
        return max(1, int(batch.qty_received * 10))
    return int(batch.qty_received)


async def link(db: AsyncSession, actor: Actor, *, batch: StockBatch, numbers: list[str],
               scope: set[uuid.UUID] | None = None) -> LinkResult:
    if scope is not None and batch.branch_id not in scope:
        raise NotFound("batch not found")
    if not numbers:
        raise SupplyError("no labels given")
    if len(numbers) > MAX_LINK:
        raise SupplyError(f"link at most {MAX_LINK} labels at a time")
    parsed = [parse_number(n) for n in numbers]
    seen: set[str] = set()
    for n in parsed:
        if n in seen:
            raise SupplyError(f"label {n} was scanned twice")
        seen.add(n)
    if batch.status != "available":
        raise Conflict(f"batch {batch.batch_no} is {batch.status}")
    product = await db.get(Product, batch.product_id)
    rows = {row.number: row for row in await db.scalars(
        select(Label).where(Label.number.in_(parsed)).with_for_update())}
    for n in parsed:
        row = rows.get(n)
        if row is None:
            raise SupplyError(f"label {n} was never printed by Sribees Supply")
        if row.status != "printed":
            where = "this batch" if row.batch_id == batch.batch_id else "another batch"
            raise Conflict(f"label {n} is already {row.status} on {where}")
    already = await _labels_on(db, batch.batch_id)
    limit = label_limit(batch, product)
    if already + len(parsed) > limit:
        raise SupplyError(f"{product.name}: {already + len(parsed)} labels for {limit} "
                          "received; one label per unit")
    now = datetime.now(UTC)
    for n in parsed:
        row = rows[n]
        row.status, row.batch_id, row.linked_by, row.linked_at = (
            "linked", batch.batch_id, actor.user_id, now)
    await db.flush()
    await audit.record(db, user_id=actor.user_id, action="labels.link", entity="stock_batch",
                       entity_id=batch.batch_id,
                       after={"batch_no": batch.batch_no, "count": len(parsed),
                              "first": parsed[0], "last": parsed[-1]},
                       ip_address=actor.ip_address)
    return LinkResult(batch.batch_id, parsed, already + len(parsed))


async def next_free(db: AsyncSession, *, start: str, count: int) -> list[str]:
    """'Next N from this label': the first `count` unlinked numbers of start's range,
    from start on, in order (staff stick a strip of labels in a row)."""
    first = parse_number(start)
    row = await db.get(Label, first)
    if row is None:
        raise SupplyError(f"label {first} was never printed by Sribees Supply")
    if not (0 < count <= MAX_LINK):
        raise SupplyError(f"between 1 and {MAX_LINK} labels")
    found = list(await db.scalars(
        select(Label.number).where(Label.range_id == row.range_id, Label.number >= first,
                                   Label.status == "printed")
        .order_by(Label.number).limit(count)))
    if len(found) < count:
        raise SupplyError(f"only {len(found)} unused labels left in that run from {first}")
    return found


async def required(db: AsyncSession) -> bool:
    return await settings.get_value(db, REQUIRED) >= Decimal("1")


# ---------------------------------------------------------------- lookup
async def lookup(db: AsyncSession, raw: str, scope: set[uuid.UUID] | None = None) -> dict:
    number = parse_number(raw)
    row = await db.get(Label, number)
    if row is None:
        raise NotFound(f"label {number} was never printed by Sribees Supply")
    rng = await db.get(LabelRange, row.range_id)
    out = {"number": number, "status": row.status, "range_no": rng.range_no,
           "linked_at": row.linked_at, "used_at": row.used_at, "used_ref": row.used_ref,
           "batch": None}
    if row.batch_id is not None:
        batch = await db.get(StockBatch, row.batch_id)
        if scope is not None and batch.branch_id not in scope:
            raise NotFound(f"label {number} belongs to another branch")
        product = await db.get(Product, batch.product_id)
        out["batch"] = {"batch_id": batch.batch_id, "batch_no": batch.batch_no,
                        "branch_id": batch.branch_id, "product_id": product.product_id,
                        "product_name": product.name, "unit": product.unit,
                        "received_date": batch.received_date,
                        "expiry_date": batch.expiry_date, "qty_on_hand": batch.qty_on_hand,
                        "status": batch.status}
    return out
