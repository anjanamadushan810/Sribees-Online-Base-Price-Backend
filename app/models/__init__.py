"""ORM mappings of the tables created in alembic/versions. The migrations are the source of
truth; mappings list only what the code uses."""

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    Date,
    DateTime,
    FetchedValue,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
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
    online_branch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_head_office: Mapped[bool] = mapped_column(Boolean, default=False)  # 0012
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
    # Master data Finance needs, entered once here (migration 0010).
    code: Mapped[str] = mapped_column(String(20))
    contact_name: Mapped[str | None] = mapped_column(String(150))
    phone: Mapped[str | None] = mapped_column(String(20))
    email: Mapped[str | None] = mapped_column(String(255))
    address: Mapped[str | None] = mapped_column(Text)
    credit_days: Mapped[int] = mapped_column(Integer, default=0)
    is_related_party: Mapped[bool] = mapped_column(Boolean, default=False)
    bank_name: Mapped[str | None] = mapped_column(String(100))
    bank_branch: Mapped[str | None] = mapped_column(String(100))
    bank_account_name: Mapped[str | None] = mapped_column(String(150))
    bank_account_no: Mapped[str | None] = mapped_column(String(40))

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
    # Email + password sign-in (migration 0011); the hash is argon2, never the password.
    email: Mapped[str | None] = mapped_column(String(255))
    password_hash: Mapped[str | None] = mapped_column(Text)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

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
    # Signed in with an email + password (not an SMS code): a temporary password applies.
    via_password: Mapped[bool] = mapped_column(Boolean, default=False)


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
    held_reason: Mapped[str | None] = mapped_column(String(30))
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


# ---------------------------------------------------------------- orders (0004)
class SupplyOrder(Base):
    __tablename__ = "supply_orders"

    order_id: Mapped[uuid.UUID] = _uuid_pk()
    order_no: Mapped[str] = mapped_column(String(20), server_default=FetchedValue())
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("suppliers.supplier_id"))
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.branch_id"))
    status: Mapped[str] = mapped_column(String(10), default="draft")
    note: Mapped[str | None] = mapped_column(Text)
    delivery_charge: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    supplier_note: Mapped[str | None] = mapped_column(Text)
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    cancel_note: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    submitted_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _created()
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    supplier_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answered_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # 0006: Finance's answer (ORDER_APPROVAL=finance)
    finance_po_no: Mapped[str | None] = mapped_column(String(20))
    reject_reason: Mapped[str | None] = mapped_column(Text)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by_name: Mapped[str | None] = mapped_column(String(150))
    # 0012: back-order closed by hand (decision 31)
    rest_closed_reason: Mapped[str | None] = mapped_column(Text)
    rest_closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rest_closed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    lines: Mapped[list["SupplyOrderLine"]] = relationship(
        lazy="selectin", order_by="SupplyOrderLine.heading", cascade="all, delete-orphan")


class SupplyOrderLine(Base):
    __tablename__ = "supply_order_lines"

    line_id: Mapped[uuid.UUID] = _uuid_pk()
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("supply_orders.order_id"))
    supplier_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    offer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    online_product_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    online_variant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    heading: Mapped[str] = mapped_column(String(200))
    label: Mapped[str] = mapped_column(String(120), default="")
    boxes: Mapped[int] = mapped_column(Integer)
    units_per_box: Mapped[int] = mapped_column(Integer)
    units: Mapped[int] = mapped_column(Integer, Computed("boxes * units_per_box"))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    mrp: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    line_total: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), Computed("boxes * units_per_box * unit_price"))


class BasePrice(Base):
    __tablename__ = "base_prices"

    base_price_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    online_product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    online_variant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    supplier_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    order_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    line_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    mrp: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    recorded_at: Mapped[datetime] = _created()
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OutboxEvent(Base):
    __tablename__ = "supply_outbox"

    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created()
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), server_default=FetchedValue())


class FeedState(Base):
    __tablename__ = "feed_state"

    feed: Mapped[str] = mapped_column(String(20), primary_key=True)
    last_seq: Mapped[int] = mapped_column(BigInteger, default=0)
    gaps: Mapped[dict] = mapped_column(JSONB, default=dict)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = _created()


