"""Who does a staff action (moved from Finance's purchasing module with the stock code)."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class Actor:
    user_id: uuid.UUID
    role: str
    ip_address: str | None = None


def _now() -> datetime:
    return datetime.now(UTC)
