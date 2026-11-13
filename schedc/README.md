# schedc — 本地周期计划编译器

把带 IANA 时区的本地周期规则编译成 UTC 计划，并以**原子、不可变的版本**
存入 PostgreSQL 16。只依赖 Python 3.11 标准库（`zoneinfo`，使用本机
`/usr/share/zoneinfo` 时区数据）+ `psycopg` 3。

```
schedc/
  schedc/            # Python 包
    models.py        # 数据模型、校验、规范化与稳定哈希
    compiler.py      # 本地日期枚举 + DST 解析 -> UTC 计划
    store.py         # 版本化存储：publish / query / verify / migrate
    cli.py           # generate / compile / publish / query / verify / migrate
  sql/001_init.sql   # 数据库迁移
  tests/             # 41 个 pytest 用例（编译器纯函数 + 数据库存储）
```

## 规则 JSON

```json
{
  "rules": [{
    "rule_id": "sh-daily",              // 唯一，^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$
    "timezone": "Asia/Shanghai",        // IANA 名称，必须能被本机 zoneinfo 加载
    "local_time": "09:00:00",           // 当地 HH:MM:SS
    "start_date": "2026-01-01",         // 生效日期范围，两端均包含
    "end_date": "2026-12-31",
    "frequency": {"type": "daily"}
               | {"type": "weekly",  "weekdays": [1, 3]}   // ISO 1=周一 .. 7=周日
               | {"type": "monthly", "day": 31}            // 1..31；该月无此日则跳过，不挪到月末
               | {"type": "monthly", "day": "L"},          // 月末（闰年 2 月 = 29 日）
    "on_nonexistent": "skip" | "error", // 当地时刻不存在（春季跳时）的策略
    "on_repeated": "first" | "second" | "both"  // 当地时刻重复（秋季回拨）的策略
  }],
  "exceptions": [
    {"rule_id": "sh-daily", "date": "2026-03-09", "action": "skip"},
    {"rule_id": "sh-daily", "date": "2026-03-11", "action": "replace",
     "replace_time": "15:00:00"}
  ]
}
```

语义要点：

* **DST 解析**：对每个当地日期逐一用 `zoneinfo` 做「当地 → UTC → 当地」
  往返验证，只接受真实存在的时刻（绝不直接附 `tzinfo` 了事）。不存在时刻：
  `error` 使整个编译失败；`skip` 记入排除清单（含日期与原因）。重复时刻：
  `first`/`second` 按真实 UTC 先后取第一次/第二次；`both` 生成两个计划，
  各自带偏移与 UTC；普通时刻不因 `both` 重复。日/周规则**不是**固定秒数递增
  （跨跳时边界 UTC 间隔是 23h/25h，测试里有断言）。
* **例外**：按 `(rule_id, 当地日期)` 唯一，优先于基础周期。`replace` 即使
  当天不符合周/月周期也生成当天计划，但不得越出生效日期范围（越界例外记入
  排除清单 `exception_out_of_range`）。
* **编译窗口**：`compile` 接收 UTC 起止时刻，输出严格落在**半开区间**
  `[start, end)` 内的计划；窗口最长 400 天，规则最多 100 条。
* **计划项**：`rule_id`、原计划当地日期、最终当地时刻、UTC 时刻、偏移、
  重复分支（`none`/`first`/`second`）、命中例外的依据；按
  `(UTC, rule_id, 当地日期)` 稳定排序。`plan_id` 由规范规则内容 + 当地日期 +
  实际 UTC 时刻 SHA-256 生成，相同输入重编译结果一致。

## 版本化存储

`publish` 把规则、例外、编译区间、计划、排除清单作为**一个版本**在单个
事务里写入，全部成功才移动 `current_version` 指针：

* **乐观并发**：发布者对 `current_version` 单行 `SELECT ... FOR UPDATE`
  串行化；两个基于同一 `expected_version` 的并发发布至多一个成功，另一个
  收到 `version changed`（退出码 3）。
* **幂等重试**：`request_id` 唯一。相同 `request_id` + 相同内容（规范化
  规则+例外+窗口的 SHA-256）重试返回原版本（`replayed: true`）；相同
  `request_id` 换内容则拒绝（退出码 4）。
* **失败安全**：编译失败（如 DST `error` 策略）在写库前中止；发布中途进程
  被杀，事务整体回滚，已有版本不受任何影响。
* `query --version N` 可读取历史版本；`verify` 重新校验任一版本。

## 本机完整命令