class ProcessedEvent(Base):
    __tablename__ = "processed_events"

    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    feed: Mapped[str] = mapped_column(String(20))
    seq: Mapped[int] = mapped_column(BigInteger)
    event_type: Mapped[str] = mapped_column(String(40))
    outcome: Mapped[str] = mapped_column(String(12))
    processed_at: Mapped[datetime] = _created()


class SupplierDevice(Base):
    """0007: a phone's FCM token, tied to the supplier session that registered it."""

    __tablename__ = "supplier_devices"

    token: Mapped[str] = mapped_column(String(400), primary_key=True)
    supplier_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("supplier_users.supplier_user_id"))
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("supplier_sessions.session_id"))
    platform: Mapped[str] = mapped_column(String(20), default="android")
    created_at: Mapped[datetime] = _created()
    last_seen_at: Mapped[datetime] = _created()


# ======================================================================= stock (0012)
# Moved from Finance with the same columns (joint-go-live plan, phase B). journal_id
# columns hold the id of the journal.requested event Finance posts from.
class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    description: Mapped[str] = mapped_column(String(300))
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id")
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DocCounter(Base):
    __tablename__ = "doc_counters"

    prefix: Mapped[str] = mapped_column(String(10), primary_key=True)
    next_no: Mapped[int] = mapped_column(Integer, default=1)


class Product(Base):
    __tablename__ = "products"

    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    sku: Mapped[str | None] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(255))
    unit: Mapped[str] = mapped_column(String(10), default="each")
    track_expiry: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    category: Mapped[str | None] = mapped_column(String(100))
    # 0014: unit / loose (kg) / packed (this row is one pack size of parent_product_id)
    stock_mode: Mapped[str] = mapped_column(
        String(8),
        default=lambda ctx: "loose" if ctx.get_current_parameters().get("unit") == "kg" else "unit",
    )
    parent_product_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.product_id")
    )
    weight_grams: Mapped[int | None] = mapped_column(Integer)
    online_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    # 0016: sellable (Online's) / bulk (repacked only, kg) / material (bags, labels)
    kind: Mapped[str] = mapped_column(String(10), default="sellable")
    weight_kg: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))




class StockBatch(Base):
    __tablename__ = "stock_batches"

    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    batch_no: Mapped[str] = mapped_column(String(30))
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.branch_id")
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.product_id")
    )
    supplier_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("suppliers.supplier_id")
    )
    received_date: Mapped[date] = mapped_column(Date)
    expiry_date: Mapped[date | None] = mapped_column(Date)
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    qty_received: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    qty_on_hand: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=Decimal("0"))
    value_on_hand: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    status: Mapped[str] = mapped_column(String(12), default="available")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())




class StockMove(Base):
    __tablename__ = "stock_moves"

    move_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stock_batches.batch_id")
    )
    move_date: Mapped[date] = mapped_column(Date)
    move_type: Mapped[str] = mapped_column(String(20))
    qty: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    value: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    doc_type: Mapped[str] = mapped_column(String(20))
    doc_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    journal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True)
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())




class StockTransfer(Base):
    __tablename__ = "stock_transfers"

    transfer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    transfer_no: Mapped[str] = mapped_column(String(20))
    from_branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.branch_id")
    )
    to_branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.branch_id")
    )
    status: Mapped[str] = mapped_column(String(20))
    shipped_date: Mapped[date] = mapped_column(Date)
    received_date: Mapped[date | None] = mapped_column(Date)
    value_shipped: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    value_received: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    notes: Mapped[str | None] = mapped_column(Text)
    shipped_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id")
    )
    received_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id")
    )
    ship_journal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True)
    )
    receive_journal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True)
    )
    adjustment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stock_adjustments.adjustment_id")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    lines: Mapped[list["TransferLine"]] = relationship(
        lazy="selectin", order_by="TransferLine.line_no"
    )


class TransferLine(Base):
    __tablename__ = "transfer_lines"

    transfer_line_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    transfer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stock_transfers.transfer_id")
    )
    line_no: Mapped[int] = mapped_column(SmallInteger)
    from_batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stock_batches.batch_id")
    )
    qty: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    value: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    qty_received: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    to_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stock_batches.batch_id")
    )


