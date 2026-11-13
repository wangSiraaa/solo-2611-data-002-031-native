"""实验执行：初始化、基线、预算内的组合探索、状态恢复。

隔离约定：
- 数据只进 {tag}_main（实验）/ {tag}_tpl（写测试模板）/ {tag}_w_*（写测试克隆，随测随删）
- 实验索引名一律 {tag}_ix_ 前缀；业务索引（PK、orders_channel_idx）绝不触碰
- state.json 记录进度；中断后重启可识别并清理自己遗留的实验索引
"""
from __future__ import annotations

import json
import math
import time
from itertools import combinations

from .candidates import SourcedCandidate
from .config import Config
from .db import list_experiment_objects
from .measure import measure_workload, median
from .writebench import WRITE_TABLES, measure_write_cost


# ---------------------------------------------------------------- state
def load_state(cfg: Config) -> dict:
    if cfg.state_path.exists():
        return json.loads(cfg.state_path.read_text())
    return {"stage": "new", "combos": {}, "explore_log": []}


def save_state(cfg: Config, state: dict):
    cfg.state_path.write_text(json.dumps(state, indent=1))


def log(state: dict, msg: str):
    state["explore_log"].append(f"{time.strftime('%H:%M:%S')} {msg}")
    print(f"  [explore] {msg}", flush=True)


# ---------------------------------------------------------------- catalog
def drop_stray_experiment_objects(cfg: Config, conn, state: dict | None = None) -> list[str]:
    """删除本任务遗留的实验对象（中断恢复）。只删 {tag} 前缀对象，返回清单。"""
    objs = list_experiment_objects(conn, cfg)
    dropped = []
    for ix in objs["indexes"]:
        conn.execute(f"DROP INDEX IF EXISTS {ix['schema']}.{ix['name']}")
        dropped.append(f"索引 {ix['schema']}.{ix['name']}")
    for sch in objs["schemas"]:
        if sch.startswith(cfg.schema_write_prefix):
            conn.execute(f"DROP SCHEMA IF EXISTS {sch} CASCADE")
            dropped.append(f"schema {sch}")
    if dropped and state is not None:
        log(state, f"恢复: 清理中断遗留实验对象 {dropped}")
    return dropped


# ---------------------------------------------------------------- combos
def _create_combo_indexes(cfg: Config, conn, combo_id: str,
                          cands: list[SourcedCandidate]) -> list[dict]:
    out = []
    for i, sc in enumerate(cands):
        name = f"{cfg.index_prefix}{combo_id}_{i}"
        t0 = time.perf_counter()
        conn.execute(sc.cand.ddl(name, cfg.schema_main))
        build_s = time.perf_counter() - t0
        size = conn.execute(
            "SELECT pg_relation_size(%s::regclass)",
            (f"{cfg.schema_main}.{name}",),
        ).fetchone()[0]
        out.append({"name": name, "def": sc.cand.label(),
                    "build_s": round(build_s, 3), "size_bytes": size})
    return out


def _drop_combo_indexes(cfg: Config, conn, combo_id: str):
    rows = conn.execute(
        """
        SELECT c.relname, n.nspname FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relname LIKE %s AND n.nspname = %s
        """,
        (f"{cfg.index_prefix}{combo_id}_%", cfg.schema_main),
    ).fetchall()
    for name, sch in rows:
        conn.execute(f"DROP INDEX IF EXISTS {sch}.{name}")


