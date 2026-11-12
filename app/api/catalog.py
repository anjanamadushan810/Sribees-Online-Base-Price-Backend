"""Supplier catalogue endpoints.

/supplier/catalog/...                 the supplier app; supplier id from the token only
/suppliers/{supplier_id}/catalog/...  staff (purchasing/owner) reading, or onboarding a
                                      supplier on their behalf (import, template)
"""

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, SupplierPrincipal, current_supplier, require
from app.api.schemas import (
    CatalogOut,
    CatalogProductOut,
    CatalogSectionOut,
    HeldOfferOut,
    ImportIn,
    ImportPreviewOut,
    ImportResultOut,
    MessageOut,
    OfferIn,
    OfferOut,
    OfferPatch,
    OfferPriceOut,
    PasteIn,
    ProductIn,
    ProductPatch,
    SectionIn,
    SectionPatch,
    TemplateIn,
)
from app.core.db import get_db
from app.services import catalog
from app.services.catalog import Actor, Resolution

router = APIRouter(tags=["catalogue"])


def _actor(p: SupplierPrincipal) -> Actor:
    return Actor(supplier_user_id=p.user.supplier_user_id)


async def _catalog_out(db: AsyncSession, supplier_id: uuid.UUID) -> CatalogOut:
    supplier, sections, by_section, by_product = await catalog.load(db, supplier_id)
    out = []
    for s in sections:
        sec = CatalogSectionOut.model_validate(s)
        for p in by_section[s.section_id]:
            prod = CatalogProductOut.model_validate(p)
            prod.offers = [OfferOut.model_validate(o) for o in by_product[p.product_id]]
            sec.products.append(prod)
        out.append(sec)
    return CatalogOut(supplier_id=supplier.supplier_id, supplier_name=supplier.name,
                      header=supplier.price_list_header, footer=supplier.price_list_footer,
                      sections=out)


def _resolutions(body: ImportIn) -> dict[int, Resolution]:
    return {r.line_no: Resolution(skip=r.skip, price=r.price, mrp=r.mrp,
                                  units_per_box=r.units_per_box, label=r.label)
            for r in body.resolutions}


# ================================================================= supplier app
@router.get("/supplier/catalog", response_model=CatalogOut)
async def my_catalog(p: SupplierPrincipal = Depends(current_supplier),
                     db: AsyncSession = Depends(get_db)):
    return await _catalog_out(db, p.supplier_id)


@router.post("/supplier/catalog/import/preview", response_model=ImportPreviewOut)
async def my_import_preview(body: PasteIn, p: SupplierPrincipal = Depends(current_supplier),
                            db: AsyncSession = Depends(get_db)):
    return await catalog.preview(db, p.supplier_id, body.text)


@router.post("/supplier/catalog/import", response_model=ImportResultOut)
async def my_import(body: ImportIn, p: SupplierPrincipal = Depends(current_supplier),
                    db: AsyncSession = Depends(get_db)):
    result = await catalog.import_text(db, p.supplier_id, body.text,
                                       resolutions=_resolutions(body), actor=_actor(p),
                                       use_header_footer=body.use_header_footer)
    return ImportResultOut(**{k: v for k, v in result.__dict__.items() if k != "changed"})


@router.get("/supplier/catalog/message", response_model=MessageOut)
async def my_message(p: SupplierPrincipal = Depends(current_supplier),
                     db: AsyncSession = Depends(get_db)):
    return MessageOut(text=await catalog.message(db, p.supplier_id))


@router.put("/supplier/catalog/template", status_code=204)
async def my_template(body: TemplateIn, p: SupplierPrincipal = Depends(current_supplier),
                      db: AsyncSession = Depends(get_db)):
    await catalog.set_template(db, p.supplier_id, header=body.header, footer=body.footer,
                               actor=_actor(p))


@router.post("/supplier/catalog/sections", response_model=CatalogSectionOut, status_code=201)
async def my_add_section(body: SectionIn, p: SupplierPrincipal = Depends(current_supplier),
                         db: AsyncSession = Depends(get_db)):
    return await catalog.add_section(db, p.supplier_id, title=body.title, actor=_actor(p))


@router.patch("/supplier/catalog/sections/{section_id}", response_model=CatalogSectionOut)
async def my_update_section(section_id: uuid.UUID, body: SectionPatch,
                            p: SupplierPrincipal = Depends(current_supplier),
                            db: AsyncSession = Depends(get_db)):
    return await catalog.update_section(db, p.supplier_id, section_id,
                                        changes=body.model_dump(exclude_unset=True),
                                        actor=_actor(p))


