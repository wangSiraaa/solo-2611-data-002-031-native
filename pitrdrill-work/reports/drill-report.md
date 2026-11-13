# PostgreSQL 16 PITR 演练报告

- 生成时间: 2026-11-13T14:14:09+0000
- 任务标识: `pitrdrill`
- 演练根目录(清理边界): `/workspace/pitrdrill-work`
- 工具版本: pitrdrill /usr/lib/postgresql/16/bin

## 1. 源实例身份
| 项 | 值 |
| --- | --- |
| system identifier | 7707656682046675350 |
| 数据目录 | /workspace/pitrdrill-work/source/data |
| socket 目录 | /tmp/pitrdrill-sockets/pitrdrill-work/source |
| 端口 | 55432 |
| WAL 段大小(字节) | 16777216 |
| 当前时间线 | 1 |
| 日志路径 | /workspace/pitrdrill-work/source/source.log |
| PID | 6558 |

## 2. 物理基础备份
| 名称 | 说明 | 起始LSN | checkpoint LSN | 时间线 | system id | 完整性 | 自带WAL段数 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| b-foreign-identity | 故障演练: 外来 pg_control(错身份) | 0/2000028 | 0/2000060 | 1 | 7707656955484969715 | corrupt | 1 |
| b1-early | 早期基础备份(心跳开始后) | 0/2000028 | 0/2000060 | 1 | 7707656682046675350 | verify_rc=0 | 1 |
| b2-mid | 中期基础备份(rp_early 之后) | 0/5000028 | 0/5000060 | 1 | 7707656682046675350 | verify_rc=0 | 1 |

## 3. 命名恢复点(恢复目标以名称指定, 非墙钟)
| 名称 | LSN | 时间线 | 说明 | 创建时间 |
| --- | --- | --- | --- | --- |
| rp_early | 0/3003600 | 1 | 早期恢复点(误操作之前) | 2026-11-13T14:04:29+0000 |
| rp_late | 0/6002540 | 1 | 晚期恢复点(误操作之前, 含正常更新删除) | 2026-11-13T14:04:31+0000 |

## 4. WAL 归档清单
- 归档目录: `/workspace/pitrdrill-work/archive/wal`
- WAL 段总数: 13
- 首段: `000000010000000000000001`, 末段: `00000001000000000000000B`
- 归档索引: `/workspace/pitrdrill-work/archive/archive-index.jsonl`

### 归档集合
| 集合 | 路径 | 段数 | 排除/备注 |
| --- | --- | --- | --- |
| set-full | /workspace/pitrdrill-work/archive-sets/set-full | 9 |  |
| set-full-copy | /workspace/pitrdrill-work/archive-sets/set-full-copy | 9 |  |
| set-gap | /workspace/pitrdrill-work/archive-sets/set-gap | 8 | 000000010000000000000003 |
| set-tli2 | /workspace/pitrdrill-work/archive-sets/set-tli2 | 10 |  |
| set-trunc | /workspace/pitrdrill-work/archive-sets/set-trunc | 10 |  |
| set-waiting | /workspace/pitrdrill-work/archive-sets/set-waiting | 8 | 000000010000000000000003 |

## 5. 事务见证与关键事件
| 标记 | commit LSN | event_seq | 时间 |
| --- | --- | --- | --- |
| MARKER-M1 | 0/30013B0 | 1 | 2026-11-13T14:04:29+0000 |
| MARKER-M2 | 0/60021A8 | 3 | 2026-11-13T14:04:31+0000 |
| MARKER-M3 | 0/7000768 | 4 | 2026-11-13T14:04:31+0000 |

- **误操作事件**: 删除 parent [3, 7], 污染 parent 5, 新增 parent 9001, LSN 区间 0/7001F50 -> 0/70024E0

