"""Supplier-app sign-in and profile. Mounted under /supplier so a gateway rule or a log
filter can tell supplier traffic from staff traffic at a glance."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import SupplierPrincipal, client_ip, current_supplier
from app.api.schemas import (
    CodeSentOut,
    PhoneCodeIn,
    PhoneIn,
    RefreshIn,
    SupplierMeOut,
    TokensOut,
)
from app.core.config import get_settings
from app.core.db import get_db
from app.models import Supplier
from app.services import supplier_auth
from app.services.errors import SupplyError

router = APIRouter(prefix="/supplier", tags=["supplier app"])


async def _keep_failure(db: AsyncSession, call):
    """A wrong code must still count towards the code's attempts: commit, then raise."""
    try:
        return await call
    except SupplyError:
        await db.commit()
        raise


def _out(t: supplier_auth.SupplierTokens) -> TokensOut:
    return TokensOut(access_token=t.access_token, refresh_token=t.refresh_token,
                     expires_in=t.expires_in)


@router.post("/auth/request-code", response_model=CodeSentOut)
async def request_code(body: PhoneIn, request: Request, db: AsyncSession = Depends(get_db)):
    await supplier_auth.request_code(db, phone=body.phone, ip=client_ip(request))
    return CodeSentOut(resend_after_seconds=get_settings().otp_resend_seconds)


@router.post("/auth/verify", response_model=TokensOut)
async def verify(body: PhoneCodeIn, request: Request, db: AsyncSession = Depends(get_db)):
    return _out(await _keep_failure(db, supplier_auth.verify_code(
        db, phone=body.phone, code=body.code, ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )))


@router.post("/auth/refresh", response_model=TokensOut)
async def refresh(body: RefreshIn, db: AsyncSession = Depends(get_db)):
    return _out(await supplier_auth.refresh(db, refresh_token=body.refresh_token))


@router.post("/auth/logout", status_code=204)
async def logout(p: SupplierPrincipal = Depends(current_supplier),
                 db: AsyncSession = Depends(get_db)):
    await supplier_auth.revoke_session(db, p.session_id)


@router.get("/me", response_model=SupplierMeOut)
async def me(p: SupplierPrincipal = Depends(current_supplier),
             db: AsyncSession = Depends(get_db)):
    supplier = await db.get(Supplier, p.supplier_id)
    return SupplierMeOut(
        supplier_user_id=p.user.supplier_user_id, full_name=p.user.full_name,
        phone=p.user.phone, language=p.user.language, supplier_id=p.supplier_id,
        supplier_name=supplier.name,
    )
