# 单月分区迁入工具：设计与复现说明

把漏建的 2026-01 月分区补齐：已落入 DEFAULT 分区的 1 月记录迁到独立分区，后续写入正确路由。
一次性工具，只处理 `partition_input.events` 的 2026-01 一个月；不做保留策略、归档、历史月份调度或管理页面。

## 交付物

```
partition_mover/            工具源码（Python 3.11，仅依赖 psycopg 3）
  bounds.py                 UTC 月份区间、pg_get_expr 分区边界表达式解析（不按表名猜范围）
  catalog.py                pg_catalog 读取：父表、分区键、分区及真实边界、行数
  config.py                 target.json 加载与校验（month/timezone=UTC/lock_timeout_ms/...）
  core.py                   plan / apply：锁内重校验、单事务迁移、提交后验证
  cli.py / __main__.py      python3 -m partition_mover plan|apply
fixtures/                   题目给定：seed.sql、target.json、writer.py
scripts/
  setup_env.sh              初始化 venv + 本地 PostgreSQL 16 + taskdb（幂等）
  reset_db.sh               重置 schema 并载入 seed
  run_demo.sh               一键复现：plan → 并发 writer → apply → tableoid 核对 → 重跑
  experiment_race.py        锁范围对照实验（事务边界设计的实证依据）
tests/                      pytest：边界解析单元测试 + 端到端（并发写入/锁超时/重跑/冲突拒绝）
SOLUTION.md                 本文档
```

## 初始化与复现命令

```sh
scripts/setup_env.sh                                  # venv、依赖、initdb、启动、createdb
export PG_DSN="host=/tmp/pgsock_$(id -un) dbname=taskdb"   # 脚本末尾会打印实际值
scripts/reset_db.sh                                   # 载入 fixtures/seed.sql

# 计划（只读，不改动任何对象）
.venv/bin/python -m partition_mover plan  --config fixtures/target.json --dsn "$PG_DSN"
# 执行（含锁内重校验与提交后验证）
.venv/bin/python -m partition_mover apply --config fixtures/target.json --dsn "$PG_DSN"

scripts/run_demo.sh                                   # 并发写入下的完整复现
.venv/bin/python -m pytest tests                      # 28 个测试（需 PG_DSN）
PG_DSN="$PG_DSN" .venv/bin/python scripts/experiment_race.py   # 锁范围对照实验
```

`plan`/`apply` 均把 JSON 报告打到 stdout（人读进度在 stderr）。apply 退出码：
`0` 已迁移/已完成；`2` 拒绝（冲突分区、同名外部对象、无 DEFAULT、计划已失效）；`3` 锁竞争在有界重试后仍失败；`1` 其他错误。

## 为什么不能直接建分区

`CREATE TABLE ... PARTITION OF ... FOR VALUES FROM ('2026-01-01') TO ('2026-02-01')` 与
`ATTACH PARTITION` 都会校验 DEFAULT 分区中不存在属于新区间的行（必要时全量扫描 DEFAULT），
而 DEFAULT 里现在就有 1 月的行，直接执行必然报错。因此正确顺序是：

1. 先建**独立普通表**（`LIKE 父表 INCLUDING ALL`，含列、约束、主键索引），挂范围 CHECK；
2. 把 1 月的行从 DEFAULT **搬**进去；
3. 给 DEFAULT 加"排除 1 月"的 CHECK（此时搬空，校验即扫描即过）；
4. `ATTACH PARTITION`——两侧都有证明性 CHECK，ATTACH 退化为**纯元数据操作**（不再扫描）；
5. DEFAULT 的隐式约束随 ATTACH 自动排除新区间，临时 CHECK 同事务内删除。

目标区间固定为配置月份的 UTC 左闭右开 `[2026-01-01T00:00:00Z, 2026-02-01T00:00:00Z)`：
月初零点**在内**、下月零点**不在内**、月末微秒 `2026-01-31T23:59:59.999999Z` 因半开区间自然在内
（代码从不计算"月末最后一微秒"这个值，杜绝精度差一错误）。timestamptz 比较按绝对时刻，
与会话时区无关；工具仍把会话 `TIME ZONE` 设为 UTC 使边界输出确定。

