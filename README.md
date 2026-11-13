# 受限两源查询执行器（orders + products）

只针对两张关系、单一受限查询语言的执行器：从 PostgreSQL 读取订单，按实际关联键
小批量请求 HTTP 商品目录，在本地完成 INNER/LEFT JOIN、ON/WHERE 过滤与投影，并输出
结果以及可核对的执行说明（真实 PG 语句与参数、真实 HTTP 请求与参数、各阶段行数）。

不做通用联邦查询平台：不含 SQL 解析器、关联图、其他数据源或模式发现。

## 环境

- Linux、Python 3.11，提供 pip 和 venv。
- PostgreSQL 16 的 `initdb`、`pg_ctl`、`psql`、`createdb` 已在 PATH 中。
- 当前为普通用户，可在自己拥有的目录初始化独立数据库。
- 依赖、数据库、运行日志及临时验证数据放在忽略目录 `.runtime/` 中。

## 目录

| 路径 | 作用 |
| --- | --- |
| `fedquery/model.py` | 允许列/类型白名单、行模型、保留重复行的 JOIN 与三值逻辑 WHERE |
| `fedquery/query.py` | 查询 JSON 校验；未知字段/列/关系明确拒绝 |
| `fedquery/sources.py` | 参数化 PostgreSQL 访问、HTTP 目录客户端、能力与配置校验 |
| `fedquery/engine.py` | 下推规划、去重分批次、JOIN/WHERE/投影编排 |
| `fedquery/insights.py` | `explain`：阶段行数、下推证据、语义声明 |
| `fedquery/run.py` | 命令行入口 |
| `fedquery/demo.py` | 跑全部样例并解释 ON/WHERE 差异 |
| `scripts/init_local.sh` | 本机初始化 PostgreSQL + 载入数据（可选启动 HTTP fixture） |
| `tests/` | 单元测试与（设置 `FEDQUERY_DSN` 时的）真实数据库集成测试 |

## 本机初始化

需要 Python 3.11、PostgreSQL 16（`initdb/pg_ctl/psql/createdb` 在 PATH 中）。

```sh
# 一次性：建库、载入 fixtures/orders.sql、安装 psycopg；打印 DSN
bash scripts/init_local.sh
# 启动商品 HTTP fixture（带请求审计日志），可另开终端
.venv/bin/python fixtures/products_api.py --port 8107 --audit products-requests.jsonl
```

也可手工执行：

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
mkdir -p .runtime
initdb -D "$PWD/.runtime/pgdata" --auth-local=trust --auth-host=scram-sha-256
# 注：某些容器的 virtiofs 上 PG 无法在数据目录建 Unix socket，故 socket 放 /tmp
pg_ctl -D "$PWD/.runtime/pgdata" -l "$PWD/.runtime/postgres.log" \
  -o "-k /tmp/pg-task-$(id -un) -c listen_addresses=''" start
createdb -h "/tmp/pg-task-$(id -un)" taskdb
psql -h "/tmp/pg-task-$(id -un)" -d taskdb -v ON_ERROR_STOP=1 -f fixtures/orders.sql
```

## 运行

```sh
export FEDQUERY_DSN="host=/tmp/pg-task-$(id -un) dbname=taskdb"

# 全部样例：结果 + 执行说明 + ON/WHERE 差异解释
.venv/bin/python -m fedquery.demo --audit products-requests.jsonl

# 单个查询 / 小批次 / 输出 JSON
.venv/bin/python -m fedquery.run --query filter_in_on --batch-size 2 \
  --audit products-requests.jsonl --json-out .runtime/run.json
