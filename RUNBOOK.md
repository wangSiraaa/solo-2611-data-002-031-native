# 单路 SSE 可靠采集客户端

把设备 SSE 读数流可靠落盘到本地 SQLite：字节级 SSE 解析、业务记录与可信断点
同事务提交、重启后用已提交的 `Last-Event-ID` 续接、重发去重、冲突/缺口/换流
显式停止并提供人工恢复入口。仅使用 Python 3.11 标准库，无第三方依赖。

## 目录结构

```
collector/
  sse.py       字节级 SSE 解析器（增量 UTF-8、CR/LF/CRLF、分块边界）
  storage.py   SQLite 持久化（记录+断点同事务、incidents、请求审计、人工恢复）
  client.py    HTTP 连接、Last-Event-ID 续接、退避重连、停止条件、崩溃测试钩子
  cli.py       命令行入口（start/stop/status/readings/requests/incidents/recover）
tests/         41 个单元/端到端测试（端到端直接拉起 fixtures/sse_upstream.py）
scripts/demo.py  六个场景的可复现演练，产物在 .runtime/demo/
```

## 快速开始

```sh
# 1. 启动模拟上游（另一个终端）
python3 fixtures/sse_upstream.py --port 8106

# 2. 前台运行采集（Ctrl-C 优雅停止）
python3 -m collector.cli run-foreground \
  --url 'http://127.0.0.1:8106/events?case=partial&client=run-1' \
  --db .runtime/collector.db --read-size 3

# 3. 另开终端查询
python3 -m collector.cli status    --db .runtime/collector.db
python3 -m collector.cli readings  --db .runtime/collector.db
python3 -m collector.cli requests  --db .runtime/collector.db   # 含每次请求头
python3 -m collector.cli incidents --db .runtime/collector.db
```

`--read-size 3` 把每次 socket 读取压到 3 字节，强制覆盖 UTF-8 多字节字符与
CRLF 被拆到不同分块的路径；生产使用默认 4096 即可。

## 守护进程入口

```sh
python3 -m collector.cli start --url '<URL>' --db .runtime/c.db \
  --pidfile .runtime/c.pid --log .runtime/c.log \
  --max-attempts 100 --max-run-seconds 3600
python3 -m collector.cli status --db .runtime/c.db
python3 -m collector.cli stop   --pidfile .runtime/c.pid
```

`start` 以后台进程方式拉起 `run-foreground`（新会话、日志重定向），pid 写入
pidfile；`stop` 发 SIGTERM 并等待优雅退出，超时再 SIGKILL。也可以用
`--stop-file path`：该文件出现即在下一次重连间隙优雅停止。

### 退避与停止条件（避免忙循环）

- 每次连接失败/正常结束后按 `initial_backoff * factor^n` 指数退避，上限
  `max_backoff`，并叠加 0.5–1.0 倍随机抖动；一旦某次连接真正读到数据，失败
  计数回落到初始退避。
- 三重停止条件：`--max-attempts N`（尝试次数）、`--max-run-seconds T`
  （总时长）、SIGTERM/SIGINT/停止文件。阻塞态（冲突/缺口/换流）立即退出
  重连循环，不做任何自动重试。

## 可靠断点与两个崩溃时点的恢复语义

**可信断点只来自数据库已提交事务。** 解析器看到 `id:` 行、收到半个 JSON 都
不写库；只有空行结束的完整事件，才在**同一个事务**里插入 `readings` 业务行
并更新 `streams.last_event_id`（`PRAGMA synchronous=FULL`，提交返回即落盘）。

| 终止时点 | 数据库状态 | 重启后行为 |
|---|---|---|
| 完整事件已接收、**提交前**被杀 | 事务回滚：无业务行、断点不前进 | 断点仍指向上一条；上游重放该事件，正常入账，不缺不重 |
| 事务**提交成功后**被杀 | 业务行与新断点一致落盘 | 带新 `Last-Event-ID` 重连；上游若重放最后事件，内容一致→去重，不重复入账 |
| 断在半个码位/半个 JSON（无空行） | 解析器不派发事件，库内无任何痕迹 | 同上：断点停在最后完整事件，重放后补齐 |

用真实中断验证（`os._exit(137)` 模拟 SIGKILL，不执行任何 Python 清理）：

```sh
COLLECTOR_CRASH_AFTER_EVENT=3  python -m collector.cli run-foreground ...  # 提交前
COLLECTOR_CRASH_AFTER_COMMIT=3 python -m collector.cli run-foreground ...  # 提交后
```

