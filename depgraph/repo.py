"""核心存储逻辑：建图、批次变更、增量可达闭包维护、查询与校验。

设计要点
--------
1. 所有变更批次在 *单个事务* 内完成：取事务级咨询锁 -> 幂等检查 ->
   先在 Python 侧用独立参考算法判定最终图是否仍为 DAG ->
   直接边 DML 与闭包增量 DML 一起提交。
2. 闭包从不整表清空重建。每条边变更只波及“端点及其祖先 × 端点及其后代”
   这两个区域（同时按旧图和新图取并集），区域外的 closure 行原封不动，
   其 last_changed_operation 自然保留。
3. 菱形等替代路径：区域内按 *最终直接边* 重新求受限可达，仍可达的对
   因 NOT EXISTS 命中而保留旧行、旧标记；只有真正消失的对才被删除。
4. 并发：pg_advisory_xact_lock 把所有写批次串行化，两个各自“看似安全”
   的边合起来成环时，后到者在持锁状态下基于最新图重新校验并被拒绝，
   数据库不会留下环或与直接图不一致的闭包。
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
from collections.abc import Iterable, Sequence

import psycopg
from psycopg.types.json import Json

from . import reference

# 固定咨询锁键（任意选取的 63 位常量），保证同库内所有写批次互斥。
ADVISORY_LOCK_KEY = 914728365001
INIT_MARKER = "INIT"

# 崩溃注入点（仅供测试与演练，正常运行为 None）
CRASH_BEFORE_COMMIT = "before_commit"   # 直接边+闭包都改完，COMMIT 之前
CRASH_MID_CLOSURE = "mid_closure"       # 闭包删除完成、插入之前
CRASH_AFTER_COMMIT = "after_commit"     # COMMIT 之后、回执返回之前
VALID_CRASH_POINTS = {
    CRASH_BEFORE_COMMIT,
    CRASH_MID_CLOSURE,
    CRASH_AFTER_COMMIT,
}


class DepgraphError(Exception):
    """工具层错误基类。"""


class BatchRejected(DepgraphError):
    """整批拒绝：输入非法、未知节点、自环或最终图有环。"""

    def __init__(self, reason: str, cycle: list[int] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.cycle = cycle


class IdempotencyConflict(DepgraphError):
    """同一 operation_id 携带了与首次不同的批次内容。"""


# --------------------------------------------------------------------------- #
# 读取辅助
# --------------------------------------------------------------------------- #

def _executemany(conn: psycopg.Connection, sql: str, seq) -> None:
    """psycopg 3 的 executemany 挂在 cursor 上，统一经此调用。"""
    with conn.cursor() as cur:
        cur.executemany(sql, seq)


def load_nodes(conn: psycopg.Connection) -> list[int]:
    rows = conn.execute("SELECT node_id FROM nodes ORDER BY node_id").fetchall()
    return [row[0] for row in rows]


def load_edges(conn: psycopg.Connection) -> list[tuple[int, int]]:
    rows = conn.execute(
        "SELECT prerequisite, dependent FROM edges "
        "ORDER BY prerequisite, dependent"
    ).fetchall()
    return [(row[0], row[1]) for row in rows]


def load_closure(conn: psycopg.Connection) -> dict[tuple[int, int], str]:
    rows = conn.execute(
        "SELECT ancestor, descendant, last_changed_operation FROM closure"
    ).fetchall()
    return {(row[0], row[1]): row[2] for row in rows}


# --------------------------------------------------------------------------- #
# 批次规范化与幂等指纹
# --------------------------------------------------------------------------- #

def normalize_operations(raw: object) -> list[dict]:
    """把外部 JSON 规范化为按 seq 排序的内部结构，并做格式校验。"""
    if not isinstance(raw, list) or not raw:
        raise BatchRejected("operations 必须是非空数组")
    normalized: list[dict] = []
    seen_seq: set[int] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise BatchRejected("每个操作必须是对象")
        seq = item.get("seq")
        op = item.get("op")
        u = item.get("prerequisite")
        v = item.get("dependent")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            raise BatchRejected(f"非法 seq: {seq!r}")
        if seq in seen_seq:
            raise BatchRejected(f"seq 重复: {seq}")
        if op not in ("add", "remove"):
            raise BatchRejected(f"seq={seq} 的 op 必须是 add 或 remove")
        if not isinstance(u, int) or isinstance(u, bool) or u <= 0:
            raise BatchRejected(f"seq={seq} 的 prerequisite 非法: {u!r}")
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            raise BatchRejected(f"seq={seq} 的 dependent 非法: {v!r}")
        seen_seq.add(seq)
        normalized.append(
            {"seq": seq, "op": op, "prerequisite": u, "dependent": v}
        )
    normalized.sort(key=lambda d: (d["seq"], d["op"], d["prerequisite"],
                                   d["dependent"]))
    return normalized


def canonical_hash(operations: Sequence[dict]) -> str:
    """对规范化后的批次内容取稳定指纹，作为幂等重试的比对依据。"""
    payload = json.dumps(operations, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _net_effect(
    operations: Sequence[dict],
) -> tuple[set[tuple[int, int]], set[tuple[int, int]]]:
    """按 seq 折叠出净新增/净删除边集合。

    同一边先增后删则两者都不出现（净效果为无变化），反之亦然；
    重复 add 已存在边、remove 不存在边后续与当前边集比对后也归为无变化。
    """
    adds: set[tuple[int, int]] = set()
    removes: set[tuple[int, int]] = set()
    for item in operations:
        pair = (item["prerequisite"], item["dependent"])
        if item["op"] == "add":
            adds.add(pair)
            removes.discard(pair)
        else:
            removes.add(pair)
            adds.discard(pair)
    return adds, removes


# --------------------------------------------------------------------------- #
# 初始化
# --------------------------------------------------------------------------- #

def initialize_graph(
    conn: psycopg.Connection,
    nodes: Iterable[int],
    edges: Iterable[tuple[int, int]],
    reset: bool = False,
) -> dict:
    """初始化固定节点集合与初始 DAG，一次性物化 INIT 版本闭包。"""
    nodes = list(nodes)
    edges = list(edges)
    if any(not isinstance(n, int) or isinstance(n, bool) or n <= 0 for n in nodes):
        raise BatchRejected("node_id 必须为正整数")
    if len(nodes) != len(set(nodes)):
        raise BatchRejected("node_id 存在重复")

    # 独立参考算法先验：未知节点、自环、环都在落库前拒绝。
    adj = reference.build_adjacency(edges, nodes)
    cycle = reference.find_cycle(adj)
    if cycle is not None:
        raise BatchRejected("初始图存在环，拒绝初始化", cycle=cycle)

    with conn.transaction():
        if reset:
            # 保留 schema_migrations，清空全部业务表
            conn.execute(
                "TRUNCATE operations, closure, edges, nodes RESTART IDENTITY"
            )
        elif conn.execute("SELECT 1 FROM nodes LIMIT 1").fetchone() is not None:
            raise DepgraphError("节点集合已存在；如需重建请显式使用 reset")

        _executemany(conn, "INSERT INTO nodes (node_id) VALUES (%s)",
                     [(n,) for n in nodes])
        # 重复直接边在这里被主键折叠为一行。
        _executemany(
            conn,
            "INSERT INTO edges (prerequisite, dependent, last_changed_operation) "
            "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            [(u, v, INIT_MARKER) for u, v in set(edges)],
        )
        # 初始闭包：递归 CTE 一次生成。
        # 不使用 CYCLE...USING path：path 数组会让不同路由成为不同行，
        # UNION 无法去重，在多路径 DAG 上路径数组合级爆炸并引发 OOM。
        # 图已由参考算法 find_cycle 验证为 DAG；纯 UNION 仅按两列去重，
        # 即使遇环也会在无新行时收敛，不会无限递归。
        conn.execute(
            """
            WITH RECURSIVE reach(ancestor, descendant) AS (
                SELECT prerequisite, dependent FROM edges
                UNION
                SELECT r.ancestor, e.dependent
                FROM reach r
                JOIN edges e ON e.prerequisite = r.descendant
            )
            INSERT INTO closure (ancestor, descendant, last_changed_operation)
            SELECT DISTINCT ancestor, descendant, %s
            FROM reach
            WHERE ancestor <> descendant
            ON CONFLICT DO NOTHING
            """,
            (INIT_MARKER,),
        )

    ref_pairs = reference.reachable_pairs(edges, nodes)
    connected: set[int] = set()
    for u, v in set(edges):
        connected.add(u)
        connected.add(v)
    return {
        "nodes": len(nodes),
        "direct_edges": len(set(edges)),
        "reachable_pairs": len(ref_pairs),
        "isolated_nodes": sorted(set(nodes) - connected),
    }


# --------------------------------------------------------------------------- #
# 批次变更
# --------------------------------------------------------------------------- #

def apply_change(
    conn: psycopg.Connection,
    operation_id: str,
    raw_operations: Sequence[dict],
    crash_at: str | None = None,
) -> dict:
    """解释并提交一个变更批次，返回新增/消失可达对回执。

    crash_at 仅用于崩溃演练：在指定位置 SIGKILL 当前进程，
    由于落点都在 COMMIT 之前（或提交之后），数据库端保持原子语义。
    """
    if crash_at is not None and crash_at not in VALID_CRASH_POINTS:
        raise DepgraphError(f"未知崩溃注入点: {crash_at}")
    if not isinstance(operation_id, str) or not operation_id.strip():
        raise BatchRejected("operation_id 必须是非空字符串")

    operations = normalize_operations(raw_operations)
    digest = canonical_hash(operations)

    with conn.transaction():
        # 1) 全库写批次互斥（事务级，提交/回滚自动释放）。
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_LOCK_KEY,))

        # 2) 幂等检查：同一 operation_id 已提交过。
        row = conn.execute(
            "SELECT request_hash, added_pairs, removed_pairs "
            "FROM operations WHERE operation_id = %s",
            (operation_id,),
        ).fetchone()
        if row is not None:
            if row[0] != digest:
                raise IdempotencyConflict(
                    f"operation_id={operation_id} 已用于不同内容的批次，拒绝复用"
                )
            return {
                "operation_id": operation_id,
                "status": "committed",
                "replayed": True,
                "added_pairs": [tuple(p) for p in row[1]],
                "removed_pairs": [tuple(p) for p in row[2]],
            }

        nodes = set(load_nodes(conn))
        current_edges = set(load_edges(conn))

        # 3) 静态校验：未知节点 / 自环整批拒绝（不区分 add、remove）。
        for item in operations:
            u, v = item["prerequisite"], item["dependent"]
            if u == v:
                raise BatchRejected(
                    f"seq={item['seq']} 出现自环 {u} -> {u}，整批拒绝",
                    cycle=[u, u],
                )
            if u not in nodes or v not in nodes:
                raise BatchRejected(
                    f"seq={item['seq']} 引用未知节点 {u} -> {v}，整批拒绝"
                )

        adds, removes = _net_effect(operations)
        # 与当前直接边比对，折叠 no-op：重复 add、remove 不存在的边。
        adds -= current_edges
        removes &= current_edges
        delta = {(u, v, "add") for u, v in adds} | {
            (u, v, "remove") for u, v in removes
        }
        final_edges = (current_edges - removes) | adds

        # 4) 只看 *最终* 直接图判环：允许批次内先增后删形成临时环。
        adj = reference.build_adjacency(final_edges, nodes)
        cycle = reference.find_cycle(adj)
        if cycle is not None:
            raise BatchRejected(
                "应用整批操作后直接图存在环，整批拒绝", cycle=cycle
            )

        # 5) Python 独立参考算法预算可达对差异（稍后与 SQL 实际差异对账）。
        old_pairs = reference.reachable_pairs(current_edges, nodes)
        new_pairs = reference.reachable_pairs(final_edges, nodes)
        expected_added = sorted(new_pairs - old_pairs)
        expected_removed = sorted(old_pairs - new_pairs)

        # 6) 把净边变化放进临时表，后续 SQL 全部集合化、区域化。
        #    先 DROP 再建：同一连接连续提交多个批次时避免重名冲突。
        conn.execute("DROP TABLE IF EXISTS tmp_delta")
        conn.execute(
            "CREATE TEMP TABLE tmp_delta ("
            "prerequisite BIGINT NOT NULL, dependent BIGINT NOT NULL, "
            "kind TEXT NOT NULL) ON COMMIT DROP"
        )
        _executemany(
            conn,
            "INSERT INTO tmp_delta VALUES (%s, %s, %s)", list(delta)
        )

        # 最终直接边（临时视图）：旧边去掉净删除，并上净新增。
        conn.execute("DROP TABLE IF EXISTS tmp_new_edges")
        conn.execute(
            """
            CREATE TEMP TABLE tmp_new_edges ON COMMIT DROP AS
            SELECT e.prerequisite, e.dependent
            FROM edges e
            LEFT JOIN tmp_delta d
              ON d.prerequisite = e.prerequisite
             AND d.dependent = e.dependent
             AND d.kind = 'remove'
            WHERE d.prerequisite IS NULL
            UNION
            SELECT d.prerequisite, d.dependent
            FROM tmp_delta d
            WHERE d.kind = 'add'
            """
        )

        # 7) 受影响区域：所有变更边端点为种子。
        #    区域沿 *旧闭包* 双向扩展（祖先向上、后代向下），保证“变更前
        #    存在过的所有相关可达对”都落在区域内——例如删除 1->2 后，节点 2
        #    在最终图里没有入边，但若只沿最终边取祖先，2 会漏出区域，
        #    旧的 (2,4)/(2,5)/(2,6) 闭包行便无法被删除。
        #    区域内的新可达集合则按 *最终直接边* 重算（见 tmp_new_reach），
        #    两相结合即可同时正确处理新增捷径与删除支路。
        conn.execute("DROP TABLE IF EXISTS tmp_region_anc")
        conn.execute(
            """
            CREATE TEMP TABLE tmp_region_anc ON COMMIT DROP AS
            WITH RECURSIVE seeds(anc) AS (
                SELECT prerequisite FROM tmp_delta
                UNION
                SELECT dependent FROM tmp_delta
            ),
            up(node) AS (
                SELECT anc FROM seeds
                UNION
                SELECT c.ancestor
                FROM closure c JOIN up ON c.descendant = up.node
            )
            SELECT DISTINCT node FROM up
            """
        )
        conn.execute("DROP TABLE IF EXISTS tmp_region_desc")
        conn.execute(
            """
            CREATE TEMP TABLE tmp_region_desc ON COMMIT DROP AS
            WITH RECURSIVE seeds(anc) AS (
                SELECT prerequisite FROM tmp_delta
                UNION
                SELECT dependent FROM tmp_delta
            ),
            down(node) AS (
                SELECT anc FROM seeds
                UNION
                SELECT c.descendant
                FROM closure c JOIN down ON c.ancestor = down.node
            )
            SELECT DISTINCT node FROM down
            """
        )

        # 8) 仅在“祖先区域 × 后代区域”内按最终边重算受限可达。
        #    注意：递归遍历本身不能按区域裁边（否则祖先在区域内、路径经过
        #    非种子节点的对会漏算），区域约束只施加在最终输出上——这样
        #    重算集合恰好等于被删除的闭包子集，区域外的行完全不参与。
        #    同样不使用 CYCLE...USING path 以避免多路径 DAG 的路径爆炸；
        #    最终图已在步骤 4 由参考算法判定为 DAG。
        conn.execute("DROP TABLE IF EXISTS tmp_new_reach")
        conn.execute(
            """
            CREATE TEMP TABLE tmp_new_reach ON COMMIT DROP AS
            WITH RECURSIVE reach(ancestor, descendant) AS (
                SELECT prerequisite, dependent FROM tmp_new_edges
                UNION
                SELECT r.ancestor, ne.dependent
                FROM reach r
                JOIN tmp_new_edges ne ON ne.prerequisite = r.descendant
            )
            SELECT DISTINCT r.ancestor, r.descendant
            FROM reach r
            WHERE r.ancestor IN (SELECT node FROM tmp_region_anc)
              AND r.descendant IN (SELECT node FROM tmp_region_desc)
              AND r.ancestor <> r.descendant
            """
        )

        # 9) 直接边更新（删除净删、插入净增，主键折叠重复）。
        conn.execute(
            """
            DELETE FROM edges e
            USING tmp_delta d
            WHERE e.prerequisite = d.prerequisite
              AND e.dependent = d.dependent
              AND d.kind = 'remove'
            """
        )
        conn.execute(
            """
            INSERT INTO edges (prerequisite, dependent, last_changed_operation)
            SELECT prerequisite, dependent, %s
            FROM tmp_delta
            WHERE kind = 'add'
            ON CONFLICT (prerequisite, dependent) DO NOTHING
            """,
            (operation_id,),
        )

        # 10) 闭包增量（关键：不做区域全量重建）。
        #     a) 只删除“区域内、且按最终边已不可达”的旧行；
        #        仍可达的对（如菱形里另一条路径支撑的可达关系）不在删除集合，
        #        其 last_changed_operation 原样保留。
        deleted = conn.execute(
            """
            DELETE FROM closure
            WHERE ancestor IN (SELECT node FROM tmp_region_anc)
              AND descendant IN (SELECT node FROM tmp_region_desc)
              AND ancestor <> descendant
              AND NOT EXISTS (
                  SELECT 1 FROM tmp_new_reach nr
                  WHERE nr.ancestor = closure.ancestor
                    AND nr.descendant = closure.descendant
              )
            RETURNING ancestor, descendant
            """
        ).fetchall()

        if crash_at == CRASH_MID_CLOSURE:
            # 模拟进程在“闭包删了一半、尚未补回”时被 kill -9。
            os.kill(os.getpid(), signal.SIGKILL)

        #     b) 插入区域内新出现的可达对；已存在的行 NOT EXISTS 命中、原样保留。
        inserted = conn.execute(
            """
            INSERT INTO closure (ancestor, descendant, last_changed_operation)
            SELECT nr.ancestor, nr.descendant, %s
            FROM tmp_new_reach nr
            WHERE NOT EXISTS (
                SELECT 1 FROM closure c
                WHERE c.ancestor = nr.ancestor
                  AND c.descendant = nr.descendant
            )
            RETURNING ancestor, descendant
            """,
            (operation_id,),
        ).fetchall()

        sql_removed = sorted((r[0], r[1]) for r in deleted)
        sql_added = sorted((r[0], r[1]) for r in inserted)

        # 双实现对账：SQL 物化差异必须与独立参考算法完全一致。
        if sql_added != expected_added or sql_removed != expected_removed:
            raise DepgraphError(
                "内部一致性校验失败：闭包增量与参考遍历不符 "
                f"(sql_added={sql_added}, expected={expected_added}; "
                f"sql_removed={sql_removed}, expected={expected_removed})"
            )

        # 11) 登记回执，之后同一 operation_id + 同内容重试直接回放。
        conn.execute(
            "INSERT INTO operations "
            "(operation_id, request_hash, status, added_pairs, removed_pairs) "
            "VALUES (%s, %s, 'committed', %s, %s)",
            (
                operation_id,
                digest,
                Json(expected_added),
                Json(expected_removed),
            ),
        )

        if crash_at == CRASH_BEFORE_COMMIT:
            # 模拟提交前崩溃：事务随连接断开而回滚，重试应看不到任何痕迹。
            os.kill(os.getpid(), signal.SIGKILL)

    if crash_at == CRASH_AFTER_COMMIT:
        # 提交已落盘但回执丢失：重试必须回放同一份回执且状态不变。
        os.kill(os.getpid(), signal.SIGKILL)

    return {
        "operation_id": operation_id,
        "status": "committed",
        "replayed": False,
        "added_pairs": expected_added,
        "removed_pairs": expected_removed,
    }


# --------------------------------------------------------------------------- #
# 查询与校验
# --------------------------------------------------------------------------- #

def _require_node(conn: psycopg.Connection, node_id: int) -> None:
    row = conn.execute(
        "SELECT 1 FROM nodes WHERE node_id = %s", (node_id,)
    ).fetchone()
    if row is None:
        raise BatchRejected(f"节点不存在: {node_id}")


def ancestors(conn: psycopg.Connection, node_id: int) -> list[int]:
    """从物化闭包直接取全部祖先，按节点编号稳定排序。"""
    _require_node(conn, node_id)
    rows = conn.execute(
        "SELECT ancestor FROM closure WHERE descendant = %s ORDER BY ancestor",
        (node_id,),
    ).fetchall()
    return [row[0] for row in rows]


def descendants(conn: psycopg.Connection, node_id: int) -> list[int]:
    """从物化闭包直接取全部后代，按节点编号稳定排序。"""
    _require_node(conn, node_id)
    rows = conn.execute(
        "SELECT descendant FROM closure WHERE ancestor = %s ORDER BY descendant",
        (node_id,),
    ).fetchall()
    return [row[0] for row in rows]


def verify(conn: psycopg.Connection) -> dict:
    """用独立遍历算法从直接边重建参考闭包，与物化表逐对比较。"""
    nodes = load_nodes(conn)
    edges = load_edges(conn)
    materialized = load_closure(conn)

    self_rows = [pair for pair in materialized if pair[0] == pair[1]]
    reference_pairs = reference.reachable_pairs(edges, nodes)
    materialized_pairs = set(materialized)

    missing = sorted(reference_pairs - materialized_pairs)
    extra = sorted(materialized_pairs - reference_pairs)

    # 直接边自身也不允许有环（闭包表无法表达，但参考算法会暴露异常结构）。
    adj = reference.build_adjacency(edges, nodes)
    cycle = reference.find_cycle(adj)

    marker_distribution: dict[str, int] = {}
    for marker in materialized.values():
        marker_distribution[marker] = marker_distribution.get(marker, 0) + 1

    ok = not missing and not extra and not self_rows and cycle is None
    return {
        "ok": ok,
        "nodes": len(nodes),
        "direct_edges": len(edges),
        "closure_rows": len(materialized_pairs),
        "reference_pairs": len(reference_pairs),
        "missing_pairs": missing,
        "extra_pairs": extra,
        "self_pairs": [list(p) for p in self_rows],
        "cycle": cycle,
        "marker_distribution": marker_distribution,
    }
