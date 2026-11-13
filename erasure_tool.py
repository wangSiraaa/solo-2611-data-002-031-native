#!/usr/bin/env python3
"""
erasure_tool.py — 删除请求的可复查计划与可恢复执行（Python 3.11，仅标准库）

工作目录（由 fixtures/prepare.py 生成）内的数据源：
  business.sqlite  业务库（customers / orders；restricted=1 表示受限，不对普通业务开放）
  export.jsonl     已登记公共导出副本（manifest.json 的 registered_exports 逐一登记）
  holds.json       保留令：仅覆盖指定订单的原始内容，不覆盖客户联系方式，
                   也不授权把该订单继续留在公共导出中
  request.json     删除请求（显式 subject_id 列表，不做身份推断）

子命令：
  plan    生成可复查计划 plan.json（只读，不修改任何数据源）
  apply   复核计划依据（请求/保留令哈希、行版本、文件校验和）后分步执行：
          数据库事务 -> 登记导出原子替换；execution.json 逐步落盘，中断后可续办
  read    普通业务读取入口：仅返回未删除且未受限的对象
  status  报告各来源真实完成状态与剩余受限记录（不修改任何内容）
  verify  依据 plan.json 核对数据库外键、终态、导出内容与未请求对象指纹
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

JOURNAL_NAME = "execution.json"
PLAN_NAME = "plan.json"
EXPORT_TMP_SUFFIX = ".erasure-tmp"

AUDIT_DDL = """
CREATE TABLE IF NOT EXISTS erasure_audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT NOT NULL,
  plan_id TEXT NOT NULL,
  subject_id TEXT,
  order_id TEXT,
  action TEXT NOT NULL,        -- delete_order / delete_customer / restrict_order /
                               -- skeleton_customer / step_marker:database
  hold_reason TEXT,            -- 仅 restrict_order 记录保留令理由；绝不写入客户联系方式
  detail TEXT,
  created_at TEXT NOT NULL
);
"""


class StaleError(Exception):
    """复核或执行时发现数据/保留令与计划依据不一致。"""


# ---------------------------------------------------------------- 基础工具

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_sha256(obj) -> str:
    """语义级哈希：键排序后的紧凑 JSON，避免纯格式差异造成误判。"""
    blob = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_bytes(blob.encode("utf-8"))


def short(sha: str) -> str:
    return sha[:12] + "…"


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    with tmp.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fsync_dir(path.parent)


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def connect(db_path: Path) -> sqlite3.Connection:
    # isolation_level=None：自动提交模式，事务由 BEGIN IMMEDIATE/COMMIT 显式控制
    db = sqlite3.connect(db_path, isolation_level=None)
    db.execute("PRAGMA foreign_keys=ON")
    return db


def simulate_crash(point: str) -> None:
    """测试钩子：模拟进程在指定阶段之后被杀死（不清理、不落盘）。"""
    print(f"!!! 模拟崩溃：进程在 {point} 之后异常退出", flush=True)
    os._exit(1)


# ---------------------------------------------------------------- plan

def build_plan(workdir: Path) -> dict:
    manifest = load_json(workdir / "manifest.json")
    request = load_json(workdir / "request.json")
    holds_doc = load_json(workdir / "holds.json")
    db = connect(workdir / manifest["database"])
    try:
        active_holds = [h for h in holds_doc.get("holds", []) if h.get("active")]
        hold_by_order = {h.get("order_id"): h for h in active_holds}

        requested = list(request.get("subject_ids", []))
        subjects = []
        used_holds: set[str] = set()
        for sid in requested:
            cust = db.execute(
                "SELECT revision FROM customers WHERE subject_id=?", (sid,)).fetchone()
            orders = db.execute(
                "SELECT order_id, revision, amount_cents, note FROM orders"
                " WHERE subject_id=? ORDER BY order_id", (sid,)).fetchall()
            order_plans = []
            retained_any = False
            for oid, rev, amount, note in orders:
                hold = hold_by_order.get(oid)
                if hold is not None:
                    # 保留令仅覆盖该订单原始内容：订单转入受限状态，不扩张到客户资料
                    retained_any = True
                    used_holds.add(oid)
                    order_plans.append({
                        "order_id": oid,
                        "action": "retain_restricted",
                        "revision": rev,
                        "content_sha256": canonical_sha256({
                            "order_id": oid, "subject_id": sid,
                            "amount_cents": amount, "note": note}),
                        "hold": {"reason": hold.get("reason"),
                                 "retain": hold.get("retain")},
                    })
                else:
                    order_plans.append({"order_id": oid, "action": "delete",
                                        "revision": rev})
            if cust is None:
                cust_plan = {"action": "absent"}
            elif retained_any:
                # 仅保留维持外键所需的 subject_id 骨架，清除姓名/邮箱/电话
                cust_plan = {"action": "skeletonize", "revision": cust[0]}
            else:
                cust_plan = {"action": "delete", "revision": cust[0]}
            subjects.append({
                "subject_id": sid,
                "action": "erase_with_retention" if retained_any else "erase",
                "customer": cust_plan,
                "orders": order_plans,
            })

        ignored_holds = [h for h in active_holds
                         if h.get("order_id") not in used_holds]

        # 未请求对象的指纹：verify 据此证明其业务值未被改动
        if requested:
            ph = ",".join("?" for _ in requested)
            rows_c = db.execute(
                f"SELECT * FROM customers WHERE subject_id NOT IN ({ph})"
                " ORDER BY subject_id", requested).fetchall()
            rows_o = db.execute(
                f"SELECT * FROM orders WHERE subject_id NOT IN ({ph})"
                " ORDER BY order_id", requested).fetchall()
        else:
            rows_c = db.execute("SELECT * FROM customers ORDER BY subject_id").fetchall()
            rows_o = db.execute("SELECT * FROM orders ORDER BY order_id").fetchall()
        untouched_fp = canonical_sha256({"customers": rows_c, "orders": rows_o})
    finally:
        db.close()

    # 登记导出副本的清理计划：已删除或受限对象一律不留在公共文件，
    # 保留令不授权订单原文继续出现在导出中，因此按请求对象整行剔除。
    export_plans = []
    for rel in manifest.get("registered_exports", []):
        raw = (workdir / rel).read_bytes()
        lines = raw.splitlines(keepends=True)
        kept = []
        line_plans = []
        for idx, line in enumerate(lines):
            rec = json.loads(line)
            drop = rec.get("subject_id") in requested
            line_plans.append({
                "line": idx, "kind": rec.get("kind"),
                "subject_id": rec.get("subject_id"),
                "order_id": rec.get("order_id"),
                "action": "drop" if drop else "keep",
            })
            if not drop:
                kept.append(line)
        cleaned = b"".join(kept)
        export_plans.append({
            "path": rel,
            "sha256": sha256_bytes(raw),
            "records": len(lines),
            "planned_sha256": sha256_bytes(cleaned),
            "planned_records": len(kept),
            "lines": line_plans,
        })

    return {
        "plan_id": uuid.uuid4().hex[:12],
        "created_at": now_iso(),
        "request_id": request.get("request_id"),
        "request_sha256": canonical_sha256(request),
        "holds_sha256": canonical_sha256(holds_doc),
        "database": manifest["database"],
        "subjects": subjects,
        "holds_ignored": ignored_holds,
        "untouched_fingerprint": untouched_fp,
        "exports": export_plans,
    }


def cmd_plan(args) -> int:
    workdir = Path(args.workdir)
    plan = build_plan(workdir)
    out = Path(args.plan_out) if args.plan_out else workdir / PLAN_NAME
    atomic_write(out, (json.dumps(plan, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))

    print(f"计划 {plan['plan_id']} 已写入 {out}（请求 {plan['request_id']}）")
    for subj in plan["subjects"]:
        sid = subj["subject_id"]
        print(f"  对象 {sid}: {subj['action']}")
        ca = subj["customer"]["action"]
        rev = subj["customer"].get("revision")
        print(f"    客户行: {ca}" + (f" (revision={rev})" if rev is not None else ""))
        for op in subj["orders"]:
            extra = ""
            if op["action"] == "retain_restricted":
                extra = (f" 保留令 reason={op['hold'].get('reason')}"
                         f" retain={op['hold'].get('retain')}"
                         f" 内容校验 {short(op['content_sha256'])}")
            print(f"    订单 {op['order_id']}: {op['action']}"
                  f" (revision={op['revision']}){extra}")
    for h in plan["holds_ignored"]:
        print(f"  保留令未生效（订单不属于本请求或不存在）: {h.get('order_id')}")
    for ex in plan["exports"]:
        dropped = sum(1 for l in ex["lines"] if l["action"] == "drop")
        print(f"  导出副本 {ex['path']}: 当前 {short(ex['sha256'])}，"
              f"剔除 {dropped}/{ex['records']} 行，清理后 {short(ex['planned_sha256'])}")
    print(f"  未请求对象指纹: {short(plan['untouched_fingerprint'])}")
    print("下一步：python3 erasure_tool.py apply --workdir " + str(workdir))
    return 0


# ---------------------------------------------------------------- apply

def load_journal(workdir: Path):
    p = workdir / JOURNAL_NAME
    return load_json(p) if p.exists() else None


def save_journal(workdir: Path, journal: dict) -> None:
    journal["updated_at"] = now_iso()
    atomic_write(workdir / JOURNAL_NAME,
                 (json.dumps(journal, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def ensure_audit_table(db: sqlite3.Connection) -> None:
    db.executescript(AUDIT_DDL)


def db_marker_exists(db: sqlite3.Connection, plan_id: str) -> bool:
    try:
        row = db.execute(
            "SELECT 1 FROM erasure_audit WHERE plan_id=?"
            " AND action='step_marker:database' LIMIT 1", (plan_id,)).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


def validate_db_basis(workdir: Path, plan: dict, db: sqlite3.Connection) -> list[str]:
    """apply 前复核计划依据：请求、保留令、行版本任一变化都拒绝旧计划。"""
    errors = []
    if canonical_sha256(load_json(workdir / "request.json")) != plan["request_sha256"]:
        errors.append("request.json 与计划时不一致")
    if canonical_sha256(load_json(workdir / "holds.json")) != plan["holds_sha256"]:
        errors.append("holds.json 与计划时不一致（保留令已新增/变更，需重新 plan）")
    for subj in plan["subjects"]:
        sid = subj["subject_id"]
        cp = subj["customer"]
        row = db.execute("SELECT revision FROM customers WHERE subject_id=?",
                         (sid,)).fetchone()
        if cp["action"] == "absent":
            if row is not None:
                errors.append(f"{sid}: 计划时客户不存在，现已出现")
        elif row is None:
            errors.append(f"{sid}: 客户行已不存在（计划依据 revision={cp['revision']}）")
        elif row[0] != cp["revision"]:
            errors.append(f"{sid}: 客户行版本 {row[0]} ≠ 计划依据 {cp['revision']}")
        for op in subj["orders"]:
            orow = db.execute("SELECT revision FROM orders WHERE order_id=?",
                              (op["order_id"],)).fetchone()
            if orow is None:
                errors.append(f"{op['order_id']}: 订单行已不存在")
            elif orow[0] != op["revision"]:
                errors.append(f"{op['order_id']}: 订单行版本 {orow[0]}"
                              f" ≠ 计划依据 {op['revision']}")
    return errors


def audit(db, plan, subject_id, order_id, action, hold_reason=None, detail=None):
    db.execute(
        "INSERT INTO erasure_audit(request_id,plan_id,subject_id,order_id,action,"
        "hold_reason,detail,created_at) VALUES (?,?,?,?,?,?,?,?)",
        (plan["request_id"], plan["plan_id"], subject_id, order_id, action,
         hold_reason, detail, now_iso()))


def apply_database(plan: dict, db: sqlite3.Connection) -> None:
    """单事务执行全部数据库动作；行版本作为乐观锁，任何并发变化都会失败回滚。"""
    try:
        db.execute("BEGIN IMMEDIATE")
        for subj in plan["subjects"]:
            sid = subj["subject_id"]
            for op in subj["orders"]:
                oid = op["order_id"]
                if op["action"] == "delete":
                    cur = db.execute(
                        "DELETE FROM orders WHERE order_id=? AND revision=?",
                        (oid, op["revision"]))
                    if cur.rowcount != 1:
                        raise StaleError(f"{oid}: 删除时行版本已变化，事务回滚")
                    audit(db, plan, sid, oid, "delete_order")
                elif op["action"] == "retain_restricted":
                    cur = db.execute(
                        "UPDATE orders SET restricted=1, revision=revision+1"
                        " WHERE order_id=? AND revision=?", (oid, op["revision"]))
                    if cur.rowcount != 1:
                        raise StaleError(f"{oid}: 受限标记时行版本已变化，事务回滚")
                    # 审计只记录保留令理由，不复制客户联系方式
                    audit(db, plan, sid, oid, "restrict_order",
                          hold_reason=op["hold"].get("reason"),
                          detail=json.dumps({"retain": op["hold"].get("retain")},
                                            ensure_ascii=False))
            cp = subj["customer"]
            if cp["action"] == "delete":
                cur = db.execute(
                    "DELETE FROM customers WHERE subject_id=? AND revision=?",
                    (sid, cp["revision"]))
                if cur.rowcount != 1:
                    raise StaleError(f"{sid}: 删除客户时行版本已变化，事务回滚")
                audit(db, plan, sid, None, "delete_customer")
            elif cp["action"] == "skeletonize":
                cur = db.execute(
                    "UPDATE customers SET name=NULL, email=NULL, phone=NULL,"
                    " restricted=1, revision=revision+1"
                    " WHERE subject_id=? AND revision=?", (sid, cp["revision"]))
                if cur.rowcount != 1:
                    raise StaleError(f"{sid}: 骨架化时行版本已变化，事务回滚")
                audit(db, plan, sid, None, "skeleton_customer")
        # 事务标记与数据同事务提交：崩溃后凭它识别“数据库步骤其实已完成”
        audit(db, plan, None, None, "step_marker:database")
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise


def build_cleaned_export(path: Path, entry: dict) -> bytes:
    """由当前文件（已校验等于计划时的原始内容）重推导清理结果。"""
    lines = path.read_bytes().splitlines(keepends=True)
    drop_idx = {l["line"] for l in entry["lines"] if l["action"] == "drop"}
    return b"".join(line for i, line in enumerate(lines) if i not in drop_idx)


def cmd_apply(args) -> int:
    workdir = Path(args.workdir)
    plan_path = Path(args.plan) if args.plan else workdir / PLAN_NAME
    plan = load_json(plan_path)
    crash_after = args.crash_after

    journal = load_journal(workdir)
    if journal is not None and journal.get("plan_id") != plan["plan_id"]:
        print(f"拒绝：{JOURNAL_NAME} 属于计划 {journal.get('plan_id')}，"
              f"与当前计划 {plan['plan_id']} 不一致；请人工核对后处理旧执行记录。")
        return 3
    if journal is None:
        journal = {
            "request_id": plan["request_id"],
            "plan_id": plan["plan_id"],
            "overall": "in_progress",
            "created_at": now_iso(),
            "steps": {"database": {"status": "pending"},
                      "export": {"status": "pending"}},
        }

    # ---- 步骤 1：数据库 -------------------------------------------------
    if journal["steps"]["database"]["status"] != "done":
        db = connect(workdir / plan["database"])
        try:
            ensure_audit_table(db)
            if db_marker_exists(db, plan["plan_id"]):
                # 上次在事务提交后、写执行记录前崩溃：以库内标记为准恢复
                journal["steps"]["database"] = {
                    "status": "done", "at": now_iso(), "recovered": True}
                save_journal(workdir, journal)
                print("database: 检测到库内事务标记，上次其实已提交，恢复为 DONE")
            else:
                errors = validate_db_basis(workdir, plan, db)
                if errors:
                    print("拒绝执行旧计划，依据已变化：")
                    for e in errors:
                        print(f"  - {e}")
                    print("请重新运行 plan 生成新计划。")
                    return 2
                save_journal(workdir, journal)  # 首个变更前落盘执行记录
                try:
                    apply_database(plan, db)
                except StaleError as e:
                    print(f"拒绝：{e}")
                    return 2
                if crash_after == "db_committed":
                    simulate_crash("db_committed")
                journal["steps"]["database"] = {"status": "done", "at": now_iso()}
                save_journal(workdir, journal)
                print("database: 删除/受限保留/骨架化已提交")
        finally:
            db.close()
    else:
        print("database: 已完成，跳过")

    # ---- 步骤 2：登记导出副本替换 ---------------------------------------
    if journal["steps"]["export"]["status"] != "done":
        files = journal["steps"]["export"].setdefault("files", {})
        for entry in plan["exports"]:
            finfo = files.get(entry["path"], {})
            if finfo.get("status") == "done":
                continue
            path = workdir / entry["path"]
            cur = sha256_file(path)
            if cur == entry["planned_sha256"]:
                # 上次在替换成功后、写执行记录前崩溃
                files[entry["path"]] = {"status": "done", "at": now_iso(),
                                        "recovered": True, "sha256": cur}
                save_journal(workdir, journal)
                print(f"export {entry['path']}: 文件已是目标内容，恢复为 DONE")
                continue
            if cur != entry["sha256"]:
                print(f"拒绝：{entry['path']} 校验和 {short(cur)} 与计划依据 "
                      f"{short(entry['sha256'])} 及目标值均不一致，"
                      f"疑似外部改动，停止（数据库步骤状态保持不变）。")
                journal["overall"] = "failed"
                save_journal(workdir, journal)
                return 3
            cleaned = build_cleaned_export(path, entry)
            if sha256_bytes(cleaned) != entry["planned_sha256"]:
                print("拒绝：重推导的清理结果与计划校验和不一致，停止。")
                return 3
            tmp = path.with_name(path.name + EXPORT_TMP_SUFFIX)
            with tmp.open("wb") as f:
                f.write(cleaned)
                f.flush()
                os.fsync(f.fileno())
            if crash_after == "export_tmp":
                simulate_crash("export_tmp")
            os.replace(tmp, path)  # 原子替换
            fsync_dir(path.parent)
            if crash_after == "export_replaced":
                simulate_crash("export_replaced")
            new_sha = sha256_file(path)
            if new_sha != entry["planned_sha256"]:
                print(f"拒绝：替换后校验和 {short(new_sha)} 与计划目标不一致。")
                journal["overall"] = "failed"
                save_journal(workdir, journal)
                return 3
            files[entry["path"]] = {"status": "done", "at": now_iso(),
                                    "from_sha256": cur, "to_sha256": new_sha}
            save_journal(workdir, journal)
            print(f"export {entry['path']}: 已原子替换 "
                  f"({short(cur)} -> {short(new_sha)})")
        journal["steps"]["export"]["status"] = "done"
        journal["steps"]["export"]["at"] = now_iso()
        save_journal(workdir, journal)
    else:
        print("export: 已完成，跳过")

    done_all = all(s.get("status") == "done" for s in journal["steps"].values())
    journal["overall"] = "completed" if done_all else "in_progress"
    save_journal(workdir, journal)
    print()
    print_report(workdir, plan, journal)
    return 0 if done_all else 1


# ---------------------------------------------------------------- read

def cmd_read(args) -> int:
    """普通业务读取入口：只暴露 restricted=0 的对象；已删除/受限一律不可见。"""
    workdir = Path(args.workdir)
    manifest = load_json(workdir / "manifest.json")
    if args.export:
        for rel in manifest.get("registered_exports", []):
            print(f"== 公共导出 {rel} ==")
            sys.stdout.write((workdir / rel).read_text(encoding="utf-8"))
        return 0
    db = connect(workdir / manifest["database"])
    try:
        if args.subject:
            sid = args.subject
            row = db.execute(
                "SELECT subject_id,name,email,phone FROM customers"
                " WHERE subject_id=? AND restricted=0", (sid,)).fetchone()
            if row is None:
                print(f"{sid}: 无普通业务记录（不存在、已删除或受限）")
                return 0
            print(f"客户 {row[0]} name={row[1]} email={row[2]} phone={row[3]}")
            for o in db.execute(
                    "SELECT order_id,amount_cents,note FROM orders"
                    " WHERE subject_id=? AND restricted=0 ORDER BY order_id",
                    (sid,)):
                print(f"  订单 {o[0]} amount_cents={o[1]} note={o[2]}")
            return 0
        customers = db.execute(
            "SELECT subject_id,name,email,phone FROM customers"
            " WHERE restricted=0 ORDER BY subject_id").fetchall()
        n_orders = 0
        for c in customers:
            print(f"客户 {c[0]} name={c[1]} email={c[2]} phone={c[3]}")
            for o in db.execute(
                    "SELECT order_id,amount_cents,note FROM orders"
                    " WHERE subject_id=? AND restricted=0 ORDER BY order_id",
                    (c[0],)):
                n_orders += 1
                print(f"  订单 {o[0]} amount_cents={o[1]} note={o[2]}")
        print(f"（普通视图共 {len(customers)} 名客户 / {n_orders} 笔订单；"
              f"已删除或受限对象不出现）")
        return 0
    finally:
        db.close()


# ---------------------------------------------------------------- status

def restricted_records(db: sqlite3.Connection):
    """剩余受限记录：仅列出标识与保留理由，不复制客户联系方式。"""
    try:
        orders = db.execute(
            "SELECT o.order_id, o.subject_id,"
            " (SELECT a.hold_reason FROM erasure_audit a"
            "   WHERE a.order_id=o.order_id AND a.action='restrict_order'"
            "   ORDER BY a.id DESC LIMIT 1)"
            " FROM orders o WHERE o.restricted=1 ORDER BY o.order_id").fetchall()
        skeletons = db.execute(
            "SELECT subject_id FROM customers WHERE restricted=1"
            " ORDER BY subject_id").fetchall()
    except sqlite3.OperationalError:
        orders = db.execute(
            "SELECT order_id, subject_id, NULL FROM orders"
            " WHERE restricted=1 ORDER BY order_id").fetchall()
        skeletons = db.execute(
            "SELECT subject_id FROM customers WHERE restricted=1"
            " ORDER BY subject_id").fetchall()
    return orders, skeletons


def print_report(workdir: Path, plan: dict, journal: dict | None) -> None:
    manifest = load_json(workdir / "manifest.json")
    db = connect(workdir / manifest["database"])
    try:
        marker = db_marker_exists(db, plan["plan_id"]) if plan else False
        print("各来源完成情况：")
        db_step = (journal or {}).get("steps", {}).get("database", {})
        db_state = "DONE" if (db_step.get("status") == "done" or marker) else "PENDING"
        note = ""
        if marker and db_step.get("status") != "done":
            note = "（依据库内事务标记，执行记录尚未更新）"
        print(f"  数据库 {manifest['database']}: {db_state}{note}")
        for sid in [s["subject_id"] for s in plan["subjects"]] if plan else []:
            crow = db.execute("SELECT restricted FROM customers WHERE subject_id=?",
                              (sid,)).fetchone()
            cstate = ("骨架保留(受限)" if crow[0] else "仍在(普通)") if crow else "已删除"
            o_total, o_restr = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(restricted),0) FROM orders"
                " WHERE subject_id=?", (sid,)).fetchone()
            print(f"    {sid}: 客户={cstate} 剩余订单={o_total} (受限={o_restr})")
        exports = {e["path"]: e for e in plan["exports"]} if plan else {}
        for rel in manifest.get("registered_exports", []):
            sha = sha256_file(workdir / rel)
            entry = exports.get(rel)
            if entry and sha == entry["planned_sha256"]:
                state = "DONE（已替换为清理后内容）"
            elif entry and sha == entry["sha256"]:
                state = "PENDING（仍是原始内容，待替换）"
            else:
                state = f"未知内容（{short(sha)}，与计划两侧均不符）"
            print(f"  导出副本 {rel}: {state}")
        tmp = [rel for rel in manifest.get("registered_exports", [])
               if (workdir / (rel + EXPORT_TMP_SUFFIX)).exists()]
        for rel in tmp:
            print(f"  提示：{rel}{EXPORT_TMP_SUFFIX} 为中断遗留临时文件，续办时会被覆盖")
        orders, skeletons = restricted_records(db)
        print("剩余受限记录（普通业务与公共导出均不可见）：")
        if not orders and not skeletons:
            print("  （无）")
        for oid, sid, reason in orders:
            print(f"  受限订单 {oid} (subject={sid}) 保留理由: {reason or '未记录'}")
        for (sid,) in skeletons:
            print(f"  骨架客户 {sid}（仅 subject_id，联系方式已清除）")
        overall = (journal or {}).get("overall", "not_started")
        print(f"总体状态: {overall}")
    finally:
        db.close()


def cmd_status(args) -> int:
    workdir = Path(args.workdir)
    plan_path = Path(args.plan) if args.plan else workdir / PLAN_NAME
    plan = load_json(plan_path) if plan_path.exists() else None
    journal = load_journal(workdir)
    if journal is None and plan is None:
        print("尚未生成计划或执行记录。")
        return 0
    print_report(workdir, plan, journal)
    return 0


# ---------------------------------------------------------------- verify

def cmd_verify(args) -> int:
    workdir = Path(args.workdir)
    plan_path = Path(args.plan) if args.plan else workdir / PLAN_NAME
    plan = load_json(plan_path)
    journal = load_journal(workdir)
    checks: list[tuple[bool, str]] = []

    def check(ok: bool, msg: str):
        checks.append((bool(ok), msg))

    db = connect(workdir / plan["database"])
    try:
        fk_violations = db.execute("PRAGMA foreign_key_check").fetchall()
        check(not fk_violations, f"外键完整性 foreign_key_check（违规 {len(fk_violations)} 条）")

        db_done = ((journal or {}).get("steps", {}).get("database", {})
                   .get("status") == "done") or db_marker_exists(db, plan["plan_id"])
        check(db_done, "数据库步骤已完成（执行记录或库内事务标记）")
        if db_done:
            for subj in plan["subjects"]:
                sid = subj["subject_id"]
                cp = subj["customer"]
                crow = db.execute(
                    "SELECT name,email,phone,restricted,revision FROM customers"
                    " WHERE subject_id=?", (sid,)).fetchone()
                if cp["action"] == "delete":
                    check(crow is None, f"{sid}: 客户行已删除")
                elif cp["action"] == "skeletonize":
                    check(crow is not None and crow[0] is None and crow[1] is None
                          and crow[2] is None and crow[3] == 1
                          and crow[4] == cp["revision"] + 1,
                          f"{sid}: 客户仅余 subject_id 骨架（联系方式已清除、受限）")
                for op in subj["orders"]:
                    oid = op["order_id"]
                    orow = db.execute(
                        "SELECT subject_id,amount_cents,note,restricted,revision"
                        " FROM orders WHERE order_id=?", (oid,)).fetchone()
                    if op["action"] == "delete":
                        check(orow is None, f"{oid}: 订单已删除")
                    else:
                        ok = (orow is not None and orow[3] == 1
                              and orow[4] == op["revision"] + 1
                              and canonical_sha256({
                                  "order_id": oid, "subject_id": orow[0],
                                  "amount_cents": orow[1], "note": orow[2]})
                              == op["content_sha256"])
                        check(ok, f"{oid}: 订单原文受限保留且内容未变")
                        reason = db.execute(
                            "SELECT hold_reason FROM erasure_audit"
                            " WHERE order_id=? AND action='restrict_order'"
                            " ORDER BY id DESC LIMIT 1", (oid,)).fetchone()
                        check(reason is not None and reason[0],
                              f"{oid}: 审计已记录保留理由（不含客户联系方式）")

        if True:
            requested = [s["subject_id"] for s in plan["subjects"]]
            if requested:
                ph = ",".join("?" for _ in requested)
                rows_c = db.execute(
                    f"SELECT * FROM customers WHERE subject_id NOT IN ({ph})"
                    " ORDER BY subject_id", requested).fetchall()
                rows_o = db.execute(
                    f"SELECT * FROM orders WHERE subject_id NOT IN ({ph})"
                    " ORDER BY order_id", requested).fetchall()
            else:
                rows_c = db.execute("SELECT * FROM customers ORDER BY subject_id").fetchall()
                rows_o = db.execute("SELECT * FROM orders ORDER BY order_id").fetchall()
            check(canonical_sha256({"customers": rows_c, "orders": rows_o})
                  == plan["untouched_fingerprint"],
                  "未请求对象业务值不变（指纹一致）")

        restricted_ids = {r[0] for r in db.execute(
            "SELECT order_id FROM orders WHERE restricted=1")}
    finally:
        db.close()

    requested_set = {s["subject_id"] for s in plan["subjects"]}
    for entry in plan["exports"]:
        path = workdir / entry["path"]
        sha = sha256_file(path)
        check(sha == entry["planned_sha256"],
              f"{entry['path']}: 内容校验和 == 计划清理目标 {short(entry['planned_sha256'])}")
        leaked_subj, leaked_order = set(), set()
        for line in path.read_bytes().splitlines():
            rec = json.loads(line)
            if rec.get("subject_id") in requested_set:
                leaked_subj.add(rec.get("subject_id"))
            if rec.get("order_id") in restricted_ids:
                leaked_order.add(rec.get("order_id"))
        check(not leaked_subj, f"{entry['path']}: 不含已删除/受限对象（{sorted(leaked_subj) or '无泄漏'}）")
        check(not leaked_order, f"{entry['path']}: 不含受限订单原文（{sorted(leaked_order) or '无泄漏'}）")

    ok_all = True
    for ok, msg in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {msg}")
        ok_all = ok_all and ok
    print(f"verify: {'全部通过' if ok_all else '存在失败项'}（共 {len(checks)} 项）")
    return 0 if ok_all else 1


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="生成可复查计划（只读）")
    p.add_argument("--workdir", required=True)
    p.add_argument("--plan-out", default=None)
    p.set_defaults(fn=cmd_plan)

    p = sub.add_parser("apply", help="复核依据并分步执行，可断点续办")
    p.add_argument("--workdir", required=True)
    p.add_argument("--plan", default=None)
    p.add_argument("--crash-after",
                   choices=["db_committed", "export_tmp", "export_replaced"],
                   default=None, help="测试钩子：在指定阶段后模拟进程崩溃")
    p.set_defaults(fn=cmd_apply)

    p = sub.add_parser("read", help="普通业务读取入口（不含已删除/受限对象）")
    p.add_argument("--workdir", required=True)
    p.add_argument("--subject", default=None)
    p.add_argument("--export", action="store_true", help="显示公共导出文件内容")
    p.set_defaults(fn=cmd_read)

    p = sub.add_parser("status", help="报告各来源真实完成状态与剩余受限记录")
    p.add_argument("--workdir", required=True)
    p.add_argument("--plan", default=None)
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("verify", help="依据计划核对数据库、外键与导出内容")
    p.add_argument("--workdir", required=True)
    p.add_argument("--plan", default=None)
    p.set_defaults(fn=cmd_verify)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
