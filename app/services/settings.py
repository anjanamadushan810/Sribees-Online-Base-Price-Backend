"""Owner-editable thresholds (approval limits, price tolerance) and document numbers."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DocCounter, Setting
from app.services import audit
from app.services.errors import NotFound, SupplyError

PO_APPROVAL_LIMIT = "po_approval_limit"
EXPENSE_APPROVAL_LIMIT = "expense_approval_limit"
WRITEOFF_APPROVAL_LIMIT = "writeoff_approval_limit"
INVOICE_PRICE_TOLERANCE = "invoice_price_tolerance"
# percentages: below 100 (a 100% margin or waste has no meaning)
PERCENT_KEYS = ("min_margin_pct", "repack_normal_waste_pct")


async def get_value(db: AsyncSession, key: str) -> Decimal:
    value = await db.scalar(select(Setting.value).where(Setting.key == key))
    if value is None:
        raise NotFound(f"setting {key} does not exist")
    return value


async def set_value(
    db: AsyncSession, *, key: str, value: Decimal, user_id: uuid.UUID, ip_address: str | None
) -> Setting:
    setting = await db.get(Setting, key, with_for_update=True)
    if setting is None:
        raise NotFound(f"setting {key} does not exist")
    if key in PERCENT_KEYS and not (0 <= value < 100):
        raise SupplyError(f"{key} is a percentage from 0 up to (not including) 100")
    before = str(setting.value)
    setting.value = value
    setting.updated_by = user_id
    setting.updated_at = datetime.now(UTC)
    await db.flush()
    await audit.record(db, user_id=user_id, action="setting.update", entity="setting",
                       entity_id=key, before={"value": before}, after={"value": str(value)},
                       ip_address=ip_address)
    return setting


async def next_doc_no(db: AsyncSession, prefix: str) -> str:
    """PO-000001, GRN-000001, ... The counter row is locked until the transaction ends,
    so two users never get the same number."""
    stmt = insert(DocCounter).values(prefix=prefix, next_no=2)
    stmt = stmt.on_conflict_do_update(
        index_elements=[DocCounter.prefix], set_={"next_no": DocCounter.next_no + 1}
    ).returning(DocCounter.next_no - 1)
    n = await db.scalar(stmt)
    return f"{prefix}-{n:06d}"
