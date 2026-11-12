"""Request and response bodies. Money travels as strings in JSON so no float ever touches it."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, PlainSerializer

Money = Annotated[Decimal, PlainSerializer(lambda v: f"{v:.2f}", return_type=str)]
Role = Literal["owner", "purchasing", "branch_manager", "accountant"]
Language = Literal["si", "en"]


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------- staff auth
class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=200)


class TicketIn(BaseModel):
    ticket: str


class TicketCodeIn(BaseModel):
    ticket: str
    code: str = Field(min_length=6, max_length=8)


class RefreshIn(BaseModel):
    refresh_token: str


class TokensOut(BaseModel):
    status: Literal["ok"] = "ok"
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105
    expires_in: int


class StepOut(BaseModel):
    status: Literal["totp_required", "totp_setup_required"]
    ticket: str


class TotpSetupOut(BaseModel):
    secret: str
    otpauth_uri: str


class SessionOut(ORM):
    session_id: uuid.UUID
    user_agent: str | None
    ip_address: str | None
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime
    current: bool = False


class BranchOut(ORM):
    branch_id: uuid.UUID
    code: str
    name: str
    online_branch_id: int | None
    is_active: bool


class BranchIn(BaseModel):
    code: str = Field(min_length=1, max_length=20)
    name: str = Field(min_length=2, max_length=120)
    online_branch_id: int | None = None


class UserOut(ORM):
    user_id: uuid.UUID
    email: str
    full_name: str
    role: str
    all_branches: bool
    is_active: bool
    totp_enabled: bool
    last_login_at: datetime | None
    branches: list[BranchOut]


class MeOut(UserOut):
    permissions: list[str]
    two_factor: bool = True


class UserIn(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=2, max_length=150)
    role: Role
    password: str = Field(min_length=12, max_length=200)
    branch_ids: list[uuid.UUID] = []
    all_branches: bool = False


# ---------------------------------------------------------------- suppliers (staff side)
class SupplierUserOut(ORM):
    supplier_user_id: uuid.UUID
    phone: str
    full_name: str
    language: str
    is_active: bool
    last_login_at: datetime | None


class SupplierOut(ORM):
    supplier_id: uuid.UUID
    name: str
    notes: str | None
    is_active: bool
    created_at: datetime
    users: list[SupplierUserOut]


class SupplierIn(BaseModel):
    name: str = Field(min_length=2, max_length=150)
    notes: str | None = Field(default=None, max_length=2000)


class SupplierPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=150)
    notes: str | None = Field(default=None, max_length=2000)
    is_active: bool | None = None


class SupplierUserIn(BaseModel):
    phone: str = Field(min_length=9, max_length=20)
    full_name: str = Field(min_length=2, max_length=150)
    language: Language = "si"


class SupplierUserPatch(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=150)
    language: Language | None = None
    is_active: bool | None = None


# ---------------------------------------------------------------- supplier app
class PhoneIn(BaseModel):
    phone: str = Field(min_length=9, max_length=20)


class PhoneCodeIn(BaseModel):
    phone: str = Field(min_length=9, max_length=20)
    code: str = Field(min_length=6, max_length=6, pattern=r"^\d{6}$")


class CodeSentOut(BaseModel):
    # Same answer for every number: never reveals whether a number is registered.
    status: Literal["sent_if_registered"] = "sent_if_registered"
    resend_after_seconds: int


class SupplierMeOut(BaseModel):
    supplier_user_id: uuid.UUID
    full_name: str
    phone: str
    language: str
    supplier_id: uuid.UUID
    supplier_name: str


# ---------------------------------------------------------------- supplier catalogue
NoteList = list[Annotated[str, Field(max_length=300)]]


class OfferOut(ORM):
    offer_id: uuid.UUID
    label: str
    price: Money
    mrp: Money | None
    units_per_box: int | None
    extra: list[str]
    is_active: bool


class CatalogProductOut(ORM):
    product_id: uuid.UUID
    heading: str
    notes_before: list[str]
    notes_after: list[str]
    is_active: bool
    offers: list[OfferOut] = []


class CatalogSectionOut(ORM):
    section_id: uuid.UUID
    title: str
    notes: list[str]
    is_active: bool
    products: list[CatalogProductOut] = []


class CatalogOut(BaseModel):
    supplier_id: uuid.UUID
    supplier_name: str
    header: str
    footer: str
    sections: list[CatalogSectionOut]


class PasteIn(BaseModel):
    text: str = Field(min_length=1, max_length=200_000)


class ResolutionIn(BaseModel):
    line_no: int = Field(ge=1)
    skip: bool = False
    price: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    mrp: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    units_per_box: int | None = Field(default=None, gt=0, le=100_000)
    label: str | None = Field(default=None, max_length=120)


class ImportIn(PasteIn):
    resolutions: list[ResolutionIn] = []
    use_header_footer: bool = True


class FlaggedLineOut(BaseModel):
    line_no: int
    raw: str
    heading: str
    section: str
    flags: list[str]
    price: Money
    mrp: Money | None
    units_per_box: int | None
    label: str
    extra: list[str]


class ChangedOfferOut(BaseModel):
    line_no: int
    heading: str
    label: str
    old_price: Money
    new_price: Money
    old_mrp: Money | None
    new_mrp: Money | None
    old_units: int | None
    new_units: int | None


class ImportPreviewOut(BaseModel):
    sections: int
    products: int
    offers: int
    new_sections: int
    new_products: int
    new_offers: int
    changed_offers: list[ChangedOfferOut]
    same_offers: int
    flagged: list[FlaggedLineOut]
    reshaped: list[dict] = []
    has_header: bool
    has_footer: bool


class ImportResultOut(BaseModel):
    sections_new: int
    products_new: int
    offers_new: int
    offers_changed: int
    offers_same: int
    offers_skipped: int
    not_in_paste: int


class TemplateIn(BaseModel):
    header: str = Field(default="", max_length=5000)
    footer: str = Field(default="", max_length=5000)


class MessageOut(BaseModel):
    text: str


class SectionIn(BaseModel):
    title: str = Field(min_length=1, max_length=150)


class SectionPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=150)
    notes: NoteList | None = None
    is_active: bool | None = None


class ProductIn(BaseModel):
    section_id: uuid.UUID
    heading: str = Field(min_length=1, max_length=200)
    notes_before: NoteList = []
    notes_after: NoteList = []


class ProductPatch(BaseModel):
    section_id: uuid.UUID | None = None
    heading: str | None = Field(default=None, min_length=1, max_length=200)
    notes_before: NoteList | None = None
    notes_after: NoteList | None = None
    is_active: bool | None = None


class OfferIn(BaseModel):
    label: str = Field(default="", max_length=120)
    price: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    mrp: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    units_per_box: int | None = Field(default=None, gt=0, le=100_000)


class OfferPatch(BaseModel):
    label: str | None = Field(default=None, max_length=120)
    price: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    mrp: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    units_per_box: int | None = Field(default=None, gt=0, le=100_000)
    is_active: bool | None = None


class OfferPriceOut(ORM):
    price: Money
    mrp: Money | None
    units_per_box: int | None
    source: str
    changed_at: datetime
