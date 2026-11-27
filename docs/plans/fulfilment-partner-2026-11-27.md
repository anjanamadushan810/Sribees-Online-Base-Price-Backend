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
| F3 | Initial partner set = **every product listed for Matugama that the store does not carry**. Staff flip products to `own` one by one later. |
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

### Supply (owner of the feature)
- Migration 0030:
  - `suppliers.kind` (`wholesale` default | `fulfilment_partner`).
  - `supplier_branches (supplier_id, branch_id)`; a partner serves only its listed branches.
  - `branch_sourcing (branch_id, online_product_id, online_variant_id, mode, partner_id)`; no row = `own`.
  - `partner_items (supplier_id, branch_id, product/variant, price_to_us, stock_qty, updated_at)`; price history in `partner_price_changes` (alert source, F10).
  - `fulfilment_requests (request_no FR-######, online_order_id, order_number, branch_id, supplier_id, status new→seen→ready→handed→billed, ...)` + `fulfilment_lines (product/variant, qty, price_to_us, ticked_qty)`.
  - `partner_statements` (weekly, F9): lines = handed requests; status open→sent→paid.
- Splitting: Online sends `order.placed` (items + branch) on `/internal/supply/events`. For each line in a branch with a partner: qty from own stock first (F2), the rest → one fulfilment request per order. Idempotent on the event id.
- Handover (F8): staff app / web scans the order slip → ticks lines → **pass-through receipt**: purchase of the ticked qty at `price_to_us` into Matugama + immediate consumption by the Online order (no batch left on the shelf, no QR). Finance gets purchase + COGS via the existing outbox; base price recorded.
- Stock push to Online: available(partner product) = Nimali `stock_qty` minus open requests; sent like own stock but tagged `source=partner`.
- RBAC: supplier token for a partner sees only its own requests/items (with the customer's name, phone, address: F14). Staff scoping uses the existing `branch_scope`; new routes get the same checks plus a test that a branch-B manager gets 404 on Matugama requests.

### Online backend (small)
- Emit `order.placed` with items into the existing supply events feed (with branch id).
- Accept partner availability in the Supply stock push (no new customer UI).
- Partner pricing (F15-F17): `branch_inventory.source` (`own`|`partner`, pushed by Supply);
  `PUT /internal/supply/partner-price` (service token) sets the Matugama branch price / weight
  prices of a partner product only. Nimali never gets an Online admin login: the app talks to
  Supply, Supply checks the product is this partner's at this branch, audits, then calls Online.
  `/admin/inventory` + weight branch-price refuse a branch manager on a `partner` product
  (super admin may).

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
