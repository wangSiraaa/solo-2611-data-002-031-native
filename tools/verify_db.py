"""Independent database/history verifier.

This program is NOT imported or called by the assistant.  It inspects the
SQLite items/history tables directly, purely to audit an assistant run after
the fact.  It reports observed rows; it does not assert exactly-once delivery.
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path


def load_changes(path: str) -> list[dict]:
    rows = []
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        for record in csv.DictReader(handle):
            try:
                price = Decimal(record["price"])
            except (InvalidOperation, TypeError, KeyError):
                price = None
            rows.append({
                "code": record.get("catalog_code", ""),
                "name": record.get("name", ""),
                "unit": record.get("unit", ""),
                "price": price,
                "price_text": record.get("price", ""),
                "expected_old_name": record.get("expected_old_name", ""),
            })
    return rows


def inspect(db_path: str | Path, changes: list[dict], baseline: dict | None = None) -> dict:
    """Read-only snapshot of items/history plus per-code plan comparison."""
    path = Path(db_path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        items = {
            row["code"]: dict(row)
            for row in conn.execute("SELECT code,name,unit,price,revision FROM items")
        }
        history = [dict(row) for row in conn.execute(
            "SELECT seq,code,name,unit,price,revision FROM history ORDER BY seq"
        )]
    finally:
        conn.close()
    baseline = baseline or {}
    results = []
    for change in changes:
        code = change["code"]
        current = items.get(code)
        writes = [h for h in history if h["code"] == code]
        plan_writes = [
            h for h in writes
            if h["name"] == change["name"]
            and h["unit"] == change["unit"]
            and price_equal(h["price"], change["price"])
        ]
        plan_matches = bool(current) and (
            current["name"] == change["name"]
            and current["unit"] == change["unit"]
            and price_equal(current["price"], change["price"])
        )
        base = baseline.get(code)
        unchanged = bool(current and base) and (
            current["name"] == base.get("name")
            and current["unit"] == base.get("unit")
            and price_equal(current["price"], base.get("price"))
            and current["revision"] == base.get("revision")
        )
        results.append({
            "code": code,
            "exists": current is not None,
            "current": current,
            "plan_matches_current": plan_matches,
            "history_writes": writes,
            "plan_valued_history_writes": plan_writes,
            "plan_valued_write_count": len(plan_writes),
            "unchanged_from_baseline": unchanged if base else None,
        })
    return {
        "items": results,
        "all_codes": sorted(items),
        "history_total": len(history),
    }


def price_equal(left: str | None, right: Decimal | None) -> bool:
    if left is None or right is None:
        return False
    try:
        return Decimal(str(left).strip()) == right
    except InvalidOperation:
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="独立校验 SQLite 现值与写入历史")
    ap.add_argument("--db", required=True)
    ap.add_argument("--csv", required=True, help="计划用的 CSV（用于对比目标值）")
    ap.add_argument("--baseline", help="查询前基线 JSON（可选，来自独立快照）")
    ap.add_argument("--json", dest="json_out", help="输出 JSON 路径")
    ap.add_argument("--expect", choices=("any", "applied", "unchanged"), default="any",
                    help="applied=要求现值等于计划值；unchanged=要求仍为基线值")
    args = ap.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"数据库不存在: {db_path}", file=sys.stderr)
        return 2

    baseline = {}
    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        if "items" in baseline:
            baseline = {item["code"]: item for item in baseline["items"]}

    changes = load_changes(args.csv)
    try:
        snapshot = inspect(db_path, changes, baseline)
    except sqlite3.Error as exc:
        print(f"读取数据库失败: {exc}", file=sys.stderr)
        return 2

    ok = True
    for entry in snapshot["items"]:
        if args.expect == "applied" and not entry["plan_matches_current"]:
            ok = False
        if args.expect == "unchanged":
            if entry["unchanged_from_baseline"] is False:
                ok = False
            if entry["plan_valued_write_count"]:
                ok = False

    output = {
        "verifier": "independent-sqlite/v1",
        "db": str(db_path.resolve()),
        "note": "写入历史条数不等于恰好一次写入证明；仅描述已提交记录。",
        "items": snapshot["items"],
        "all_codes": snapshot["all_codes"],
        "history_total": snapshot["history_total"],
        "expect": args.expect,
        "expectation_ok": ok,
    }
    text = json.dumps(output, ensure_ascii=False, indent=2)
    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