def run_combo(cfg: Config, conn, combo_id: str, cands: list[SourcedCandidate],
              baseline: dict, write_ops: list[dict], state: dict) -> dict:
    """单个组合：建索引 -> ANALYZE -> 读测量 -> 结果一致性 -> 写测量 -> 约束判定。"""
    res: dict = {
        "combo_id": combo_id,
        "candidates": sorted(sc.cand.label() for sc in cands),
        "indexes": [], "status": "created", "error": None, "correct": None,
        "space_mb": 0.0, "write_ratio": None, "feasible": None,
        "violations": [], "weighted_geomean": None, "per_query": {},
    }
    cdir = cfg.archive / "combos" / combo_id
    cdir.mkdir(parents=True, exist_ok=True)
    try:
        # 1. 实际建索引（记录每个索引构建耗时与真实大小）
        res["indexes"] = _create_combo_indexes(cfg, conn, combo_id, cands)
        res["space_mb"] = round(sum(ix["size_bytes"] for ix in res["indexes"]) / 1e6, 2)
        # 2. 统计信息：建索引后 ANALYZE 相关表（方式记入 meta）
        for t in sorted({sc.cand.table for sc in cands}):
            conn.execute(f"ANALYZE {cfg.schema_main}.{t}")
        # 3. 读测量（全部参数组，不允许只保留有利组）
        meas = measure_workload(cfg, conn, cdir, combo_id)
        # 4. 结果一致性：与无新增索引基线逐 (q,g) 比对，一致才比较性能
        mismatches = []
        for qid, groups in meas.items():
            for gname, m in groups.items():
                base_h = baseline["measure"][qid][gname]["hash"]
                if m["hash"] != base_h:
                    mismatches.append(f"{qid}/{gname}")
        res["correct"] = not mismatches
        if mismatches:
            res["error"] = f"结果与基线不一致: {mismatches}"
        # 5. 写入代价（同一模板副本 + 相同操作流，独立克隆测量）
        wb = measure_write_cost(cfg, conn, combo_id, [sc.cand for sc in cands],
                                write_ops, cdir)
        if wb["status"] == "ok":
            base_w = baseline["write"]["times_s"]
            res["write_ratio"] = round(median(wb["times_s"]) / median(base_w), 4)
        elif wb["status"] == "skipped":
            res["write_ratio"] = 1.0
            res.setdefault("notes", []).append(wb["reason"])
        else:
            res["error"] = (res["error"] or "") + f" 写测量失败: {wb.get('error')}"
        # 6. 约束判定
        budget_mb = cfg.constraints["space_budget_mb"]
        wmax = cfg.constraints["write_slowdown_max"]
        if res["space_mb"] > budget_mb:
            res["violations"].append(f"空间 {res['space_mb']:.1f}MB > 预算 {budget_mb}MB")
        if res["write_ratio"] is not None and res["write_ratio"] > wmax:
            res["violations"].append(f"写入倍率 {res['write_ratio']} > 阈值 {wmax}")
        if res["correct"] is False:
            res["violations"].append("结果与基线不一致")
        if res["error"]:
            res["violations"].append(f"测量异常: {res['error']}")
        res["feasible"] = not res["violations"]
        # 7. 加权几何平均加速比
        score, per_query = _score(cfg, baseline["measure"], meas)
        res["weighted_geomean"] = score
        res["per_query"] = per_query
        res["status"] = "done" if not res["error"] else "failed"
    except Exception as e:  # 超时/建索引失败等：保留原因，不视为成功
        res["status"] = "failed"
        res["error"] = f"{type(e).__name__}: {e}"
        res["feasible"] = False
        res["violations"].append(f"运行失败: {res['error']}")
    finally:
        try:
            _drop_combo_indexes(cfg, conn, combo_id)
        except Exception as e:
            res["error"] = (res["error"] or "") + f" 清理索引失败: {e}"
        (cdir / "combo.json").write_text(json.dumps(res, indent=1))
    return res


def _score(cfg: Config, base: dict, meas: dict) -> tuple[float | None, dict]:
    """加权几何平均加速比 + 每查询汇总。"""
    per_query: dict = {}
    wsum, lsum = 0.0, 0.0
    for q in cfg.queries:
        q_rows = []
        for g in q.param_groups:
            b = median(base[q.qid][g["name"]]["times_ms"])
            c = median(meas[q.qid][g["name"]]["times_ms"])
            if b <= 0 or c <= 0:
                continue
            sp = b / c
            w = g["weight"]
            wsum += w
            lsum += w * math.log(sp)
            q_rows.append({
                "group": g["name"], "weight": w,
                "baseline_median_ms": round(b, 3), "combo_median_ms": round(c, 3),
                "speedup": round(sp, 3),
                "combo_times_ms": meas[q.qid][g["name"]]["times_ms"],
                "indexes_used": meas[q.qid][g["name"]]["indexes_used"],
                "node_types_base": base[q.qid][g["name"]]["node_types"],
                "node_types_combo": meas[q.qid][g["name"]]["node_types"],
            })
        per_query[q.qid] = q_rows
    if wsum == 0:
        return None, per_query
    return round(math.exp(lsum / wsum), 4), per_query


