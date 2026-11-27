# Fulfilment partner (Nimali Stores) for Matugama — plan

Owner interview 2026-11-27 (3 MCQ rounds). Status: PLAN, nothing built.

## Why

SRIBEES Online lists more products for Matugama than the store carries. Today, when an
order holds products the store does not stock, the Matugama manager buys them by hand
from **Nimali Stores** (a grocery next to the store), adds our own items and hands the
order to delivery; Nimali's paper bill is paid per order. Over time Matugama will carry
more products itself and Nimali's share should fall to 0 %. Every current order flow must
keep working unchanged.

## Owner decisions

| # | Decision |
|---|---|
| F1 | Nimali is a **fulfilment partner** supplier, linked to **Matugama only** (server-enforced). |
| F2 | Sourcing per (branch, product): `own` or `partner`. **Own stock first; when own stock runs out the shortfall goes to the partner** automatically. |
| F3 | Initial partner set = **every product listed for Matugama that the store does not carry**. Staff flip products to `own` one by one later. *(Night 11-27: classified in Online admin via `sourcing_group`, see Architecture.)* |
| F4 | Nimali's app shows **our product list in our names**; Nimali enters, per product, **the price they sell to us** and **their stock count**. No matching. |
| F5 | Nimali's own cost price is never asked for and never stored. Their price to us = our base price (`base_prices`). |
| F6 | **We see Nimali's real stock count**; it caps what customers can order for partner lines. |
| F7 | Nimali's shop is next to the store: they **walk the goods over**, no delivery fee. |
| F8 | **No QR labels** on partner goods: Matugama staff scan the bag's order slip and tick the items. |
| F9 | Bill made **automatically per order**, confirmed in the app; **paid weekly** from one statement. |
| F10 | Nimali changes a price → **applies at once**, our staff get an alert. |
| F11 | **No "not available" button** for Nimali (the customer side cannot show a short line yet). Shortages are rare because F6 caps orders; a real one is solved by phone. |
| F12 | **No accept deadline** / no escalation timer. |
| F13 | Build on development, test end to end on devsupply, **ask the owner before prod**. |
| F14 | (round 4) Nimali **sees the customer's name, phone and address** on its requests, **for ever** (owner: otherwise orders get mixed up). The partner app is operated by **our own staff member working inside Nimali Stores**, so no privacy-policy change (owner 2026-11-27). |
| F15 | The Price app's **customer-price feature moves into Nimali's app** for partner products: the Matugama selling price, weight-option prices for loose goods, barcode scan, daily/weekly/monthly/long tabs. NOT cashback, NOT competitor prices, NOT the price floor. |
| F16 | **No margin rule:** Nimali may set any customer price. Our staff get an alert, red when the customer price is below Nimali's price to us (a loss). |
| F17 | **One owner per price:** products with sourcing `partner` are priced only by Nimali; `own` products stay with the Matugama manager in the Price app. Owner / super admin can still override. An `own` product whose stock ran out and is filled by Nimali (F2) keeps the manager's price. |

## Architecture guardrails (owner 2026-11-27: Nimali must not harm the Online architecture; at 0 % Sribees carries on alone)

- G1 **Bolt-on, never woven in.** All partner logic lives in new Supply tables/modules
  (`app/services/partner*.py`). No existing table changes meaning; new columns default to
  today's behaviour (`suppliers.kind='wholesale'`, Online `branch_inventory.source='own'`).
- G2 **Generic, not "Nimali".** The code knows "a fulfilment partner of a branch", never a name
  or Matugama (branches are never hard-wired). Nimali is one data row.
- G3 **No sourcing row = own = exactly today.** The split, the price lock and the partner stock
  push all start from `branch_sourcing`; empty table = the system behaves byte-for-byte as now.
- G4 **Customer app and checkout unchanged.** Partner goods reach the customer through the same
  `branch_inventory` stock and price; the app never knows a partner exists.
- G5 **Finance unchanged.** The handover posts the normal purchase + consumption events; Finance
  has no partner concept.
- G6 **Clean exit.** Flipping a product to `own` (or unlinking the partner) immediately: stops new
  requests for it, sends Online its own stock (0 if none yet) instead of the partner count, lifts
  the manager price lock, removes it from the partner app. Open requests finish normally; the
  last statement is paid as usual. History stays for audit.
- G7 **Partner down ≠ shop down.** If the partner app or account is off, own lines still pack and
  ship; only partner lines wait. Supply never blocks an Online order on the partner.

## Architecture

### Decision 2026-11-27 night (tech lead): ONE place decides "does the branch carry it"
Ashen merged Online #51 the same evening: `branch_inventory.sourcing_group` = `store` (Our
Store) / `external_daily` / `external_slow` (Other Products) / null, edited in the admin's
branch inventory. That is exactly own/partner, so **Online's sourcing_group is the single
source of truth** and Supply only mirrors it. P1's own `branch_sourcing` table and `seed`
were removed before anything was pushed. F3 becomes: staff classify in Online admin;
"Other Products" go to the partner; `store`/unclassified stay own (= today).

