"""ORM mappings of the tables created in alembic/versions. The migrations are the source of
truth; mappings list only what the code uses."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class Branch(Base):
    __tablename__ = "branches"

    branch_id: Mapped[uuid.UUID] = _uuid_pk()
    code: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(120))
    online_branch_id: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _created()


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(150))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20))
    all_branches: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    totp_secret_enc: Mapped[str | None] = mapped_column(Text)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _created()

    branches: Mapped[list[Branch]] = relationship(secondary="user_branches", lazy="selectin")


class UserBranch(Base):
    __tablename__ = "user_branches"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), primary_key=True
    )
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.branch_id"), primary_key=True
    )


class UserSession(Base):
    __tablename__ = "user_sessions"

    session_id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.user_id"))
    refresh_hash: Mapped[str] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created()
    last_used_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Supplier(Base):
    __tablename__ = "suppliers"

    supplier_id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(150))
    notes: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _created()
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id")
    )
    price_list_header: Mapped[str] = mapped_column(Text, default="")
    price_list_footer: Mapped[str] = mapped_column(Text, default="")

    users: Mapped[list["SupplierUser"]] = relationship(
        back_populates="supplier", lazy="selectin", order_by="SupplierUser.created_at"
    )


class SupplierUser(Base):
    __tablename__ = "supplier_users"

    supplier_user_id: Mapped[uuid.UUID] = _uuid_pk()
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("suppliers.supplier_id")
    )
    phone: Mapped[str] = mapped_column(String(12))
    full_name: Mapped[str] = mapped_column(String(150))
    language: Mapped[str] = mapped_column(String(2), default="si")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()

    supplier: Mapped[Supplier] = relationship(back_populates="users", lazy="selectin")


class SupplierOtp(Base):
    __tablename__ = "supplier_otps"

    otp_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    phone: Mapped[str] = mapped_column(String(12))
    supplier_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("supplier_users.supplier_user_id")
    )
    code_hash: Mapped[str | None] = mapped_column(String(64))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SupplierSession(Base):
    __tablename__ = "supplier_sessions"

    session_id: Mapped[uuid.UUID] = _uuid_pk()
    supplier_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("supplier_users.supplier_user_id")
    )
    refresh_hash: Mapped[str] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created()
    last_used_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    __tablename__ = "audit_log"

    audit_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    occurred_at: Mapped[datetime] = _created()
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id")
    )
    supplier_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("supplier_users.supplier_user_id")
    )
    action: Mapped[str] = mapped_column(String(60))
    entity: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)
    ip_address: Mapped[str | None] = mapped_column(String(64))


# ---------------------------------------------------------------- supplier catalogue (0002)
class SupplierSection(Base):
    __tablename__ = "supplier_sections"

    section_id: Mapped[uuid.UUID] = _uuid_pk()
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("suppliers.supplier_id")
    )
    title: Mapped[str] = mapped_column(String(150))
    notes: Mapped[list] = mapped_column(JSONB, default=list)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SupplierProduct(Base):
    __tablename__ = "supplier_products"

    product_id: Mapped[uuid.UUID] = _uuid_pk()
    supplier_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    section_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    heading: Mapped[str] = mapped_column(String(200))
    notes_before: Mapped[list] = mapped_column(JSONB, default=list)
    notes_after: Mapped[list] = mapped_column(JSONB, default=list)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SupplierOffer(Base):
    __tablename__ = "supplier_offers"

    offer_id: Mapped[uuid.UUID] = _uuid_pk()
    supplier_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    label: Mapped[str] = mapped_column(String(120), default="")
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    mrp: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    units_per_box: Mapped[int | None] = mapped_column(Integer)
    extra: Mapped[list] = mapped_column(JSONB, default=list)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SupplierOfferPrice(Base):
    __tablename__ = "supplier_offer_prices"

    history_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    offer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    supplier_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    mrp: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    units_per_box: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(10))
    supplier_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    changed_at: Mapped[datetime] = _created()


# ---------------------------------------------------------------- matching (0003)
class OnlineProduct(Base):
    __tablename__ = "online_products"

    online_product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    sku: Mapped[str | None] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(255))
    name_si: Mapped[str | None] = mapped_column(String(255))
    display_size: Mapped[str | None] = mapped_column(String(80))
    display_size_unit: Mapped[str | None] = mapped_column(String(32))
    size_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    size_kind: Mapped[str | None] = mapped_column(String(8))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    synced_at: Mapped[datetime] = _created()


class OnlineVariant(Base):
    __tablename__ = "online_variants"

    online_variant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    online_product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("online_products.online_product_id"))
    sku: Mapped[str | None] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(120), default="")
    weight_grams: Mapped[int | None] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class OfferMatch(Base):
    __tablename__ = "offer_matches"

    match_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    offer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    supplier_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    online_product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("online_products.online_product_id"))
    online_variant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("online_variants.online_variant_id"))
    status: Mapped[str] = mapped_column(String(10))
    score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()


class SyncState(Base):
    __tablename__ = "sync_state"

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
