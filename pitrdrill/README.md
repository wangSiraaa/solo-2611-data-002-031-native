# pitrdrill — PostgreSQL 16 时间点恢复(PITR)演练助手

可重复地在**本机、隔离、自建**的 PostgreSQL 16 实例上验证：
一份物理基础备份 + 连续 WAL 归档，确实能够通过**真实 WAL 重放**恢复到误操作之前，
而不只是“备份文件存在”。

- 仅依赖 Python 3.11 标准库 + 本机 `postgresql-16` 命令行工具（`initdb/pg_ctl/pg_basebackup/psql/pg_waldump/pg_controldata/...`），**无需任何 Python 第三方驱动**（通过 `psql` 子进程交互）。
- 所有实例/目录均带 `pitrdrill` 任务标识，使用独立端口与独立 Unix socket，**不连接、不停止用户已有数据库**。
- 恢复目录与源实例完全分离；恢复全程由 PostgreSQL 重放 WAL 完成，**不使用 SQL 导入、查询结果复制或手工撤销**。

---

## 一条命令启动完整本地演练

```bash
cd /workspace/pitrdrill
python3 pitrdrill.py demo            # 完整时长(默认)
python3 pitrdrill.py demo --quick    # 缩短时长的冒烟模式(约 3~4 分钟)
python3 pitrdrill.py demo --rebuild  # 显式清理上一轮本演练资源后重建
```

在已有演练根目录上再次执行 `demo` 默认会**拒绝覆盖**（避免误删正在使用的恢复实例）；
需要显式重建时加 `--rebuild`，它等价于先执行
`cleanup --yes --include-source --include-archive`，只清理本演练根目录内带 `pitrdrill` 标记的资源。

`demo` 自动完成：源实例初始化 → 持续写入的合成业务库（父子表、序列、更新、删除、事务见证标记、命名 restore point、误操作）→ 真实物理基础备份与 WAL 归档 → 两次成功恢复 → 四类故障注入 → 生成 Markdown/JSON 报告。

成功恢复后**只对隔离实例做提升与核验，绝不替换源数据库**；源实例演练结束仍在运行。

---

## 演练时间线（恢复目标全部以命名 restore point 指定，不用客户端墙钟）

```
心跳写入 → 基础备份 b1-early
        → 见证标记 MARKER-M1 → restore point rp_early
        → 基础备份 b2-mid
        → 目标前正常 UPDATE/DELETE
        → 见证标记 MARKER-M2 → restore point rp_late
        → 见证标记 MARKER-M3(目标之后)
        → 误操作 mishap(删除 parent 3/7、污染 parent 5、新增 parent 9001)
```

成功恢复：

| 恢复实例 | 基础备份 | 目标 | 动作 | 含义 |
| --- | --- | --- | --- | --- |
| `R1-early` | b1-early | `rp_early` | 到达后暂停 | 目标之前没有误操作，验证暂停态（只读） |
| `R2-late` | b2-mid | `rp_late` | 到达后**提升** | 含目标前更新/删除、排除目标后事务；产生可区分的新时间线 |

故障注入（全部作用于隔离副本，不动真实备份与源归档）：

| 场景 | 制造方式 | 预期判定 |
| --- | --- | --- |
| 缺 WAL 段 `set-gap` | 删除目标之前的一个中间段 | 预检 `ARCHIVE_GAP`；强制启动后 PG `FATAL: recovery ended before configured recovery target was reached` |
| 归档迟到/等待 `set-waiting` | 门控 `restore_command`，先挡住 WAL | `WAITING_ARCHIVE` 受限等待超时并保留诊断；补齐并放行后真实 `TARGET_REACHED` |
| 错备份身份 `b-foreign-identity` | 用另一个 initdb 集群的 `pg_control` 替换 | 预检 `WRONG_DATABASE_IDENTITY`（外加 `pg_verifybackup` 校验失败） |
| 错误时间线 `set-tli2` | 段名时间线前缀改为 2 | 预检 `WRONG_TIMELINE` |
| 截断归档 `set-trunc` | 截断段尾部 8KiB | 预检大小/sha256/`pg_waldump` 报警；强制启动后 PG 报告 `archive file ... has wrong size` |

报告严格区分四种（含提升共五种）结果分类，**绝不把“进程仍在运行/端口已监听”当作成功**：

- `TARGET_REACHED` 目标已到达（日志 `recovery stopping at restore point` + `pausing at the end of recovery` 双重确认，并用 SQL 取 `pg_is_in_recovery/pg_last_wal_replay_lsn` 与 `pg_controldata` 交叉取证）
- `WAITING_ARCHIVE` 归档仍缺失而等待（等待总时长受配置预算限制，超时保留诊断，**不静默恢复到更早位置**）
- `FAILED` 恢复失败
- `PROMOTED` 已提升为新时间线（以当前 WAL 文件名前缀与 `pg_wal/*.history` 确认，与源时间线和原归档严格区分）

---

## 核验（独立 SQL，逐表）

`verify` 在隔离实例上核对：

1. 目标见证标记存在/更晚标记不存在（`pitrdrill_witness`）
2. 误删除未发生（parent 3/7 仍在）、误更新未发生（parent 5 未被 `MISHAP` 污染）、目标后新增不存在（parent 9001）、误操作事件日志不存在
3. 父子外键约束定义存在、无悬空子记录；提升后再用真实违规插入验证约束拒绝
4. 序列对象完好；提升后验证 `nextval` 推进与 identity 列后续插入行为
5. 重放 LSN 不越过目标 restore point（容差一个 WAL 段）
6. 提升后新时间线 > 源时间线且生成 `.history` 文件