# ---------------------------------------------------------------- explore
def _priority_singles(cands: list[SourcedCandidate], cfg: Config) -> list[SourcedCandidate]:
    """单索引试验顺序：支撑查询数多者优先，其次键列少者。

    列序对照候选（R2b 规则）必定纳入——它们存在的意义就是验证复合列顺序。
    """
    def qcount(sc):
        return len({s.split(":")[0] for s in sc.sources})
    ordered = sorted(cands, key=lambda sc: (-qcount(sc), len(sc.cand.key_cols),
                                            sc.cand.table, sc.cand.key_cols))
    quota = cfg.explore["single_phase_quota"]
    picked = ordered[:quota]
    have = {id(sc) for sc in picked}
    for sc in ordered:
        if id(sc) not in have and any(s.endswith(":R2b") for s in sc.sources):
            picked.append(sc)
    return picked


def explore(cfg: Config, conn, cands: list[SourcedCandidate], baseline: dict,
            write_ops: list[dict], state: dict) -> dict:
    """预算内的组合探索：单索引 -> 成对 -> 三元。state 为准，可中断恢复。

    返回 {frozenset(labels): combo_json}（含本次与此前已完成的）。
    """
    budget = cfg.explore["max_combos"]
    max_idx = cfg.explore["max_indexes_per_combo"]
    by_label = {sc.cand.label(): sc for sc in cands}

    tested: dict[frozenset, dict] = {}
    for _cid, cinfo in state["combos"].items():
        tested[frozenset(cinfo["candidates"])] = cinfo

    def run_one(cand_list: list[SourcedCandidate]) -> dict | None:
        """返回组合结果 json；已测过返回旧结果；预算用尽返回 None。"""
        labels = frozenset(sc.cand.label() for sc in cand_list)
        if labels in tested:
            log(state, f"跳过（此前已试验）: {sorted(labels)}")
            return tested[labels]
        if len(state["combos"]) >= budget:
            log(state, f"预算 {budget} 用尽，停止")
            return None
        combo_id = f"k{len(state['combos']) + 1:02d}"
        while any(c.get("combo_id") == combo_id for c in state["combos"].values()):
            combo_id = f"k{int(combo_id[1:]) + 1:02d}"
        log(state, f"试验 {combo_id}: {sorted(labels)}")
        r = run_combo(cfg, conn, combo_id, cand_list, baseline, write_ops, state)
        state["combos"][combo_id] = r
        tested[labels] = r
        save_state(cfg, state)
        log(state, f"{combo_id} 完成: score={r['weighted_geomean']} "
                   f"space={r['space_mb']:.1f}MB write={r['write_ratio']} "
                   f"feasible={r['feasible']} {r['violations']}")
        return r

    # ---- Phase A: 单索引（数量与构成由 _priority_singles 决定，含强制列序对照）
    singles = _priority_singles(cands, cfg)
    log(state, f"阶段A: 单索引试验 {len(singles)} 个（候选共 {len(cands)} 个）")
    single_res: dict[str, dict] = {}
    for sc in singles:
        r = run_one([sc])
        if r is None:
            return tested
        single_res[sc.cand.label()] = r

    def sscore(label: str) -> float:
        r = single_res.get(label)
        return (r or {}).get("weighted_geomean") or 0.0

    # ---- Phase B: 成对（按单索引得分积排序，取前列；预留三元名额）
    ranked = sorted(single_res, key=lambda l: -sscore(l))
    pairs = sorted(
        ((sscore(a) * sscore(b), a, b) for a, b in combinations(ranked[:6], 2)),
        reverse=True,
    )
    log(state, f"阶段B: 成对候选 {len(pairs)} 个，按单索引得分积排序")
    tested_pairs: dict[tuple[str, str], dict] = {}
    for _p, a, b in pairs:
        if len(state["combos"]) >= budget - 2:
            break
        r = run_one([by_label[a], by_label[b]])
        if r is None:
            return tested
        tested_pairs[(a, b)] = r

    # ---- Phase C: 三元（最优对+次优单索引）与"最大化"组合（top3，预期可能越约束）
    if tested_pairs and len(state["combos"]) < budget and max_idx >= 3:
        (a, b), _ = max(tested_pairs.items(),
                        key=lambda kv: kv[1].get("weighted_geomean") or 0)
        rest = [l for l in ranked if l not in (a, b)]
        if rest:
            if run_one([by_label[a], by_label[b], by_label[rest[0]]]) is None:
                return tested
    if len(state["combos"]) < budget and max_idx >= 3 and len(ranked) >= 3:
        log(state, "阶段C: 最大化组合 top3（可能触发空间/写入约束，用于对照）")
        run_one([by_label[l] for l in ranked[:3]])

    log(state, f"探索结束: 实际试验 {len(state['combos'])} 个组合（预算 {budget}）")
    return tested
