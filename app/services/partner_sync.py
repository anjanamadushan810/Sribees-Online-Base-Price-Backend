"""Fulfilment partner figures to SRIBEES Online (plan P3; ONLINE_PARTNER_SYNC).

Each worker round:

1. **Branch flag.** Online is told whether each branch's partner is working
   (PUT /internal/supply/branches/{id}/partner). While it is on, Online lets the partner price
   the branch's Other Products and locks those prices against branch admins (F17).
2. **Stock.** The partner's own count of each Other Products item goes to Online as the
   branch's stock (PUT /internal/supply/partner-stock), whenever it differs from what Online
   last took. A price is mandatory (owner 2026-11-28): an item without the partner's price is
   sent as 0, so it cannot be sold. A loose product (weighed into the bag) goes in grams.
3. **Clean exit (G6).** When a branch's partner is unlinked, its items Online holds a partner
   count for go to 0 first, then the flag goes off.

Off (the default) = nothing is sent; the partner still gets its orders (P2).
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict

import httpx
from sqlalchemy import and_, case, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.models import (
    Branch,
    OnlineBranchListing,
    OnlineProduct,
    OnlineVariant,
    PartnerItem,
    Supplier,
    SupplierBranch,
    SyncState,
)
from app.services.errors import SupplyError
from app.services.partners import EXTERNAL, PARTNER

log = logging.getLogger("supply.partner_sync")
FLAGS = "partner_flags"
TIMEOUT_SECONDS = 20


def enabled() -> bool:
    s = get_settings()
    return bool(s.online_partner_sync and s.online_api_url and s.online_service_token)


async def call(client: httpx.AsyncClient, path: str, body: dict) -> dict:
    s = get_settings()
    if not s.online_api_url or not s.online_service_token:
        raise SupplyError("the link to SRIBEESonline is not set up yet")
    try:
        r = await client.put(f"{s.online_api_url.rstrip('/')}{path}", json=body,
                             timeout=TIMEOUT_SECONDS,
                             headers={"Authorization": f"Bearer {s.online_service_token}"})
    except httpx.HTTPError as exc:
        raise SupplyError(f"SRIBEESonline cannot be reached ({type(exc).__name__})") from None
    if r.status_code == 404:
        raise SupplyError("SRIBEESonline does not take partner figures (switched off)")
    if r.status_code != 200:
        detail = ""
        try:
            detail = str(r.json().get("detail") or "")[:200]
        except ValueError:
            pass
        raise SupplyError(f"SRIBEESonline answered {r.status_code} {detail}".strip())
    return r.json()


def _ref(item: PartnerItem) -> dict:
    variant = item.online_variant_id
    return {"product_id": str(item.online_product_id),
            "variant_id": None if variant == item.online_product_id else str(variant)}


def _wanted(item: PartnerItem) -> int:
    """What Online should hold for the item: the partner's count, but 0 while it has no
    price (owner 2026-11-28: a price is mandatory, an unpriced item cannot be sold)."""
    return item.stock_qty if item.price_to_us is not None else 0


async def _entries(db: AsyncSession, rows: list[PartnerItem], count) -> list:
    """[(rows with the count each was sent at, Online item)]. A loose product (weighed into
    the bag) is one item in grams: the partner's packs of each weight option added up."""
    pids = {r.online_product_id for r in rows}
    modes = dict((await db.execute(select(OnlineProduct.online_product_id,
                                          OnlineProduct.stock_mode)
                                   .where(OnlineProduct.online_product_id.in_(pids)))).all())
    grams = dict((await db.execute(select(OnlineVariant.online_variant_id,
                                          OnlineVariant.weight_grams)
                                   .where(OnlineVariant.online_product_id.in_(pids)))).all())
    out, loose = [], defaultdict(list)
    for r in rows:
        if modes.get(r.online_product_id) == "loose":
            loose[r.online_product_id].append(r)
        else:
            n = count(r)
            out.append(([(r.item_id, n)], {**_ref(r), "stock": n}))
    for pid, rs in loose.items():
        total = sum(count(r) * (grams.get(r.online_variant_id) or 0) for r in rs)
        out.append(([(r.item_id, count(r)) for r in rs],
                    {"product_id": str(pid), "variant_id": None, "stock": 0, "grams": total}))
    return out


