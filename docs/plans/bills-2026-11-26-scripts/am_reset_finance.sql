-- Finance side of am_reset_supply.sql (owner 2026-11-27).
BEGIN;
CREATE TEMP TABLE xp ON COMMIT DROP AS SELECT po_id FROM purchase_orders WHERE supply_order_no IN
  ('SPO-000013','SPO-000014','SPO-000015','SPO-000016','SPO-000019','SPO-000020','SPO-000023','SPO-000024','SPO-000026','SPO-000028');
CREATE TEMP TABLE xr ON COMMIT DROP AS SELECT supply_receipt_id FROM supply_receipts WHERE po_id IN (SELECT po_id FROM xp);
CREATE TEMP TABLE xi ON COMMIT DROP AS SELECT invoice_id FROM supply_receipts WHERE supply_receipt_id IN (SELECT supply_receipt_id FROM xr) AND invoice_id IS NOT NULL;
CREATE TEMP TABLE xj ON COMMIT DROP AS
  SELECT journal_id FROM supplier_invoices WHERE invoice_id IN (SELECT invoice_id FROM xi) AND journal_id IS NOT NULL
  UNION SELECT journal_id FROM journals WHERE source_ref IN (SELECT 'supply:receipt:'||supply_receipt_id FROM xr);
SELECT 'pos', count(*) FROM xp UNION ALL SELECT 'receipts', count(*) FROM xr UNION ALL SELECT 'invoices', count(*) FROM xi
UNION ALL SELECT 'journals', count(*) FROM xj
UNION ALL SELECT 'debits', sum(debit)::bigint FROM journal_lines WHERE journal_id IN (SELECT journal_id FROM xj)
UNION ALL SELECT 'credits', sum(credit)::bigint FROM journal_lines WHERE journal_id IN (SELECT journal_id FROM xj);
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
        jsonb_build_object('supply_orders', 'SPO-000013..16, 19, 20, 23, 24, 26, 28 (Amjath + Munchee quick buys)',
                           'removed', 'PO-000004..13 with their supply receipts, invoices and journals',
                           'why', 'owner 2026-11-27: one order per supplier, received again by staff with QR labels'));
SELECT 'left pos', count(*) FROM purchase_orders WHERE po_id IN (SELECT po_id FROM xp)
UNION ALL SELECT 'all journals', count(*) FROM journals
UNION ALL SELECT 'ledger off by', (SELECT coalesce(sum(debit),0)-coalesce(sum(credit),0) FROM journal_lines)::bigint;
\if :{?commit}
COMMIT;
\else
ROLLBACK;
\endif