```bash
# 0. 启动本地 PostgreSQL 16（如无现成实例）
export PATH=/usr/lib/postgresql/16/bin:$PATH
initdb -D /tmp/pgdata -U schedc_admin --auth=trust --encoding=UTF8
mkdir -p /tmp/pgsock && chmod 700 /tmp/pgsock
pg_ctl -D /tmp/pgdata -o "-p 54329 -k /tmp/pgsock -c listen_addresses=localhost" \
  -l /tmp/pgdata/server.log start
createdb -h 127.0.0.1 -p 54329 -U schedc_admin schedc

# 1. 环境
cd /workspace/schedc
pip install "psycopg[binary]" pytest
export SCHEDC_DSN="postgresql://schedc_admin@localhost:54329/schedc"

# 2. 数据库迁移（应用 sql/*.sql，记录于 schema_migrations）
python3 -m schedc migrate

# 3. 生成合成规则（覆盖全部验证场景）
python3 -m schedc generate --out rules.json

# 4. 编译（纯函数，不落库）；窗口 [2026-01-01Z, 2027-01-01Z)
python3 -m schedc compile --rules rules.json \
  --start 2026-01-01T00:00:00Z --end 2027-01-01T00:00:00Z --out compiled.json

# 5. 发布版本 1（基于「尚无版本」expected-version=0）
python3 -m schedc publish --rules rules.json \
  --start 2026-01-01T00:00:00Z --end 2027-01-01T00:00:00Z \
  --expected-version 0 --request-id req-2026-v1

# 6. 查询当前版本 / 历史版本
python3 -m schedc query
python3 -m schedc query --version 1

# 7. 校验版本完整性（本地<->UTC 往返、唯一性、哈希、重编译一致性…）
python3 -m schedc verify

# 8. 幂等重试：相同 request-id + 相同内容 -> 返回原版本
python3 -m schedc publish --rules rules.json \
  --start 2026-01-01T00:00:00Z --end 2027-01-01T00:00:00Z \
  --expected-version 0 --request-id req-2026-v1     # -> version 1, replayed: true

# 9. 发布中途被杀（故障注入，演示原子回滚；已有版本不受影响）
SCHEDC_FAULT_AFTER_PLANS=100 python3 -m schedc publish --rules rules.json \
  --start 2026-01-01T00:00:00Z --end 2026-06-01T00:00:00Z \
  --expected-version 1 --request-id req-killed || true
python3 -m schedc query | python3 -c "import json,sys; print(json.load(sys.stdin)['version'])"
# -> 仍为 1

# 10. 测试（编译器 30 项 + 存储 11 项，含并发发布/中途终止/提交后重试）
createdb -h 127.0.0.1 -p 54329 -U schedc_admin schedc_test
python3 -m pytest -q
```

## 手算核对表（合成规则，窗口 2026-01-01T00:00:00Z – 2027-01-01T00:00:00Z）

纽约 2026：03-08 02:00→03:00 春季跳时；11-01 02:00→01:00 秋季回拨。

| 规则 | 预期 |
|---|---|
| `ny-spring-skip` 02:30 每天 03-01..03-15 | 03-07 EST(-05:00)→07:30Z；**03-08 不存在→排除清单**；03-09 EDT(-04:00)→06:30Z；共 14 计划 + 1 排除 |
| `ny-fall-first` 01:30 每天 10-25..11-08 | 11-01 取第一次：05:30Z（-04:00，branch=first）；共 15 |
| `ny-fall-second` 同上 | 11-01 取第二次：06:30Z（-05:00，branch=second）；共 15 |
| `ny-fall-both` 同上 | 11-01 生成 05:30Z 与 06:30Z 两个计划；共 16 |
| `sh-daily` 09:00 每天 | 每天 01:00Z（+08:00 固定），共 365 |
| `ny-month-end` 21:00 月末 | 02-28（非闰年）等 12 个当地日；12-31 21:00 EST = 2027-01-01T02:00Z 落在窗口外 → 共 11 |
| `sh-monthly-31` 10:00 每月 31 日 | 仅 1/3/5/7/8/10/12 月的 31 日 02:00Z，共 7；短月跳过且**不**挪到 30 日 |
| `ny-weekly-mon` 周一 09:00 + 例外 | 03-09 被 skip 覆盖（排除清单）；03-11（周三）由 replace 新增 15:00→19:00Z；03-16 改为 07:30→11:30Z；共 5 计划 + 1 排除 |
| `utc-midnight` 00:00 每天 | 首项 2026-01-01T00:00:00Z（含端点），末项 2026-12-31T00:00:00Z；2027-01-01T00:00:00Z 被半开区间排除；共 365 |

合计 **813 计划 + 2 排除**，`content_hash` 前 16 位 `82e3ec53d973932a`
（tzdata 一致时重编译恒定）。

闰年二月末单独验证（测试用例）：窗口 2024-02，`monthly L` → 2024-02-29；
`monthly 29` 在 2025-02 → 0 计划；在 2024-02 → 恰好 02-29，不挪到 02-28。