或直接运行 `python3 scripts/demo.py`，并查看
`.runtime/demo/04_*`、`.runtime/demo/05_*`、`.runtime/demo/06_*`、`.runtime/demo/07_*`
中的 status/requests/readings 快照。`tests/test_e2e.py` 的
`CrashRecoveryTests` 用真实子进程断言退出码 137 与重启后的序号连续性。

## 去重、冲突与人工恢复

- **重发去重**：`(stream_id, sequence)` 为主键。重发事件先按排序键重新
  规范化 JSON 再比对 sha256（缩进/空白差异不影响判定）；内容一致只增加
  `duplicate_count`，不插第二行、不推进断点。
- **冲突**：同流同序号但内容不同 → 原始 `readings` 行绝不覆盖，新内容写入
  `incidents(kind='conflict')`，流置 `stopped_conflict` 并停止重连。
  ```sh
  python3 -m collector.cli incidents --db <db>
  python3 -m collector.cli recover conflict --db <db> --incident <ID> --note '保留原值'
  ```
  恢复策略固定为保留原始记录；确认后该序号的后续重发按重复计数，新序号继续入账。
- **410 保留窗口缺口**：收到 410 不允许"从当前数据继续还宣称连续"，写
  `incidents(kind='retention_gap')` 并置 `stopped_gap`。两种人工选择：
  ```sh
  # 接受缺口：清空本流可信断点，重连不带 Last-Event-ID，从上游当前可用位置
  # 重拉（重拉到的旧序号由去重拦截）；连续性缺口永久保留在 incidents 中
  python3 -m collector.cli recover gap --db <db> --incident <ID> --accept-restart
  # 或保持停止、记录放弃裁决
  python3 -m collector.cli recover gap --db <db> --incident <ID> --abandon
  ```
- **流标识改变**：新流事件到达时不沿用旧流序号空间，写
  `incidents(kind='rotation')`，旧流置 `stopped_rotation`：
  ```sh
  python3 -m collector.cli recover rotate --db <db> --incident <ID> --note '确认换流'
  ```
  确认后新流以独立序号空间从 1 开始；旧流作为历史流永久保留停止态。
- 防御性：收到跳号（缺序号）置 `stopped_gap`；JSON 损坏或 SSE id 与载荷
  `stream_id/sequence` 不自洽时记 `malformed`，均不产生业务记录。

## 状态输出内容

`status` 至少包含：流标识、状态（running/stopped_conflict/stopped_rotation/
stopped_gap/stopped_error）、可信断点 `last_event_id`、已提交最大序号、
重复数、阻塞原因，以及全部未处置 incidents（缺口/冲突/换流详情）。
`--json` 输出机器可读格式；存在未处置 incident 时退出码为 2。

## 数据库表

| 表 | 用途 |
|---|---|
| `streams` | 每流一行：状态、可信断点、最大序号、重复数、阻塞原因 |
| `readings` | 业务记录，主键 `(stream_id, sequence)`，含原始 data、规范化 JSON、内容哈希 |
| `incidents` | conflict / rotation / retention_gap / gap / malformed 及处置记录 |
| `request_log` | 每次连接的 URL、**完整请求头（含 Last-Event-ID）**、响应状态与保存计数 |
| `recovery_log` | 人工恢复动作审计 |

WAL 模式 + `synchronous=FULL`：采集进程写的同时，可用 `status`/`sqlite3`
只读查询；所有运行产物（db、日志、演示快照）都在 `.runtime/`（已在
`.gitignore` 忽略）。

## 测试与演练

```sh
python3 -m unittest discover -s tests -v   # 41 个测试，含真实 fixture 端到端
python3 scripts/demo.py                    # 六场景演练 + 守护进程，产物 .runtime/demo/
```

演练覆盖：半条消息断开恢复、完整消息重发去重、提交前/提交后进程被杀与重启、
冲突停止与人工确认、410 缺口与两种恢复选择、换流确认。每个阶段保存
`status.json / requests.json / readings.json / incidents.json` 四类核对材料，
`requests.json` 中可逐请求核对 `Last-Event-ID` 请求头与落库断点的一致性。

## SSE 解析约定

- 增量 UTF-8 解码：多字节字符跨分块不产生替换字符；非法起始字节立即报错，
  流结束时残留的不完整字节随半条消息静默丢弃（不派发、不推进断点）。
- 行结束支持 LF/CR/CRLF，CRLF 被拆到两个分块不会产生多余空行。
- `:` 开头为注释；空行才结束事件块；仅注释/仅 `retry` 的块不产生事件。
- 多个 `data:` 行以 LF 拼接；支持 `event`/`id`/`retry` 字段，`id` 含 NUL、
  `retry` 非整数均按规范忽略；未知字段忽略。
