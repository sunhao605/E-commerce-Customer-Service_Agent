-- 仅修正用户指出的已全额退款订单；必须有同一用户的支付、已批准申请和退款流水。
-- 不触碰余额、资金流水或审批记录；重复执行不会再次修改。
START TRANSACTION;
UPDATE order_header o
JOIN after_sale_request a ON a.order_no = o.order_no AND a.user_id = o.user_id
JOIN balance_transaction r ON r.after_sale_no = a.request_id AND r.order_no = o.order_no AND r.user_id = o.user_id
JOIN balance_transaction p ON p.order_no = o.order_no AND p.user_id = o.user_id AND p.type = 'PAYMENT'
SET o.status = 'REFUNDED', o.payment_status = 'REFUNDED', o.fulfillment_status = 'CANCELED', o.cancel_allowed = 0
WHERE o.order_no = 'SO20261009061332122-26971b7b' AND o.user_id = 'U1001'
  AND a.request_id = 'AS-20261009062604598-17039d42'
  AND a.status = 'APPROVED' AND a.request_type = 'REFUND'
  AND a.amount = o.total_amount AND r.type = 'REFUND' AND r.amount = o.total_amount
  AND p.amount = -o.total_amount AND o.total_amount > 0
  AND o.status IN ('PAID_PENDING_SHIPMENT', 'PENDING_SHIPMENT') AND o.payment_status = 'PAID'
  AND o.fulfillment_status = 'UNSHIPPED' AND o.shipped_at IS NULL AND o.delivered_at IS NULL
  AND (o.logistics_no IS NULL OR o.logistics_no = '');
SELECT ROW_COUNT() AS repaired_order_rows;
COMMIT;
