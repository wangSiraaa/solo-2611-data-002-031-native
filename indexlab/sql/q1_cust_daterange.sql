-- 等值(客户) + 范围(日期) + 排序 LIMIT
SELECT o.order_id, o.order_date, o.status, o.total
FROM orders o
WHERE o.customer_id = $1
  AND o.order_date >= $2
  AND o.order_date <  $3
ORDER BY o.order_date DESC, o.order_id DESC
LIMIT 20;
