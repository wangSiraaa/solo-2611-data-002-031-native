# 数据工程独立任务初始工作区

本仓库用于独立的数据工程任务。本次需求以当前会话提供的完整题面为准；初始快照没有业务实现、测试答案或业务数据。

## 本次交付：旧系统 PTY 录入助手

- 实现与运行说明：[docs/OPERATIONS.md](docs/OPERATIONS.md)
- 真实 PTY 演示记录：[docs/DEMO.md](docs/DEMO.md)（原始记录在 `demo/records/`）
- 助手代码：`terminal_assistant/`（只经 PTY 观察屏幕、发送按键，不读数据库）
- 独立校验器：`tools/verify_db.py`（助手从不调用）
- 测试：`tests/`（含真实 PTY 端到端与守护测试）

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest tests/ -q
.venv/bin/python demo/run_demo.py
```

## 环境

- Linux、Python 3.11，提供 pip 和 venv。
- PostgreSQL 16 的 `initdb`、`pg_ctl`、`psql`、`createdb` 已加入 PATH。
- 当前为普通用户，可在自己拥有的目录初始化独立数据库。
- Python 标准库包含 SQLite；根据题目选择实际需要的技术。

原生环境初始化示例见 [environment/README.md](environment/README.md)。请将依赖、数据库、运行日志及临时验证数据放在忽略目录中，并为最终实现提供清楚的运行说明。
