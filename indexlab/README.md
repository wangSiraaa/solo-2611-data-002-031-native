# indexlab — 工作负载驱动的索引实验助手

在本机合成数据库上：**提出候选 → 实际建索引 → 对照测量 → 给出可解释建议**。
只操作带任务标识的独立实验 schema，不修改任何其他对象。

- Python 3.11 / PostgreSQL 16（本实验实测 16.16）
- 驱动：psycopg 3；SQL 解析：pglast（libpg_query）；配置：PyYAML
- 不依赖任何数据库扩展；不关闭顺序扫描、不干预计划器

## 依赖安装

```bash
python3.11 -m venv venv
venv/bin/pip install -r requirements.txt   # psycopg[binary], pglast, PyYAML
```

连接参数走环境变量（默认 `127.0.0.1:5433`，用户 `postgres`，库名 `idxlabdb`）：

```bash
export PGHOST=127.0.0.1 PGPORT=5433 PGUSER=postgres
```

## 实验隔离方案

任务标识 `tag: ixl`（见 `workload.yaml`），本工具只创建/删除以下对象：

| 对象 | 名称 | 用途 |
|---|---|---|
| 实验 schema | `ixl_main` | 合成数据 + 实验索引的测量现场 |
| 模板 schema | `ixl_tpl` | 写入测量的"同一初始数据副本" |
| 写测试克隆 | `ixl_w_<combo>_r<n>` | 每个组合独立克隆，测完即删 |
| 实验索引 | `ixl_ix_<combo>_<n>` | 全部带前缀，与业务索引区分 |
| 归档目录 | `archive/ixl/` | 原始计划、测量值、状态、报告 |

预置业务索引（`orders_pkey`、`orders_channel_idx` 等主键与既有索引）**绝不删除**；
清理只匹配 `ixl_` 前缀，并通过系统目录（`pg_class`/`pg_namespace`）核对结果。

## 可复现本机命令

```bash
cd indexlab
venv/bin/python -m indexlab init          # 建schema+生成数据(seed固定)+ANALYZE+元信息
venv/bin/python -m indexlab gen-workload  # 解析参数组、生成固定写操作流
venv/bin/python -m indexlab candidates    # 解析查询结构 -> 候选索引(附来源)
venv/bin/python -m indexlab run           # 基线 + 预算内组合实验（可中断恢复）
venv/bin/python -m indexlab report        # 查看建议报告（archive/ixl/report.md）
venv/bin/python -m indexlab status        # 进度与本任务对象
venv/bin/python -m indexlab recover       # 识别并清理中断遗留的本任务对象
venv/bin/python -m indexlab cleanup --indexes   # 只删实验索引（目录核对）
venv/bin/python -m indexlab cleanup --all       # 删除全部实验 schema
```

一键脚本：`bash run_demo.sh`（含初始化、完整实验、报告；约 3-5 分钟）。

## 设计要点

**合成数据**（`datagen.py`，seed=20261113）：customers 20k / products 3k /
orders 300k / order_items ~617k。前 15 个客户为热点（约 25% 订单）；订单日期向近期
倾斜；状态与日期相关（近期多 open）；类目与价格带相关；地区与等级相关；
`orders.total` 由明细累加（跨表相关）。行数与 `bit_xor(hashtextextended())`
指纹记入 `archive/ixl/meta.json`，同 seed 可完整复现。

**工作负载**（`workload.yaml` + `sql/`）：5 条参数化只读 SQL（等值+范围、连接、
排序 LIMIT、少量列、三表聚合），11 个参数组（热点/冷客户、窄/宽日期、热门/冷门
商品）带权重；写入为固定操作流（800 插入订单含明细 + 1200 更新状态，每 100 操作
一提交），操作流存 `archive/ixl/workload.json`。

**候选生成**（`candidates.py`）：pglast 解析真实查询结构（等值/范围/连接/排序/
输出列），按规则 R1 单列、R2 等值+范围、R2b 列序对照、R3 等值+排序、R4 INCLUDE
覆盖、R5 连接键生成有限候选（B-tree，≤3 列键，≤4 个 INCLUDE 列；不做部分/表达式/
分区索引）。与已有索引等价或左前缀冗余的候选剔除并记录原因。

**组合探索**（`explore.py`）：预算 16 个组合。阶段A 单索引（按支撑查询数排序，
列序对照候选强制纳入）→ 阶段B 成对（按单索引得分积）→ 阶段C 三元（最优对+次优、
最大化对照）。实际检查过的组合全部列入报告，未测试组合不做任何结论。

**测量**（`measure.py` / `writebench.py`）：每个组合实际 CREATE INDEX（记录构建
耗时与 `pg_relation_size` 真实大小）→ ANALYZE → 全部参数组 warmup+5 次交错计时
+ `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` 归档 → 结果哈希与无索引基线逐组比对，
一致才比较性能。写入代价每次从 `ixl_tpl` 全新克隆（LIKE INCLUDING ALL + INSERT
SELECT），同一操作流独立测 2 次；绝不复用被前一组合修改的数据。

**约束与推荐**：空间预算 16MB（按真实大小）、写入倍率阈值 1.20（配置可调）。
违反任一约束或结果不一致的组合不得推荐。推荐 = 可行组合中加权几何平均加速最高者
（权重=参数组权重），仅对"已测试组合 + 当前数据分布"负责。

## 本次实验结果摘要（archive/ixl/）

- 13 条候选（4 条因与已有索引等价/冗余被剔除），实际测试 15 个组合
- **推荐 k09 `orders(order_date, customer_id)`**：加权几何平均 **5.34x**，
  5.9MB（预算 16MB 内），写入倍率 1.15（阈值 1.20 内）；q1 各参数组 18~70x、
  q5 达 21x，访问路径 Seq→Idx；q2/q3 未使用新索引（如实报告）
- **反直觉 1（单列不如复合，列顺序在查询级影响显著）**：
  `orders(customer_id)` 2.43x、`orders(order_date)` 3.86x，复合均 >5.2x；
  两种列序在工作负载级接近，但 q5/open_web 中位数 7.56ms vs 0.33ms（差 23 倍）
  ——左前缀列决定索引能为哪些查询做定位与有序扫描
- **反直觉 2（读快但约束不允许）**：k10 读加速 11.28x、k12 10.91x，
  但空间超预算且写入倍率 1.23~1.29 > 1.20，依规则不得推荐
- 中断恢复已实测：运行中 `kill -9` 后目录中遗留 `ixl_ix_k13_*` 索引与
  `ixl_w_k13_r1` 克隆 schema，重启 `run` 时自动识别、清理（日志以"恢复："前缀
  记入 `state.json` 的 explore_log）并从未完成的组合续跑；`recover` 命令可手动
  触发同样逻辑，业务索引不受影响

## 已知局限

见报告"局限"一节：推荐仅覆盖已测试组合与当前合成分布；计时为单机重复测量分布
（min~max），不承诺固定加速倍数；未纳入部分/表达式索引与扩展统计。