### Supply (owner of the feature)
- Migration 0030:
  - `suppliers.kind` (`wholesale` default | `fulfilment_partner`).
  - `supplier_branches (supplier_id, branch_id UNIQUE, fills_store_shortfall)`: one partner per
    branch; `fills_store_shortfall` (F2, default OFF) also sends the part of an Our Store line
    the shelf cannot cover. Keep it off until the branch's stock in Supply is complete (go-live
    count), else goods still on the shelf would be bought again.
  - `online_branch_listings (branch_id, online_product_id, sourcing_group)`: mirror of Online,
    from the catalogue sync (`listings` per product).
  - `partner_items (supplier_id, branch_id, variant, price_to_us, stock_qty)`; price history in
    `partner_price_changes` (alert source, F10).
  - `fulfilment_requests (FR-######, online_order_id UNIQUE, status new→seen→ready→handed |
    cancelled, customer name/phone/address)` + `fulfilment_lines (qty, price_to_us snapshot,
    stock_taken, ticked_qty)`.
- Splitting (P2, built): the existing Online `order.status` events (feed
  `/internal/supply/events`, worker online_events.py) drive `partner_requests.apply_event` in a
  savepoint (G7). Live statuses pending/confirmed/processing/packing create ONE request per
  order for its Other Products lines; the first event of a new order has no lines (Online writes
  it before its items, test in Online), so Supply reads `GET /internal/supply/orders/{id}`.
  Edits are followed until the partner marks the bag ready; a cancel gives the partner's stock
  back. Push texts partner_request / partner_changed / partner_cancelled.
- Handover (P4): staff scan the order slip → tick lines → **pass-through receipt**: purchase of
  the ticked qty at `price_to_us` into the branch + immediate consumption by the Online order
  (no batch left on the shelf, no QR). Finance gets purchase + COGS via the existing outbox;
  base price recorded.
- Weekly statement (P5, F9): handed requests of the week, status open→sent→paid.
- Partner availability to Online (P3): available = partner `stock_qty` (already reduced by
  open requests).
- RBAC: a partner token sees only its own requests/items (with the customer's details: F14);
  staff routes use `branch_scope`; tests prove a branch-B manager gets 403/404 on Matugama.

### Online backend (small, additive)
- Built (P2): catalogue `listings` per product (active branch rows + sourcing_group) and
  `GET /internal/supply/orders/{id}` (customer name, phone, address, notes, lines). Same
  service-token / never-through-the-edge locks.
- **Release dependency:** `/internal/supply/events` (pack-scan, commit 5ed4788) exists only on
  Online `development`; prod `main` does not have it yet. The partner flow works on prod only
  after that endpoint reaches main (owner MCQ at release).
- Partner pricing (P3, F15-F17): `PUT /internal/supply/partner-price` (service token) sets the
  branch price / weight prices of an Other Products item of a branch with a partner. Nimali
  never gets an Online admin login: app → Supply (checks the item is this partner's at this
  branch, audits) → Online. `/admin/inventory` refuses a branch manager's price change on an
  Other Products item of a branch with a partner (super admin may).

### Supplier app (same Flutter app, partner mode)
- Tabs: Requests (push on new; customer name/phone/address shown), My items (price to us +
  stock + **customer price**, barcode scan, review-level tabs, weight prices), Statements.
- Price screens are ported from the Sribees Price app (main 6896179), re-pointed at Supply.
- Sinhala + English, as now.

### Staff app / web
- "Partner handover" scan screen; sourcing switch (own/partner) per product; partner share % on the dashboard; price-change alerts.

## Risk tier
- **High:** pass-through receipt + statements (money into Finance) → fresh-context reviewer.
- **Medium:** order splitting, stock push (wrong availability = oversold order).

## Done = all of these pass
1. `run_supply_tests.py` green incl. new tests: split own-first (own 2 + order 5 → FR qty 3); partner only serves Matugama (other branch → no FR, 403 on link); partner token sees customer fields only on its own requests (other partner → 404); partner can price only its own `partner` products at its own branch (own product / other branch → 403); manager price change on a `partner` product refused in Online; branch-B manager → 404 on Matugama FR; replayed `order.placed` makes no second FR; pass-through receipt leaves Matugama on-hand unchanged and writes one purchase + one consumption; weekly statement total = sum of handed lines.
2. Migration 0030 replayed twice on a throwaway PG 18 without error.
3. Dev E2E on devsupply: Online dev order with 1 own + 1 partner product → FR in the partner app → bag scan → order packs → statement shows the line at `price_to_us`.
4. Existing flows unchanged: every pre-existing test passes; a product with no sourcing row behaves exactly as today.
5. **Exit test (G6):** a test flips every partner product to `own` / unlinks the partner → a new
   `order.placed` creates no request, Online gets own stock (0) for those products, a manager
   price change on them is accepted again, the partner app list is empty, an open request can
   still be handed over and billed.
6. **Partner-off test (G7):** partner account disabled → an order with own + partner lines still
   packs its own lines; nothing raises.

## Phases
P1 schema + sourcing + partner items (backend) · P2 Online `order.placed` + split · P3 partner app mode + customer-price screens (F15) and Online partner-price endpoint · P4 handover + pass-through receipt + Finance · P5 statements + alerts + dashboard share · P6 dev E2E → owner MCQ for prod.

## Status
| Phase | State |
|---|---|
| P1 | Built 2026-11-27 (local `feat/fulfilment-partner`): partner kind, one partner per branch, partner items + price alerts, staff sourcing view/share. |
| P2 | Built 2026-11-27 night (local): sourcing mirrored from Online `sourcing_group`; fulfilment requests from order events (create / follow edits / cancel / ready), partner app request routes, push texts. Supply suite 295 pass; 0030 replayed twice on PG 18. Online branch `feat/fulfilment-partner` (off main 982fb1d): catalogue `listings` + `GET /internal/supply/orders/{id}`, 3 new tests. |
