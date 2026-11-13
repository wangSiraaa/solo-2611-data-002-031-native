# 工序依赖维护工具（depgraph）

在 PostgreSQL 16 上维护工序节点之间的有向依赖（`prerequisite → dependent`，
前者必须先完成），同时保存**直接边表**与**物化可达闭包表**，让上下游查询
不必每次重新遍历整张图。变更以批次方式解释，只有最终直接图仍是 DAG 才提交；
闭包随直接边在**同一事务**内做**区域化增量**更新，绝不清空重建整张可达表。

只使用合成数据，不接触任何真实生产资料。演示规模控制在 2000 节点以内。

---

## 1. 目录结构

```
sql/0001_init_schema.sql      迁移：nodes / edges / closure / operations
depgraph/
  db.py                       连接、迁移管理
  reference.py                独立参考算法（三色 DFS 找环、迭代 DFS 求可达）
  repo.py                     建图、批次变更、增量闭包、查询、校验、崩溃注入点
  synthetic.py                合成 DAG 生成器（chain / diamond / layered）
  cli.py                      命令行：init/change/ancestors/descendants/verify/synthetic
examples/                     可手查小图与各类批次 JSON
tests/                        35 个测试（含并发、进程崩溃重试、独立逐对校验）
run_tests.py                  有序测试运行器（崩溃演练放最后）
requirements.txt
.env.example                  连接串样例
```

---

## 2. 本机环境准备（一次性）

需要 Python 3.11 与 PostgreSQL 16 的服务端/客户端二进制（`initdb`、`pg_ctl`、
`psql`、`createdb` 在 PATH 上）。

```sh
# 1) Python 虚拟环境与驱动
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2) 初始化并启动一个独立 PostgreSQL 实例
#    注意：数据目录放在原生文件系统（如 /tmp）。virtiofs/网络挂载上
#    PostgreSQL 数据文件可能出现权限/稳定性问题。
mkdir -p /tmp/pg-task
initdb -D /tmp/pg-task/pgdata --auth-local=trust --auth-host=scram-sha-256
pg_ctl -D /tmp/pg-task/pgdata -l "$PWD/.runtime/postgres.log" \
       -o "-k /tmp/pg-task -p 55432 -c listen_addresses=''" start
createdb -h /tmp/pg-task -p 55432 taskdb

# 3) 连接串（也可 export DEPGRAPH_DSN，见 .env.example）
export DEPGRAPH_DSN="host=/tmp/pg-task port=55432 dbname=taskdb user=node"
```

停止实例：`pg_ctl -D /tmp/pg-task/pgdata stop`

> 仓库内默认连接串写死为上述 socket/端口/库名；也可用全局 `--dsn` 覆盖。
> 迁移在任意命令首次连接时自动执行并登记到 `schema_migrations`。

---

## 3. 命令与数据格式

图 JSON（`init`）：

```json
{"nodes": [1, 2, 3], "edges": [[1, 2], [2, 3]]}
```

批次 JSON（`change`）：操作按 `seq` 升序解释。

```json
{
  "operation_id": "op-001",
  "operations": [
    {"seq": 1, "op": "add",    "prerequisite": 2, "dependent": 1},
    {"seq": 2, "op": "remove", "prerequisite": 1, "dependent": 2}
  ]
}
```

命令：

```sh
# 初始化固定节点集合与初始 DAG（重复直接边由主键折叠，孤立节点保留）
.venv/bin/python -m depgraph.cli init examples/small_graph.json --reset

# 提交变更批次，返回新增/消失的可达对（按节点编号稳定排序）
.venv/bin/python -m depgraph.cli change examples/batch_diamond_keep_alternative.json

# 上下游查询（直接读物化闭包，O(结果) 而非全图遍历）
.venv/bin/python -m depgraph.cli ancestors 6
.venv/bin/python -m depgraph.cli descendants 3

# 独立遍历重建参考闭包，与物化表逐对比较
.venv/bin/python -m depgraph.cli verify

# 生成合成数据（chain/diamond/layered，节点数 <= 2000，可带孤立节点）
.venv/bin/python -m depgraph.cli synthetic layered 200 --isolated 5 --extra-edges 60 --out /tmp/g.json
```

退出码：`0` 成功；`1` verify 发现不一致；`2` 批次被拒绝（未知节点/自环/最终
成环，环路径随 JSON 的 `cycle` 字段给出，首尾节点相同且每条边可核验）；
`3` 幂等冲突；`4` 其他工具/数据库错误。

---

## 4. 关键设计与正确性保证

### 4.1 批次语义：只看最终直接图

- 操作按 `seq` 排序后折叠出**净新增 / 净删除**边集合；同一边先增后删（或反之）
  净效果为无变化。重复 `add` 已存在边、`remove` 不存在边都视为无变化。
- 未知节点、自环、**最终**图成环 → 整批拒绝（单事务回滚，不留半成品）。
- 允许批次内先增后删形成**临时环**：判环只对净效果作用后的最终直接图进行，
  因此“合法整批反转”不会被中间步骤误杀（见 `examples/batch_reverse_chain.json`）。
- 环路径由独立的三色 DFS（`reference.find_cycle`）给出，形如 `[3, 5, 2, 3]`。

### 4.2 单事务 + 咨询锁的并发安全

每个批次在一个事务内执行：

