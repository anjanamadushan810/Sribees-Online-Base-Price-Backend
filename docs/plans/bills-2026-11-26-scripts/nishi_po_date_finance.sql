BEGIN;
UPDATE purchase_orders SET order_date = DATE '2026-11-22' WHERE supply_order_no = 'SPO-000031' AND po_no = 'PO-000014';
INSERT INTO audit_log (user_id, action, entity, after) VALUES (NULL, 'po.date_set', 'purchase_order', jsonb_build_object('po', 'PO-000014', 'order_date', '2026-11-22', 'why', 'owner 2026-11-27: the Nishi bills date'));
SELECT po_no, order_date FROM purchase_orders WHERE po_no='PO-000014';
COMMIT;