## 6. 隔离恢复结果(四类状态严格区分)
| 恢复实例 | 基础备份 | 目标 | 归档集合 | 结果 | 提升后时间线 | 失败原因/诊断 |
| --- | --- | --- | --- | --- | --- | --- |
| F-gap-preflight | b1-early | rp_early | set-gap | 恢复失败 | - | ARCHIVE_GAP: 目标之前仍存在归档缺口(等待 5s 后): 000000010000000000000003 |
| F-gap-running | b1-early | rp_early | set-gap | 恢复失败 | - | 恢复实例启动阶段即退出: 2026-11-13 14:04:55.403 GMT [6826] FATAL:  recovery ended before configured recovery target was reached |
| F-identity | b-foreign-identity | rp_early | set-full-copy | 恢复失败 | - | WRONG_DATABASE_IDENTITY: 备份 system identifier 7707656955484969715 != 源库 7707656682046675350(拿错数据库的基础备份); BACKUP_CORRUPT: pg_verifybackup: error: checksum mismat |
| F-timeline | b1-early | rp_early | set-tli2 | 恢复失败 | - | WRONG_TIMELINE: 归档集合存在错误时间线段 -> 000000020000000000000001: 时间线 2 与要求的 1 不符(错误时间线); 000000020000000000000002: 时间线 2 与要求的 1 不符(错误时间线); 000000020000000000000003: 时间 |
| F-trunc-preflight | b1-early | rp_early | set-trunc | 恢复失败 | - | ARCHIVE_TRUNCATED_OR_CORRUPT: 000000010000000000000003: 大小异常 16769024 != 预期 16777216(疑似截断/损坏); 000000010000000000000003: sha256 不匹配(疑似篡改/写入不完整) |
| F-trunc-running | b1-early | rp_early | set-trunc | 恢复失败 | - | 恢复实例启动阶段即退出: 2026-11-13 14:05:33.633 GMT [7012] FATAL:  archive file "000000010000000000000003" has wrong size: 16769024 instead of 16777216 |
| F-wait-released | b1-early | rp_early | set-waiting | 目标已到达 | - | - |
| F-wait-timeout | b1-early | rp_early | set-waiting | 归档仍缺失而等待 | - | 归档尚未到位, 恢复在等待 WAL: 000000010000000000000003(门控/归档迟到); ARCHIVE_WAIT_TIMEOUT: 等待缺失 WAL 超过预算 30s, 保留诊断, 未静默恢复到更早位置 |
| R1-early | b1-early | rp_early | set-full | 目标已到达 | - | - |
| R2-late | b2-mid | rp_late | set-full | 已提升为新时间线 | 2 | - |

### 恢复实例 `F-gap-preflight` — 恢复失败
- 数据目录: `/workspace/pitrdrill-work/restores/F-gap-preflight/data`
- socket: `/tmp/pitrdrill-sockets/pitrdrill-work/restore/F-gap-preflight`  端口: 0  PID: -1
- 日志: `/workspace/pitrdrill-work/restores/F-gap-preflight/restore.log`
- 预检通过: **False**
  - 硬错误: ARCHIVE_GAP: 目标之前仍存在归档缺口(等待 5s 后): 000000010000000000000003
  - 缺口: 需 2 段, 备份自带覆盖 0 段, 缺失 ['000000010000000000000003']

### 恢复实例 `F-gap-running` — 恢复失败
- 数据目录: `/workspace/pitrdrill-work/restores/F-gap-running/data`
- socket: `/tmp/pitrdrill-sockets/pitrdrill-work/restore/F-gap-running`  端口: 55433  PID: -1
- 日志: `/workspace/pitrdrill-work/restores/F-gap-running/restore.log`

### 恢复实例 `F-identity` — 恢复失败
- 数据目录: `/workspace/pitrdrill-work/restores/F-identity/data`
- socket: `/tmp/pitrdrill-sockets/pitrdrill-work/restore/F-identity`  端口: 0  PID: -1
- 日志: `/workspace/pitrdrill-work/restores/F-identity/restore.log`
- 预检通过: **False**
  - 硬错误: WRONG_DATABASE_IDENTITY: 备份 system identifier 7707656955484969715 != 源库 7707656682046675350(拿错数据库的基础备份); BACKUP_CORRUPT: pg_verifybackup: error: checksum mismatch for file "global/pg_control"
  - 缺口: 需 2 段, 备份自带覆盖 0 段, 缺失 []