```

`run.py` 逐查询打印：结果行、下推规划、阶段行数、**实际执行的 PG 语句与参数**、
**实际发出的 HTTP 请求与参数**，以及从 provider 审计日志读回的条目（证明请求
确实发生且参数生效）。`--json-out` 给出同样信息的结构化版本。

## 查询语言

字段固定为：`name`（可选）、`orders_region`、`join`（`INNER`/`LEFT`）、
`product_category`（`null` 表示无谓词）、`filter_placement`（`ON`/`WHERE`）、
`select`（允许列引用列表）。未知字段、未知列、未知关系、非法枚举值都会以非零
退出码明确拒绝。

关联固定为 `orders.product_id = products.id`；两表与类型由 `source.json` 声明，
执行器只接受声明的 `fed_input.orders` 与声明内的类型。

## 下推与语义要点

- **订单过滤下推到 PG**：`region = %s` 走参数化语句，从不下传拼接字面量。
- **商品投影**：只请求 `select` 需要的列（`id` 始终需要），通过 `fields=` 生效。
- **商品 category 条件**：
  - `ON` 中：下推到 provider 的 `category=` 过滤（被拒的行本就不该匹配，
    不改变 LEFT JOIN 的存活集合）。
  - `WHERE` 中：**不下推**。否则会把本该以“右侧补 NULL”存活的订单提前丢掉。
    本地在联查之后判定，且 `NULL` 比较不为真。
  - 无论是否远端已过滤，本地都保留判定（`local_recheck=true`），不省略必要逻辑。
- **关联键下推**：只取订单中非 NULL 的实际 `product_id`，去重后按可配置
  `--batch-size`（默认 32，上限 256）分小批请求；同一键不重复请求；
  **零个有效键时完全不访问目录**。远端返回的 NULL 右键也不会进入匹配桶。
- **重复行保留**：目录中同一 `id` 的多条记录以 `dict[int, list[Row]]` 存放，
  一个订单对每条匹配右行各产生一行，绝不用字典覆盖重复键。
- **NULL 语义**：`NULL` 键不与 `NULL` 匹配；LEFT JOIN 无满足 ON 的右行时右侧补
  NULL；WHERE 中 `category = v` 对 NULL 不为真，因此补 NULL 行被移除。
- **金额**：PostgreSQL `numeric` 直接以 `Decimal` 处理；输出序列化为精确字符串
  （如 `"18.00"`），全程不经过二进制浮点。

## ON 与 WHERE 的差异（同一输入）

`fixtures/queries.json` 中 `filter_in_on` 与 `filter_in_where` 唯一差别是
`filter_placement`：

- `filter_in_on`：LEFT JOIN 的 ON 附带 `category='food'`。北区每个订单都保留；
  商品 10 有两条 food 记录，故订单 1、6 各产生两行；订单 2（home）、订单 3
  （category 为 NULL）与订单 4（NULL 键）无满足 ON 的右行，右侧补 NULL 各保留
  一行。共 **8 行**。
- `filter_in_where`：先形成同一个 LEFT JOIN（8 行），再用 WHERE 要求联查结果中
  `category='food'` 为真；上述补 NULL 行全部不满足而被移除，只剩订单 1、6 的
  各两行，共 **4 行**。

执行输出分别给出 `joined_rows`、`where_category_removed` 与 `output_rows`，
可直接复核该差异。

## 测试

```sh
.venv/bin/python -m pytest -q tests/                      # 仅单元测试会运行
FEDQUERY_DSN="host=/tmp/pg-task-$(id -un) dbname=taskdb" \
  .venv/bin/python -m pytest -q tests/                    # 含真实 PG + 本地 provider
```

集成测试在进程内起一个与 `fixtures/products_api.py` 行为一致的 provider，覆盖：
ON/WHERE 行数差异、重复键、NULL 键与 NULL category、空订单集不发请求、小批次
去重请求、PG 参数化，以及**与单机 PostgreSQL 等价 SQL 的逐行比对**。

## 本机验证记录

在 PostgreSQL 16.16 / psycopg 3.3.9 / Python 3.11.2 上执行：

- `pytest`：24 passed（含 3 个样例与单机 SQL 的逐行比对）。
- `demo`：`filter_in_on` 8 行、`filter_in_where` 4 行、`inner` 0 行；
  HTTP 请求分别为 `ids=10,20,30,40&category=food&fields=...`、
  `ids=10,20,30,40&fields=...`、`ids=99&fields=id,category`，与审计日志一致。
