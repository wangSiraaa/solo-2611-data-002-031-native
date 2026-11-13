# 本地在线回填演示（records → canonical_records）

把一张**仍在写入**的原始表在线转换成规范表的完整本机演示，基于 Python 3.11 +
PostgreSQL 16，不连接任何真实系统。不停写、不一次性复制、不回填旧值覆盖新值。
原生数据库初始化背景见 [environment/README.md](environment/README.md)。

## 一、业务规则

源表（原始表，字段均非空，主键禁止修改）：

```sql
records(id BIGINT PRIMARY KEY, raw_code TEXT, amount_cents BIGINT, active BOOLEAN)
```

规范表（迁移目标）：

| 源字段       | 规范字段 | 规则 |
|--------------|----------|------|
| id           | id       | 相同主键 |
| raw_code     | code     | 去首尾空白，且**仅**把 ASCII 小写 `a-z` 转大写；`é` 等非 ASCII 字符保持原样 |
| amount_cents | amount   | 精确十进制 `amount_cents / 100`（`NUMERIC(18,2)`，绝不经过浮点） |
| active       | active   | 原样 |

去首尾空白后 code 为空串属于**格式错误**：不写入规范表，而在 `migration_errors`
记录失败位置，并明确阻断 finalize。

源表仅额外增加一个迁移辅助列 `row_version`（触发器维护，单调递增），原业务字段
语义完全保留。

## 二、核心机制（onlmig/engine.py）

1. **可靠变更捕获**：`records_audit` 行级触发器在同一事务内向 `record_changes`
   写 I/U/D 变更。源写入与变更记录同生共死——回滚的写入在变更日志里同样不存在，
   迁移天然看不到。
2. **不把“最大编号”当“全部已提交”**：`change_seq` 来自 SEQUENCE，长事务可能先
   领号后提交，编号间出现“洞”。引擎维护连续水位线 `watermark`，只在连续前缀推进；
   洞位之后已可见的变更可以先应用（版本守卫保证幂等），并通过**洞位补扫**在迟到
   事务提交后补处理。回滚事务也会消耗 sequence，形成永久洞，由
   `count_unapplied_changes` 按“每个 id 最后一条变更的最终影响是否生效”判定，
   而不是简单比较最大编号。
3. **回填与变更竞争**：块事务先取主键范围内 id，再逐行 `SELECT ... FOR UPDATE`
   读取当前版本——删除已提交则读不到行直接跳过（不可能复活）；并发写未提交则行锁
   阻塞至其提交。版本守卫 `canonical.row_version < EXCLUDED.row_version` 保证旧
   回填/旧变更无法覆盖新版本。
4. **崩溃续跑**：块进度、水位线、状态、失败位置全部持久化在数据库；工作与进度在
   同一事务提交。SIGKILL（演示用 `os._exit`）后未提交工作整体回滚，resume 从最后
   已提交进度继续，目标表不清空、不全量重拷。
5. **单实例互斥**：固定 key 的 `pg_advisory_lock`，第二个迁移进程立即被拒绝。
6. **finalize**：先 `LOCK TABLE records IN SHARE MODE` 短暂阻止源写入，排空已提交
   变更并补齐回填，再在确定边界逐行核验；只有规范表与该边界上的源表**完全对应**且
   无格式错误时才标记 `READY`，否则持久化 `FAILED` 与失败位置并以非零码退出。

## 三、目录结构

```
onlmig/
  db.py         本机连接配置（Unix socket，环境变量可覆盖）
  schema.py     表结构、触发器、进度表/错误表 DDL
  normalize.py  规范化规则（Python 与核验共用同一份实现）
  engine.py     迁移引擎：分块回填、变更接续、水位线、版本守卫、finalize/verify
  writer.py     源表写入函数与并发写入器（只生成本地 I/U/D 事务，含按比例回滚）
  cli.py        命令行入口
  demo.py       全自动演示编排（46 项断言）
requirements.txt
```

## 四、完整本机运行步骤

### 1. 准备独立 PostgreSQL 实例与 Python 环境

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

mkdir -p .runtime /tmp/onlmig_sock
initdb -D "$PWD/.runtime/pgdata" --auth-local=trust --auth-host=scram-sha-256 -U node
# 注意：若工作目录文件系统不支持 Unix socket 权限位（本仓库即如此），
# socket 放到 /tmp 下；数据与日志仍在忽略目录 .runtime/
pg_ctl -D "$PWD/.runtime/pgdata" -l "$PWD/.runtime/postgres.log" \
  -o "-k /tmp/onlmig_sock -c listen_addresses='' -c fsync=on -c full_page_writes=on" start