### 恢复实例 `F-timeline` — 恢复失败
- 数据目录: `/workspace/pitrdrill-work/restores/F-timeline/data`
- socket: `/tmp/pitrdrill-sockets/pitrdrill-work/restore/F-timeline`  端口: 0  PID: -1
- 日志: `/workspace/pitrdrill-work/restores/F-timeline/restore.log`
- 预检通过: **False**
  - 硬错误: WRONG_TIMELINE: 归档集合存在错误时间线段 -> 000000020000000000000001: 时间线 2 与要求的 1 不符(错误时间线); 000000020000000000000002: 时间线 2 与要求的 1 不符(错误时间线); 000000020000000000000003: 时间线 2 与要求的 1 不符(错误时间线); 000000020000000000000004: 时间线 2 与要求的 1 不符(错误时间线); 000000020000000000000005: 时间线 2 与要求的 1 不符(错误时间线); 000000020000000000000006: 时间线 2 与要求的 1 不符(错误时间线); 
  - 缺口: 需 2 段, 备份自带覆盖 0 段, 缺失 []

### 恢复实例 `F-trunc-preflight` — 恢复失败
- 数据目录: `/workspace/pitrdrill-work/restores/F-trunc-preflight/data`
- socket: `/tmp/pitrdrill-sockets/pitrdrill-work/restore/F-trunc-preflight`  端口: 0  PID: -1
- 日志: `/workspace/pitrdrill-work/restores/F-trunc-preflight/restore.log`
- 预检通过: **False**
  - 硬错误: ARCHIVE_TRUNCATED_OR_CORRUPT: 000000010000000000000003: 大小异常 16769024 != 预期 16777216(疑似截断/损坏); 000000010000000000000003: sha256 不匹配(疑似篡改/写入不完整)
  - 缺口: 需 2 段, 备份自带覆盖 0 段, 缺失 []

### 恢复实例 `F-trunc-running` — 恢复失败
- 数据目录: `/workspace/pitrdrill-work/restores/F-trunc-running/data`
- socket: `/tmp/pitrdrill-sockets/pitrdrill-work/restore/F-trunc-running`  端口: 55434  PID: -1
- 日志: `/workspace/pitrdrill-work/restores/F-trunc-running/restore.log`
- 预检通过: **False**
  - 硬错误: ARCHIVE_TRUNCATED_OR_CORRUPT: 000000010000000000000003: 大小异常 16769024 != 预期 16777216(疑似截断/损坏); 000000010000000000000003: sha256 不匹配(疑似篡改/写入不完整)
  - 缺口: 需 2 段, 备份自带覆盖 0 段, 缺失 []

### 恢复实例 `F-wait-released` — 目标已到达
- 数据目录: `/workspace/pitrdrill-work/restores/F-wait-released/data`
- socket: `/tmp/pitrdrill-sockets/pitrdrill-work/restore/F-wait-released`  端口: 55433  PID: 6869
- 日志: `/workspace/pitrdrill-work/restores/F-wait-released/restore.log`
- 目标到达证据:
```json
{
  "controldata_state": "in archive recovery",
  "controldata_timeline": "1",
  "last_wal_replay_lsn": "0/3003600",
  "log_line": "recovery stopping at restore point \"rp_early\"",
  "log_path": "/workspace/pitrdrill-work/restores/F-wait-released/restore.log",
  "pg_is_in_recovery": "true",
  "recovery_pause": "confirmed-by-log"
}
```

### 恢复实例 `F-wait-timeout` — 归档仍缺失而等待
- 数据目录: `/workspace/pitrdrill-work/restores/F-wait-timeout/data`
- socket: `/tmp/pitrdrill-sockets/pitrdrill-work/restore/F-wait-timeout`  端口: 55433  PID: 6837
- 日志: `/workspace/pitrdrill-work/restores/F-wait-timeout/restore.log`