async def _send_stock(client, sessionmaker, online_branch: uuid.UUID, entries: list) -> dict:
    """Marks what Online took (or refused for good: reclassified, batches). Never retried in
    a loop; a new count from the partner tries again."""
    counts: dict[str, int] = defaultdict(int)
    for start in range(0, len(entries), 500):
        chunk = entries[start:start + 500]
        answer = await call(client, "/internal/supply/partner-stock", {
            "branch_id": str(online_branch), "items": [item for _, item in chunk]})
        results = answer.get("results") or []
        async with sessionmaker() as db, db.begin():
            for (marks, _), res in zip(chunk, results, strict=False):
                counts[res.get("result", "unknown")] += 1
                for item_id, n in marks:
                    item = await db.get(PartnerItem, item_id, with_for_update=True)
                    if item is not None:
                        item.online_stock_sent = n
    return dict(counts)


async def _fill_rows(sessionmaker, linked: set[uuid.UUID]) -> int:
    """Every Other Products pack size a linked partner fills gets its row (stock 0, no
    price), so an item the partner has not priced yet is sent to Online as 0."""
    async with sessionmaker() as db, db.begin():
        missing = (await db.execute(
            select(SupplierBranch.supplier_id, OnlineBranchListing.branch_id,
                   OnlineVariant.online_product_id, OnlineVariant.online_variant_id)
            .join(OnlineBranchListing, OnlineBranchListing.branch_id == SupplierBranch.branch_id)
            .join(OnlineProduct, OnlineProduct.online_product_id
                  == OnlineBranchListing.online_product_id)
            .join(OnlineVariant, OnlineVariant.online_product_id
                  == OnlineProduct.online_product_id)
            .outerjoin(PartnerItem, and_(
                PartnerItem.supplier_id == SupplierBranch.supplier_id,
                PartnerItem.branch_id == SupplierBranch.branch_id,
                PartnerItem.online_variant_id == OnlineVariant.online_variant_id))
            .where(SupplierBranch.branch_id.in_(linked),
                   OnlineBranchListing.sourcing_group.in_(EXTERNAL),
                   OnlineProduct.is_active.is_(True), OnlineVariant.is_active.is_(True),
                   PartnerItem.item_id.is_(None)).limit(5000))).all()
        for supplier_id, branch_id, product_id, variant_id in missing:
            db.add(PartnerItem(supplier_id=supplier_id, branch_id=branch_id,
                               online_product_id=product_id, online_variant_id=variant_id,
                               stock_qty=0))
    return len(missing)


async def _flags(db: AsyncSession) -> dict:
    state = await db.get(SyncState, FLAGS)
    return dict(state.value) if state else {}


async def _save_flag(sessionmaker, branch_id: uuid.UUID, active: bool) -> None:
    async with sessionmaker() as db, db.begin():
        state = await db.get(SyncState, FLAGS, with_for_update=True)
        value = dict(state.value) if state else {}
        value[str(branch_id)] = active
        if state is None:
            db.add(SyncState(key=FLAGS, value=value))
        else:
            state.value = value


