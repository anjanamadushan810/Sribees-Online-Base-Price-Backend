# Held without QR (owner 2026-11-27)

## Task (owner)
On busy days part of a delivery is checked (count, price) and kept aside WITHOUT QR labels;
later, when needed, QR labels go on and it is taken into the shop. The future SriBees
Wholesale section will also sell goods without unique QR tracking.

## MCQ answers (owner 2026-11-27)
1. Held goods count in stock and Finance the day they arrive (paid; bill matches at once).
2. Expiry is entered for the held part too (expiry alerts work).
3. Held stock is NOT sold on SRIBEES Online; only labelled units are (packing scans the QR).
4. Only "Hold" now; Wholesale later reuses the unlabelled stock.
5. Hold = a batch part at receiving (like "another batch"), in the staff app and the web.
6. Taking in = part by part: choose a held batch, scan N QR labels -> N into the shop and Online.
7. Any receiving staff may hold; who/when is recorded (audit) and the manager sees it.
8. Reminder = shown on the Supply web home and the staff app Receive tab (no push).

## Design
- Migration 0029: `receipt_lines.hold`, `receipt_draft_lines.hold`, table `stock_releases`
  (one row per take-in: units, Online send state; its id is Online's idempotency key).
- Receiving: a held part takes no labels, gets no `online_stock_sends` row. Weighed goods
  cannot be held yet.
- `labels.link` refuses a held batch unless called by a release (so Online never misses units).
- `app/services/held.py`: list/summary (left = held units - released, capped by stock on
  hand), release (labels linked under the batch lock, labels held by a draft refused),
  worker sends pending releases to Online (`POST /internal/supply/receipts`, line_id =
  release id), price review once per received line.
- API: GET /stock/held, GET /stock/held/summary, POST /stock/held/{receipt_line_id}/release,
  POST /stock/held/releases/{id}/retry.
- Web: Receive page part switch "Hold without QR"; Home card; page /held-stock with take-in.
- Staff app 0.5.0+8: switch on both entry screens; Receive tab banner; "Held without QR"
  screen with scan-to-take-in.

## Done = these pass
- backend tests/test_held.py: hold part takes no QR; 48 = 12 QR + 36 held all in stock and
  the receipt total; generic label link on a held batch refused; release 10 -> 26 left;
  more than left refused; reused label refused; label held by a draft refused; weighed goods
  refused; Online gets only the shop part at receipt and exactly one send per release.
- full backend suite, web vitest + build, flutter analyze + test green.