class StockAdjustment(Base):
    __tablename__ = "stock_adjustments"

    adjustment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    adjustment_no: Mapped[str] = mapped_column(String(20))
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.branch_id")
    )
    kind: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(String(20))
    adjustment_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20))
    value: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    notes: Mapped[str | None] = mapped_column(Text)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id")
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id")
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_reason: Mapped[str | None] = mapped_column(String(500))
    journal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True)
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    lines: Mapped[list["AdjustmentLine"]] = relationship(
        lazy="selectin", order_by="AdjustmentLine.line_no"
    )


class AdjustmentLine(Base):
    __tablename__ = "adjustment_lines"

    adjustment_line_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    adjustment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stock_adjustments.adjustment_id")
    )
    line_no: Mapped[int] = mapped_column(SmallInteger)
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stock_batches.batch_id")
    )
    qty_change: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    value: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))


class StockCount(Base):
    __tablename__ = "stock_counts"

    count_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    count_no: Mapped[str] = mapped_column(String(20))
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.branch_id")
    )
    count_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20))
    notes: Mapped[str | None] = mapped_column(Text)
    started_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id")
    )
    submitted_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id")
    )
    adjustment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stock_adjustments.adjustment_id")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    lines: Mapped[list["CountLine"]] = relationship(lazy="selectin")


class CountLine(Base):
    __tablename__ = "count_lines"

    count_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stock_counts.count_id"), primary_key=True
    )
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stock_batches.batch_id"), primary_key=True
    )
    system_qty: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    counted_qty: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))


# ---------------------------------------------------------------- online feed (0007)


class Receipt(Base):
    """0012: goods received against an order (replaces Finance's GRN)."""

    __tablename__ = "receipts"

    receipt_id: Mapped[uuid.UUID] = _uuid_pk()
    receipt_no: Mapped[str] = mapped_column(String(20))
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("supply_orders.order_id"))
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("suppliers.supplier_id"))
    branch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.branch_id"))
    received_date: Mapped[date] = mapped_column(Date)
    invoice_no: Mapped[str | None] = mapped_column(String(60))
    invoice_total: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    notes: Mapped[str | None] = mapped_column(Text)
    total: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    journal_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    received_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"))
    created_at: Mapped[datetime] = _created()

    lines: Mapped[list["ReceiptLine"]] = relationship(
        lazy="selectin", order_by="ReceiptLine.line_no")


class ReceiptLine(Base):
    __tablename__ = "receipt_lines"

    receipt_line_id: Mapped[uuid.UUID] = _uuid_pk()
    receipt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("receipts.receipt_id"))
    line_no: Mapped[int] = mapped_column(SmallInteger)
    order_line_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("supply_order_lines.line_id"))
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.product_id"))
    units: Mapped[int] = mapped_column(Integer)
    free_units: Mapped[int] = mapped_column(Integer, default=0)
    qty: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    line_value: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    expiry_date: Mapped[date | None] = mapped_column(Date)
    mfg_date: Mapped[date | None] = mapped_column(Date)
    mrp: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stock_batches.batch_id"))


class FinancePeriod(Base):
    """0013: a Finance month and whether it is closed (from Finance's events)."""

    __tablename__ = "finance_periods"

    period_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(10))
    updated_at: Mapped[datetime] = _created()


# ======================================================================= QR labels (0014)
class LabelRange(Base):
    """A printed run of label numbers (one PDF of A4 sticker sheets)."""

    __tablename__ = "label_ranges"

    range_id: Mapped[uuid.UUID] = _uuid_pk()
    range_no: Mapped[str] = mapped_column(String(20))
    first_body: Mapped[int] = mapped_column(BigInteger)
    last_body: Mapped[int] = mapped_column(BigInteger)
    count: Mapped[int] = mapped_column(Integer)
    branch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("branches.branch_id"))
    cols: Mapped[int] = mapped_column(Integer)
    rows: Mapped[int] = mapped_column(Integer)
    printed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"))
    created_at: Mapped[datetime] = _created()


class Label(Base):
    """One sticker: printed -> linked (to one batch, for ever) -> used (packed) / void."""

    __tablename__ = "labels"

    number: Mapped[str] = mapped_column(String(10), primary_key=True)
    range_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("label_ranges.range_id"))
    status: Mapped[str] = mapped_column(String(8), default="printed")
    batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stock_batches.batch_id"))
    linked_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"))
    linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    used_ref: Mapped[str | None] = mapped_column(String(80))
