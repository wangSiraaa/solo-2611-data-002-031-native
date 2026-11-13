# 供应商编码迁移 CLI（supplier-migrate）

批量、原子地交换/重排供应商业务编码，并同步迁移所有引用表，保证**不可变 `entity_id`
所代表的供应商身份**在迁移前后不变。全程使用 PostgreSQL 16 事务与延迟外键校验，
不在提交前暴露任何占位编码，也不长期关闭外键。

## 数据模型

| 表 | 关键列 | 说明 |
| --- | --- | --- |
| `suppliers` | `entity_id` PK（不可变，触发器强制）、`code` UNIQUE NOT NULL、`name` | 编码精确比较（`COLLATE "C"`，大小写敏感） |
| `purchase_orders` | `supplier_code` → `suppliers.code` | 只迁移引用编码，业务内容不变 |
| `contacts` | `supplier_code` → `suppliers.code` | 同上 |
| `migration_operations` | `operation_id` PK、`mapping_hash`、`receipt` | 与迁移**同一事务**写入的幂等回执 |

两个外键均为 `DEFERRABLE INITIALLY IMMEDIATE`。apply 事务内执行
`SET CONSTRAINTS ALL DEFERRED`，提交时统一校验；约束从不被删除或禁用。

## 映射语义（整批校验，任一错误整批拒绝并给出行号）

CSV 头必须为 `old_code,new_code`，编码按原字符串精确比较：

1. 所有 `old_code` 在映射内唯一（`duplicate_source`）。
2. 所有 `new_code` 在映射内唯一（`duplicate_target`）。
3. 每个 `old_code` 必须当前存在（`source_missing`）。
4. 每个 `new_code` 必须是本映射的某个旧码，或当前未被使用（`target_occupied`）。
   ——这天然允许交换与多节点循环，也允许链式改名最终落到空闲码。

含义以**操作开始时的供应商身份**为准：每个 `old_code` 对应一个 `entity_id`，该实体
的 `code` 只被替换一次。`A→B, B→A` 的实现走"占位码 → 最终码"两阶段，绝不会因两次
字符串替换把订单转回错误实体。

## 命令（全部输出 JSON）

```sh
python -m supplier_migrate.cli [--dsn DSN] <command> ...
```

- `init-db`：建表（可重复执行）。
- `seed-demo [--reset]`：载入合成数据（8 供应商 24 订单 16 联系人，含空闲码
  `FREE-1..3`）。
- `plan --mapping FILE`：**只读**预览（只读事务 + REPEATABLE READ 快照），列出受影响
  实体、每个实体的关联表行数与汇总，不写任何数据。
- `apply --mapping FILE --operation-id ID [--fault-at STAGE]`：原子迁移。
  - 成功：写入回执（与数据同事务）。
  - 相同 `operation-id` + 相同规范化映射：返回**原回执**（`"replayed": true`），
    不重复迁移。
  - 相同 `operation-id` 但映射不同：`operation_id_conflict` 拒绝。
  - apply 在锁内**重新核验**所有前置条件，不信任旧预览。
- `add-order --supplier-code CODE --expected-entity-id ENT [--order-ref REF ...]`：
  仅当 `CODE` 当前仍解析到 `ENT` 时插入订单；否则以 `stale_entity_reference`
  明确拒绝。
- `verify --operation-id ID`：按不可变 `entity_id` 复核已提交操作。
- `export-receipt --operation-id ID [--output FILE]`：导出回执 + 核验报告。

`--fault-at` 为测试用故障注入点：

| 值 | 注入位置 | 结果 |
| --- | --- | --- |
| `after_suppliers` | 供应商改写后、关联表改写前 | 回滚，库完全不变 |
| `after_child_update` | 关联表改写后、提交前 | 回滚，库完全不变 |
| `after_commit` | COMMIT 之后 | 已持久化；同 `operation-id` 重试返回原回执 |

## 并发正确性

`apply` 与 `add-order` 的第一步都取同一把事务级 advisory lock
（`pg_advisory_xact_lock`），因此二者按数据库实际提交顺序串行化：

- 若 add-order 先提交：新订单属于迁移前编码，随后被 apply 一并迁移，跟随原实体。
- 若 apply 先提交：旧编码已属于别的实体，add-order 读到新状态后**明确拒绝**。

绝不出现"编码被别人换走却静默绑到错误实体"的情况。`add-order` 的供应商查询还带
`FOR SHARE` 并在同一事务内完成查找+插入。

## 本地运行（Linux / PostgreSQL 16）

```sh
# 1. 依赖
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -r requirements-dev.txt        # pytest

# 2. 启动私有数据库（自动 initdb/start/createdb/init-db）
#    注意：本工作区可能位于 virtiofs 共享挂载，PostgreSQL 无法 chmod 数据文件，
#    因此集群放在 /tmp 本地盘，而非仓库目录。
scripts/setup_local_db.sh
export SUPPLIER_DB_DSN="dbname=supplier_migrate host=/tmp/supplier-pg-sock"
export SUPPLIER_TEST_DSN="dbname=supplier_test host=/tmp/supplier-pg-sock"

# 3. 合成数据 + 预览 + 迁移
.venv/bin/python -m supplier_migrate.cli --dsn "$SUPPLIER_DB_DSN" seed-demo --reset
.venv/bin/python -m supplier_migrate.cli --dsn "$SUPPLIER_DB_DSN" plan  \
    --mapping examples/swap.csv
.venv/bin/python -m supplier_migrate.cli --dsn "$SUPPLIER_DB_DSN" apply \
    --mapping examples/swap.csv --operation-id demo-swap

# 4. 核验 / 导出回执
.venv/bin/python -m supplier_migrate.cli --dsn "$SUPPLIER_DB_DSN" verify \
    --operation-id demo-swap
.venv/bin/python -m supplier_migrate.cli --dsn "$SUPPLIER_DB_DSN" export-receipt \
    --operation-id demo-swap --output /tmp/demo-receipt.json

# 5. 测试（24 项，含交换/三节点循环/链/占用/重复/空操作/竞争/三个故障点）
.venv/bin/python -m pytest tests -q
```

停止数据库：`pg_ctl -D /tmp/supplier-pgdata stop -m fast`。

## 目录

```
supplier_migrate/
  migrations/001_schema.sql   # 表、不可变触发器、可延迟外键、回执表
  mapping.py                  # CSV 解析、整批校验、规范化与哈希
  db.py                       # plan/apply/add_order/verify，事务与锁
  cli.py                      # 命令行
  seed.py                     # 合成数据
examples/*.csv                # 各类映射样例
scripts/setup_local_db.sh     # 一键本地环境
tests/                        # 端到端场景与竞争测试
```
