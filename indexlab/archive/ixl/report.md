# 索引实验报告

## 1. 实验元信息

- PostgreSQL: `PostgreSQL 16.16 (Debian 16.16-1.pgdg12+1) on aarch64-unknown-linux-gnu, compiled by gcc (Debian 12.2.0-14+deb12u1) 12.2.0, 64-bit`
- 统计信息收集: 装载后 ANALYZE；每次建实验索引后 ANALYZE 相关表（default_statistics_target=100，未使用扩展统计）
- 固定 seed: `20261113`；数据规模: {'customers': 20000, 'products': 3000, 'orders': 300000, 'order_items': 617267}
- 原始数据指纹(count/bit_xor_hash): customers=20000/1608580215553320884, products=3000/6772911118179428254, orders=300000/8168442791449482746, order_items=617267/-991252237240343292
- 关键参数: {'shared_buffers': '16384', 'work_mem': '4096', 'max_parallel_workers_per_gather': '2', 'random_page_cost': '4', 'effective_cache_size': '524288', 'default_statistics_target': '100', 'jit': 'on'}
- 计时方法: 每(查询,参数组) warmup 1 次后交错计时 5 次，报告中位数与分布；写入测量每次从模板全新克隆，重复 3 次

## 2. 候选索引（由查询结构解析生成，附来源）

| 候选 | 来源(查询:规则) |
|---|---|
| `order_items(product_id)` | q2_product_orders:R1, q3_category_sales:R5 |
| `order_items(product_id) INCLUDE(qty, unit_price)` | q2_product_orders:R4 |
| `orders(customer_id)` | q1_cust_daterange:R1, q4_cust_status_fewcols:R1 |
| `orders(customer_id, order_date)` | q1_cust_daterange:R2, q1_cust_daterange:R3 |
| `orders(customer_id, order_date) INCLUDE(order_id, status, total)` | q1_cust_daterange:R4 |
| `orders(customer_id, status, order_date)` | q4_cust_status_fewcols:R3 |
| `orders(customer_id, status, order_date) INCLUDE(order_id)` | q4_cust_status_fewcols:R4 |
| `orders(order_date)` | q1_cust_daterange:R1, q3_category_sales:R1 |
| `orders(order_date, customer_id)` | q1_cust_daterange:R2b |
| `orders(status)` | q4_cust_status_fewcols:R1, q5_status_channel_recent:R1 |
| `orders(status, channel, order_date)` | q5_status_channel_recent:R3 |
| `orders(status, channel, order_date) INCLUDE(order_id, customer_id, total)` | q5_status_channel_recent:R4 |
| `products(category)` | q3_category_sales:R1 |

剔除与已有索引等价/冗余的候选 4 条：
- `order_items(order_id)` — 键为已有索引 order_items_pkey 的左前缀且 INCLUDE 被覆盖（冗余）（来源 q2_product_orders:R5, q3_category_sales:R5）
- `orders(channel)` — 与已有索引 orders_channel_idx 定义等价（来源 q5_status_channel_recent:R1）
- `orders(order_id)` — 与已有索引 orders_pkey 定义等价（来源 q2_product_orders:R5, q3_category_sales:R5）
- `products(product_id)` — 与已有索引 products_pkey 定义等价（来源 q3_category_sales:R5）

## 3. 组合试验表（全部为实际执行过的组合）

