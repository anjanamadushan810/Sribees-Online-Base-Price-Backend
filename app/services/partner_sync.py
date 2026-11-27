"""Fulfilment partner figures to SRIBEES Online (plan P3; ONLINE_PARTNER_SYNC).

Each worker round:

1. **Branch flag.** Online is told whether each branch's partner is working
   (PUT /internal/supply/branches/{id}/partner). While it is on, Online lets the partner price
   the branch's Other Products and locks those prices against branch admins (F17).
2. **Stock.** The partner's own count of each Other Products item it has entered goes to
   Online as the branch's stock (PUT /internal/supply/partner-stock), whenever it differs from
   what Online last took. An item the partner never entered is not sent: Online keeps the
   figure staff set until the partner counts it (no product drops to 0 at switch-on).
3. **Clean exit (G6).** When a branch's partner is unlinked, its items Online holds a partner
   count for go to 0 first, then the flag goes off.

Off (the default) = nothing is sent; the partner still gets its orders (P2).
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict

import httpx
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.models import (
    Branch,
    OnlineBranchListing,
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


async def _send_stock(client, sessionmaker, online_branch: uuid.UUID,
                      items: list[tuple[uuid.UUID, dict, int]]) -> dict:
    """items: (item_id, ref, count). Marks what Online took (or refused for good)."""
    counts: dict[str, int] = defaultdict(int)
    for start in range(0, len(items), 500):
        chunk = items[start:start + 500]
        answer = await call(client, "/internal/supply/partner-stock", {
            "branch_id": str(online_branch),
            "items": [{**ref, "stock": n} for _, ref, n in chunk]})
        results = answer.get("results") or []
        async with sessionmaker() as db, db.begin():
            for (item_id, _, n), res in zip(chunk, results, strict=False):
                outcome = res.get("result", "unknown")
                counts[outcome] += 1
                item = await db.get(PartnerItem, item_id, with_for_update=True)
                if item is not None:
                    # "set": Online holds it. Anything else (reclassified, loose, batches):
                    # never retried in a loop; a new count from the partner tries again.
                    item.online_stock_sent = n
    return dict(counts)


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
            if rows:
                done = await _send_stock(client, sessionmaker, online_branch,
                                         [(r.item_id, _ref(r), 0) for r in rows])
                counts["zeroed"] += sum(done.values())
        await call(client, f"/internal/supply/branches/{online_branch}/partner",
                   {"active": want})
        await _save_flag(sessionmaker, branch_id, want)
        counts["flag_on" if want else "flag_off"] += 1

    if linked:
        async with sessionmaker() as db:
            rows = (await db.execute(
                select(PartnerItem)
                .join(SupplierBranch, and_(SupplierBranch.branch_id == PartnerItem.branch_id,
                                           SupplierBranch.supplier_id == PartnerItem.supplier_id))
                .join(OnlineBranchListing, and_(
                    OnlineBranchListing.branch_id == PartnerItem.branch_id,
                    OnlineBranchListing.online_product_id == PartnerItem.online_product_id))
                .where(PartnerItem.branch_id.in_(linked),
                       PartnerItem.updated_by.is_not(None),
                       OnlineBranchListing.sourcing_group.in_(EXTERNAL),
                       or_(PartnerItem.online_stock_sent.is_(None),
                           PartnerItem.online_stock_sent != PartnerItem.stock_qty))
                .limit(2000))).scalars().all()
        per_branch: dict[uuid.UUID, list] = defaultdict(list)
        for r in rows:
            per_branch[r.branch_id].append((r.item_id, _ref(r), r.stock_qty))
        for branch_id, items in per_branch.items():
            for outcome, n in (await _send_stock(client, sessionmaker, branches[branch_id],
                                                 items)).items():
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
