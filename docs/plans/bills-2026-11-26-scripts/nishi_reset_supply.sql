BEGIN;
CREATE TEMP TABLE xo ON COMMIT DROP AS SELECT order_id FROM supply_orders WHERE order_id IN ('c606d120-fc45-403e-aaeb-0e8bc06cee89','602fa158-9c86-412d-970f-7ce15c307094','0b7dac32-8ca4-4683-a9d1-3ae79277a97c');
CREATE TEMP TABLE xr ON COMMIT DROP AS SELECT receipt_id FROM receipts WHERE order_id IN (SELECT order_id FROM xo);
CREATE TEMP TABLE xb ON COMMIT DROP AS SELECT batch_id FROM receipt_lines WHERE receipt_id IN (SELECT receipt_id FROM xr);
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
        jsonb_build_object('orders', 'SPO-000010, SPO-000011, SPO-000012 (Nishi quick buys)',
                           'why', 'owner 2026-11-27: one Nishi order, received by staff with QR labels'));
SELECT 'left orders', count(*) FROM supply_orders WHERE order_id IN ('c606d120-fc45-403e-aaeb-0e8bc06cee89','602fa158-9c86-412d-970f-7ce15c307094','0b7dac32-8ca4-4683-a9d1-3ae79277a97c')
UNION ALL SELECT 'batches now', count(*) FROM stock_batches
UNION ALL SELECT 'stock value now', sum(value_on_hand)::bigint FROM stock_batches;
COMMIT;
