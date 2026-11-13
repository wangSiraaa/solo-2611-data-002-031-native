"""CLI：init / gen-workload / candidates / run / report / status / cleanup / recover。"""
from __future__ import annotations

import argparse
import json
import sys

import psycopg

from . import report as report_mod
from .candidates import dedup_against_existing, generate
from .config import Config, dsn, load_config
from .datagen import create_schema, load_initial, param_context
from .db import (connect, existing_index_keys, list_experiment_objects,
                 server_meta, table_fingerprint)
from .explore import (drop_stray_experiment_objects, explore, load_state,
                      save_state, log)
from .measure import measure_workload
from .sqlparse_util import parse_query
from .writebench import gen_write_ops, measure_write_cost

TABLES = ["customers", "products", "orders", "order_items"]


def _ensure_db(cfg: Config):
    dsn0 = dsn(cfg).replace(f"dbname={cfg.dbname}", "dbname=postgres")
    conn = psycopg.connect(dsn0, autocommit=True)
    exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s",
                          (cfg.dbname,)).fetchone()
    if not exists:
        conn.execute(f"CREATE DATABASE {cfg.dbname}")
    conn.close()


# ---------------------------------------------------------------- init
def cmd_init(cfg: Config):
    """创建实验 schema、生成数据、建立模板副本、ANALYZE、记录元信息。"""
    state = load_state(cfg)
    if state.get("stage") not in ("new",):
        print(f"state={state.get('stage')}：已初始化过。如需重来请先 cleanup --all。")
        return
    _ensure_db(cfg)
    with connect(cfg) as conn:
        print(f"[init] 生成 schema {cfg.schema_main} 与数据（seed={cfg.seed}）...")
        create_schema(conn, cfg.schema_main)
        counts = load_initial(cfg, conn, cfg.schema_main)
        print(f"[init] 行数: {counts}")
        # 模板副本：写测试的"同一初始数据副本"
        print(f"[init] 克隆模板 schema {cfg.schema_tpl} ...")
        conn.execute(f"DROP SCHEMA IF EXISTS {cfg.schema_tpl} CASCADE")
        conn.execute(f"CREATE SCHEMA {cfg.schema_tpl}")
        for t in TABLES:
            conn.execute(
                f"CREATE TABLE {cfg.schema_tpl}.{t} "
                f"(LIKE {cfg.schema_main}.{t} INCLUDING ALL)")
            conn.execute(
                f"INSERT INTO {cfg.schema_tpl}.{t} SELECT * FROM {cfg.schema_main}.{t}")
        # 统计信息收集方式：装载后 ANALYZE（默认采样目标）
        for sch in (cfg.schema_main, cfg.schema_tpl):
            for t in TABLES:
                conn.execute(f"ANALYZE {sch}.{t}")
        meta = server_meta(conn)
        meta.update({
            "seed": cfg.seed,
            "counts": counts,
            "stats": {
                "method": "装载后 ANALYZE；每次建实验索引后 ANALYZE 相关表",
                "default_statistics_target": cfg.stats["default_statistics_target"],
            },
            "fingerprints": {t: table_fingerprint(conn, cfg.schema_main, t)
                             for t in TABLES},
            "param_context": param_context(cfg),
        })
        (cfg.archive / "meta.json").write_text(json.dumps(meta, indent=1))
    state["stage"] = "init_done"
    save_state(cfg, state)
    print("[init] 完成，元信息已写入 archive/meta.json")