| 组合 | 索引 | 构建耗时s | 空间MB | 写入倍率 | 结果一致 | 可行 | 加权几何平均加速 | 备注 |
|---|---|---|---|---|---|---|---|---|
| k01 | `order_items(product_id)` | 0.23 | 4.4 | 1.122 | True | True | 1.5435 |  |
| k02 | `orders(customer_id)` | 0.11 | 2.6 | 1.0966 | True | True | 2.4291 |  |
| k03 | `orders(order_date)` | 0.10 | 2.2 | 1.1137 | True | True | 3.8595 |  |
| k04 | `orders(status)` | 0.11 | 2.1 | 1.1299 | True | True | 1.3759 |  |
| k05 | `order_items(product_id) INCLUDE(qty, unit_price)` | 0.43 | 19.5 | 1.2637 | True | False | 1.5572 | 空间 19.5MB > 预算 16MB; 写入倍率 1.2637 > 阈值 1.2 |
| k06 | `products(category)` | 0.00 | 0.0 | 1.0 | True | True | 0.9079 |  |
| k07 | `orders(customer_id, order_date)` | 0.13 | 5.9 | 1.1551 | True | True | 5.225 |  |
| k08 | `orders(customer_id, order_date) INCLUDE(order_id, status, total)` | 0.31 | 14.7 | 1.2631 | True | False | 7.3249 | 写入倍率 1.2631 > 阈值 1.2 |
| k09 | `orders(order_date, customer_id)` | 0.14 | 5.9 | 1.15 | True | True | 5.3408 |  |
| k10 | `orders(customer_id, order_date) INCLUDE(order_id, status, total)`<br>`orders(order_date, customer_id)` | 0.56 | 20.7 | 1.2259 | True | False | 11.2754 | 空间 20.7MB > 预算 16MB; 写入倍率 1.2259 > 阈值 1.2 |
| k11 | `orders(customer_id, order_date) INCLUDE(order_id, status, total)`<br>`orders(customer_id, order_date)` | 0.41 | 20.7 | 1.2555 | True | False | 6.7719 | 空间 20.7MB > 预算 16MB; 写入倍率 1.2555 > 阈值 1.2 |
| k12 | `orders(customer_id, order_date) INCLUDE(order_id, status, total)`<br>`orders(order_date)` | 0.38 | 16.9 | 1.2936 | True | False | 10.9071 | 空间 16.9MB > 预算 16MB; 写入倍率 1.2936 > 阈值 1.2 |
| k13 | `orders(order_date, customer_id)`<br>`orders(customer_id, order_date)` | 0.29 | 11.9 | 1.3269 | True | False | 4.2574 | 写入倍率 1.3269 > 阈值 1.2 |
| k14 | `orders(order_date, customer_id)`<br>`orders(order_date)` | 0.24 | 8.1 | 1.1737 | True | True | 4.0299 |  |
| k15 | `orders(customer_id, order_date) INCLUDE(order_id, status, total)`<br>`orders(order_date, customer_id)`<br>`orders(customer_id, order_date)` | 0.57 | 26.6 | 1.4034 | True | False | 9.8858 | 空间 26.6MB > 预算 16MB; 写入倍率 1.4034 > 阈值 1.2 |

写入基线: [0.3386, 0.3433, 0.319] s（同一初始数据模板克隆，相同操作流 4000 条）

## 4. 每类查询的加速/变慢与计划变化

### q1_cust_daterange — 等值(客户)+范围(日期)+排序LIMIT

| 参数组 | 基线中位数ms (分布) | 推荐组合中位数ms | 加速比 | 实际使用的新索引 | 访问路径变化 |
|---|---|---|---|---|---|
| hot_narrow | 6.0 (5.8~6.4) | 0.2 | 38.45x | `ixl_ix_k09_0` | Seq→Idx |
| cold_narrow | 6.1 (5.8~7.1) | 0.3 | 18.03x | `ixl_ix_k09_0` | Seq→Idx |
| hot_wide_old | 6.4 (6.0~7.1) | 0.1 | 70.40x | `ixl_ix_k09_0` | Seq→Idx |

### q2_product_orders — 连接(明细-订单)+等值(商品)+排序LIMIT

| 参数组 | 基线中位数ms (分布) | 推荐组合中位数ms | 加速比 | 实际使用的新索引 | 访问路径变化 |
|---|---|---|---|---|---|
| popular | 8.9 (8.4~10.5) | 9.1 | 0.98x | （未使用新索引） | 不变 |
| rare | 8.8 (8.3~10.3) | 9.2 | 0.96x | （未使用新索引） | 不变 |

### q3_category_sales — 三表连接+聚合(类目销量)

| 参数组 | 基线中位数ms (分布) | 推荐组合中位数ms | 加速比 | 实际使用的新索引 | 访问路径变化 |
|---|---|---|---|---|---|
| cat_hot | 28.9 (27.5~30.4) | 30.8 | 0.94x | （未使用新索引） | 不变 |
| cat_cold | 13.7 (13.4~28.3) | 16.3 | 0.84x | （未使用新索引） | 不变 |

### q4_cust_status_fewcols — 双等值+少量列(覆盖索引候选)

| 参数组 | 基线中位数ms (分布) | 推荐组合中位数ms | 加速比 | 实际使用的新索引 | 访问路径变化 |
|---|---|---|---|---|---|
| hot_open | 6.5 (6.3~7.0) | 6.5 | 1.00x | （未使用新索引） | 不变 |
| hot_shipped | 6.2 (5.7~6.3) | 0.3 | 18.32x | `ixl_ix_k09_0` | Seq→Idx |

### q5_status_channel_recent — 双等值+排序LIMIT