@router.post("/supplier/catalog/products", response_model=CatalogProductOut, status_code=201)
async def my_add_product(body: ProductIn, p: SupplierPrincipal = Depends(current_supplier),
                         db: AsyncSession = Depends(get_db)):
    return await catalog.add_product(db, p.supplier_id, section_id=body.section_id,
                                     heading=body.heading, notes_before=body.notes_before,
                                     notes_after=body.notes_after, actor=_actor(p))


@router.patch("/supplier/catalog/products/{product_id}", response_model=CatalogProductOut)
async def my_update_product(product_id: uuid.UUID, body: ProductPatch,
                            p: SupplierPrincipal = Depends(current_supplier),
                            db: AsyncSession = Depends(get_db)):
    return await catalog.update_product(db, p.supplier_id, product_id,
                                        changes=body.model_dump(exclude_unset=True),
                                        actor=_actor(p))


@router.post("/supplier/catalog/products/{product_id}/offers", response_model=OfferOut,
             status_code=201)
async def my_add_offer(product_id: uuid.UUID, body: OfferIn,
                       p: SupplierPrincipal = Depends(current_supplier),
                       db: AsyncSession = Depends(get_db)):
    return await catalog.add_offer(db, p.supplier_id, product_id, label=body.label,
                                   price=body.price, mrp=body.mrp,
                                   units_per_box=body.units_per_box, actor=_actor(p))


@router.patch("/supplier/catalog/offers/{offer_id}", response_model=OfferOut)
async def my_update_offer(offer_id: uuid.UUID, body: OfferPatch,
                          p: SupplierPrincipal = Depends(current_supplier),
                          db: AsyncSession = Depends(get_db)):
    return await catalog.update_offer(db, p.supplier_id, offer_id,
                                      changes=body.model_dump(exclude_unset=True),
                                      actor=_actor(p))


@router.get("/supplier/catalog/offers/{offer_id}/history", response_model=list[OfferPriceOut])
async def my_offer_history(offer_id: uuid.UUID,
                           p: SupplierPrincipal = Depends(current_supplier),
                           db: AsyncSession = Depends(get_db)):
    return await catalog.offer_history(db, p.supplier_id, offer_id)


# ================================================================= staff
@router.get("/suppliers/{supplier_id}/catalog", response_model=CatalogOut)
async def supplier_catalog(supplier_id: uuid.UUID,
                           _: Principal = Depends(require("suppliers.read")),
                           db: AsyncSession = Depends(get_db)):
    return await _catalog_out(db, supplier_id)


@router.get("/suppliers/{supplier_id}/catalog/message", response_model=MessageOut)
async def supplier_message(supplier_id: uuid.UUID,
                           _: Principal = Depends(require("suppliers.read")),
                           db: AsyncSession = Depends(get_db)):
    return MessageOut(text=await catalog.message(db, supplier_id))


@router.get("/suppliers/{supplier_id}/catalog/offers/{offer_id}/history",
            response_model=list[OfferPriceOut])
async def supplier_offer_history(supplier_id: uuid.UUID, offer_id: uuid.UUID,
                                 _: Principal = Depends(require("suppliers.read")),
                                 db: AsyncSession = Depends(get_db)):
    return await catalog.offer_history(db, supplier_id, offer_id)


@router.post("/suppliers/{supplier_id}/catalog/import/preview",
             response_model=ImportPreviewOut)
async def supplier_import_preview(supplier_id: uuid.UUID, body: PasteIn,
                                  _: Principal = Depends(require("suppliers.write")),
                                  db: AsyncSession = Depends(get_db)):
    return await catalog.preview(db, supplier_id, body.text)


@router.post("/suppliers/{supplier_id}/catalog/import", response_model=ImportResultOut)
async def supplier_import(supplier_id: uuid.UUID, body: ImportIn,
                          p: Principal = Depends(require("suppliers.write")),
                          db: AsyncSession = Depends(get_db)):
    result = await catalog.import_text(db, supplier_id, body.text,
                                       resolutions=_resolutions(body),
                                       actor=Actor(user_id=p.user_id),
                                       use_header_footer=body.use_header_footer)
    return ImportResultOut(**{k: v for k, v in result.__dict__.items() if k != "changed"})


@router.get("/held-offers", response_model=list[HeldOfferOut])
async def held_offers(supplier_id: uuid.UUID | None = None,
                      _: Principal = Depends(require("suppliers.read")),
                      db: AsyncSession = Depends(get_db)):
    """Staff label list: price lines held back (e.g. our price above the selling price)."""
    return await catalog.held_offers(db, supplier_id)