# ---------------------------------------------------------------- gen-workload
def cmd_gen_workload(cfg: Config):
    """解析参数符号、生成固定写入操作流，写入 archive/workload.json。"""
    meta = json.loads((cfg.archive / "meta.json").read_text())
    ctx = meta["param_context"]
    cfg = load_config(ctx)  # 用数据上下文解析 hot1/cold/...
    with connect(cfg) as conn:
        prices = {r[0]: float(r[1]) for r in
                  conn.execute(f"SELECT product_id, price FROM {cfg.schema_main}.products")}
    ops = gen_write_ops(cfg, prices)
    doc = {
        "queries": [
            {"qid": q.qid, "file": q.file, "kind": q.kind, "desc": q.desc,
             "param_groups": q.param_groups}
            for q in cfg.queries
        ],
        "write_ops": ops,
        "write_ops_desc": f"{cfg.writebench['n_insert_orders']} 插入订单(含明细) + "
                          f"{cfg.writebench['n_update_orders']} 更新状态，"
                          f"每 {cfg.writebench['ops_per_commit']} 操作一提交",
    }
    (cfg.archive / "workload.json").write_text(json.dumps(doc, indent=1))
    n_groups = sum(len(q.param_groups) for q in cfg.queries)
    print(f"[gen-workload] {len(cfg.queries)} 查询 / {n_groups} 参数组 / "
          f"{len(ops)} 写操作 -> archive/workload.json")


# ---------------------------------------------------------------- candidates
def cmd_candidates(cfg: Config) -> list:
    meta = json.loads((cfg.archive / "meta.json").read_text())
    cfg = load_config(meta["param_context"])
    structures = [parse_query(q.qid, q.sql) for q in cfg.queries]
    cands = generate(structures, cfg.explore["max_candidates"])
    with connect(cfg) as conn:
        existing = existing_index_keys(conn, cfg.schema_main)
    kept, dropped = dedup_against_existing(cands, existing)
    doc = {
        "kept": [sc.to_json() for sc in kept],
        "dropped": dropped,
        "existing_indexes": [
            {"name": e["name"], "table": e["table"], "key": list(e["key_cols"]),
             "include": list(e["include_cols"]), "primary": e["is_primary"]}
            for e in existing
        ],
    }
    (cfg.archive / "candidates.json").write_text(json.dumps(doc, indent=1))
    print(f"[candidates] 保留 {len(kept)} 条候选，剔除 {len(dropped)} 条"
          f"（与已有索引等价/冗余）")
    for sc in kept:
        print(f"  {sc.cand.label():45s} <- {', '.join(sc.sources)}")
    for d in dropped:
        print(f"  剔除 {d['candidate']:36s} {d['reason']}")
    return kept


# ---------------------------------------------------------------- run
def cmd_run(cfg: Config):
    meta = json.loads((cfg.archive / "meta.json").read_text())
    cfg = load_config(meta["param_context"])
    state = load_state(cfg)
    wl = json.loads((cfg.archive / "workload.json").read_text())
    write_ops = wl["write_ops"]
    cand_doc = json.loads((cfg.archive / "candidates.json").read_text())

    from .candidates import Candidate, SourcedCandidate
    kept = [
        SourcedCandidate(
            Candidate(c["table"], tuple(c["key_cols"]), tuple(c["include_cols"])),
            list(c["sources"]))
        for c in cand_doc["kept"]
    ]

    with connect(cfg, schema=cfg.schema_main,
                 timeout_ms=cfg.measurement["statement_timeout_ms"]) as conn:
        # 中断恢复：先清理自己遗留的实验对象
        drop_stray_experiment_objects(cfg, conn, state)

        # 基线（无新增索引）
        if not state.get("baseline_done"):
            print("[run] 基线测量（无新增索引）...")
            bdir = cfg.archive / "baseline"
            meas = measure_workload(cfg, conn, bdir, "baseline")
            wb = measure_write_cost(cfg, conn, "base", [], write_ops, bdir)
            if wb["status"] != "ok":
                raise SystemExit(f"基线写测量失败，无法计算写入倍率: {wb.get('error')}")
            (bdir / "baseline.json").write_text(json.dumps(
                {"measure": meas, "write": wb}, indent=1))
            state["baseline_done"] = True
            save_state(cfg, state)
            print("[run] 基线完成")
        baseline = json.loads((cfg.archive / "baseline" / "baseline.json").read_text())

        print(f"[run] 组合探索（预算 {cfg.explore['max_combos']}）...")
        explore(cfg, conn, kept, baseline, write_ops, state)
        state["stage"] = "done"
        save_state(cfg, state)
    print("[run] 完成。用 `python -m indexlab report` 查看建议。")


