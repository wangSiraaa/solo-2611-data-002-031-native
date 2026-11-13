# 验证记录

记录本实现的测试覆盖与一次原始 SQL 独立核对结果。全部为合成数据。

复现：

```sh
bash scripts/pg_start.sh
bash scripts/run_tests.sh
bash scripts/verify_raw.sh
```

## 1. 测试套件（24 项，全部通过）

`pytest -q` → `24 passed`。

功能与语义：

| 测试 | 验证点 |
| --- | --- |
| `test_multicurrency_entry_balances_each_currency` | 多币种凭证每个币种独立平衡后才入账 |
| `test_totals_equal_but_cross_currency_offset_is_rejected` | 借贷总额相等但币种间串抵 → 拒绝、零残留 |
| `test_same_currency_totals_cannot_net_other_currencies` | 单币种不能替另一币种抵账 |
| `test_unknown_account_and_currency_mismatch_are_atomic` | 未知账户 / 币种不符使整张凭证失败，无部分分录 |
| `test_identical_retry_returns_original_and_different_content_is_rejected` | 相同 `entry_id` 同内容幂等返回；换内容拒绝；键序等价输入合并 |
| `test_reverse_mirrors_and_is_single_use` | 冲销逐行镜像；原凭证只能冲一次；冲销不可再冲销 |
| `test_closed_original_reversed_in_open_period_preserves_history` | 已关账原凭证在后续开放期间冲销，历史快照不变；关闭期间拒绝新记账 |
| `test_close_is_idempotent_and_snapshot_matches_raw` | 重复关账同快照；快照等于原始聚合 |
| `test_append_only_enforced_inside_database` | 数据库触发器和约束阻止对已提交数据的 UPDATE/DELETE |
| `test_balance_rows_survive_rejected_posts` | 各类拒绝（不平衡/未知账户/结构非法/负额）均无余额或请求痕迹 |
| `test_reversal_date_is_independent_of_original_date` | 冲销期间由冲销自身记账日期决定，不受原凭证日期约束 |
| `test_historical_snapshot_unchanged_by_later_reversal` | 后续冲销 + 关账不改变历史期间快照；原 + 冲销净额为零 |
| `test_audit_detects_tampering` | 人为破坏余额后 audit 必须报错 |
| `test_accounts_load_is_idempotent_and_rejects_currency_change` | 账户重复载入幂等；改币种整体拒绝 |
| `test_close_then_reverse_same_period_is_rejected` | 原凭证所在期间关闭后，同期间冲销被拒绝 |

并发（真实进程）：

| 测试 | 验证点 |
| --- | --- |
| `test_same_entry_concurrent_retry_posts_once` | 4 进程同凭证并发重试：一条凭证、一条请求、返回一致 |
| `test_two_concurrent_reversals_yield_one_reversal` | 3 进程并发冲销同一凭证：恰好一个成功，其余 `already_reversed` |
| `test_concurrent_identical_reverse_retry_creates_one_reversal` | 4 进程完全相同冲销请求：一条冲销、一条请求 |
| `test_close_and_last_entry_race_has_clear_boundary` | 关账先持锁时，最后一笔入账要么已被拒绝要么已在快照中（不会出现记账但不在快照） |
| `test_post_race_where_post_wins_is_in_snapshot` | 入账先持锁时，随后关账快照包含该凭证 |
| `test_concurrent_close_returns_identical_snapshot` | 3 进程并发关账返回同一快照，仅一个期间与 close 请求 |

进程终止（SIGKILL）：

| 测试 | 验证点 |
| --- | --- |
| `test_kill_after_insert_before_commit_leaves_nothing` | 插入分录后、提交前终止：凭证/分录/余额/请求均无残留；重试恰好入账一次 |
| `test_kill_after_commit_before_response_keeps_committed_effect` | 提交后、响应前终止：效果已提交；同内容重试幂等返回原结果 |
| `test_kill_before_commit_of_reversal_and_close` | 冲销与关账在提交前终止均无残留 |

## 2. 原始 SQL 独立核对（`scripts/verify_raw.sh` 实测输出）

场景：2026-03 入账 6 张（含多币种）、1 张串抵凭证被拒、关账 2026-03、在
2026-04 冲销 demo-004。以下 SQL **直接读原始表**，不经过 CLI 审计逻辑。

- 每个币种全账借贷差：EUR / JPY / USD 均 `diff = 0`。
- 每张凭证逐币种平衡：查询返回 0 行（无不平衡）。
- 累计余额 vs 原始分录聚合：0 行不匹配。
- `period_snapshots` vs 2026-03 原始聚合：0 行不匹配。
- 原凭证 demo-004 + 冲销 rev-004 每账户净额：`receivable-usd` 与
  `revenue-usd` 均 `net = 0`。
- 冲销逐行镜像检查：0 行不匹配。
- 关账计数 vs 原始计数：2026-03 `entry_count=6 / posting_count=12`，原始
  `6 / 12`。
- CLI `audit` 结论：`"ok": true`，各问题列表为空。

## 3. 变异测试（证明并发测试确有约束力）

为确认测试不是“永远为真”，临时移除实现中的全局写锁后再跑并发测试，
测试必须失败；恢复后必须通过：

- 移除 `pg_advisory_xact_lock`：`test_two_concurrent_reversals_yield_one_reversal`
  失败——第二个冲销撞上 `entries_one_reversal_per_original_uidx`，返回
  `database_error`（唯一索引兜底），而非预期的 `already_reversed`。说明该
  测试确实依赖应用层串行化。
- 同时移除 advisory lock 与 `close` 的 `LOCK TABLE ... SHARE MODE`：
  `test_close_and_last_entry_race_has_clear_boundary` 失败——入账在关账之后
  仍成功，出现“已记账但不在快照”的坏边界。恢复锁后通过。

（变异只改内存中的工作副本，测试后已用备份还原；`grep MUTANT` 为空。）

## 4. 关于数据库运行位置的说明

首次把集群数据目录放在 `/workspace/.runtime` 时，FUSE 文件系统导致
PostgreSQL 偶发 `Permission denied`（`base/.../1247_fsm`）。改为默认
`/tmp/ledger-pgdata` 后测试连续通过。这是环境限制，不是业务代码问题；
`scripts/pg_start.sh` 与 CLI 默认参数均指向 `/tmp` 下的独立集群。

## 5. 已知边界

- 单写者锁模型针对单实例本地库，吞吐量以正确性为先；若需多实例写入，
  可把 advisory lock 换成表行锁并保留 `close` 的 `SHARE` 表锁。
- `account_balances` 由同事务维护，设计为单写者；`audit` 可发现任何外部
  篡改（如测试所示）。
- 空期间关账允许，快照 `accounts` 为空数组。
- `load-accounts` 不校验“账户是否已被引用后再改币种”；由于币种写在
  `accounts` 且不可变，改币种会被 `accounts_conflict` 拒绝。
