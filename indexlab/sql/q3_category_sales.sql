-- 三表连接 + 聚合
SELECT p.category, count(*) AS n_lines, sum(i.qty) AS qty_sold
FROM order_items i
JOIN products p ON p.product_id = i.product_id
JOIN orders o   ON o.order_id   = i.order_id
WHERE p.category = $1
  AND o.order_date >= $2
  AND o.order_date <  $3
GROUP BY p.category;
