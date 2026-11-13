# 有限 GraphQL 服务：客户 + 最近订单（批量加载，每父对象独立 LIMIT）

固定模型见 `fixtures/schema.graphql`（两层：Customer → Order，固定字段与别名）。
实现：`app/`（graphql-core + psycopg3 异步），HTTP 入口 + 每请求 SQL 计数，
`demo.py` 一键复现题目要求的完整流程（21 项断言）。

## 运行

```sh
# 1. 依赖（Python 3.11）
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# 2. 数据库（PostgreSQL 16；/workspace 是 virtiofs 不支持 Unix socket，
#    故 socket 放在 /tmp/pgsock，数据目录仍在 .runtime/）
mkdir -p .runtime /tmp/pgsock
initdb -D "$PWD/.runtime/pgdata" --auth-local=trust --auth-host=scram-sha-256
pg_ctl -D "$PWD/.runtime/pgdata" -l "$PWD/.runtime/postgres.log" \
  -o "-k /tmp/pgsock -c listen_addresses=''" start
createdb -h /tmp/pgsock taskdb
export PG_DSN="host=/tmp/pgsock dbname=taskdb"
psql "$PG_DSN" -v ON_ERROR_STOP=1 -f fixtures/seed.sql

# 3. 服务（默认 127.0.0.1:8000，PORT 可改）
.venv/bin/python -m app.server

# 4. 端到端演示：重置数据 → 别名查询 → between_requests.sql → 同一查询 →
#    非法参数 / 无订单客户 / 故障 / 扩缩检查
.venv/bin/python demo.py
```

停止数据库：`pg_ctl -D "$PWD/.runtime/pgdata" stop`。

## 接口

`POST /graphql`，JSON 体 `{"query": "...", "variables": {...}, "operationName": "..."}`。
响应在 `data`/`errors` 之外带 `extensions.sqlCount` 与 `extensions.sql`（每条 SQL 及参数），
即每请求的查询记录。

## 设计：批量加载如何保持每父对象独立限制

- **加载键**：`(customer_id, first)`。别名不参与键——`recent`/`repeated` 同为
  `first:1` 时是同一次加载（请求内缓存直接复用），`history` 的 `first:2` 是另一次加载；
  所选子字段也不影响键（总是取整行，字段裁剪交给 GraphQL 层）。
- **批次**：一个执行节拍内收集的键按 `first` 分组，每个不同的 `first` 值恰好一条 SQL，
  与客户数无关（`WHERE customer_id = ANY(数组)`）。给定别名查询固定 3 条 SQL：
  1 条客户 + 2 条订单（first∈{1,2}）。
- **每父对象 LIMIT**：用窗口函数而不是全局 LIMIT——

  ```sql
  row_number() OVER (PARTITION BY customer_id
                     ORDER BY created_at DESC, id DESC) AS rn
  ... WHERE rn <= $first
  ```

  `created_at` 相同（种子中订单 11/12 同为 `2026-11-01T09:00:00Z`）由 `id DESC` 稳定决胜。
- **回填**：批量函数按输入键的顺序返回等长结果，每个等待中的 Future 只拿到自己客户的行；
  无订单客户得到真正的空数组，与"父对象不存在""查询失败"严格区分（后两者分别是
  不返回该客户 / 抛出 GraphQL 错误）。
- **缓存边界**：`RequestContext`（连接、SQL 日志、BatchLoader）随请求创建、随请求销毁。
  请求内等价加载复用同一 Future；下一请求从空缓存开始，因此能看到
  `between_requests.sql` 提交的新订单 14 和新名字「甲-更新」。

## 为什么全局 LIMIT 和共享缓存是错的

- **全局 LIMIT**：`... WHERE customer_id = ANY(...) ORDER BY created_at DESC, id DESC LIMIT N`
  的 N 作用于整个结果集。订单多的客户会占满名额，其余客户一行都拿不到——
  限制必须按父对象分区（`PARTITION BY` + `rn <= N`），而不是对合集截断。
- **共享缓存**：把加载结果放在模块级字典或跨请求的 DataLoader 里，命中的是旧快照，
  已提交的 INSERT/UPDATE 不可见（演示中请求 2 必须出现订单 14 与「甲-更新」）。
  同理，若键里漏掉 `first` 或错用别名当键，`first:1` 与 `first:2` 会互相污染，
  或不同查询参数被错误合并——所以键只能是 `(customer_id, first)`，且只活在单个请求内。

## 错误语义

- `first` 缺失/非整 → GraphQL 校验错误（执行前）；`first` 超出 1..5 → 字段级
  GraphQL 错误（"between 1 and 5"），绝不返回空数组冒充成功。
- 数据库异常 → 明确的 GraphQL 错误（如 `orders batch query failed: ...`），
  由 `errors` 数组呈现；不存在的客户 ID 被忽略，无订单客户返回 `[]`——三者互不混淆。

## 文件

| 文件 | 作用 |
| --- | --- |
| `app/schema.py` | 固定 SDL、customers/orders 解析器、参数校验 |
| `app/loaders.py` | `BatchLoader`（键、分批、回填）+ 每 `first` 一条的窗口 SQL |
| `app/db.py` | 每请求连接与 `RequestContext`、SQL 计数 |
| `app/server.py` | HTTP 入口，parse → validate → execute，附 `extensions.sqlCount` |
| `demo.py` | 全流程演示与 21 项断言 |