1. `pg_advisory_xact_lock` 取固定事务级咨询锁，把所有写批次**串行化**；
2. 幂等检查（见 4.4）；
3. Python 侧用独立参考算法对最终图判环；
4. 直接边 DML 与闭包增量 DML；
5. 登记回执，随事务一起提交。

两个进程各自加一条“看似安全”的边、合起来成环（A→B 与 B→A）时，后到者在
持锁状态下基于**最新**直接图重新判环并被拒绝；至多一个批次成功，数据库不会
留下环或与直接图不一致的闭包。

### 4.3 区域化增量闭包（不清空重建）

每条边变化只波及两个区域，区域外的 `closure` 行完全不触碰：

- **祖先区域 / 后代区域**：以所有变更边端点为种子，沿**旧闭包**向上/向下扩展，
  保证“变更前存在过的相关可达对”都落入区域；
- 在区域内按**最终直接边**重算受限可达（递归只按 `(ancestor, descendant)`
  两列去重，区域约束只施加在最终输出，不裁剪遍历路径）；
- 删除集合 = 区域内旧行中、按最终边已不可达的对；
- 插入集合 = 区域内新出现、当前闭包尚不存在的对。

由此得到两条关键性质：

- **菱形/多替代路径**：删除一条边后，只要还存在另一条路径，相应可达对就不在
  删除集合中，行与其 `last_changed_operation` 原样保留（测试
  `test_diamond_alternative_path_keeps_pairs`、`test_three_disjoint_paths` 覆盖）。
- **未改变记录不被重写**：保留行不经过 DELETE/INSERT，标记保持原值。每次提交
  还会把 SQL 实际增量与 Python 独立参考遍历的结果做集合对账，不符则整体报错回滚。

> 递归 CTE 刻意**不使用** `CYCLE ... USING path`：path 数组会让不同路由成为
> 不同行，`UNION` 无法去重，在多路径 DAG 上路径数组合级膨胀并引发 OOM。
> 判环已在 SQL 之前由参考算法完成；纯 `UNION` 仅按两列去重，天然收敛。

### 4.4 幂等回执

- 成功批次把规范化后批次内容的 SHA-256 指纹与新增/消失可达对写入 `operations`。
- 相同 `operation_id` + 相同内容重试：直接回放原回执，`replayed=true`，
  不重复改数据、不重写闭包标记。
- 相同 `operation_id` + 不同内容：返回幂等冲突并拒绝（退出码 3）。
- 被拒绝的批次不写 `operations`，因此用同一 id 修正内容后可正常重试。

---

## 5. 独立校验（verify）

`verify` 完全不依赖维护闭包所用的递归 SQL，而是：

1. 用 `reference.py` 的迭代 DFS 从**直接边**重新生成参考可达集合；
2. 与物化 `closure` 逐对比较，报告 `missing_pairs` / `extra_pairs` / 自身对；
3. 再跑一次三色 DFS 确认直接边无环；
4. 输出各 `last_changed_operation` 的行数分布，便于核查标记是否被意外重写。

测试 `VerifyCorruptionTest` 会人为删行/插假行/直接造环，确认 verify 都能发现。

---

## 6. 崩溃演练与重试

`change --crash-at {before_commit,mid_closure,after_commit}` 用于演练进程被杀：

| 注入点 | 时机 | 数据库端结果 | 重试行为 |
| --- | --- | --- | --- |
| `before_commit` | 直接边+闭包改完、COMMIT 前 | 事务随连接断开整体回滚 | 从零正常提交 |
| `mid_closure` | 闭包删除完成、插入前 | 整体回滚，闭包保持提交前状态 | 从零正常提交 |
| `after_commit` | COMMIT 后、回执返回前 | 数据已落盘 | 回放原回执 `replayed=true` |

测试通过 `subprocess` 真正 `kill -9` 子进程验证。注意 PostgreSQL 后端被
SIGKILL 会触发整个集群的 WAL 崩溃恢复（这是 PG 的标准崩溃语义，不是单库
回滚），恢复期间连接会短暂被拒；测试连接侧用 `connect_with_recovery_retry`
等待恢复完成，并把崩溃演练放在 `run_tests.py` 的最后执行。

---

## 7. 运行测试

```sh
export DEPGRAPH_DSN="host=/tmp/pg-task port=55432 dbname=taskdb user=node"
.venv/bin/python run_tests.py
```

覆盖：链、菱形、三条替代路径、孤立节点、合法整批反转、拒绝环/未知节点/自环、
幂等回放与冲突、并发 A→B/B→A（至多一个成功）、并发互不相交边（双双成功）、
三个崩溃注入点后的状态核对、2000 节点规模、verify 损坏探测、CLI 端到端。
共 35 个用例，本机约 15 秒。

---

## 8. 可手查小图

`examples/small_graph.json`：链 `1→2→3`，菱形 `3→{4,5}→6→7`，节点 `8` 孤立。
初始 7 条直接边、20 个可达对。配套批次：

- `batch_diamond_keep_alternative.json`：删 `3→5`，`(3,6)` 等经 `3→4→6` 保留；
- `batch_diamond_drop.json`：删 `3→4`（4 的唯一入边），观察整支路可达对消失；
- `batch_reverse_chain.json`：先加反向边（临时环）再删正向边的合法整批反转；
- `batch_reject_cycle.json`：最终成环，返回可核验环路径；
- `batch_reject_input.json`：自环/未知节点整批拒绝；
- `batch_replay_conflict.json`：复用 operation_id 但换内容，触发幂等冲突。
