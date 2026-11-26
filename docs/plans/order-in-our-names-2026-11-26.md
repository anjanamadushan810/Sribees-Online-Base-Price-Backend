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
3. Goods that were not ordered can be added by staff during receiving, but only **products we
   already have** on Online (search and add). A brand-new product is made **only by the super
   admin** in Online admin (owner 2026-11-26, later the same day; replaces the request idea).
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
| P4 | DROPPED (owner): new products only by the super admin in Online admin | - |
| P5 | Matching removed: menu, page, API, services, suggestion worker; tests updated; tables kept for history | no /matching routes; whole suite green |
| P6 | Price watch: a bill price more than 10% above the last base price is flagged to the owner | a test flags it |
| P7 | Reset today's buys to 3 orders (after the staff count of Nishi's 11 lines): dumps first, guards off only inside that transaction, Supply and Finance both | Supply: 3 received orders, totals = bills; Finance: 3 POs/receipts that match |
| P8 | Release: Supply backend+web, staff app new version (APK), Online endpoint; push to 810/610 main+development; AI tool workspace updated | prod versions shown; APK version = pubspec = settings screen |

Order: P5 and P1 first (the core), then P3, P4, P6, P2, P8. P7 runs when the staff count of
the 11 Nishi lines arrives.

## Status
- 2026-11-26 night: P1 + P5 done and LIVE (Supply 0021, prod/dev d72c4e7, web too; pushed
  backend 9330c9e, frontend ac6d3cc). Receiving already takes the bill price per line
  (`unit_price`) and writes the base price from it.
- P3 handed to the server session workspace-33 (owner MCQ): it builds the receive screens
  (web + staff app) with the bill price, adding existing products, transport by weight
  (base_prices.bill_price + delivered unit_price, its migration 0022), short orders close.
- P6 done and LIVE (prod/dev e100fff; pushed backend 2c80bc1, frontend 8affacc): home row
  "price_jumps" + "Prices we paid that changed" with a 10% flag. workspace-33 switches the
  query to COALESCE(bill_price, unit_price) in its 0022.
- Left: P7 (reset to 3 orders) after the store staff count Nishi's 11 lines; P2 check of the
  Nishi app on one real order; P8 staff app release comes with workspace-33's receive work.