| 参数组 | 基线中位数ms (分布) | 推荐组合中位数ms | 加速比 | 实际使用的新索引 | 访问路径变化 |
|---|---|---|---|---|---|
| open_web | 7.0 (6.6~7.8) | 0.3 | 21.41x | `ixl_ix_k09_0` | Seq→Idx |
| returned_app | 6.1 (5.9~6.3) | 2.3 | 2.73x | `ixl_ix_k09_0` | 不变 |

## 5. 反直觉发现（由实测数据支持）

- **单列不如复合**：`orders(customer_id)` 单索引 2.4291x、`orders(order_date)` 单索引 3.8595x，而复合 `(customer_id, order_date)` 5.225x、`(order_date, customer_id)` 5.3408x；列顺序在查询级影响显著：q5_status_channel_recent/open_web 中位数 (customer_id,order_date) 组 7.559ms vs (order_date,customer_id) 组 0.325ms（差 23.3 倍）——左前缀列决定索引能为哪些查询做定位与有序扫描，两种列序服务不同的查询
- **读快但约束不允许**：组合 k10 ['orders(customer_id, order_date) INCLUDE(order_id, status, total)', 'orders(order_date, customer_id)'] 读加速 11.2754x（优于所有可行组合的最佳 5.3408x），但 空间 20.7MB > 预算 16MB; 写入倍率 1.2259 > 阈值 1.2，依规则不得推荐。
- **读快但约束不允许**：组合 k12 ['orders(customer_id, order_date) INCLUDE(order_id, status, total)', 'orders(order_date)'] 读加速 10.9071x（优于所有可行组合的最佳 5.3408x），但 空间 16.9MB > 预算 16MB; 写入倍率 1.2936 > 阈值 1.2，依规则不得推荐。
- **读快但约束不允许**：组合 k15 ['orders(customer_id, order_date)', 'orders(customer_id, order_date) INCLUDE(order_id, status, total)', 'orders(order_date, customer_id)'] 读加速 9.8858x（优于所有可行组合的最佳 5.3408x），但 空间 26.6MB > 预算 16MB; 写入倍率 1.4034 > 阈值 1.2，依规则不得推荐。
- （另有 2 个读加速超过可行最优但违反约束的组合，详见组合试验表）

## 6. 推荐与局限

**推荐组合 k09**（在已测试组合与当前数据分布下最优）：
- `orders(order_date, customer_id)` — 大小 5.9MB，构建 0.141s

加权几何平均加速 **5.3408x**，空间 5.9MB（预算 16MB），写入倍率 1.15（阈值 1.2）。

### 局限

- 推荐仅适用于**本次实际测试过的组合**与当前合成数据分布；未测试的组合未参与比较，本报告不声称它们更差。
- 数据为固定 seed 的合成分布（热点客户/日期倾斜/字段相关性按构造生成），真实负载的选择性可能不同。
- 计时为本机单次实验的重复测量分布，存在缓存与噪声影响；报告给出 min~max 区间，不承诺固定加速倍数。
- 候选空间限定为普通 B-tree 单列/≤3 列复合/可选 INCLUDE；部分索引、表达式索引、扩展统计等未纳入。
- 写入代价以固定操作流在同一初始数据克隆上测得；真实写入混合比例不同结论可能变化。

## 7. 实际检查过的组合清单

- k01: ['order_items(product_id)'] -> done
- k02: ['orders(customer_id)'] -> done
- k03: ['orders(order_date)'] -> done
- k04: ['orders(status)'] -> done
- k05: ['order_items(product_id) INCLUDE(qty, unit_price)'] -> done
- k06: ['products(category)'] -> done
- k07: ['orders(customer_id, order_date)'] -> done
- k08: ['orders(customer_id, order_date) INCLUDE(order_id, status, total)'] -> done
- k09: ['orders(order_date, customer_id)'] -> done
- k10: ['orders(customer_id, order_date) INCLUDE(order_id, status, total)', 'orders(order_date, customer_id)'] -> done
- k11: ['orders(customer_id, order_date)', 'orders(customer_id, order_date) INCLUDE(order_id, status, total)'] -> done
- k12: ['orders(customer_id, order_date) INCLUDE(order_id, status, total)', 'orders(order_date)'] -> done
- k13: ['orders(customer_id, order_date)', 'orders(order_date, customer_id)'] -> done
- k14: ['orders(order_date)', 'orders(order_date, customer_id)'] -> done
- k15: ['orders(customer_id, order_date)', 'orders(customer_id, order_date) INCLUDE(order_id, status, total)', 'orders(order_date, customer_id)'] -> done

未列出的组合未被测试，其性能未知。

## 8. 失败/缺失测量记录

- 无
