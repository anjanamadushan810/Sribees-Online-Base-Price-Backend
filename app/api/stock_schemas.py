"""Stock request and response bodies, moved from Finance's schemas with the stock code
(joint-go-live plan, phase B). Money travels as strings so no float ever touches it."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

Money = Annotated[Decimal, PlainSerializer(lambda v: f"{v:.2f}", return_type=str)]
Qty = Annotated[Decimal, PlainSerializer(lambda v: f"{v.normalize():f}", return_type=str)]
QtyIn = Annotated[Decimal, Field(ge=0, max_digits=14, decimal_places=3)]
PriceIn = Annotated[Decimal, Field(ge=0, max_digits=14, decimal_places=2)]


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)

class SettingOut(ORM):
    key: str
    value: Money
    description: str
    updated_at: datetime

class SettingIn(BaseModel):
    value: PriceIn

class ProductOut(ORM):
    product_id: uuid.UUID
    sku: str | None
    name: str
    unit: str
    track_expiry: bool
    is_active: bool
    stock_mode: str
    parent_product_id: uuid.UUID | None
    weight_grams: int | None
    online_cost: Decimal | None
    kind: str
    weight_kg: Decimal | None
    category: str | None

class ProductIn(BaseModel):
    # the SRIBEESonline product id for an item we sell; Finance makes one for its own
    # bulk and material items
    product_id: uuid.UUID | None = None
    sku: str | None = Field(default=None, max_length=100)
    name: str = Field(min_length=2, max_length=255)
    unit: Literal["each", "kg"] = "each"
    track_expiry: bool = False
    kind: Literal["sellable", "bulk", "material"] = "sellable"
    weight_kg: Decimal | None = Field(default=None, gt=0, max_digits=10, decimal_places=3)
    category: str | None = Field(default=None, max_length=100)

class ProductPatch(BaseModel):
    sku: str | None = Field(default=None, max_length=100)
    name: str | None = Field(default=None, min_length=2, max_length=255)
    unit: Literal["each", "kg"] | None = None
    track_expiry: bool | None = None
    is_active: bool | None = None
    weight_kg: Decimal | None = Field(default=None, gt=0, max_digits=10, decimal_places=3)
    category: str | None = Field(default=None, max_length=100)

class ReasonIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)

class BatchOut(ORM):
    batch_id: uuid.UUID
    batch_no: str
    branch_id: uuid.UUID
    product_id: uuid.UUID
    product_name: str = ""
    supplier_id: uuid.UUID | None
    received_date: date
    expiry_date: date | None
    unit_cost: Decimal
    qty_on_hand: Qty
    value_on_hand: Money
    status: str
    labelled: int = 0  # QR labels stuck on this batch (phase C)
    unit: str = "each"  # "kg" = kept by weight (a bag that can be repacked)

class BatchQtyIn(BaseModel):
    batch_id: uuid.UUID
    qty: QtyIn

class WriteOffIn(BaseModel):
    branch_id: uuid.UUID
    reason: Literal["expired", "damaged", "theft", "other"]
    adjustment_date: date
    notes: str | None = Field(default=None, max_length=2000)
    lines: list[BatchQtyIn] = Field(min_length=1, max_length=300)

class AdjustmentLineOut(ORM):
    line_no: int
    batch_id: uuid.UUID
    qty_change: Qty
    value: Money | None

class AdjustmentOut(ORM):
    adjustment_id: uuid.UUID
    adjustment_no: str
    branch_id: uuid.UUID
    kind: str
    reason: str
    adjustment_date: date
    status: str
    value: Money
    notes: str | None
    requested_by: uuid.UUID | None
    approved_by: uuid.UUID | None
    rejected_reason: str | None
    journal_id: uuid.UUID | None
    source_id: uuid.UUID | None
    lines: list[AdjustmentLineOut]

class TransferIn(BaseModel):
    from_branch_id: uuid.UUID
    to_branch_id: uuid.UUID
    shipped_date: date
    notes: str | None = Field(default=None, max_length=2000)
    lines: list[BatchQtyIn] = Field(min_length=1, max_length=300)

class TransferReceiveLineIn(BaseModel):
    transfer_line_id: uuid.UUID
    qty_received: QtyIn

class TransferReceiveIn(BaseModel):
    received_date: date
    lines: list[TransferReceiveLineIn] = Field(min_length=1, max_length=300)

class TransferLineOut(ORM):
    transfer_line_id: uuid.UUID
    line_no: int
    from_batch_id: uuid.UUID
    qty: Qty
    value: Money
    qty_received: Qty | None
    to_batch_id: uuid.UUID | None

class TransferOut(ORM):
    transfer_id: uuid.UUID
    transfer_no: str
    from_branch_id: uuid.UUID
    to_branch_id: uuid.UUID
    status: str
    shipped_date: date
    received_date: date | None
    value_shipped: Money
    value_received: Money
    notes: str | None
    adjustment_id: uuid.UUID | None
    lines: list[TransferLineOut]

class CountStartIn(BaseModel):
    branch_id: uuid.UUID
    count_date: date
    product_ids: list[uuid.UUID] | None = None
    notes: str | None = Field(default=None, max_length=2000)

class CountEntryIn(BaseModel):
    batch_id: uuid.UUID
    counted_qty: QtyIn

class CountLineOut(ORM):
    batch_id: uuid.UUID
    batch_no: str = ""
    product_id: uuid.UUID | None = None
    product_name: str = ""
    expiry_date: date | None = None
    system_qty: Qty
    counted_qty: Qty | None

class CountOut(ORM):
    count_id: uuid.UUID
    count_no: str
    branch_id: uuid.UUID
    count_date: date
    status: str
    notes: str | None
    adjustment_id: uuid.UUID | None
    lines: list[CountLineOut]


# ---------------------------------------------------------------- receiving (Supply)
class ReceiptLineIn(BaseModel):
    order_line_id: uuid.UUID
    units: int = Field(gt=0, le=1_000_000)
    free_units: int = Field(default=0, ge=0, le=1_000_000)
    weight_kg: Decimal | None = Field(default=None, gt=0, max_digits=14, decimal_places=3)
    expiry_date: date | None = None
    mfg_date: date | None = None
    mrp: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    product_id: uuid.UUID | None = None
    labels: tuple[str, ...] = Field(default=(), max_length=5000)


class ReceiptIn(BaseModel):
    received_date: date
    invoice_no: str | None = Field(default=None, max_length=60)
    invoice_total: PriceIn | None = None
    notes: str | None = Field(default=None, max_length=2000)
    lines: list[ReceiptLineIn] = Field(min_length=1, max_length=500)


class ReceiptLineOut(ORM):
    receipt_line_id: uuid.UUID
    line_no: int
    order_line_id: uuid.UUID
    product_id: uuid.UUID
    product_name: str | None = None
    units: int
    free_units: int
    qty: Qty
    unit_price: Money
    line_value: Money
    expiry_date: date | None
    mfg_date: date | None
    mrp: Money | None
    batch_id: uuid.UUID
    landed_cost: Money = 0  # share of the supplier's delivery charge (B2)


class ReceiptOut(ORM):
    receipt_id: uuid.UUID
    receipt_no: str
    order_id: uuid.UUID
    supplier_id: uuid.UUID
    branch_id: uuid.UUID
    received_date: date
    invoice_no: str | None
    invoice_total: Money | None
    notes: str | None
    total: Money
    lines: list[ReceiptLineOut] = []
    created_at: datetime


class CloseRestIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
