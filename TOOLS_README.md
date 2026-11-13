# pgview_export：受限的视图依赖结构迁移工具

从 PostgreSQL **真实 `pg_catalog` / `pg_depend`** 读取指定视图的必要依赖闭包，
生成把源 schema 映射到目标 schema 的**可执行 DDL**。只做这一小类结构迁移，
不是通用备份平台。

## 支持范围（固定）

普通表、列、内置类型、主键、**单向**外键、列默认值、序列及其 `OWNED BY`、普通视图。

显式**不支持**并会在遇到时明确列出后停止（不静默丢弃）：函数/过程、CHECK/UNIQUE/
EXCLUDE 等约束、循环外键、分区表、物化视图、外部表、自定义类型、扩展、触发器、
角色/授权、跨 schema 依赖，以及默认值中除 `nextval` 外的函数调用。

## 设计要点

- **依赖闭包来自系统目录，不解析视图文本猜表名**，也不导出整个源 schema。
  视图语义依赖挂在 `_RETURN` 规则上、约束/默认值反向挂在列上，工具分别沿
  `pg_depend` 的正向规则边与反向载体边求闭包；`i` 类内部对象（TOAST、主键索引、
  复合类型）自动跳过。无关表（如 `unrelated`）不在闭包内，不会被导出。
- **schema 映射在 SQL AST 上完成（pglast / libpg_query），不做全局字符串替换**：
  - 视图中的 schema 限定名（`RangeVar`）改写为目标 schema；
  - `nextval('...'::regclass)` 的 regclass 字符串常量按关系名语法解析后重建；
  - 普通字符串常量里出现的同名文字（如 `'legacy sales.pending'`、
    `'legacy sales.Order Header'`）不是对象引用，**原样保留**。
- **标识符正确引用**：含空格、大写、嵌入双引号的 schema/对象/列名按
  `quote_ident` 规则加引号，避免 PG 折叠成小写而改名。
- **不复制业务行，不复制序列当前进度**：新序列只按声明的 `START WITH / INCREMENT /
  MINVALUE / MAXVALUE / CACHE / CYCLE` 初始化。
- **目标 schema 已存在即拒绝**；导出连接为只读会话，不修改源对象。
- **DDL 在单事务内应用，任一失败整体回滚**，并报告失败对象与数据库原始原因，
  不会留下半套结构却报成功。

## 环境准备

```sh
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 初始化本地实例并载入 fixtures/seed.sql（数据/日志都在 .runtime，已忽略）
bash scripts/setup_db.sh
```

## 使用

连接串默认 `host=/tmp/pg_task_sock dbname=taskdb user=node`，可用 `--dsn` 或
libpq 环境变量覆盖。

```sh
# 1) 导出：生成 out/export_manifest.json（清单+依赖边+DDL）与 out/schema_export.sql
.venv/bin/python -m pgview_export export --request fixtures/request.json --out-dir out

# 2) 应用：单事务执行；目标 schema 已存在会拒绝；失败整体回滚
.venv/bin/python -m pgview_export apply --manifest out/export_manifest.json

# 3) 验证：结构对比 + 旧 schema 引用检查 + 取号/视图/业务字符串冒烟（自动回滚）
.venv/bin/python -m pgview_export verify --manifest out/export_manifest.json
```

一键端到端（导出→应用→验证→复位到干净状态）：

```sh
bash scripts/run_demo.sh
```

## 验证内容

`verify` 在同一测试库的新 schema 中比对：

- 列名/类型/可空、主键与外键（列、引用表、动作、可延迟性）、列默认值语义等价；
- 序列全部声明属性与 `OWNED BY` 归属，且首个 `nextval` 即声明起点（无业务进度）；
- 视图定义与 `pg_depend` 依赖**只指向目标 schema**，无任何指向源 schema 的对象引用；
- 无关表与原始业务行未带入；
- 插入少量新行验证默认取号与视图 JOIN 可用，业务字符串保持不变；冒烟数据随事务回滚。

## 退出码

| 码 | 含义 |
|----|------|
| 0  | 成功 |
| 2  | DDL 应用失败（已回滚），stderr 给出失败对象与原因 |
| 3  | 验证失败 |
| 4  | 导出中止（含范围外依赖清单、目标 schema 已存在等） |
