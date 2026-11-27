-- Owner 2026-11-27: Amjath (SUP-0007) and Munchee (SUP-0008) quick buys removed so each supplier
-- has ONE order received by staff with QR labels (as Nishi SPO-000031). Includes the 5 product
-- corrections COR-000001..5 made on these goods (their stock is in the new orders' lines).
BEGIN;
CREATE TEMP TABLE xo ON COMMIT DROP AS SELECT o.order_id FROM supply_orders o JOIN suppliers s ON s.supplier_id=o.supplier_id
  WHERE s.code IN ('SUP-0007','SUP-0008') AND o.status='received';
CREATE TEMP TABLE xr ON COMMIT DROP AS SELECT receipt_id FROM receipts WHERE order_id IN (SELECT order_id FROM xo);
CREATE TEMP TABLE xb0 ON COMMIT DROP AS SELECT batch_id FROM receipt_lines WHERE receipt_id IN (SELECT receipt_id FROM xr);
CREATE TEMP TABLE xd ON COMMIT DROP AS SELECT DISTINCT doc_id FROM stock_moves WHERE batch_id IN (SELECT batch_id FROM xb0) AND move_type='correct_out';
CREATE TEMP TABLE xb ON COMMIT DROP AS SELECT batch_id FROM xb0 UNION SELECT batch_id FROM stock_moves WHERE doc_id IN (SELECT doc_id FROM xd) AND move_type='correct_in';
SELECT 'orders', count(*) FROM xo UNION ALL SELECT 'receipts', count(*) FROM xr UNION ALL SELECT 'batches', count(*) FROM xb
UNION ALL SELECT 'corrections', count(*) FROM xd
UNION ALL SELECT 'value removed', sum(value_on_hand)::bigint FROM stock_batches WHERE batch_id IN (SELECT batch_id FROM xb)
UNION ALL SELECT 'stock value before', sum(value_on_hand)::bigint FROM stock_batches
UNION ALL SELECT 'blocking refs', (SELECT count(*) FROM labels WHERE batch_id IN (SELECT batch_id FROM xb)) + (SELECT count(*) FROM picks WHERE batch_id IN (SELECT batch_id FROM xb))
  + (SELECT count(*) FROM transfer_lines WHERE from_batch_id IN (SELECT batch_id FROM xb) OR to_batch_id IN (SELECT batch_id FROM xb))
  + (SELECT count(*) FROM count_lines WHERE batch_id IN (SELECT batch_id FROM xb)) + (SELECT count(*) FROM adjustment_lines WHERE batch_id IN (SELECT batch_id FROM xb))
  + (SELECT count(*) FROM online_stock_sends WHERE receipt_id IN (SELECT receipt_id FROM xr)) + (SELECT count(*) FROM price_reviews p JOIN receipt_lines l USING(receipt_line_id) WHERE l.receipt_id IN (SELECT receipt_id FROM xr))
  + (SELECT count(*) FROM receipt_drafts WHERE receipt_id IN (SELECT receipt_id FROM xr))
  + (SELECT count(*) FROM stock_moves WHERE batch_id IN (SELECT batch_id FROM xb) AND move_type NOT IN ('grn','correct_out','correct_in'));
ALTER TABLE stock_moves DISABLE TRIGGER USER;
ALTER TABLE receipt_lines DISABLE TRIGGER USER;
ALTER TABLE stock_batches DISABLE TRIGGER USER;
ALTER TABLE supply_order_lines DISABLE TRIGGER USER;
DELETE FROM base_prices WHERE order_id IN (SELECT order_id FROM xo);
DELETE FROM stock_moves WHERE batch_id IN (SELECT batch_id FROM xb);
DELETE FROM receipt_lines WHERE receipt_id IN (SELECT receipt_id FROM xr);
DELETE FROM stock_batches WHERE batch_id IN (SELECT batch_id FROM xb);
DELETE FROM receipt_photos WHERE receipt_id IN (SELECT receipt_id FROM xr);
DELETE FROM receipts WHERE receipt_id IN (SELECT receipt_id FROM xr);
DELETE FROM supply_order_lines WHERE order_id IN (SELECT order_id FROM xo);
DELETE FROM supply_orders WHERE order_id IN (SELECT order_id FROM xo);
ALTER TABLE stock_moves ENABLE TRIGGER USER;
ALTER TABLE receipt_lines ENABLE TRIGGER USER;
ALTER TABLE stock_batches ENABLE TRIGGER USER;
ALTER TABLE supply_order_lines ENABLE TRIGGER USER;
INSERT INTO audit_log (user_id, action, entity, after)
VALUES ('21e57b57-20a2-4dc7-bdbf-cb99e4a1988f', 'reset.pre_go_live', 'supply_order',
        jsonb_build_object('orders', 'SPO-000013..16, 19, 20, 23, 24, 26, 28 (Amjath + Munchee quick buys) and corrections COR-000001..5',
                           'why', 'owner 2026-11-27: one order per supplier, received by staff with QR labels'));
SELECT 'left orders', count(*) FROM supply_orders o JOIN suppliers s ON s.supplier_id=o.supplier_id WHERE s.code IN ('SUP-0007','SUP-0008')
UNION ALL SELECT 'batches now', count(*) FROM stock_batches
UNION ALL SELECT 'stock value now', coalesce(sum(value_on_hand),0)::bigint FROM stock_batches
UNION ALL SELECT 'nishi draft lines', count(*) FROM receipt_draft_lines l JOIN receipt_drafts d USING(draft_id) WHERE d.status='open'
UNION ALL SELECT 'nishi draft qr', count(*) FROM receipt_draft_labels;
\if :{?commit}
COMMIT;
\else
ROLLBACK;
\endif
