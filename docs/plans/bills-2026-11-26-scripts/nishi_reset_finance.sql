BEGIN;
CREATE TEMP TABLE xp ON COMMIT DROP AS SELECT po_id FROM purchase_orders WHERE supply_order_id IN ('c606d120-fc45-403e-aaeb-0e8bc06cee89','602fa158-9c86-412d-970f-7ce15c307094','0b7dac32-8ca4-4683-a9d1-3ae79277a97c');
CREATE TEMP TABLE xr ON COMMIT DROP AS SELECT supply_receipt_id FROM supply_receipts WHERE supply_receipt_id IN ('d75308da-3ab3-4d87-8ae3-b4665a604c73','3bc08882-d1aa-4fd1-87d3-bf43369cc308','1cea560a-fa90-411f-a8f1-a55b2a96f1cd');
CREATE TEMP TABLE xi ON COMMIT DROP AS SELECT invoice_id FROM supply_receipts WHERE supply_receipt_id IN ('d75308da-3ab3-4d87-8ae3-b4665a604c73','3bc08882-d1aa-4fd1-87d3-bf43369cc308','1cea560a-fa90-411f-a8f1-a55b2a96f1cd') AND invoice_id IS NOT NULL;
CREATE TEMP TABLE xj ON COMMIT DROP AS
  SELECT journal_id FROM supplier_invoices WHERE invoice_id IN (SELECT invoice_id FROM xi) AND journal_id IS NOT NULL
  UNION SELECT journal_id FROM journals WHERE source_ref IN (SELECT 'supply:receipt:'||x FROM unnest(ARRAY['d75308da-3ab3-4d87-8ae3-b4665a604c73','3bc08882-d1aa-4fd1-87d3-bf43369cc308','1cea560a-fa90-411f-a8f1-a55b2a96f1cd']) x);
SELECT 'journals to remove', count(*) FROM xj;
SELECT 'debits', sum(debit), 'credits', sum(credit) FROM journal_lines WHERE journal_id IN (SELECT journal_id FROM xj);
ALTER TABLE journals DISABLE TRIGGER USER;
ALTER TABLE journal_lines DISABLE TRIGGER USER;
DELETE FROM supplier_invoice_lines WHERE invoice_id IN (SELECT invoice_id FROM xi);
UPDATE supply_receipts SET invoice_id = NULL WHERE supply_receipt_id IN (SELECT supply_receipt_id FROM xr);
DELETE FROM supply_receipt_lines WHERE supply_receipt_id IN (SELECT supply_receipt_id FROM xr);
DELETE FROM supply_receipts WHERE supply_receipt_id IN (SELECT supply_receipt_id FROM xr);
DELETE FROM supplier_invoices WHERE invoice_id IN (SELECT invoice_id FROM xi);
DELETE FROM journal_lines WHERE journal_id IN (SELECT journal_id FROM xj);
DELETE FROM journals WHERE journal_id IN (SELECT journal_id FROM xj);
DELETE FROM po_lines WHERE po_id IN (SELECT po_id FROM xp);
DELETE FROM purchase_orders WHERE po_id IN (SELECT po_id FROM xp);
ALTER TABLE journals ENABLE TRIGGER USER;
ALTER TABLE journal_lines ENABLE TRIGGER USER;
INSERT INTO audit_log (user_id, action, entity, after)
VALUES (NULL, 'reset.pre_go_live', 'purchase_order',
        jsonb_build_object('supply_orders', 'SPO-000010, SPO-000011, SPO-000012 (Nishi quick buys)',
                           'removed', '3 POs, 3 supply receipts, 3 matched invoices, 6 journals (Rs 513,630 each side twice)',
                           'why', 'owner 2026-11-27: one Nishi order, received again by staff with QR labels'));
SELECT 'left pos', count(*) FROM purchase_orders WHERE supply_order_id IN ('c606d120-fc45-403e-aaeb-0e8bc06cee89','602fa158-9c86-412d-970f-7ce15c307094','0b7dac32-8ca4-4683-a9d1-3ae79277a97c')
UNION ALL SELECT 'all journals', count(*) FROM journals
UNION ALL SELECT 'ledger off by', (SELECT coalesce(sum(debit),0)-coalesce(sum(credit),0) FROM journal_lines)::bigint;
COMMIT;
