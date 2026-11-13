"""Finance's closed months, as Supply knows them (joint-go-live plan, phase B)."""

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import FinancePeriod
from app.services.errors import Conflict, SupplyError


async def open_period(db: AsyncSession, day: date) -> None:
    """Stock moves are dated: none may land in a month Finance has closed. (A month
    Finance has not told us about is taken as open; Finance still checks when it posts.)"""
    closed = await db.scalar(select(FinancePeriod).where(
        FinancePeriod.start_date <= day, FinancePeriod.end_date >= day,
        FinancePeriod.status == "closed").limit(1))
    if closed is not None:
        raise Conflict(f"Finance has closed the month containing {day}; date it in an open "
                       "month or ask the accountant")


async def apply_period_event(db: AsyncSession, payload: dict, period_id) -> str:
    try:
        pid = uuid.UUID(str(period_id))
        start = date.fromisoformat(payload["start_date"])
        end = date.fromisoformat(payload["end_date"])
        status = payload["status"]
    except (KeyError, TypeError, ValueError):
        raise SupplyError("a period event without its dates") from None
    if status not in ("open", "closed"):
        raise SupplyError(f"period status {status!r}")
    row = await db.get(FinancePeriod, pid, with_for_update=True)
    if row is None:
        db.add(FinancePeriod(period_id=pid, start_date=start, end_date=end, status=status))
    else:
        row.start_date, row.end_date, row.status = start, end, status
        row.updated_at = datetime.now(UTC)
    return "applied"
