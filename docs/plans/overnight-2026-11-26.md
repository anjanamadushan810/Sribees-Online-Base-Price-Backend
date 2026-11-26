# Overnight plan 2026-11-26: QR labels, staff app, pack scan, rest of phase B

Owner approved the order and scope by MCQ on 2026-11-26 (see "Owner answers"). Joint go-live
is 2026-12-01 ([joint-go-live.md](joint-go-live.md)). Work runs in this order; each step
must reach "Done" before the next step starts. If a step is blocked, I write the blocker in
`QUESTIONS.md`, skip to the next step, and ask the owner in the morning.

## Owner answers (2026-11-26)
| # | Question | Answer |
|---|----------|--------|
| 1 | Scope tonight | Everything in order: C → D → E → B rest → G1 |
| 2 | Push / deploy | Push to **development** and deploy to **dev** is OK. Never main / prod without asking. |
| 3 | Sticker sheet | **Near 100 per A4, whatever fits best** (owner corrected: not exactly 100) → **8 × 12 = 96** near-square labels (25 × 23.75 mm, QR ≈ 18 mm), set in settings |
| 4 | Scanner | Phone camera **and** a Bluetooth/USB scanner (keyboard wedge) |
| 5 | Staff app delivery | Through **SriBees Hub** (APK from Actions, then the Hub catalog) |
| 6 | Blockers | Work the usual way; ask the owner in the morning; secrets never printed, kept in the D drive folder for that system |
| 7 | Number on the label | **QR only.** A small QR leaves no room for printed digits. The number is in the QR and in the PDF's list. |

## Step 0: bring in the 2026-11-25 server work (DONE before this plan)
Yesterday's Claude Code session on the AI tool server pushed to **main and development**
and deployed **to production**: Supply prod runs image 674db27 at migration 0011, Finance
prod runs 71efc69 at 0017.
- Supply (local `integrate/server-2026-11-25`): 5 backend + 16 web + 2 app commits applied.
  Web and app are byte-identical to GitHub main. The local stock migrations are renumbered
  **0012/0013 after production's 0010/0011**. If we had followed the "set 0010's
  down_revision to 0009" note instead, production would skip the stock tables. 198 tests pass.
- Finance (local `integrate/server-2026-11-25`): both folders are taken from GitHub main
  (it includes Ashen's wallet PR #6 and Yomal's UI pages), and phase B is applied on top.
  205 tests pass, ruff is clean.
- Online backend local main is fast-forwarded to 869b5d5. New Orders is fast-forwarded to 11e46dd.
- Points for the owner: (a) the server session pushed `__pycache__/*.pyc` into the Supply
  backend repo (I remove them on the next push); (b) Finance now has **Quick buy**, which
  receives goods in Finance. Decision 21 says every buy goes through Supply, so Quick buy
  moves to Supply in B5 (step 8); (c) Online's `/internal/finance/branches` is on main but
  not on development.

## Steps
### 1. C: QR labels (Supply backend + web)
- Number = 9 digits + 1 check digit (mod 10). The QR payload is the 10 digits only
  (numeric mode, version 1 even at level M), never a price.
- Tables `label_ranges` (start, count, printed_by, printed_at, branch) and `labels`
  (number → batch, linked_by/at). A label is linked once (unique + trigger, never reused).
- `POST /labels/ranges` registers the next N numbers and returns a PDF: A4, layout from
  settings (default 8 × 12 = 96), QR only, plus a last page listing the numbers.
- Web page "Labels": print a range, look up a number (batch, product, expiry).
- **Done:** tests pass for each refusal (a label linked twice, an unregistered range, a bad
  check digit); the PDF has N QR codes that decode back to the same numbers (zxing/pyzbar in
  the test); the migration replays twice on a throwaway Postgres 18; ruff passes; full suite green.

### 2. D: Supply staff app (new Flutter APK)
- New folder `supply/staff-app` and a new repo on 810 (`Sribees-Online-Supply-Staff-App`).
  Package `com.sribeesonline.supply_staff`. Sinhala + English. Staff sign-in with Supply
  staff users, branch-scoped.
- Screens: Receive an order (lines, qty, free, expiry/mfg/MRP, scan one label per unit
  or "next N from this range", invoice no + total + photo), Label lookup, Count by scan,
  Transfer ship/receive by scan, Write-off request.