暂停态实例为只读，写探针只在提升后的实例执行（符合 `recovery_target_action=pause` 语义）。

---

## 目录布局与磁盘清理边界

默认根目录（清理边界）：`/workspace/pitrdrill-work`，可用 `--root` 或环境变量 `PITRDRILL_ROOT` 覆盖。

```
<root>/
  drill-state.json            # 持久化状态(原子写): 命令、PID、阶段、LSN、身份、结果
  source/data                 # 源实例 PGDATA(initdb 创建, 带 pitrdrill-source.marker)
  source/source.log           # 源实例 PostgreSQL 日志
  archive/wal/                # WAL 归档 + 每段 .sha256 sidecar
  archive/archive-index.jsonl # 归档清单(文件/大小/sha256/时间线/逻辑段号/入库时间)
  backups/<name>/             # pg_basebackup 物理基础备份(含 backup_label、backup_manifest)
  archive-sets/<set>/         # 归档集合快照(硬链接, 含 set-manifest.json)
  restores/<name>/            # 隔离恢复 PGDATA、restore 包装脚本、restore.log
  reports/drill-report.{md,json}
  tmp/                        # 门控文件、心跳事件、暂存 WAL
  bin/pitrdrill-archive-push.sh
```

Unix socket 因部分工作目录挂载点不支持对 socket `chmod`，统一放在
`/tmp/pitrdrill-sockets/<root 名>/...`（可用 `PITRDRILL_SOCKET_ROOT` 覆盖），
数据文件仍全部位于根目录内。

### 预计空间

- 源 PGDATA（小业务库）：约 40–80 MiB
- 每个物理基础备份：约 40–80 MiB
- WAL 归档（`demo --quick`）：约 9–12 个 16 MiB 段 ≈ 160–200 MiB
- 归档集合：硬链接，几乎不额外占空间；每个隔离恢复目录：约 40–80 MiB
- `demo --quick` 典型总量：**约 0.6–1 GiB**；完整模式约 1–2 GiB。

### 清理命令（只清理本演练资源）

```bash
# 查看本工具创建的全部资源与运行状态(不做改动)
python3 pitrdrill.py status

# 清理恢复实例/备份/集合/报告/临时目录与本演练 socket, 默认保留源实例与归档
python3 pitrdrill.py cleanup --yes

# 同时停止并删除源实例
python3 pitrdrill.py cleanup --yes --include-source

# 连同 WAL 归档一并删除(彻底销毁演练)
python3 pitrdrill.py cleanup --yes --include-source --include-archive
```

清理保护：只允许操作根目录内路径；删除 PGDATA 前校验 `pitrdrill-*.marker`；
`/tmp` 下只删除按本演练根目录命名的 socket 目录。**不会触碰源库之外的任何实例、其他演练目录或仍在使用的归档。**

---

## 常用子命令（也可手工分步操作）

```bash
python3 pitrdrill.py init                       # initdb + 启动源实例 + 业务库
python3 pitrdrill.py backup b1 --note 早期备份   # pg_basebackup -X stream 真实物理备份
python3 pitrdrill.py restore-point rp_early      # 命名恢复点并等待其 WAL 归档
python3 pitrdrill.py witness MARKER-M1           # 写入事务见证标记
python3 pitrdrill.py mkset set-full              # 建立归档集合快照
python3 pitrdrill.py candidates                  # 查看备份/恢复点/集合候选与恢复前提
python3 pitrdrill.py preflight --backup b1-early --target rp_early --set set-full
python3 pitrdrill.py restore-start R1 --backup b1-early --target rp_early --set set-full
python3 pitrdrill.py restore-observe R1 --follow # 继续观察正在恢复的实例
python3 pitrdrill.py promote R1                  # 仅对 TARGET_REACHED 的隔离实例提升
python3 pitrdrill.py verify R1 --expect MARKER-M1 --absent MARKER-M2,MARKER-M3
python3 pitrdrill.py restore-stop R1             # 停止隔离实例(目录保留)
python3 pitrdrill.py restore-drop R1             # 显式重建: 删除隔离恢复目录
python3 pitrdrill.py report                      # 重新生成报告
```

强制退出后可凭 `drill-state.json` 与 PGDATA 内 marker 辨认自建实例；
再次执行 `restore-observe` 可继续观察，`restore-drop` 后再 `restore-start` 即显式重建隔离目录。

---

## 关键设计说明

- **归档身份与完整性**：每个 WAL 段入库时记录大小与 sha256（`.sha256` + JSONL 索引）。预检核对 system identifier、时间线、按逻辑段号的连续性（缺口）、段大小、sha256，并用 `pg_waldump` 解析交叉验证。
- **不误判备份自带 WAL**：`pg_basebackup -X stream` 的备份 `pg_wal` 内可能包含备份完成所需的段；缺口分析取“归档集合 ∪ 备份自带 pg_wal”，不会仅凭某段未出现在外部目录就判定不可恢复。
- **集合可继续增长**：预检允许在受限预算内等待新归档并把新段补链进集合，但严格尊重集合清单的 `excluded` 且不覆盖已存在（可能被故意截断）的文件，故障集合不会被自动“治愈”。
- **恢复配置**：PG16 全部通过 `postgresql.auto.conf` + `recovery.signal`（一次性归档恢复）/`standby.signal`（备库模式，用于归档迟到的真实等待）配置，`recovery_target_name` 指定命名恢复点，`recovery_target_action=pause` 停在可检查状态。
- **归档包装脚本**：`archive_command` 与 `restore_command` 均由工具生成包装脚本，记录每次 HIT/MISS/GATED 到恢复日志，作为 WAL 重放的过程证据。