### 恢复实例 `R1-early` — 目标已到达
- 数据目录: `/workspace/pitrdrill-work/restores/R1-early/data`
- socket: `/tmp/pitrdrill-sockets/pitrdrill-work/restore/R1-early`  端口: 55500  PID: 6665
- 日志: `/workspace/pitrdrill-work/restores/R1-early/restore.log`
- 目标到达证据:
```json
{
  "controldata_state": "in archive recovery",
  "controldata_timeline": "1",
  "last_wal_replay_lsn": "0/3003600",
  "log_line": "recovery stopping at restore point \"rp_early\"",
  "log_path": "/workspace/pitrdrill-work/restores/R1-early/restore.log",
  "pg_is_in_recovery": "true",
  "recovery_pause": "confirmed-by-log"
}
```
- 预检通过: **True**
  - 缺口: 需 2 段, 备份自带覆盖 0 段, 缺失 []
- 独立 SQL 核验: 16/16 通过 (时间线 1)
  - [PASS] 实例可连接且身份为 pitrdrill 隔离实例: pg_is_in_recovery=true, timeline=1
  - [PASS] 仍处于恢复暂停态(未自动提升): pg_is_in_recovery=true
  - [PASS] 目标标记存在: MARKER-M1: MARKER-M1|0/30013B0|1
  - [PASS] 更晚标记不存在(目标后事务已排除): MARKER-M2: count=0
  - [PASS] 更晚标记不存在(目标后事务已排除): MARKER-M3: count=0
  - [PASS] 误删除未发生: parent 3 仍存在: 3|parent-3
  - [PASS] 误删除未发生: parent 7 仍存在: 7|parent-7
  - [PASS] 误更新未发生: parent 5 未被 MISHAP 污染: payload=tick-4
  - [PASS] 目标后正常新增已排除: parent 9001 不存在: count=0
  - [PASS] 误操作事件日志不存在: mishap count=0
  - [PASS] 父子外键约束定义存在: pitrdrill_child_parent_id_fkey|pitrdrill_parent
  - [PASS] 不存在悬空子记录: orphan count=0
  - [PASS] 只读暂停态跳过违规插入探针(提升后 R2 执行真实约束拒绝): 符合只读恢复语义
  - [PASS] 序列对象在恢复态完好可读: sequences found=2, last_value=33
  - [PASS] 只读暂停态跳过写入探针(提升后 R2 执行真实插入): 符合 recovery_target_action=pause 的只读语义
  - [PASS] 重放 LSN 不越过目标 restore point(容差一个 WAL 段): replay=0/3003600 target=0/3003600

