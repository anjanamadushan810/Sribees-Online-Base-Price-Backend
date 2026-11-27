# Split one order line into batches at receiving (2026-11-27)

## Task (owner)
One ordered product can arrive with two prices and two expiry dates (e.g. 48 ordered:
30 at one MRP/expiry, 18 at another). Staff must split the line's card into parts at
receiving; each part becomes its own stock batch.

Also (owner, same session): in the staff app camera, after a QR is accepted the camera keeps
seeing it and shows a red "already scanned" that stays on screen and looks like an error.
The message should go away after about 2 seconds.

## MCQ answers (owner 2026-11-27)
1. Where: all three: Supply web Receive page, staff app form view, staff app scan view.
2. A part may differ in everything: expiry, MRP, bill price, free units, shop prices. Every
   part = own receipt line = own stock batch + own base price row + own Online batch send.
3. Web Receive gets an MRP field per part.
4. QR labels per part: each part needs as many QR labels as its units (+ free).

## What exists
- `receiving.receive(split_lines=True)` already accepts several receipt lines per order line
  (staff-app drafts use it); each receipt line -> its own batch (`RCV-x-n`), expiry, MRP,
  base price, OnlineStockSend, price review. Units of the parts together stay within due.
- Staff scan view can already add the same product twice; nothing tells staff so.
- Web POST /orders/{id}/receipts uses split_lines=False -> refuses a repeated order line.

## Build
- B1 backend: web receipts allow split lines (same rule as drafts: parts of one line sum
  to <= due; extras of the same product merge into one new order line). Test.
- B2 web: Receive page keeps a list of parts per card; "+ another batch" adds a part,
  each part has units, bill price, free, MRP, expiry, shop prices, QR; a part can be removed;
  total units of parts <= due; preview matched by position, not by order line id.
- B3 staff form view: same parts per line.
- B4 staff scan view: when the scanned product already has saved parts, say so and offer
  "another batch (different price/expiry)"; units default to what is still due.
- B5 staff camera: accepted codes are never re-read in that camera session; the message
  clears itself after 2 s; a refused code held in view is not re-reported until it leaves view.

## Guardrails
- QR never counts twice (unchanged). Expiry required on every part (unchanged).
- No migration. Finance event already carries several lines per order line (drafts path).
- dev first; prod only after an owner MCQ.

## Done = these pass
- backend test: web receipt with one order line split 30 + 18 (different expiry, MRP, bill
  price) -> 2 receipt lines, 2 batches with the right qty/expiry/unit cost, 2 base prices;
  split parts above due -> refused.
- frontend unit tests (receiveLogic): parts -> receipt lines, short units, goods total,
  problem() per part; `npm run build` + vitest green.
- staff app: `flutter analyze` clean + `flutter test` green.
- full backend suite green (run_suite.py).
