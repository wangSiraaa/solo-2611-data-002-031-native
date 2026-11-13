# 数据工程独立任务初始工作区

本仓库用于独立的数据工程任务。本次需求以当前会话提供的完整题面为准；初始快照没有业务实现、测试答案或业务数据。

## 环境

- Linux、Python 3.11，提供 pip 和 venv。
- PostgreSQL 16 的 `initdb`、`pg_ctl`、`psql`、`createdb` 已加入 PATH。
- 当前为普通用户，可在自己拥有的目录初始化独立数据库。
- Python 标准库包含 SQLite；根据题目选择实际需要的技术。

原生环境初始化示例见 [environment/README.md](environment/README.md)。请将依赖、数据库、运行日志及临时验证数据放在忽略目录中，并为最终实现提供清楚的运行说明。

## 删除/保留执行工具（本次交付）

仅需 Python 3.11 标准库。源码：`erasure_tool.py`（工具）、`demo.py`（四场景演示）。

```sh
# 1. 生成独立输入（业务库 business.sqlite + 登记导出 export.jsonl 等）
python3 fixtures/prepare.py .runtime/work-input

# 2. 生成可复查计划（只读，不改任何源）
python3 erasure_tool.py plan   --workdir .runtime/work-input

# 3. 复核依据并分步执行（数据库事务 -> 导出原子替换，可断点续办）
python3 erasure_tool.py apply  --workdir .runtime/work-input

# 4. 普通业务读取入口 / 真实状态 / 终态核对
python3 erasure_tool.py read   --workdir .runtime/work-input [--subject U03] [--export]
python3 erasure_tool.py status --workdir .runtime/work-input
python3 erasure_tool.py verify --workdir .runtime/work-input

# 一键演示：无保留删除、父键骨架、计划后新增保留令、文件替换中断续办
python3 demo.py
```

裁决与执行要点：

- **保留范围**：`holds.json` 的有效保留令只覆盖指定订单——该订单 `restricted=1` 原文受限保留（内容哈希入计划，verify 核对未变）；客户仅留 `subject_id` 骨架（姓名/邮箱/电话置空、受限），其余未保留订单照删；保留令不扩张到客户全部资料，也不授权订单原文留在公共导出。
- **计划可复查**：`plan.json` 记录每个对象的删除/最小保留/副本清理动作、行 `revision`、保留声明、导出文件当前与清理后 sha256、未请求对象指纹；计划本身不复制客户联系方式。
- **过期拒绝**：`apply` 先复核 request/holds 语义哈希与全部行版本，任一变化即拒绝（退出码 2）；行版本同时作为事务内乐观锁。退出码：0 完成 / 2 依据过期 / 3 执行记录或文件漂移。
- **可恢复**：`execution.json` 逐步落盘；数据库步骤在库内 `erasure_audit` 留同事务标记，覆盖「已提交但记录未写」的崩溃；导出用临时文件 + `os.replace` 原子替换，替换前后崩溃均可按校验和识别续办；部分完成只报 `in_progress`。
- **读取隔离**：`read` 只返回 `restricted=0` 对象；审计只记录保留令理由（如 `case-demo`），不复制客户联系方式。
