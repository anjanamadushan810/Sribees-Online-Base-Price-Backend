# Sribees Supply — PLAN

Sribees Supply = suppliers, price comparison, ordering, base price, receiving (QR labels)
and physical stock, plus the supplier mobile app. (Started as "Base Price system"; the
GitHub repos keep their Base-Price / Suplier-Mobile names.)

Status: draft, 2026-11-12. Owner interview rounds 1–3 done. Waiting for the owner's
`.md` with two real Nishi Palace WhatsApp price lists.

## 1. Goal

Sribees buys stock from suppliers. Today the first supplier, **Nishi Palace**, sends a
WhatsApp price list. We need:

1. **Supplier mobile app** — the supplier keeps its price list up to date and receives
   our orders. It knows nothing about our base-price logic.
2. **Base Price backend + web** — collects supplier offers, matches them to the Online
   catalogue, compares them, places orders, and records the **base price = the price we
   actually paid**.
3. Long term: all island-wide buying runs through this system, so it is multi-supplier,
   multi-branch from day one.

## 2. Owner decisions (MCQ 2026-11-12)

| # | Decision |
|---|---|
| 1 | Separate system that **feeds Finance**. Finance keeps ledger, GRN, stock. |
| 2 | Supplier app v1 = price list + our orders (accept / reject). Delivery, invoices, payments later. |
| 3 | Supplier login = phone number + SMS OTP (Text.lk). |
| 4 | Price entry = paste the WhatsApp text (parsed, supplier confirms) **and** a per-item form. |
| 5 | **Base price = last price actually paid** on an order. Offers are only compared. |
| 6 | Item → our product: auto suggestion (brand + name + size must all agree) + our staff confirms once. Supplier never sees our catalogue. |
| 7 | `(150)` = **units per box** (the list's own legend: "(our price / selling price)(qty per box)"). We order in whole boxes, minimum 1. |
| 13 | **The supplier app is also the supplier's own price-list tool**: add products, update prices, mark inactive; a button builds their WhatsApp price-list message from app data and shares it to WhatsApp for their other customers. App = source of truth; paste = first import only. |
| 14 | Every price line is its own offer (Promo / 4+1 free / Double / size kept as written). |
| 16 | Purchasing confirms matches and places orders; branch managers can raise requests for their branch. |
| 17 | New order → supplier gets an app push notification (FCM); no SMS. Web shows "not seen yet" after 2 h. |
| 18 | **QR labels:** pre-printed in bulk, carrying only a number. At receiving, staff stick one on each unit and link the number to the batch (order line, base price, MRP, mfg date, expiry). Receiving + linking happen **in this system**, which then posts the GRN to Finance (Finance stays the stock ledger; one GRN per receipt, idempotent). A label number links to exactly one batch; printed ranges are registered; reuse is refused. Prices never go in the QR. |
| 19 | MRP differences are kept per batch; the web shows a quiet ⓘ only. |
| 20 | **This system owns physical stock** (owner, 2026-11-12; replaces "Finance owns stock" of 2026-10-26). Finance = money only, fed by stock events. See §4b. |
| 15 | Supplier delivery charge (e.g. "from Rs 100 per box") is recorded on the order separately; Finance spreads it as landed cost. Base price = supplier price only. |
| 8 | Supplier app language: **Sinhala + English** (default Sinhala). |
| 9 | Web users: owner, purchasing/stores, branch manager (own branch), accountant (read-only). |
| 10 | Order made here → pushed to Finance as a **PO**; Finance approval rules apply; supplier sees it only after approval. |
| 11 | Every order names a branch (Matugama now; never hard-wired). |
| 12 | Supplier app delivered as an APK via GitHub Actions first; Play Store later. |

## 3. WhatsApp price-list format (real list: `backend/tests/fixtures/nishi_palace_2026-11-12.txt`)

```
🛑🛑සබන් වර්ග 🛑🛑                    <- section
🟡බේබි ශෙරමි සබන් 75g                  <- product heading
(125/165)(150)                          <- (our price / MRP)(units per box)
(424/530)(36) 4+1 free pack             <- another offer, tail = variant label
```

Measured on the real list: 143 headings, 178 price lines; 172 regular (41 of them
with a tail such as `250ml`, `Promo`, `5+1 Free`); 6 irregular, which the importer
must flag for a human, never guess:

| Line | Problem |
|---|---|
| `(660)(100)`, `(1120)(1)` | no MRP |
| `(775/1360)`, `(1460/2550)` | no box size |
| `(8490)(14400)(720)` | `/` missing (8490/14400) |
| `(29/100)(12×26)` | box written as 12×26 |

Also: decimals (`24.50`), spaces in brackets, note lines (`(NMRA Approve) 4pcs`,
`( Rathmal/Delum/Almond )`, `(Import brand)`), one heading for two brands
(`කොකාකෝලා,ස්ප්‍රයිට්`), free-text offers (`සැමන් කේස් 100ට වැඩි ... Rs 420/-සිට`),
typos (`Frre`). Header/footer (contact numbers, delivery note) are kept as the
supplier's message template.

**Two directions:**
- **Import** (first time / migration): paste → parser → review screen with flagged
  lines → supplier confirms → products + offers created.
- **Export** (every day): app data → message in the same format (sections, 🟡 headings,
  offers, header/footer) → Android share sheet → WhatsApp. Round trip test: import the
  fixture, export, re-import → identical data.

## 4. Architecture

```
Supplier app (Flutter, si/en)  ──►  Base Price API (FastAPI + Postgres)  ──►  Finance API (PO)
                                          ▲      │
Base Price web (React + antd) ────────────┘      └──► Online API (catalogue read, service account)
```

- **Stack** (same as Finance / Price Intel, so the team knows it): Python 3.12, FastAPI,
  SQLAlchemy 2, Alembic, PostgreSQL, NUMERIC money; React + TypeScript + antd; Flutter.
- **Two auth worlds, never mixed:** staff (email + password, roles, branch scope) and
  suppliers (phone + OTP, scoped to their own supplier id only). Every supplier query is
  filtered by the token's supplier id on the server.
- **Main tables:** suppliers (+ message header/footer template), supplier_users,
  supplier_sections (their categories, order), supplier_products (heading as typed,
  section, sort order, active), offers (product, label e.g. "250ml"/"Promo", price, MRP,
  units_per_box, active, sort order), offer_prices (append-only price history),
  item_matches (offer → Online product) (supplier item → Online product, confirmed_by), orders + lines
  (branch, boxes ≥ 1, units = boxes × units_per_box, price frozen at order time,
  supplier delivery charge on the order), base_prices (product, supplier,
  price, order id, date — written only when an order is approved), audit_log.
- **Finance link:** approved order → `POST` Finance PO (idempotent key = order id).
  Finance approval status flows back; only approved orders reach the supplier app.
- **Online link:** read catalogue (name, brand, size, barcode) for matching; nothing is
  written to Online.
- **Hosting:** Contabo, its own folder, compose and DB (like Finance / Price Intel);
  dev first, prod only after the owner says so.
- **Security:** OTP rate limits + lockout, short-lived tokens, no secret in logs,
  never send test SMS (OTP provider is faked in tests and on dev).

## 4b. Stock ownership moves here from Finance (decision 20)

One rule: **exactly one system holds stock quantities.** From the cut-over on, that is
this system; Finance keeps no quantities of its own.

| Moves here (from Finance dev code, with its tests) | Stays in Finance |
|---|---|
| GRN / receiving, batches, expiry, FEFO | Ledger, journals, periods, reports |
| Landed cost per batch (extra costs shared by weight) | Payables, supplier invoices & payments |
| Transfers, counts, write-offs (+ approval limits), repack | Min-margin policy + min price push to Online |
| Stock push to Online + stock authority (6d) | Payroll, expenses, banking, COD |
| Consuming stock for Online orders (outbox events) | Opening balances (value), go-live |

- **Events to Finance** (outbox, idempotent, same pattern as Online → Finance):
  `grn.received` (value, supplier, branch), `stock.consumed` (order line, FIFO cost =
  COGS), `stock.written_off`, `stock.transferred`, `stock.count_adjusted`,
  `stock.repacked`. Finance posts journals from them and stores no quantities.
- **Online** sends its order events here (for stock) as well as to Finance (for money).
- **Move, not rewrite:** Finance's stock services and tests are carried over, then
  removed from Finance in the same release. Must finish **before Finance go-live**.
- Done for this part: Finance's existing stock tests pass here; Finance's ledger
  invariant (stock account 1300 == Σ batch value here, per branch) holds in a
  cross-system check; Finance has no table holding quantities.

## 5. Phases

1. Backend skeleton, staff auth, suppliers, supplier OTP login.
2. Supplier catalogue: sections, products, offers, active/inactive, price history;
   WhatsApp import (parser + review) and export (message builder).
3. Matching (auto suggestion + confirm queue in web).
4. Orders (whole boxes, branch, delivery charge) → Finance PO → supplier accept/reject.
5. Base price page (paid price per product, history, compare offers).
5b. Receiving: label ranges, receive against an order, scan labels → batch
   (mfg/expiry/MRP), GRN → Finance.
6. Supplier app (Flutter, si/en): login, my products & prices, add/inactive, import,
   **share price list to WhatsApp**, orders. APK via Actions.
7. Dev deploy, owner test with Nishi Palace.
8. Stock move from Finance (§4b): batches/transfers/counts/write-offs/repack,
   Online order consumption, stock push to Online, events to Finance; remove stock
   from Finance. Before Finance go-live.

## 6. Done (pass/fail)

- [ ] `pytest tests/test_whatsapp_parser.py`: on the real fixture, 172 regular lines
      parse to the exact heading / label / price / MRP / units per box (expected table
      in the test), and exactly the 6 irregular lines come back flagged, none guessed.
- [ ] `pytest tests/test_whatsapp_export.py`: import fixture → export → re-import gives
      identical products and offers; inactive products are left out of the message.
- [ ] `pytest tests/test_flow.py`: supplier OTP login (fake SMS) → import → confirm →
      staff match → order of 0 boxes refused → order of 2 boxes = 2 × units_per_box
      units → Finance PO created
      once (retry does not duplicate) → approved → base price = order price.
- [ ] `pytest tests/test_supplier_isolation.py`: supplier A cannot read or change
      anything of supplier B (every supplier endpoint).
- [ ] Supplier app: `flutter analyze` clean; APK version == pubspec == profile screen.
- [ ] Dev: the owner logs in as Nishi Palace on the APK and sees a real order.

## 6a. Status 2026-11-13 — PRODUCTION LIVE

- Prod: https://supplier.sribeesonline.com (API + staff web), /opt/sribees-supply-prod,
  nightly backup, ORDER_APPROVAL=finance (orders wait for Finance; owner wants a joint,
  final go-live with Finance). Dev: https://devsupply.sribees.com (Nishi seeded).
- Online /internal/supply/catalog live on dev + prod; 889 prod products mirrored.
- Staff web (frontend/) and supplier APKs (dev + prod, debug-signed) built.
- 810 repos (public, owner's choice): Base-Price-Backend / -Frontend / Suplier-Mobile,
  main == development.
- Next: Supply -> Finance PO link; stock move Finance -> Supply (§4b); Finance prod
  go-live inputs; receiving + QR (5b); FCM; release keystore; prod branch + Nishi.

## 6b. Status (2026-11-12, local only — nothing pushed or deployed)

| Phase | State |
|---|---|
| 1 Foundation (staff auth, suppliers, supplier OTP) | done, tested |
| 2 Catalogue, WhatsApp import/export, price history | done, tested |
| 3 Matching (Online mirror, Sinhala romanizer, queue) | done, tested; Online `/internal/supply/catalog` NOT built (QUESTIONS #1) |
| 4 Orders → approval → supplier answer, base price, outbox | done, tested; approval local (owner) until Finance (QUESTIONS #9) |
| 5 Base price page | API done; staff web UI not started |
| 5b Receiving + QR labels | not started (printer/device questions) |
| 6 Supplier app (Flutter si/en) | screens done, analyze clean; not run on a device yet; APK workflow ready |
| 7 Dev deploy | Dockerfile + CI ready; needs QUESTIONS #5/#6 |
| 8 Stock move from Finance | not started |

Backend: 125 tests (`python .tools/run_supply_tests.py -q`). A fresh-context security
review found 1 high + 3 medium issues; all fixed with regression tests (commit 69dda32).

## 7. Open questions

- Label printer / label size and how many numbers per roll (before phase 5b).
- Staff scanning device for receiving (Price app, New Orders app, or a new screen).