## 并发写入下安全切换的事务边界（核心设计）

执行期间 writer 仍在向父表 INSERT。整个迁移是**一个事务**：

```
BEGIN;
  SET LOCAL lock_timeout = '500ms';        -- 配置项：获取锁的最长等待
  SET LOCAL statement_timeout = '30s';     -- 配置项：持锁期间单条语句的上限
  SET LOCAL TIME ZONE 'UTC';
  LOCK TABLE 父表 IN ACCESS EXCLUSIVE MODE;   -- 锁整棵分区树（父表+全部分区）
  -- 锁内重校验：父表 oid、DEFAULT oid、无重叠分区、目标名仍空闲（见下节）
  CREATE TABLE 新表 (LIKE 父表 INCLUDING ALL);
  COMMENT ON TABLE 新表 ...;                -- 工具溯源标记
  ALTER TABLE 新表 ADD CHECK (范围);
  WITH moved AS (DELETE FROM default WHERE 范围 RETURNING *)
    INSERT INTO 新表 SELECT ...;            -- 单语句、单快照搬移
  ALTER TABLE default ADD CHECK (NOT 范围); -- 证明 DEFAULT 已无 1 月行
  ALTER TABLE 父表 ATTACH PARTITION 新表 FOR VALUES ...;  -- 元数据级
  ALTER TABLE default DROP CHECK ...;       -- 临时约束，已被隐式约束取代
  -- 事务内自检：DEFAULT 区间内行数=0、新表行数=搬移数
COMMIT;                                     -- 唯一的生效点
```

**为什么锁整棵树而不是只锁 DEFAULT**（`scripts/experiment_race.py` 可复现）：
INSERT 进入父表后先做元组路由再锁目标分区。若只锁 DEFAULT，并发 writer 会拿着**旧的分区映射**
把 1 月的行路由到 DEFAULT，阻塞在 DEFAULT 的锁上；迁移提交后它带着过期路由决定继续，
PostgreSQL 在目标分区上重新校验分区约束，该写入以 `23514` 显式失败——不丢数据但丢了一次写入。
锁住整棵树后，writer 在**路由之前**就阻塞在父表锁上，提交后用新映射重路由，直接落入新分区，
零失败。实验输出：场景 A（树锁）writer `committed` 且探针行落在 `events_jan`；
场景 B（只锁 DEFAULT）writer `failed 23514`。

**为什么不会丢行、重复或出现路由空窗**：搬移（DELETE…RETURNING/INSERT）、DEFAULT 约束变更、
ATTACH 在同一个事务里，唯一生效点是 COMMIT。提交前任何已提交快照看到旧布局（行在 DEFAULT），
提交后看到新布局（行在新分区、新路由生效）；不存在"行已删未插"或"两边都有/都没有"的中间态。
锁内持锁期间整树静默，没有并发写入能在搬移与 ATTACH 之间插入 1 月的行。

**有限阻塞**：锁获取受 `lock_timeout` 约束（超时得 `55P03`，整事务回滚）；持锁时长受
`statement_timeout` 约束（超时回滚释放锁）；重试受 `max_attempts`（默认 3，线性退避）约束，
死锁（`40P01`）同样回滚重试。任何路径都不无限等待。预检/验证连接也设了会话级
`lock_timeout`——曾发现他人持锁时连只读计数都会无限阻塞，现已一并纳入有界语义。
代价说明：持锁期间**全表**（含其他分区）写入与读取都短暂阻塞，量级为搬移耗时（fixture 为毫秒级）；
这是一次性单月工具可接受的取舍，换取零失败与零路由异常。

## 计划与执行之间的一致性（TOCTOU）

