BEGIN;
UPDATE purchase_orders SET order_date = DATE '2026-11-18' WHERE supply_order_no = 'SPO-000035' AND po_no = 'PO-000015';
UPDATE purchase_orders SET order_date = DATE '2026-11-25' WHERE supply_order_no = 'SPO-000036' AND po_no = 'PO-000016';
INSERT INTO audit_log (user_id, action, entity, after) VALUES (NULL, 'po.date_set', 'purchase_order',
  jsonb_build_object('pos', 'PO-000015 2026-11-18 (Amjath bills), PO-000016 2026-11-25 (Munchee bills)', 'why', 'owner 2026-11-27: the bills dates'));
SELECT po_no, order_date FROM purchase_orders WHERE po_no IN ('PO-000015','PO-000016','PO-000017') ORDER BY 1;
COMMIT;