# ---------------------------------------------------------------- report
def cmd_report(cfg: Config):
    meta = json.loads((cfg.archive / "meta.json").read_text())
    cfg = load_config(meta["param_context"])
    text = report_mod.generate(cfg)
    print(text)


# ---------------------------------------------------------------- status
def cmd_status(cfg: Config):
    state = load_state(cfg)
    print(f"stage: {state.get('stage')}  baseline_done: {state.get('baseline_done')}")
    print(f"已完成组合: {len(state.get('combos', {}))}")
    with connect(cfg) as conn:
        objs = list_experiment_objects(conn, cfg)
    print(f"本任务 schema: {objs['schemas']}")
    print(f"残留实验索引: {[i['name'] for i in objs['indexes']] or '无'}")


# ---------------------------------------------------------------- cleanup
def cmd_cleanup(cfg: Config, all_objects: bool):
    with connect(cfg) as conn:
        if all_objects:
            objs = list_experiment_objects(conn, cfg)
            for sch in objs["schemas"]:
                conn.execute(f"DROP SCHEMA IF EXISTS {sch} CASCADE")
                print(f"[cleanup] 删除 schema {sch}")
        else:
            dropped = drop_stray_experiment_objects(cfg, conn)
            print(f"[cleanup] 删除实验索引 {len(dropped)} 个: {dropped or '无'}")
        # 用系统目录核对清理结果
        objs = list_experiment_objects(conn, cfg)
        if all_objects:
            assert not objs["schemas"], f"仍有残留 schema: {objs['schemas']}"
            print("[cleanup] 目录核对：本任务 schema 已全部移除")
            if cfg.state_path.exists():
                cfg.state_path.unlink()
        else:
            assert not objs["indexes"], f"仍有残留实验索引: {objs['indexes']}"
            print("[cleanup] 目录核对：无残留实验索引（业务索引未触碰）")
        biz = conn.execute(
            """
            SELECT c.relname FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_index i ON i.indexrelid = c.oid
            WHERE n.nspname = %s AND c.relkind = 'i'
            """,
            (cfg.schema_main,),
        ).fetchall()
        if not all_objects:
            print(f"[cleanup] 现存索引（业务）: {[r[0] for r in biz]}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="indexlab",
                                 description="工作负载驱动的索引实验助手")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="建实验schema并生成数据")
    sub.add_parser("gen-workload", help="生成工作负载（参数组+写操作流）")
    sub.add_parser("candidates", help="解析查询结构并生成候选索引")
    sub.add_parser("run", help="基线+预算内组合实验（可中断恢复）")
    sub.add_parser("report", help="查看建议报告")
    sub.add_parser("status", help="查看进度与本任务对象")
    sub.add_parser("recover", help="识别并清理中断遗留的本任务实验对象")
    cl = sub.add_parser("cleanup", help="清理")
    cl.add_argument("--all", action="store_true", help="删除全部实验schema")
    cl.add_argument("--indexes", action="store_true", help="仅删除实验索引")
    args = ap.parse_args(argv)

    cfg = load_config()  # 参数符号在 gen-workload/run 时用 meta 中的上下文再解析
    if args.cmd == "init":
        cmd_init(cfg)
    elif args.cmd == "gen-workload":
        cmd_gen_workload(cfg)
    elif args.cmd == "candidates":
        cmd_candidates(cfg)
    elif args.cmd == "run":
        cmd_run(cfg)
    elif args.cmd == "report":
        cmd_report(cfg)
    elif args.cmd == "status":
        cmd_status(cfg)
    elif args.cmd == "recover":
        with connect(cfg) as conn:
            dropped = drop_stray_experiment_objects(cfg, conn)
        print(f"[recover] 清理遗留实验对象: {dropped or '无'}")
    elif args.cmd == "cleanup":
        if args.all:
            cmd_cleanup(cfg, all_objects=True)
        else:
            cmd_cleanup(cfg, all_objects=False)


if __name__ == "__main__":
    main()
