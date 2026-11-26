# Supply + Finance joint go-live ("the final way")

Owner brief 2026-11-13. Go-live date **2026-12-01**, treated as a **fresh launch**: no
history import. If Online orders exist that day, the owner writes them down. Opening stock
and its cost come from the go-live count, and the owner gives the cost.

## 1. Owner decisions (MCQ round 6, 2026-11-13)

| # | Decision |
|---|---|
| 21 | **All goods buying goes through Supply**, including cash market buys (a supplier without the app). Finance's PO/GRN screens are removed. Stock comes in through one door only. |
| 22 | PO approval = **Finance's rule**: under the threshold (Rs 100,000, a Finance setting) it is approved automatically; above it the owner approves. The supplier sees an order only after approval. |
| 23 | The supplier invoice (number, total, photo) is entered **at receiving in Supply**. Finance matches it three ways with zero tolerance, as today, and the accountant sees only mismatches. |
| 24 | Online order stock is taken by **scanning the unit's QR label at packing in the New Orders app**. It is never taken automatically by FEFO. |
| 25 | **A QR label goes on every sellable unit.** Bulk weight goods (rice, flour) get one label per supplier bag; packing scans the bag and enters the weight. |
| 26 | Receiving, counts, transfers and write-offs are done in a **new Supply staff app** (Flutter APK). |
| 27 | Labels are printed on **A4 sticker sheets**. The system registers a number range and makes a PDF. Labels carry only the number, never prices. |
| 28 | At go-live **every unit on hand gets a label during the count**, so no unlabelled stock exists. |
| 29 | Stock authority (Online's sellable quantity comes from Supply) switches **ON at go-live**, with no parallel run. It may only switch after the full dev E2E (§4 G1) passes. |
| 30 | 2FA stays **off** on Finance and Supply prod (owner's choice; Claude recommended on). |
| 31 | **Short delivery = back-order.** The order stays open for the rest. Staff can "close the rest" by hand (with a reason), and the supplier sees "8 of 10 received". This replaces Finance's "a GRN closes the PO" rule. |

## 2. Shape

```
                 supply_outbox (seq, event_id)            finance_outbox (seq, event_id)
Supply  ───────────────────────────────►  Finance  ───────────────────────────────►  Supply
 orders, receipts, stock moves             PO approve/reject, invoice match result
 (Finance pulls: GET /internal/finance/events on Supply; Supply pulls Finance the same way)

Online ── order events (/internal/supply/events) ──► Supply   (what to pack, cancels, returns)
New Orders app ── pack scan ──► Online ── POST /internal/supply/pick (sync) ──► Supply
Supply ── PUT stock levels (authority) ──► Online
```

- **Both directions are pull feeds over an outbox**, using the same pattern as Online →
  Finance: a sequence cursor, gap grace, one transaction per event plus a `processed_events`
  row. No HTTP push carries money or stock, so a retry cannot double-count anything.
- **Master data:** Supply owns suppliers and receiving. Finance creates or updates its
  supplier row from the event payload, keyed by Supply's `supplier_id`. Product id is
  Online's `variant_id` in both systems.
- **One holder of quantities:** after phase B, Finance has no table that holds a
  quantity. Its journals come only from Supply events.
- Packing has to be **synchronous**: the packer must know at once that a label is wrong.
  The scan goes New Orders app → Online (staff auth there) → Supply internal API on the
  Docker network (service token). Supply checks that the label belongs to that order
  line's product, that its batch has stock and is not expired, and that the label has
  not been used already.

## 3. Phases

### A. Supply ↔ Finance PO link
- Supply: migration adds `event_id uuid` to `supply_outbox`; `GET /internal/finance/events`
  (Finance token, SHA-256 compared, refused through the edge, like Online's); a feed worker
  that pulls Finance's decisions; with `ORDER_APPROVAL=finance`, submit → `order.submitted`,
  and Finance's `po.approved`/`po.rejected` move the order on.
- Finance: generalise `feed_consumer` to several feeds ("online", "supply");
  `supply_feed.apply`: `order.submitted` → supplier upsert + PO (idempotent: unique
  `source_ref = supply:<order_id>`) → existing approval rule (auto under the threshold,
  owner above); `order.cancelled` → cancel the PO; a new `finance_outbox` table plus an
  internal endpoint for `po.approved`/`po.rejected`; the owner approves or rejects in
  Finance's PO page.
- Back-orders (31): PO status `partly_received`; `order.closed_short` closes it.
- **Done A:** `pytest supply/tests/test_finance_link.py` and
  `finance/tests/test_supply_feed.py`, each against the other side's recorded contract
  fixture: submit → PO exists once (event replayed 3× → 1 PO); under-threshold PO is auto
  approved → Supply order `approved` and visible to the supplier; over-threshold waits →
  owner approves in Finance → Supply `approved`; rejected → Supply `rejected` with the
  reason; cancel → PO cancelled. Dev E2E script `tools/e2e_po_link.ps1` passes on
  devsupply + devfinance.

### B. Stock moves to Supply (§4b of PLAN.md)
- **Move, do not rewrite:** copy Finance's `stock.py`, `stock_control.py` (transfers,
  write-offs with approval limits, counts), the repack and extra-cost parts of
  `product_costs.py`, `stock_push.py` and `parallel_run.py`, with their models, migrations
  (as Supply migrations) and tests. Keep the DB triggers (moves are the only thing that
  changes a batch; value is exact to the cent).
- Receiving (`receipts`) replaces Finance's GRN: lines against the order (back-orders
  allowed), free quantity, expiry, mfg date, MRP, invoice number, total and photo (23).
  The supplier's delivery charge is landed cost spread by weight (Finance rule).
  Cash buy (21) = an order for a no-app supplier with `payment_terms=cash`, received with
  its payment.
- Events to Finance: `receipt.posted` (value per line, invoice data), `stock.consumed`
  (order line, batch value = COGS), `stock.returned`, `stock.written_off`,
  `stock.transferred`, `stock.count_adjusted`, `stock.repacked`, `opening.stock`.
- Finance: `supply_feed` posts the journals that `purchasing.receive`, `stock_control`,
  `product_costs` and the stock parts of `online_feed` post today (same accounts);
  invoice matching runs on `receipt.posted`. Then the old code, routes, screens and
  quantity tables are removed from Finance (prod is empty, so dropping them is safe).
- **How the journals cross over (design, 2026-11-13).** The moved code keeps its calls
  to `ledger.post_journal(entry_date, source, source_ref, memo, lines=[LineIn(...)])`.
  In Supply, `ledger` is a shim with the same signature. It writes a `journal.requested`
  outbox event that carries those exact lines, the branch, and an idempotency key
  (`source_ref`), and it returns a stand-in journal id (the event id). Finance's
  `supply_feed` posts the journal as given, after these checks: debits == credits;
  each account is on the allow-list for that source (receipts 1300/2020/1210, consumption
  1300/1320/5000, write-off 1300/5100, transfers 1300/1310, repack 1300/5100/6510, opening
  1300/3xxx); the period is open; and one `source_ref` posts only once. So the accounting
  rules stay word for word what Finance runs today, and Finance stays the only writer of
  its ledger. Quantity-free documents Finance needs (a receipt's lines with value and
  invoice data, for the invoice match) travel in the same event.
- "No quantities in Finance" means no on-hand stock: no batches, moves, transfers, counts
  or repacks. Document lines (PO, invoice and receipt lines with a qty) stay, because the
  three-way match needs them.
- **Done B:** the moved Finance stock tests pass in Supply unchanged except for imports;
  `tools/check_stock_ledger.py` passes on dev (for every branch, Finance account 1300 equals
  the sum of Supply batch value, and 1310 equals the value of open transfers);
  `grep` finds no table with a qty column in Finance's schema
  (`tests/test_no_quantities.py`); Finance's full suite is green.

### C. QR labels
- `label_ranges` (registered when printed), `labels` (number → batch, one batch only,
  never reused; DB unique constraint plus a trigger). PDF of A4 sticker sheets (QR + the
  number in text, no price). The number format includes a check digit, so a mistyped
  number is refused.
- **Done C:** tests: a label linked twice is refused; a label from an unregistered range
  is refused; a bad check digit is refused; the PDF has N labels and decodes back to the
  same numbers (`pyzbar` or `zxing` in the test).

### D. Supply staff app (Flutter, new APK)
- Staff login (Supply staff auth), branch-scoped. Receive an order: lines, qty, free,
  expiry/mfg/MRP, scan label numbers per unit (or "next N from this range"), invoice
  number + total + photo. Counts (scan labels), transfers (ship/receive by scan),
  write-off request.
- **Done D:** `flutter analyze` clean; unit tests; APK version == pubspec == profile
  screen; release-signed (G3).

### E. Online + New Orders app
- Online: `/internal/supply/events` (order created/cancelled/returned/undelivered, the
  same outbox as Finance with its own token); `POST /staff/orders/{id}/pack-scan` → Supply
  `/internal/supply/pick`; the stock-levels PUT accepts Supply's token; authority uses
  Supply's levels.
- New Orders app: a packing screen that scans each unit (and the weight for bulk bags);
  an order cannot be marked packed until every line is scanned.
- Cancel after packing / return → the stock goes back into the same batch (Finance rule
  kept: returns that are not resellable go to quarantine).
- **Done E:** Online `ruff check .` + full suite green; Supply tests: wrong product label
  refused, expired batch refused, the same label twice refused, a bulk bag weight above
  what is on hand refused, cancel puts the stock back; dev E2E G1.

### F. Finance + Supply go-live (2026-12-01)
- Finance worker WORKER_SUPPLY_ONLY=true on prod (owner 2026-11-26, N5): Finance's own Online cost posting stays off, or Supply-handled orders are costed twice. Online prod: SUPPLY_PACK_SCAN_ENABLED + SUPPLY_API_URL + SUPPLY_PICK_TOKEN; Supply prod: ONLINE_PICK_TOKEN_SHA256; Supply labels_required=1.
- Finance: go-live date 2026-12-01; cash/bank opening from the CR book; supplier dues
  list (if any).
- Supply: go-live count per branch with labels (28); the owner gives the cost per line →
  `opening.stock` → Finance opening entry (Dr 1300 / Cr opening equity), approved by the
  owner.
- Stock switch ON (29); Online prod picks up Supply's levels.
- **Done F:** `tools/check_stock_ledger.py` passes on prod after the opening; Online prod
  sellable == Supply on-hand for every product (stock compare = 0 differences).

### G. Release items
- G1 full dev E2E script: order → Finance auto approval → supplier accepts in the app →
  receive with labels → pack scan in New Orders → stock pushed to Online → Finance journals
  → `check_stock_ledger` = OK.
- G2 FCM: supplier app push on a new approved order (existing Sribees Firebase project, a
  new Android app). Never send a test SMS.
- G3 release keystore for the supplier and staff APKs; backups in secrets.out plus a
  copy the owner keeps.
- G4 prod: Matugama branch + Nishi Super Palace (the owner gives the real phone number).

## 4. Risk tiers and review
High (fresh-context reviewer agent, at most 3 rounds): A (money approval), B (stock
value / journals), E pick + returns, F opening. Medium: C, D. Nothing goes to main or prod
without asking the owner.

## 5. Status
- 2026-11-13: plan written. **A built locally** (Supply 58a9b12 + 5c8181e, Finance e690575 +
  a1c193f). A fresh-context reviewer found no high-risk issue. Its 5 medium findings are
  fixed with tests: answers apply only in finance mode; a refused order comes back as
  `po.refused`; other errors still block the feed and are retried each round (bounded by
  checks); Supply exceptions are kept off Online's list and the month close; skipped
  sequence numbers are written to `last_error`. Lows 7, 10 and 11 are fixed too.
- Open lows: (6) the internal routes are reachable from every container on
  sribees_default, so set `*_ALLOWED_IPS` or use a private network; (8) every order
  overwrites the supplier's name and phone in Finance, and an existing Finance supplier
  is never linked; (9) the delivery charge (up to 25%) is not part of the approved
  amount; (12) a Supply outage delays Online rounds by up to 20 s.
- Mode flip `ORDER_APPROVAL=local -> finance`: first make sure no Supply order is
  `submitted`; orders submitted under local mode stay with the owner in Supply.
- G2 FCM built (8f696eb): Firebase app `com.sribeesonline.sribees_supplier`
  (1:25407501580:android:b629205e65e4d0c6fe4529); needs the `FCM_CREDENTIALS_JSON` env on the
  box and the `SUPPLIER_GOOGLE_SERVICES_JSON` secret.
- G3 keystore built (bc81792): needs the `SUPPLIER_KEYSTORE_B64/_PASSWORD/_ALIAS` secrets.
  Release SHA-1 is 0A:AE:BD:6B:...:3A:B7.
- 2026-11-13 evening: A + G2 + G3 deployed on dev; dev E2E PASS (SPO-000001 -> PO-000007
  approved). Supplier app 0.2.0+2 prod APK/AAB built from main (owner OK); Nishi Super
  Palace onboarded on prod (172 offers, phone registered).
- **B part 1 built locally** (Supply 3c45363 + 46d7184, Finance b5cb65d):
  - Supply migration 0008: settings, doc_counters, `branches.is_head_office`, products
    (Online variant id), batches, moves (same triggers), transfers, adjustments, counts,
    receipts + receipt_lines (append-only), back-order columns on orders.
  - Moved unchanged except imports: `stock.py`, `stock_control.py`, `settings.py`, stock
    routes and schemas, and Finance's `test_stock_control_api.py` / `test_stock_rules.py`
    (goods arrive through `tests/stock_helpers.py`, which receives the Supply way and reads
    balances from the `journal.requested` events).
  - `ledger.post_journal` shim -> `journal.requested`; Finance `_journal` posts it with an
    account allow-list per source (purchasing 1300/2020, inventory 1300/1310/5100).
    A refusal (closed period, unknown branch, wrong account) becomes a supply feed
    exception the accountant retries; Supply's stock already moved, so
    `tools/check_stock_ledger.py` must report such a gap.
  - Receiving: `POST /orders/{id}/receipts`, back-orders, `POST /orders/{id}/close-rest`,
    no cancel after goods arrived, weighed goods by kg, invoice no + total on the receipt.
- B part 1 review (fresh context, 2026-11-13): 2 HIGH, 4 MEDIUM, 4 LOW. Fixed with tests:
  #1 stock moved in Supply while Finance refuses its journal -> journal dates within 90
  days, closed months refused in Supply (finance_periods), a waiting Supply journal blocks
  Finance's month close; #3 a line is received only as a pack size of its own product;
  #4 a first receipt by kilograms makes a weighed item; #5 receipt.posted carries
  online_branch_id; #6 a receipt without its journal is flagged; #7 index for the shim.
  Open: #2 (= B4 invoice match, delivery charge as landed cost, cash buy 1210,
  `tools/check_stock_ledger.py`) - must be done before go-live; #8 set is_head_office on the
  Branches page; #9 Finance posts Supply journals without the user; #10 kg free units.
- B still to do: (B2) repack, extra costs (the supplier's delivery charge as landed cost)
  and materials from `product_costs.py`; (B3) Online order consumption. Decision 24 says
  stock leaves by **scanning the label at packing**, never by FEFO, so Finance's
  `online_feed._allocate` is not moved as is: the scan (phase E) names the batch and Supply
  posts `stock.consumed` (Dr 5000 COGS / Cr 1300) at delivery, returns back in at the
  batch's cost; plus `stock_push` and `parallel_run`; (B4) Finance invoice matching from
  `receipt.posted` (a GRN document without a batch); (B5) remove Finance's stock tables,
  routes and screens + `tests/test_no_quantities.py`; `tools/check_stock_ledger.py`.
