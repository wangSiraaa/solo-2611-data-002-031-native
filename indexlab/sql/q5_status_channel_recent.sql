-- 双等值 + 排序 LIMIT
SELECT o.order_id, o.customer_id, o.order_date, o.total
FROM orders o
WHERE o.status = $1
  AND o.channel = $2
ORDER BY o.order_date DESC, o.order_id DESC
LIMIT 10;
