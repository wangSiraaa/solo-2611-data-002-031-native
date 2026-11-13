-- 连接 + 等值(商品) + 排序 LIMIT
SELECT o.order_id, o.order_date, i.qty, i.unit_price
FROM order_items i
JOIN orders o ON o.order_id = i.order_id
WHERE i.product_id = $1
ORDER BY o.order_date DESC, o.order_id DESC
LIMIT 50;