### 恢复实例 `R2-late` — 已提升为新时间线
- 数据目录: `/workspace/pitrdrill-work/restores/R2-late/data`
- socket: `/tmp/pitrdrill-sockets/pitrdrill-work/restore/R2-late`  端口: 55500  PID: 6740
- 日志: `/workspace/pitrdrill-work/restores/R2-late/restore.log`
- 目标到达证据:
```json
{
  "controldata_state": "in archive recovery",
  "controldata_timeline": "1",
  "last_wal_replay_lsn": "0/6002540",
  "log_line": "recovery stopping at restore point \"rp_late\"",
  "log_path": "/workspace/pitrdrill-work/restores/R2-late/restore.log",
  "pg_is_in_recovery": "true",
  "promoted": {
    "current_wal": "000000020000000000000006",
    "history_files": [
      "00000002.history"
    ],
    "log": "04:46.954 GMT [6740] LOG:  listening on IPv4 address \"127.0.0.1\", port 55500\n2026-11-13 14:04:46.955 GMT [6740] LOG:  listening on Unix socket \"/tmp/pitrdrill-sockets/pitrdrill-work/restore/R2-late/.s.PGSQL.55500\"\n2026-11-13 14:04:46.959 GMT [6743] LOG:  database system was interrupted; last known up at 2026-11-13 14:04:29 GMT\n2026-11-13 14:04:47.227 GMT [6743] LOG:  starting point-in-time recovery to \"rp_late\"\n2026-11-13 14:04:47.227 GMT [6743] LOG:  starting backup recovery with redo LSN 0/5000028, checkpoint LSN 0/5000060, on timeline ID 1\n[pitrdrill-restore-fetch] request=000000010000000000000005 dest=pg_wal/RECOVERYXLOG\n[pitrdrill-restore-fetch] HIT 000000010000000000000005\n2026-11-13 14:04:47.236 GMT [6743] LOG:  restored log file \"000000010000000000000005\" from archive\n2026-11-13 14:04:47.241 GMT [6743] LOG:  redo starts at 0/5000028\n[pitrdrill-restore-fetch] request=000000010000000000000006 dest=pg_wal/RECOVERYXLOG\n[pitrdrill-restore-fetch] HIT 000000010000000000000006\n2026-11-13 14:04:47.250 GMT [6743] LOG:  restored log file \"000000010000000000000006\" from archive\n2026-11-13 14:04:47.251 GMT [6743] LOG:  completed backup recovery with redo LSN 0/5000028 and end LSN 0/5000100\n2026-11-13 14:04:47.251 GMT [6743] LOG:  consistent recovery state reached at 0/5000100\n2026-11-13 14:04:47.252 GMT [6740] LOG:  database system is ready to accept read-only connections\n[pitrdrill-restore-fetch] request=000000010000000000000007 dest=pg_wal/RECOVERYXLOG\n[pitrdrill-restore-fetch] HIT 000000010000000000000007\n2026-11-13 14:04:47.261 GMT [6743] LOG:  restored log file \"000000010000000000000007\" from archive\n2026-11-13 14:04:47.264 GMT [6743] LOG:  recovery stopping at restore point \"rp_late\", time 2026-11-13 14:04:31.298049+00\n2026-11-13 14:04:47.264 GMT [6743] LOG:  pausing at the end of recovery\n2026-11-13 14:04:47.264 GMT [6743] HINT:  Execute pg_wal_replay_resume() to promote.\n2026-11-13 14:04:48.268 GMT [6743] LOG:  received promote request\n2026-11-13 14:04:48.269 GMT [6743] LOG:  redo done at 0/60024D8 system usage: CPU: user: 0.00 s, system: 0.00 s, elapsed: 1.02 s\n2026-11-13 14:04:48.269 GMT [6743] LOG:  last completed transaction was at log time 2026-11-13 14:04:31.291149+00\n[pitrdrill-restore-fetch] request=000000010000000000000006 dest=pg_wal/RECOVERYXLOG\n[pitrdrill-restore-fetch] HIT 000000010000000000000006\n2026-11-13 14:04:48.284 GMT [6743] LOG:  restored log file \"000000010000000000000006\" from archive\n[pitrdrill-restore-fetch] request=00000002.history dest=pg_wal/RECOVERYHISTORY\n[pitrdrill-restore-fetch] MISS 00000002.history\n2026-11-13 14:04:48.291 GMT [6743] LOG:  selected new timeline ID: 2\n[pitrdrill-restore-fetch] request=00000001.history dest=pg_wal/RECOVERYHISTORY\n[pitrdrill-restore-fetch] MISS 00000001.history\n2026-11-13 14:04:48.423 GMT [6743] LOG:  archive recovery complete\n2026-11-13 14:04:48.425 GMT [6741] LOG:  checkpoint starting: force\n2026-11-13 14:04:48.427 GMT [6740] LOG:  database system is ready to accept connections\n",
    "log_selected_timeline": 2,
    "pg_is_in_recovery": "false",
    "timeline": 2
  },
  "recovery_pause": "confirmed-by-log"
}
```
- 预检通过: **True**
  - 缺口: 需 2 段, 备份自带覆盖 0 段, 缺失 []
