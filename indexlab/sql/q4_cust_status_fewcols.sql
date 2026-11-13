-- 双等值 + 少量列（覆盖/INCLUDE 候选）
SELECT o.order_id, o.order_date, o.status
FROM orders o
WHERE o.customer_id = $1
  AND o.status = $2
ORDER BY o.order_date DESC, o.order_id DESC
LIMIT 50;
