# 数据工程独立任务初始工作区

本仓库用于独立的数据工程任务。本次需求以当前会话提供的完整题面为准；初始快照没有业务实现、测试答案或业务数据。

## 本次实现：单路 SSE 可靠采集客户端

- 代码：`collector/`（仅 Python 3.11 标准库），测试：`tests/`（41 个，含真实 fixture 端到端）
- 运行与恢复说明：[RUNBOOK.md](RUNBOOK.md)
- 一键演练：`python3 scripts/demo.py`（产物在 `.runtime/demo/`，含请求头与落库核对材料）
- 模拟端：`python3 fixtures/sse_upstream.py --port 8106`（见 [fixtures/README.md](fixtures/README.md)）

## 环境

- Linux、Python 3.11，提供 pip 和 venv。
- PostgreSQL 16 的 `initdb`、`pg_ctl`、`psql`、`createdb` 已加入 PATH。
- 当前为普通用户，可在自己拥有的目录初始化独立数据库。
- Python 标准库包含 SQLite；根据题目选择实际需要的技术。

原生环境初始化示例见 [environment/README.md](environment/README.md)。请将依赖、数据库、运行日志及临时验证数据放在忽略目录中，并为最终实现提供清楚的运行说明。