`plan` 是无锁只读快照，输出待迁月份、目标区间、冲突分区、预计行数（计划时刻的精确计数）、
将执行的完整 DDL。`apply` **不信任**任何旧计划：每次尝试前重新读取目录做预检，
获取树锁之后在事务内**再校验**——父表未被重建（oid 不变）、DEFAULT 未被替换（oid 不变）、
没有新出现的重叠分区、目标表名仍空闲。任一不满足即回滚并以退出码 2 拒绝。
典型场景：plan 显示 ready 后，有人先建了重叠分区，apply 拒绝执行旧计划。

## 中断、重跑与中间对象

- **单事务 ⇒ 无持久中间态**。失败、客户端掉线、服务器崩溃都使整事务回滚：新表、COMMENT、
  CHECK、搬移、ATTACH 全部消失，不会留下"行已搬出但未挂载"的不可见数据。
- **重跑幂等**：以目录状态识别"已完成"——存在边界精确等于目标区间的已挂载分区（按边界识别，
  不按名字；`not_named_by_month` 也能被认出）→ 报告 `already_done`，退出码 0。
  客户端在 COMMIT 后掉线导致"实际成功但报告失败"的情形，重跑即由此路径安全收敛。
- **同名对象**：目标名已存在且不是本月的已挂载分区时，一律拒绝（退出码 2），
  **绝不删除或复用**。对象带本工具 marker comment 时会识别为"本工具遗留"并提示人工检查
  （单事务设计下实际不会产生）；无标记则视为他人对象。两种情况都不当作可删垃圾。
- 工具只删除一种对象：事务内自建、提交前即删的临时 CHECK `pmv_excl_YYYYMM`。

## 失败语义：自动回滚 vs 人工处理

**自动回滚（无需人工）**：锁超时/死锁（重试耗尽后退出码 3，库无任何变化，稍后重跑即可）；
语句超时；锁内重校验发现计划失效；事务内自检失败；进程/连接/服务器崩溃。

**需要人工处理（工具明确拒绝，退出码 2）**：出现重叠分区（先解决冲突再跑）；目标名被外部
对象占用（改名或人工清理后重跑）；父表无 DEFAULT 分区（前提不成立）；提交后验证发现异常
（理论上不可达，如 DEFAULT 仍有区间内行——报告会给出计数，需人工核查）。

**不做的规避**：不关闭/不绕过外键，不临时摘除分区约束，不删任何非本工具创建的对象。

## 验证与测试

`tests/`（28 个测试，`PG_DSN` 指向实例后 `.venv/bin/python -m pytest`）：

- **边界归属**：月初零点、月末微秒（`23:59:59.999999`）入新分区；下月零点留 DEFAULT；
  12 月跨年 rollover；`MINVALUE/MAXVALUE`、带分钟偏移与微秒的字面量解析。
- **并发写入**：writer（40 行，1/2 月交替）与 apply 并行；以其真实提交记录核对——
  迁移后数据集合 = 迁移前集合 ∪ writer 已提交集合，无丢失/重复/多余；每行 `tableoid`
  与其 `occurred_at` 应属分区一致；writer 零失败（阻塞毫秒级，远低于其 3s statement_timeout）。
- **锁超时**：外部持整树锁 → apply 两次尝试均 `55P03` 回滚、库无变化（退出码 3）；
  释放后同一命令成功。
- **重复执行**：apply→apply 第二次 `already_done`；plan 亦报告已完成。
- **冲突拒绝**：plan 后他人建重叠分区 → apply 退出码 2 且库无变化；
  同名外部表 → 拒绝且该表分毫未动。
- **目录驱动**：12 月分区凭边界（而非名字）被识别为已覆盖；空月份迁移 0 行仍正确挂载。

`apply` 报告记录：每次尝试的实际锁等待（`lock_wait_ms`）、迁入行数（`moved_rows`）、
最终分区边界（提交后从目录重读的 `FOR VALUES FROM ... TO ...`）及全部提交后校验结果。

## 限制（有意为之）

单表、单月、RANGE 单列分区键（`timestamptz`/`timestamp`）；父表有用户触发器或
identity/generated 列时拒绝执行（ATTACH 不会传播触发器，搬移语义也可能不同）；
不做保留策略、归档、历史月份调度与管理页面。
