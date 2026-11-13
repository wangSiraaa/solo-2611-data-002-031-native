# 数据连接器宿主（Connector Host）

一个在 Linux / Python 3.11 上运行的数据连接器宿主：通过子进程 stdin/stdout 上的
**Content-Length 分帧 JSON-RPC 2.0** 调度多个独立团队提供的本地采集程序。
单个连接器卡死、stdout 写乱内容或进程崩溃，只会隔离该连接器，其他连接器继续服务。

仅依赖 Python 标准库（含 SQLite），不访问任何外部系统。

## 目录结构

```
connector-host/
├── PROTOCOL.md                  # 线路协议完整说明（分帧、JSON-RPC、分批、取消、错误处置）
├── config/host.json             # 每连接器独立的在途/消息大小/超时/数据上限配置
├── host/                        # 宿主
│   ├── framing.py               # Content-Length 增量分帧（粘连/半头/半个中文字符）
│   ├── storage.py               # SQLite 持久层 + JSONL 结果落盘 + 清单导出
│   ├── supervisor.py            # 单连接器监管器（管道任务、在途表、超时/取消/隔离）
│   ├── app.py                   # 宿主应用与命令编排
│   └── __main__.py              # CLI 入口（list-sources/query/cancel/status/restart/export/shell）
├── connectors/                  # 独立可执行的合成连接器（可单独拷贝给其他团队）
│   ├── common.py                # 连接器公共运行时（独立分帧实现 + 过滤引擎 + 工作线程）
│   ├── fast_connector.py        # A 快速连接器（支持取消）
│   ├── reorder_connector.py     # B 乱序连接器（请求反序完成，不支持取消）
│   ├── progress_connector.py    # C 进度连接器（批间 progress 通知，支持取消）
│   └── chaos_connector.py       # D 混沌连接器（8 类故障注入）
├── scripts/
│   ├── generate_data.py         # 生成确定性本地数据（含中文，作为真值来源）
│   ├── ground_truth.py          # 独立真值校验器（不导入连接器代码）
│   └── run_demo.py              # 端到端合成演示 + 指标采集
├── data/                        # 生成的本地 JSONL 数据与来源目录
├── logs/                        # 各连接器有界 stderr 诊断日志
└── runs/                        # SQLite、逐查询结果文件、演示报告与导出清单
```

## 一、本机运行步骤

```bash
cd connector-host

# 1. 语法检查（无需任何第三方依赖）
python3 -m py_compile host/*.py connectors/*.py scripts/*.py

# 2. 生成本地合成数据（确定性，可复现）
python3 scripts/generate_data.py data

# 3. 运行完整端到端演示（约 30~40 秒）
python3 scripts/run_demo.py
```

演示结束会打印每个场景的 PASS/FAIL、真值比对结果、在途请求峰值与 RSS，
并在 `runs/demo-<时间戳>/` 下写出：

- `manifest.json`：全部查询的导出清单（成功结果 + 失败原因 + 能力版本 + 批数/记录数）；
- `demo-report.json`：场景结果、真值比对、在途峰值、各连接器与宿主 RSS 峰值；
- `host.db`：SQLite 状态库；`results/<连接器>/*.jsonl`：逐查询结果；
- `logs/*.stderr.log`：有界 stderr 诊断。

## 二、宿主命令样例

```bash
# 来源目录（stdout 只输出 JSON，诊断日志走 stderr）
python3 -m host --config config/host.json list-sources
python3 -m host --config config/host.json list-sources --connector fast

# 查询：投影 + 受控过滤（中文条件用 JSON 字符串）
python3 -m host --config config/host.json query \
  --connector fast --source fast_orders \
  --fields id,name,region,amount \
  --filter 'region:eq="华北"' --filter 'amount:gt=100'

# 异步提交（立即返回 query_id），随后取消
python3 -m host --config config/host.json query \
  --connector progress --source progress_inventory --fields id,name --no-wait
python3 -m host --config config/host.json cancel --query-id q-xxxxxxxx

# 运行态（连接器状态、在途、stderr 尾部、指标、全部查询）
python3 -m host --config config/host.json status

# 重启故障连接器（只建新进程，不自动重发任何旧请求）
python3 -m host --config config/host.json restart --connector chaos

# 导出清单（含能力版本、查询参数、批数、记录数、失败原因）
python3 -m host --config config/host.json export --out runs/manifest.json

# 交互式 REPL（输入 help；exit 时关闭管道并回收全部子进程）
python3 -m host --config config/host.json shell
```

## 三、三个（+1）连接器的行为

| 连接器 | 能力版本 | 取消 | 行为 |
|---|---|---|---|
| fast | 1.0.0 | 支持 | 请求后立即小批量快速返回 |
| reorder | 2.3.1 | **不支持** | 多请求按到达顺序反转延迟，后发先至，验证按 id 关联而非顺序配对 |
| progress | 3.1.0 | 支持 | 每批之间发纯进度 `progress` 通知，数据仍走带 queryId/batchNo 的 dataBatch |
| chaos | 4.0.0-chaos | 可配置 | 由 `CHAOS_MODE` 注入故障，供演示与验收 |

## 四、演示覆盖的场景

`run_demo.py` 确定性覆盖：分帧字节级模糊（1 字节块/半头/半个中文字符/粘连多帧）、
正常过滤投影、交错数据批、相反完成顺序、stderr 洪水、极小字节块、未知响应 id、
声明长度过大、子进程半途退出、取消竞争的两种终态、挂起超时杀进程、重启不重发。
**每个故障场景之后都对健康连接器再发查询，证明故障源被隔离。**

## 五、关键设计与不变量

- **三条管道独立任务**：stdout 读、stdin 写、stderr 排空各自一个 asyncio 任务，
  stderr 洪水或超大响应都不会卡住写入，从根本上避免双向管道死锁。
- **单一终态 future**：每个查询只有一个 future 作为终态同步点，请求 id 与 queryId 双层关联，
  通知无 id 永不与结果配对。
- **批序与归属强校验**：只有 1..N 连续到齐且记录数与终态声明一致，才写 `succeeded`。
- **未完成不冒充成功**：失败/取消查询只存元数据与失败原因，结果路径为空。
- **在途/消息/超时/数据上限按连接器独立配置**，超限分别排队、拒帧、结束进程、失败终止。
- **异常退出明确失败**：连接器进程消失时其全部在途请求收到明确失败，已完成结果保留。
- **优雅关闭**：宿主退出关闭所有管道并 `wait()` 回收自己创建的子进程，无孤儿进程。
- **指标**：每 0.1s 从 `/proc/<pid>/statm` 采样连接器 RSS，并记录宿主自身 RSS 与在途峰值。
