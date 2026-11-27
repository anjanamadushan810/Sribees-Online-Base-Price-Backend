# Barcode-first receiving, batch prices and expiry alerts (2026-11-27)

Status: **plan, waiting for the owner's OK**. Branch: `development` only. Main / production only after
the owner says so. Risk: **high** (stock quantities and customer prices) -> fresh-context reviewer, no
unfixed HIGH before any production question.

## The task (owner, 2026-11-27)
Store staff take a new delivery into stock one product at a time:
1. Scan the product's **barcode** -> the product is identified.
2. The system asks for **quantity**, **MRP** (the old one shown, editable) and **expiry date** (calendar).
3. The QR scanner opens and wants **exactly that many** pre-printed QR labels; staff stick one on each
   unit and scan it.
4. Saved: the product becomes **one batch** in inventory, and each QR is linked to that batch.
5. Next product from the same delivery: repeat.
Then: easy inventory management, and **alerts for goods close to expiry**.

## Owner decisions (MCQ 2026-11-27)
| # | Question | Decision |
|---|----------|----------|
| 1 | Unknown barcode | **Linked on first scan**: staff search our product once; from then on the barcode finds it |
| 2 | Link to a Supply order | **Pick the order first**, then scan. A product not on the order = "added at receiving" (existing P3 rule: existing Online products only) |
| 3 | Expiry date | **Always required** (no "no expiry" option) |
| 4 | QR labels | **One QR per unit** (48 soaps = 48 scans); the scanner stays open until the count is reached |
| 5 | Alert lead time | **Per product**: category default + product override; nothing set -> 30 days |
| 6 | Where alerts show | **Supply web home**, **staff app** (list + push), **Online admin** (inventory manager) |
| 7 | No barcode (loose rice, local goods) | **Tap the line in the order list**, then the same steps |
| 8 | MRP change | Old stock sells at the old price; **the new batch starts at the new price automatically** (Online batches already sell in receipt order, PR #49) |
| 9 | New batch selling price | **Same discount % as now** (old MRP 200 sold at 180 -> new MRP 220 sells at 198). A list (photo, name, base price from the order, MRP, selling price) goes to the **branch manager's dashboard**; they may edit the selling price and submit |
| 10 | Expired batch | **Cannot be sold**: packing scan of its QR is refused; it appears on a write-off list |

## What exists today (code read 2026-11-27)
- Staff app `receive.dart`: order list -> tap line -> units, free, weight, MRP, expiry, mfg, QR bag
  (limit = units + free). Expiry is optional. No barcode.
- Supply `stock_batches.expiry_date` (nullable), `labels` linked to a batch, receiving with transport
  split by weight (0022), added-at-receiving lines + bill photo (0023).
- Online `inventory_batches` (PR #49): `expires_on`, `listed_price`, `selling_price`, FIFO by sequence.
  `POST /internal/supply/receipts` (mig 128) adds stock once per receipt line (off until
  `SUPPLY_STOCK_IN_ENABLED`). FIFO does **not** skip expired batches.
- **No product barcode anywhere** (Online or Supply). No expiry alert anywhere.
- Supply roles already include `branch_manager` (own branch).

## Starting point and numbers (peer sessions, 2026-11-27)
- Start from Supply main @ 7b9597c (= pushed + deployed); 810 Backend f5f7a22, Frontend 0745cb3,
  Staff app 6480114 (0.3.0+3: bill price, transport, short-close, unordered product, bill photo).
  Online 610 main 638ce15.
- Supply migration **0024 is reserved by workspace-33** (max stock + automatic draft POs) -> this work
  uses **0025+**. Online uses **129+**.

## Design (architect decisions, owner may overrule)
- **D1 Barcodes live in Supply**: `product_barcodes(barcode PK, product_id, variant_id, created_by,
  created_at)`. One barcode -> one pack size; one product may have several barcodes (new packaging).
  Digits only, 8-14 long, EAN/UPC check digit verified (a mis-read is refused, not linked). Re-linking a
  barcode to another product = branch manager or above, audited.
- **D2 Draft receipt saved after every product** (server side, one draft per order per user): a phone
  crash or a dead battery loses nothing. "Finish" turns the draft into the real receipt (transport split by
  weight, bill no + total + photo, Online stock push), exactly as today. The QR labels are **reserved**
  while in a draft, so the same QR cannot go on two products.
- **D3 Expiry date rules**: required on every line; must be after the received date; a date more than
  10 years ahead is refused (a typing mistake). One batch = one expiry date. A box with two dates -> scan
  the product twice (two lines, two batches).
- **D4 New batch price** (Online stock-in): `selling = floor(new MRP x current selling / current MRP)`
  to whole rupees, never above MRP. No current price -> selling = MRP. The batch starts selling only when
  the older batches are gone (FIFO, no change). A **price review** row goes to the Supply web dashboard
  of the branch's manager (photo, name, base price, old MRP / selling, new MRP / proposed selling).
  The manager submits as is or edited; Supply calls Online to set that batch's selling price.
  Guards: selling <= MRP; selling below the base price is blocked (owner only). Not reviewed yet = the
  automatic price stays (sales never wait for a review).
- **D5 Alert days live in Online** (the product master): `categories.expiry_alert_days`,
  `products.expiry_alert_days` (null = use the category, then 30). Set in Online admin; sent to Supply in
  the catalog sync.
- **D6 Expired = not sellable**: Online FIFO skips batches with `expires_on < today (Asia/Colombo)`; the
  product shows out of stock if only expired units are left. Supply refuses a packing scan of an expired
  QR. Write-off list in Supply (existing write-off flow), not automatic.
- **D7 Alerts**: one daily job (06:00 Asia/Colombo) builds the list: `near` (within the product's alert
  days, amber) and `expired` (red). Shown on the Supply web home, the staff app (list + one push per day
  to the branch's store staff, no SMS), and an Expiry page in Online admin for the inventory manager.

## Phases
| # | Work | Repos |
|---|------|-------|
| P1 | Barcodes: table, check-digit validation, lookup + link API, audit | Supply backend |
| P2 | Draft receipts (save per product, reserve QRs, finish = today's receive) | Supply backend |
| P3 | Staff app barcode-first flow: pick order -> scan -> product screen (qty, old MRP editable, expiry calendar required) -> QR scanner until exactly N -> saved -> back to scan; unknown barcode -> search + link; no barcode -> tap line | Staff app |
| P4 | Expiry required (backend + web Receive page + quick buy) | Supply backend + frontend |
| P5 | Online: alert days (category + product, admin form), FIFO skips expired, batch price rule D4, set-batch-price internal API, catalog sends alert days | Online backend + admin |
| P6 | Price review list for the branch manager (Supply web) | Supply backend + frontend |
| P7 | Expiry alerts: daily job, Supply web home, staff app list + push, Online admin Expiry page, packing scan refuses expired | Supply, Online, staff app, New Orders app |
| P8 | Dev end-to-end on devsupply + Online dev, then fresh-context review | all |

## Guardrails
- Online stock is only ever **increased** by receiving, once per receipt line (existing rule).
- A QR label belongs to one batch only; a QR from an unregistered range, already used, or reserved by
  another draft is refused with the reason.
- The scanner never accepts more QRs than the quantity; finishing a product with fewer is refused.
- **A QR can never count twice** (owner 2026-11-27: "24 in the batch, 23 scans and the batch is
  empty"). Four layers: (1) the app refuses a QR already scanned anywhere in this delivery, not only
  on this product; (2) every scan is checked by the server at once (draft reservation, a DB unique key
  on the label number, so two phones cannot take the same QR); (3) at finish each label moves
  printed -> linked under a row lock, so it belongs to one batch for ever; (4) at packing a used QR is
  refused (exists today). Batch quantity = the number of linked labels, never a typed number alone.
- Customer prices change only through D4 (automatic, same discount) or a manager's review; never above MRP.
- Money stays in Finance; nothing here posts journals beyond today's receipt events.
- Other sessions' files are not touched; changes are staged by name.

## Done (all must pass)
1. Test: barcode `4792036000012` (valid) links once and the next lookup returns the same product; a
   wrong check digit (`4792036000013`) is refused.
2. Test: draft with 3 products saved one by one survives a restart; finishing it gives the same receipt
   and batches as today's receive call (transport split tests still pass unchanged).
3. Test: quantity 48 -> the 49th QR is refused; finishing with 47 is refused; a QR reserved by another
   draft is refused.
3b. Test (double scan): the same QR on two products of one draft is refused; the same QR in two drafts
   at once (two phones) -> only one wins; a QR already linked to a batch is refused at receiving; a
   used QR is refused at packing; after receiving 24 units, exactly 24 distinct labels are on the batch.
4. Test: a receipt line without an expiry date, or with expiry <= received date, is refused (422).
5. Test (Online): MRP 200 / selling 180, new batch MRP 220 -> batch selling 198; old batch still sells
   first at 180; after it is gone the price is 198.
6. Test (Online): a batch with `expires_on` = yesterday is skipped by FIFO; only-expired stock = out of
   stock.
7. Test: manager edits the review price to 195 -> Online batch selling = 195; 230 (above MRP) refused;
   below base price refused for branch_manager, allowed for owner.
8. Test: category days 3, product override 60, none -> 30: the daily list puts each batch in the right
   group; one push per branch per day.
9. Test: packing scan of an expired QR is refused.
10. Full Supply + Online suites green; `ruff check .`; `flutter analyze` clean on the staff app.
11. Dev E2E on a phone: one real order, 2 barcoded products + 1 tapped line, QRs scanned, batches on
    devsupply and Online dev show the right expiry and price; screenshot kept in this folder.
12. Fresh-context reviewer: no unfixed HIGH.

## Open (assumptions, say if wrong)
- A1 The branch manager's dashboard = **Supply web** (it has the base price and the `branch_manager`
  role). Online admin gets only the Expiry page.
- A2 A batch's price can be changed by review any time; the change applies to its unsold units.
- A3 Barcodes are linked by store staff (any role that may receive); changing an existing link needs a
  branch manager.

## Status (2026-11-27, local commits, nothing pushed or deployed)
| Phase | State | Where |
|-------|-------|-------|
| P1 barcodes | done | Supply 7fc3a03 (mig 0025) |
| P2 drafts + QR held per scan | done | Supply e9da1ec (mig 0026); receive() takes split_lines/draft_id |
| P3 staff app flow | done, not built | Staff app 0.4.0+4 54ba44a (receive_scan.dart) |
| P4 expiry required | done | Supply 0e750cf (API, web Receive, Quick buy) |
| P5 Online | done (Online 0fb1b5d; full suite 2806 pass, 5 referral-promotion failures are on clean main too) | Online branch feat/batch-expiry-price (mig 129): FIFO skips expired, sweep (BATCH_EXPIRY_ENABLED, off), new MRP = same discount, dated line = batch, batch price API, alert days on product/category, admin GET /inventory/expiring |
| P6 price reviews | done | Supply fd613f5 (mig 0027) + web home 06973b8 |
| P7 expiry alerts | partly | Supply 07d87c4 (mig 0028, GET /stock/expiring), web home, staff app list eedb709. **Not done:** daily push to staff (the staff app has no push yet), Online admin Expiry page + alert-day fields (admin repo) |
| P8 dev E2E + reviewer | not started | |

Design decisions taken while building (owner may overrule):
- A price review row is made only when the new batch's price differs from the old one or sells
  below what we paid (not for every delivery).
- Re-linking or removing a barcode = purchasing / owner (products.write); store staff link new ones.
- Online: loose and packed goods have no batches, so their expiry is enforced by Supply's packing
  scan (an expired QR is refused), not by Online's FIFO.
- Migration 0025's down_revision is 0023 until workspace-33's 0024 lands; then set it to "0024".