createdb -h /tmp/onlmig_sock -U node taskdb
```

连接参数可用环境变量覆盖：`ONLMIG_PGHOST`（默认 `/tmp/onlmig_sock`）、
`ONLMIG_PGDB`（默认 `taskdb`）、`ONLMIG_PGUSER`、`ONLMIG_PGPORT`。

### 2. 一键自动演示（推荐）

```sh
.venv/bin/python -m onlmig.demo
```

约 11 秒完成，自动覆盖全部要求场景，46 项检查全部通过后退出码为 0：

- 约 3000 行合成历史数据（合法行 + 空白 code 错误行）
- 分块边界内插入（迁移期间新增到已扫描主键范围）
- 扫描前后更新、已扫描范围内删除
- 先读旧行后收到删除、删后再插（显式快照竞争，验证不复活、不旧值覆盖）
- 事务回滚（回滚写入在源表与变更日志中均无痕迹）
- **提交次序颠倒**：早领号晚提交的事务，验证不冒进水位线且最终正确
- 并发写入器（多线程 I/U/D + 按比例回滚）
- 迁移进程在块提交前、变更批次提交前、finalize 提交前分别被杀死后续跑
- 第二进程被咨询锁拒绝
- 格式错误两次阻断 finalize，修复后才 READY
- 逐主键逐字段精确比较、目标无额外行、每个成功事务均已反映

### 3. 手工 CLI 流程

```sh
PY=".venv/bin/python -m onlmig.cli"

$PY setup                       # 安装表结构与变更捕获触发器
$PY writer insert 1 " hello " 12345 true
$PY writer insert 2 "   " 500 true      # 空白 code -> 格式错误
$PY writer update 1 " world " 99900 false
$PY writer delete 2
$PY writer run --seconds 5 --threads 3 --rollback-pct 8   # 并发写入器

$PY start --chunk 500           # 建立迁移（持久化进度 + 互斥锁）
$PY step                        # 一个回填块 + 一轮变更排空
$PY resume                      # 持续推进到回填完成、无待处理变更
$PY status                      # 进度、待处理变更、失败位置、块记录
$PY finalize                    # 阻止写入→排空→核验；错误则退出码 3
$PY verify                      # 只读核验；不一致退出码 4
$PY reset                       # 演示用：清空全部表与进度
```

### 4. 故障注入（演示进程被杀死）

环境变量 `ONLMIG_FAULT=注入点:第几次命中[:睡眠秒数]`，注入点：

- `step_commit`：回填块提交前
- `drain_commit`：变更批次提交前
- `finalize_commit`：finalize 提交前

```sh
ONLMIG_FAULT=step_commit:1 $PY step      # 第 1 次块提交前 os._exit(2)
$PY resume                                # 从持久化进度继续，目标不清空
ONLMIG_FAULT=finalize_commit:1 $PY finalize
$PY finalize                              # READY 仅在重跑成功后出现
```

### 5. 停止数据库

```sh
pg_ctl -D "$PWD/.runtime/pgdata" stop
```

## 五、进度与失败位置表

| 表 | 内容 |
|----|------|
| `migration_meta` | 单行：状态 INIT/RUNNING/FAILED/READY、回填前沿、连续水位线、已应用最大编号、块大小、失败阶段与信息 |
| `migration_chunks` | 每个回填块一条：主键区间（含空区间跳跃记录）、扫描/应用/错误行数、完成时间 |
| `migration_errors` | 每个当前错误行一条：id、row_version、阶段（backfill/change/finalize）、原始 code 与原因 |
| `record_changes` | 触发器写入的可靠变更流（change_seq、id、I/U/D、row_version） |

## 六、设计边界与取舍

- 规范表多保留一个 `row_version` 列用于版本守卫；业务四字段语义与题目要求一致。
- finalize 的 SHARE 锁会短暂阻塞源写入（允许读取），这是题目允许的“短暂阻止”。
- 回滚消耗的 SEQUENCE 编号不会复用，属 PostgreSQL 标准语义；因此水位线允许小于
  最大变更号，完成判据是“水位线之后不存在影响未应用的已提交变更”。
- 所有数据、日志、依赖均位于 `.runtime/`、`.venv/` 等忽略目录，不进入 Git。
