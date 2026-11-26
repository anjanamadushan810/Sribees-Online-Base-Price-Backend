# Order in our own product names (owner 2026-11-26)

## Why
A supplier's bill names are not reliable: Nishi's bill had one "Aya napkin" line, but the
goods came in two colours, and we keep those as two products. Matching supplier names to
our products is busy work, and it gives wrong answers. So we order in **our** (SRIBEES
Online) product names. When the goods arrive, staff check them, put a QR label on each, and
submit what really came. What the supplier's bill calls an item does not matter.

## Owner decisions (MCQ 2026-11-26)
1. Orders name Online products (pack sizes). Supplier names are not used.
2. The **matching screen and its code are removed completely**. The suppliers' own price
   lists stay in the supplier app (their WhatsApp list tool), but they are not linked to our
   products.
3. Goods that were not ordered can be added by staff during receiving (staff app):
   - an existing Online product: search and add;
   - a new item: photo, name, MRP and price. It becomes a **request**. The owner approves it,
     and then it becomes a hidden Online product.
4. The real bill price is entered **at receiving** by staff. The order price is the default,
   and staff change it to the bill price. A photo of the bill is attached. That price is the
   base price.
5. Today's Nishi lines that were not entered (Aya, Velvet, Signal 40g, ...): store staff check
   the goods and tell the owner. Then today's buys are reset to one order per supplier
   (3 orders).

## Work and "done" (pass/fail)
| # | Work | Done when |
|---|------|-----------|
| P1 | Orders line = Online pack size + units + price (no supplier offer needed); new-order screen searches our products with photos; units, not boxes | a test makes, approves and receives an order with no supplier offers; the web new-order screen picks products by photo/name |
| P2 | Supplier app shows our product names on orders (Nishi) | the supplier order API returns our names; the app is checked on one order |
| P3 | Staff app receive: per line received units, bill price (default order price), QR per unit, bill photo; add an unordered existing product | a test receives with a changed price and an added product; base price = bill price |
| P4 | New item request at receiving: photo + name + MRP + price -> owner approves in Supply web -> hidden Online product made (Online internal endpoint) -> stock lands on it | a test covers request -> approve -> product -> stock; Online endpoint test; until approval the line is held, not lost |
| P5 | Matching removed: menu, page, API, services, suggestion worker; tests updated; tables kept for history | no /matching routes; whole suite green |
| P6 | Price watch: a bill price more than 10% above the last base price is flagged to the owner | a test flags it |
| P7 | Reset today's buys to 3 orders (after the staff count of Nishi's 11 lines): dumps first, guards off only inside that transaction, Supply and Finance both | Supply: 3 received orders, totals = bills; Finance: 3 POs/receipts that match |
| P8 | Release: Supply backend+web, staff app new version (APK), Online endpoint; push to 810/610 main+development; AI tool workspace updated | prod versions shown; APK version = pubspec = settings screen |

Order: P5 and P1 first (the core), then P3, P4, P6, P2, P8. P7 runs when the staff count of
the 11 Nishi lines arrives.
