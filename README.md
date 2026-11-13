# 合成多币种账簿 CLI（PostgreSQL 16 + Python 3.11）

本地独立的模拟账簿，用于验证**批量入账**与**月末关账**。仅处理合成数据。

核心保证：

- 账户币种固定；凭证可多币种，但**每个币种分别借贷相等**，不允许币种间串抵。
- 未知账户、币种不符、不平衡 → 整张凭证失败，绝不落部分分录。
- 凭证及分录提交后不可修改/删除；相同 `entry_id` 且规范内容相同重试返回原结果，
  换内容拒绝。
- `reverse` 生成借贷互换的完整冲销凭证；原凭证仅能成功冲销一次；冲销不可再冲销；
  已关账原凭证可在后续开放期间冲销，历史快照保持原样。
- `close` 按“账户 × 币种”保存借贷净额快照；关闭后拒绝该期间新记账；
  关账与入账有明确先后边界；重复关账返回同一快照。
- 任何成功入账/冲销，其明细、累计余额、关联关系、请求记录**同事务提交或回滚**。

## 快速开始

```sh
bash scripts/pg_start.sh        # 启动独立 PG16 集群（/tmp 下，socket 免密）
.venv/bin/python -m ledger init-db
bash scripts/demo.sh            # 合成数据 → 入账 → 冲销 → 关账 → 试算 → 审计
bash scripts/run_tests.sh       # 24 项测试（并发、崩溃、原始 SQL 核对）
```

详细运行说明、连接参数与设计见 **[docs/RUNBOOK.md](docs/RUNBOOK.md)**。

## 命令

`init-db`、`reset-db`、`load-accounts`、`post`、`reverse`、`close`、
`trial-balance`、`audit`、`show-entry`、`demo-data`、`sql`（只读）。

```
ledger post --entry-file entry.json
ledger reverse --reversal-json '{"entry_id":"R1","reverses_entry_id":"E1","entry_date":"2026-04-02"}'
ledger close --period 2026-03
ledger trial-balance --period 2026-03
ledger audit
```

## 文档

- [docs/RUNBOOK.md](docs/RUNBOOK.md) — 运行、参数、一致性模型、目录结构
- [docs/MANUAL_EXAMPLE.md](docs/MANUAL_EXAMPLE.md) — 小规模手算样例
- [docs/VERIFICATION.md](docs/VERIFICATION.md) — 测试矩阵与原始 SQL 核对结果
- [migrations/](migrations/) — 迁移脚本

依赖：Python 3.11、PostgreSQL 16、`psycopg[binary]`（已装入 `.venv`）。
数据库、日志、临时验证数据均在忽略目录或 `/tmp`，不提交到 Git。
