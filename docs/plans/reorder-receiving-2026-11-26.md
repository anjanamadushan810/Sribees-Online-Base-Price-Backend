# Receiving, transport cost by weight, and automatic reorder (2026-11-26)

Status: **phase 1 being built** (owner: "start", 2026-11-26). Branch: `development` only. Main / production only
after the owner says so.

## The task (owner, 2026-11-26)
Nishi Super Palace supplies about 100 products to the Matugama store. When a product's
stock falls below its minimum, a purchase order list (up to the maximum) is prepared. The
supplier delivers what they can. Supplier prices come in two ways: a price per item, or a
price per item plus one transport charge for the whole order, which the system splits by
weight to get the price of each item delivered to our store.

## Owner decisions (MCQ 2026-11-26)
| # | Question | Decision |
|---|----------|----------|
| 1 | Where min / max are set | Online admin: a **maximum** next to the existing Low Stock Threshold (minimum) |
| 2 | How Matugama sells | **Online orders only**, so Online branch stock is the stock figure |
| 3 | PO creation | A **draft** is prepared; staff check, edit and send it |
| 4 | Base price | **Delivered price** (supplier price + transport share); supplier price is still kept. Replaces PLAN.md decision 15 |
| 5 | Transport charge | **Our staff** enter it from the bill when goods arrive |
| 6 | Short delivery | The order **closes**; the shortage returns in the next draft. Replaces decision 31 (back-order stays open) |
| 7 | Where receiving is done | **Web dashboard and staff app** |
| 8 | Several suppliers | A **main supplier** per product; a cheaper one is shown; staff can change it in the draft |
| 9 | Online stock | Received units are **added to Online stock when receiving is saved** |
| 10 | Whole boxes | Do not pass the maximum (max 100, stock 30, box 48: 1 box). If not even one box fits, 1 box |
| 11 | Weight | The weight entered on the **Online product** (used for delivery) |
| 12 | Order of work | Phase 1 first: receiving, transport, Online stock |

## Found in the code (2026-11-26)
- Delivery charge exists, but only the supplier can enter it (app, at accept); it is split
  by **value** (`services/receiving.py: _landed_shares`) and the base price excludes it.
- There is no web receiving page. Quick buy has no transport field.
- Online has `low_stock_threshold` (default 10 on every row) and **no maximum**.
- Online's `/internal/supply/catalog` does not send `Product.weight`; Supply guesses a
  default pack size's weight from the display size.
- Nothing sends received stock from Supply to Online.
- Online `main` has inventory batches (PR #49, migration 127): unit products; each batch
  has a purchase cost, a listed price and a selling price. Once a product has batches,
  stock can only be added as a batch.

## Assumptions to confirm with the owner
| # | Assumption |
|---|------------|
| A1 | **Confirmed by the owner 2026-11-26.** Selling and listed price of a new Online batch = the current Matugama price, shown on the receiving page and editable there |
| A2 | Base price = supplier unit price + (the line's transport share / paid units). Free units add weight to the split but do not lower the base price |
| A3 | The transport charge is part of the supplier's bill (bill total = goods + transport) |
| A4 | A charge the supplier typed in the app is pre-filled; the staff figure wins. The 25% of goods cap stays; above it only the owner may save |
| A5 | A product with no Matugama row on Online gets a hidden (inactive) row; customers do not see it until staff switch it on |
| A6 | A product is reordered only when it has a maximum and a main supplier |

## Guardrails
- Online stock is **increased** by the received units, never set to an absolute figure.
- Each receipt line is applied to Online **once** (idempotent by receipt line id; retried
  from the Supply outbox).
- Receipts saved before the feature is switched on are **never** sent (the 2026-11-26
  market bills must not be added retroactively). Switch is OFF by default.
- Staff stop typing the same goods into Online admin once the switch is ON (double count).
- Finance's stock push must not overwrite Online stock: `finance_stock_authority` stays
  OFF; check the Finance full worker before the 12-01 go-live.
- Teammates' work in the Online repo is not touched; changes are staged by name.
- High risk (stock and money): a fresh-context reviewer, no unfixed HIGH, before any
  production question.

## Phase 1: receiving, transport by weight, Online stock
1. Online: the supply catalog sends the product weight; Supply stores unit weight in grams
   (a weight option's `weight_grams` wins).
2. Supply: transport charge entered at receiving, split by weight over the delivered lines;
   cents add up exactly. A delivered line with no weight stops the save, names the product
   and says to add the weight in Online.
3. Supply: the delivered base price is recorded at receiving.
4. Supply: receiving closes the order, including a short one.
5. Web page "Receive goods" (ordered list, received units, expiry, bill number and total,
   transport, selling price); Quick buy gets the transport field; staff app the same
   transport field and close rule.
6. Online `POST /internal/supply/receipts` adds the stock (unit: batch when A1 applies;
   packed and loose: their own counters), and Supply sends it.

**Done (all must pass):**
- Test: charge 4,000; A 100 x 100 g at 200, B 60 x 500 g at 400 gives delivered unit
  prices **210.00** and **450.00**; shares sum to exactly 4,000.
- Tests: uneven split keeps the total; missing weight saves nothing; a short receipt closes
  the order; the same receipt sent twice adds stock once; switch OFF sends nothing; each
  stock mode (unit, unit with batches, packed, loose).
- Both full suites and `ruff check .` green; web and app tests green; screenshot of the
  receiving page in Sinhala and English.
- On dev: receive 60 units and the Online dev Matugama stock rises by exactly 60.

## Phase 2: minimum / maximum and automatic draft orders
1. Online: `max_stock_level` per branch row (migration), field in admin beside the minimum
   (maximum must be above minimum); a feed of stock, reserved, minimum and maximum.
2. Supply: main supplier per product, cheaper offer shown.
3. Draft builder (daily and on a button): available = stock - reserved. When available is
   **below** the minimum: need = maximum - available - units on open orders; boxes by
   decision 10. One draft per supplier and branch.
4. Dashboard lists the drafts and the products left out (no maximum, no main supplier, not
   matched).

**Done (all must pass):**
- Tests: the 30 / 100 / 48 example gives 1 box; need under one box gives 1 box; open orders
  are subtracted; a second run adds no duplicate; no maximum means not ordered; the main
  supplier is chosen.
- On dev, with Nishi's list: every line of the draft matches a hand calculation; send,
  approve, receive part, and the shortage appears in the next draft.

## Before real use
Matugama stock must be counted and correct in Online; weights filled in for the supplied
products; maximums entered.