async def run_once(sessionmaker: async_sessionmaker, client: httpx.AsyncClient) -> dict:
    counts: dict[str, int] = defaultdict(int)
    async with sessionmaker() as db:
        branches = {b: o for b, o in (await db.execute(
            select(Branch.branch_id, Branch.online_branch_id)
            .where(Branch.online_branch_id.is_not(None)))).all()}
        linked = set(await db.scalars(
            select(SupplierBranch.branch_id)
            .join(Supplier, Supplier.supplier_id == SupplierBranch.supplier_id)
            .where(Supplier.is_active.is_(True), Supplier.kind == PARTNER)))
        sent = await _flags(db)

    for branch_id, online_branch in branches.items():
        want = branch_id in linked
        was = bool(sent.get(str(branch_id)))
        if want == was:
            continue
        if not want:
            # exit: Online's partner counts for this branch go to 0 before the flag goes off
            async with sessionmaker() as db:
                rows = list(await db.scalars(select(PartnerItem).where(
                    PartnerItem.branch_id == branch_id, PartnerItem.online_stock_sent > 0)))
                entries = await _entries(db, rows, lambda _r: 0)
            if entries:
                done = await _send_stock(client, sessionmaker, online_branch, entries)
                counts["zeroed"] += sum(done.values())
        await call(client, f"/internal/supply/branches/{online_branch}/partner",
                   {"active": want})
        await _save_flag(sessionmaker, branch_id, want)
        counts["flag_on" if want else "flag_off"] += 1

    if linked:
        added = await _fill_rows(sessionmaker, linked)
        if added:
            counts["rows_added"] += added
        wanted = case((PartnerItem.price_to_us.is_(None), 0), else_=PartnerItem.stock_qty)
        async with sessionmaker() as db:
            changed = (await db.execute(
                select(PartnerItem)
                .join(SupplierBranch, and_(SupplierBranch.branch_id == PartnerItem.branch_id,
                                           SupplierBranch.supplier_id == PartnerItem.supplier_id))
                .join(OnlineBranchListing, and_(
                    OnlineBranchListing.branch_id == PartnerItem.branch_id,
                    OnlineBranchListing.online_product_id == PartnerItem.online_product_id))
                .where(PartnerItem.branch_id.in_(linked),
                       OnlineBranchListing.sourcing_group.in_(EXTERNAL),
                       PartnerItem.online_stock_sent.is_distinct_from(wanted))
                .limit(2000))).scalars().all()
            # a loose product goes as one figure: take all its pack sizes, not just the changed
            keys = {(r.branch_id, r.supplier_id, r.online_product_id) for r in changed}
            rows = {r.item_id: r for r in changed}
            for branch_id, supplier_id, product_id in keys:
                for r in await db.scalars(select(PartnerItem).where(
                        PartnerItem.branch_id == branch_id,
                        PartnerItem.supplier_id == supplier_id,
                        PartnerItem.online_product_id == product_id)):
                    rows.setdefault(r.item_id, r)
            per_branch: dict[uuid.UUID, list] = defaultdict(list)
            for r in rows.values():
                per_branch[r.branch_id].append(r)
            plans = {b: await _entries(db, rs, _wanted) for b, rs in per_branch.items()}
        for branch_id, entries in plans.items():
            for outcome, n in (await _send_stock(client, sessionmaker, branches[branch_id],
                                                 entries)).items():
                counts[f"stock_{outcome}"] += n
    return dict(counts)


async def send_customer_price(client: httpx.AsyncClient, online_branch: uuid.UUID,
                              item_ref: dict, price) -> dict:
    return await call(client, "/internal/supply/partner-price",
                      {"branch_id": str(online_branch), **item_ref, "price": str(price)})


async def current_prices(client: httpx.AsyncClient, online_branch: uuid.UUID,
                         refs: list[tuple[uuid.UUID, uuid.UUID]]) -> dict:
    """{variant_id: selling price} from Online for (product, variant) pairs; {} when Online
    cannot answer (the app shows the list without prices)."""
    if not refs:
        return {}
    from app.services import online_stock

    try:
        answer = await online_stock.stock_info(client, online_branch, refs)
    except SupplyError as exc:
        log.info("customer prices not read: %s", exc.message)
        return {}
    out = {}
    for (_, vid), row in zip(refs, answer.get("items") or [], strict=False):
        price = row.get("selling_price") or row.get("listed_price")
        if price is not None:
            out[vid] = str(price)
    return out