- 独立 SQL 核验: 17/17 通过 (时间线 2)
  - [PASS] 实例可连接且身份为 pitrdrill 隔离实例: pg_is_in_recovery=false, timeline=2
  - [PASS] 已提升为独立主库(新时间线): pg_is_in_recovery=false, timeline=2, 提升记录时间线=2
  - [PASS] 提升产生可区分的新时间线(>源时间线 1)且生成 history 文件: new timeline=2, history=['00000002.history']
  - [PASS] 目标标记存在: MARKER-M1: MARKER-M1|0/30013B0|1
  - [PASS] 目标标记存在: MARKER-M2: MARKER-M2|0/60021A8|3
  - [PASS] 更晚标记不存在(目标后事务已排除): MARKER-M3: count=0
  - [PASS] 误删除未发生: parent 3 仍存在: 3|parent-3
  - [PASS] 误删除未发生: parent 7 仍存在: 7|parent-7
  - [PASS] 误更新未发生: parent 5 未被 MISHAP 污染: payload=tick-24
  - [PASS] 目标后正常新增已排除: parent 9001 不存在: count=0
  - [PASS] 误操作事件日志不存在: mishap count=0
  - [PASS] 父子外键约束定义存在: pitrdrill_child_parent_id_fkey|pitrdrill_parent
  - [PASS] 不存在悬空子记录: orphan count=0
  - [PASS] 只读暂停态跳过违规插入探针(提升后 R2 执行真实约束拒绝): 符合只读恢复语义
  - [PASS] 序列对象在恢复态完好可读: sequences found=2, last_value=34
  - [PASS] 只读暂停态跳过写入探针(提升后 R2 执行真实插入): 符合 recovery_target_action=pause 的只读语义
  - [PASS] 重放 LSN 不越过目标 restore point(容差一个 WAL 段): replay=0/6002540 target=0/6002540

## 7. 故障注入演练
- `b-foreign-identity` (wrong_identity): {"backup": "b-foreign-identity", "demo": "wrong_identity", "foreign_system_identifier": "7707656955484969715", "path": "/workspace/pitrdrill-work/backups/b-foreign-identity"}
- `set-gap` (gap): {"demo": "gap", "forced_start_result": {"log_tail": ["2026-11-13 14:04:55.403 GMT [6826] LOG:  redo done at 0/2000100 system usage: CPU: user: 0.00 s, system: 0.00 s, elapsed: 0.00 s", "2026-11-13 14:04:55.403 GMT [6826] FATAL:  recovery ended before configured recovery target was reached", "2026-11
- `set-tli2` (wrong_timeline): {"demo": "wrong_timeline", "renamed": [["000000010000000000000007", "000000020000000000000007"], ["000000010000000000000008", "000000020000000000000008"], ["000000010000000000000001", "000000020000000000000001"], ["000000010000000000000006", "000000020000000000000006"]], "set": "set-tli2"}
- `set-trunc` (truncated): {"demo": "truncated", "segment": "000000010000000000000003", "set": "set-trunc", "size_after": 16769024, "size_before": 16777216, "source_archive_untouched": true}
- `set-waiting` (gated_archive_wait): {"demo": "gated_archive_wait", "gated_segment": "000000010000000000000003", "released_result": "TARGET_REACHED", "timeout_budget_seconds": 30, "timeout_reasons": ["\u5f52\u6863\u5c1a\u672a\u5230\u4f4d, \u6062\u590d\u5728\u7b49\u5f85 WAL: 000000010000000000000003(\u95e8\u63a7/\u5f52\u6863\u8fdf\u5230

## 8. 磁盘占用与清理边界
| 对象 | 路径 | 占用 |
| --- | --- | --- |
| 源实例 | /workspace/pitrdrill-work/source | 158.4MiB |
| WAL 归档 | /workspace/pitrdrill-work/archive | 176.0MiB |
| 基础备份 | /workspace/pitrdrill-work/backups | 137.9MiB |
| 归档集合 | /workspace/pitrdrill-work/archive-sets | 160.0MiB |
| 恢复实例 | /workspace/pitrdrill-work/restores | 468.7MiB |

清理命令(仅清理本演练, 不触碰其他目录): `python3 pitrdrill.py --root /workspace/pitrdrill-work cleanup --yes`
