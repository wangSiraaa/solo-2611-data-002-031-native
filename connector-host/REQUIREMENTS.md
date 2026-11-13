# 需求可追溯说明

本表把题面每条要求映射到实现位置与演示/验证场景，便于评审核对。

| # | 题面要求 | 实现位置 | 验证 |
|---|---|---|---|
| 1 | stdin/stdout 上 JSON-RPC 2.0，stderr 仅诊断 | `connectors/common.py`、`host/supervisor.py` | 全部演示；`logs/*.stderr.log` |
| 2 | ASCII Content-Length 头 + 空行 + UTF-8 JSON，长度按字节 | `host/framing.py`、`connectors/common.py` | `scripts/test_framing.py`、framing-fuzz |
| 3 | 一次读到多消息/半头/半个中文字符 | `FrameAssembler.feed` 增量缓冲 | fuzz 用 1/2/3 字节块 |
| 4 | 连接器先响应 initialize，声明 source/参数/是否可取消 | `common.py:capability/run` | list-sources 输出 |
| 5 | 宿主 list-sources/query/cancel/status 入口 | `host/app.py`、`host/__main__.py` | 命令样例、run-script |
| 6 | query 带来源、字段列表、受控过滤 | `common.py:apply_filters/project` | 受控拒绝边界测试 |
| 7 | 三个连接器：快速/乱序/进度 | `fast/reorder/progress_connector.py` | 场景 01/02/03 |
| 8 | 同进程多在途请求，按 id 关联不按顺序 | `supervisor._on_response` 按 rpc_id 弹表 | 03 反序完成（inflight 峰值 6） |
| 9 | 通知无 id，不能当查询结果 | `_dispatch` 区分 id 有无；progress 仅进度 | 02 交错批 + progress 通知 |
| 10 | dataBatch 带 query_id + 递增 batch_no，终态声明总批数/总记录数 | `_on_data_batch`、`_on_response` | 全部成功查询真值比对 |
| 11 | 宿主核对批序与归属才标记完整 | 批序/归属/计数三重校验 | 06/07/08 及 mismatch 分支 |
| 12 | 不同查询记录不串 | 按 query_id 分文件、归属校验 | 02 三查询交错无串批 |
| 13 | 未知 id/重复终态/超长等错误定位连接器+偏移并终止 | `ProtocolError(connector,offset)`、`_protocol_fail` | 06(offset=562)/07(offset=485) |
| 14 | 只终止受影响连接器，其他继续 | `terminate` 仅作用本 supervisor | 每个故障后 isolation-* 探针 |
| 15 | 每连接器在途/消息大小/等待时间/数据上限 | `config/host.json`、supervisor 限额 | inflight 峰值=上限、data cap 测试 |
| 16 | 写管道与读 stdout 同时进行，防死锁 | writer/stdout/stderr 三独立任务 | 04 stderr 洪水压力测试 |
| 17 | stderr 持续排空且有界保留 | `_stderr_loop` + deque + 日志上限 | 04b、洪水压测（131072 字节封顶） |
| 18 | 异常退出时在途请求全部明确失败，已完成结果保留 | `_stdout_loop` EOF -> terminate -> fail 作业 | 08 half-exit |
| 19 | 可重启，但不悄悄重发/拼接部分结果 | `restart` 重建空表，无重发逻辑 | 11/11b（查询数仅 +1） |
| 20 | 取消为独立通知，处理取消与终态竞争并展示终态 | `cancel_query`、`_on_response` 竞争分支 | 09a/09c/09b 三种终态 |
| 21 | 不支持/不响应取消则超时结束进程再报告，不连累他人 | `_on_timeout/_on_cancel_grace` | 10 hang（其他连接器 alive） |
| 22 | 退出关管道并回收子进程 | `HostApp.shutdown` + `proc.wait` | 每次运行后 leftover=0 |
| 23 | status 不把已启动进程误当查询完成 | 查询仅在核对后写 succeeded | status 空查询数为 0 |
| 24 | 结果保存能力版本/参数/批数/记录数/失败原因 | `storage.queries`、export manifest | manifest 字段检查 |
| 25 | 不保存未完成查询为成功数据 | 失败只存元数据，result_path 为空 | manifest 失败项 records 为空 |
| 26 | 来源目录与结果导出 | `list_sources`、`Storage.export_manifest` | export 命令 |
| 27 | 演示覆盖 8 类指定场景 | `scripts/run_demo.py` | 29 项检查全 PASS |
| 28 | 独立真值逐查询比对 | `scripts/ground_truth.py`（不导入连接器） | 27 条成功查询全部 match |
| 29 | 记录在途峰值与 RSS | `supervisor.Metrics` + `/proc/*/statm` | demo-report 指标区 |
| 30 | 证明故障源不阻止其他源 | 每故障后 fast+progress 探针 | 全部 isolation-* PASS |

## 终态词汇表（status / manifest 的 terminal_state）

- `completed`：正常核对完成，无取消竞争。
- `completed (cancel request lost the race)`：cancel 已登记，但成功终态先被核对。
- `cancelled (cancel won the race)`：连接器在终态前确认取消。
- `rpc-error` / `protocol-mismatch` / `data-cap-exceeded` / `failed`：各类失败，均带 fail_reason。