- Scanning: camera (mobile_scanner) plus a keyboard-wedge text field that is always focused.
- Release keystore made now. Its secrets go to secrets.out + `D:\sribees online github\supply-staff\`.
  Actions builds the APK (dev API on development).
- **Done:** `flutter analyze` is clean; unit tests pass; CI APK version == pubspec == profile
  screen; signed with the release key; the APK installs and signs in against devsupply.

### 3. E: pack scan (Online backend + New Orders app)
- Supply `POST /internal/pick` (service token): it checks that the label belongs to the
  order line's product, that the batch has stock and is not expired, and that the label was
  not scanned twice. Bulk bag = label + weight (no more than on hand).
- Online development `POST /staff/orders/{id}/pack-scan` → Supply (synchronous). This is
  **behind a setting, OFF** until go-live, so prod packing is unchanged.
- New Orders app: a packing screen where every unit is scanned (weight for bags). "Packed"
  is locked until every line is scanned when the setting is ON.
- Cancel after packing / return → the stock goes back to the same batch (or quarantine).
- **Done:** Online `ruff check .` + full suite green; Supply tests: each refusal above,
  and a cancel puts the stock back; New Orders `flutter analyze` + tests pass; version bumped;
  a fresh-context reviewer agent finds no unfixed HIGH.

### 4. B3: stock leaves at delivery (`stock.consumed` Dr 5000 / Cr 1300 at the scanned batch cost)
### 5. B4: Finance invoice match from `receipt.posted` (zero tolerance; the accountant sees only mismatches)
### 6. B2: repack, landed cost (the supplier's delivery charge), materials
### 7. Test dates: the hard-coded 2026-09/10 dates in Supply tests become relative (they would fail after 2026-12-31)
### 8. B5: remove Finance stock tables/routes/screens; Quick buy → Supply cash buy; `tests/test_no_quantities.py`; `tools/check_stock_ledger.py`
- **Done for 4–8:** both suites green; `check_stock_ledger.py` = OK on dev; a fresh-context
  reviewer finds no unfixed HIGH (money/stock = high risk).

### 9. Push + dev deploy + G1
- Push development only (gitleaks first): Supply ×4 repos, Finance ×2, Online backend
  (development), New Orders (development). Deploy devsupply, devfinance, and Online dev.
- G1 script on dev: order → approval → supplier accepts → receive with labels → pack scan →
  stock to Online → journals → `check_stock_ledger` OK.
- Hub: the staff app is added to the Hub catalog as a **private app (owner's invite only)**
  once G1 passes.
- **Done:** G1 prints PASS; every dev deploy is green; nothing is on main or prod.

## Never tonight
main / prod pushes or deploys; real SMS; paid AI tests; deleting data; printing secrets;
`git add -A`; touching other people's open branches.

## Morning report
`supply/docs/plans/overnight-2026-11-26.md` §Status (below) + a handoff memory: what is
done, what is left, questions for the owner.

## Status (2026-11-26 10:30 SLST)
| Step | State |
|---|---|
| 0 Server 11-25 work merged | DONE |
| 1 C QR labels | DONE: backend + web Labels page; 8 x 12 PDF decodes (zxing test); live on devsupply (smoke: LBL-000001) |
| 2 D staff app | CODE DONE 0.1.0+1 (analyze clean, tests green, release keystore made + backed up in D:\sribees online github\supply-staff\). NOT BUILT: the 810 token cannot write the new private repo (QUESTIONS N1) |
| 3 E pack scan | DONE behind switches (Online SUPPLY_PACK_SCAN_ENABLED=false). Fresh-context review: 5 HIGH + 5 MEDIUM fixed with tests (gap wait, dead letter, refund without goods, RTO, bag returned twice, journal ref per event, retry, outbox required, weighed/no-pack-size lines, held units guarded) |
| 4 B3 delivery consumption | DONE as part of E (stock out at delivery, sales journal; Finance allows source "sales") |
| 5 B4 invoice match | DONE (Finance mig 0018): typed invoice with the exact total posts itself, else invoice_mismatch exception |
| 6 B2 repack / landed cost | NOT STARTED |
| 7 Test dates | DONE (D() helper) |
| 8 B5 remove Finance stock, Quick buy to Supply | NOT STARTED |
| 9 Push + dev deploy | Pushed development: Supply x3, Finance backend, Online backend, New Orders. Deployed dev: Supply API + web (alembic 0016), Finance API + web (0018; its dev .env had lost the Supply link keys after the 11-25 deploy, restored). Online dev deploy + New Orders APK: see handoff. G1 full E2E not run (pack-scan tokens not wired on dev yet). Nothing on main or prod. |

Open from the review (not fixed, low or needs the owner): returns go straight back on sale (Finance's old rule was quarantine); a partial return re-links the first labels, not the stickers that came back (scan at return later); a run printed for one branch can be stuck on another branch's stock; a scan racing a cancellation can leave one hold until the order's next event; the super-admin override and the legacy PUT status route skip the packed check; Finance's own Online cost posting must be off at go-live (WORKER_SUPPLY_ONLY) or Supply-handled orders are costed twice.
Online suite: 5 failures in referral promotion tests are on origin/development without these changes (pre-existing).

## Release day before (2026-11-26 afternoon; owner MCQ: Supply + Finance + staff app to production)
- PRODUCTION: Supply API + web (alembic 0017, image 57385bb) and Finance API (0018, ef86e79) are live. The box
  settings files were kept as they were (Finance WORKER_SUPPLY_ONLY=true, Supply ORDER_APPROVAL=finance). DB dumps
  were taken first (backups/pre-release-20261126.sql.gz on both). 810 main == development on all four repos.
- The staff app repo stays on 310 (owner). Prod APK 0.1.0+1 is built from main with the release key, in sibees/builds/.
- Quick buy moved to Supply (the owner's first priority): POST /quick-buys + a web page; Finance matches the bill.
- Left: Finance's own Quick buy / PO / GRN screens still exist (B5 hides or removes them); B2; prod E2E; Hub.
