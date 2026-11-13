"""End-to-end tests driving the real fixture through a real PTY.

The assistant under test never touches SQLite; these tests use the independent
verifier (tools/verify_db.py) to audit results, exactly as a separate program.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from verify_db import inspect, load_changes  # noqa: E402

from terminal_assistant.cli import main as cli_main  # noqa: E402

ALL_FAULTS = (
    "drop_after_save_once",
    "reject_save_once",
    "conflict_before_save_once",
    "unknown_prompt_once",
    "query_unavailable_once",
)
CSV = ROOT / "fixtures" / "changes.csv"


def prepare(tmp_path: Path, **faults) -> dict:
    config = {name: False for name in ALL_FAULTS}
    config.update(faults)
    from pathlib import Path as P
    P(tmp_path / "faults-run.json").write_text(json.dumps(config), encoding="utf-8")
    return config


def run_assistant(tmp_path: Path, csv: Path = CSV, extra: list[str] | None = None) -> tuple[int, dict]:
    report = tmp_path / "report.json"
    argv = [
        "--csv", str(csv),
        "--db", str(tmp_path / "legacy.sqlite"),
        "--faults", str(tmp_path / "faults-run.json"),
        "--app", str(ROOT / "fixtures" / "legacy_app.py"),
        "--report", str(report),
        "--raw-dir", str(tmp_path / "raw"),
        "--yes",
        "--settle", "0.15",
        *(extra or []),
    ]
    code = cli_main(argv)
    data = json.loads(report.read_text(encoding="utf-8"))
    return code, data


def by_code(report: dict) -> dict:
    return {item["code"]: item for item in report["items"]}


def db_state(tmp_path: Path) -> dict:
    return inspect(tmp_path / "legacy.sqlite", load_changes(str(CSV)))


def test_happy_path_confirmed_and_verified(tmp_path):
    prepare(tmp_path)
    code, report = run_assistant(tmp_path)
    assert code == 0
    items = by_code(report)
    assert items["A001"]["status"] == "confirmed"
    assert items["A002"]["status"] == "confirmed"
    assert items["A001"]["readback_values"] == {
        "name": "中华铅笔", "unit": "支", "price": "1.50", "revision": 2,
    }
    snap = db_state(tmp_path)
    assert all(i["plan_matches_current"] for i in snap["items"])
    # Every item was written exactly once *in this observed history*.
    assert all(i["plan_valued_write_count"] == 1 for i in snap["items"])
    # Report records plan, actions, feedback, readback, disposition.
    a1 = items["A001"]
    assert a1["plan"] and a1["current"] and a1["actions"]
    assert a1["save_feedback"].startswith("保存成功")


def test_unchanged_record_is_not_saved(tmp_path):
    csv = tmp_path / "unchanged.csv"
    csv.write_text(
        "catalog_code,name,unit,price,expected_old_name\n"
        "A001,铅笔,支,1.20,铅笔\n",
        encoding="utf-8",
    )
    prepare(tmp_path)
    code, report = run_assistant(tmp_path, csv=csv)
    assert code == 0
    assert by_code(report)["A001"]["status"] == "unchanged"
    snap = inspect(tmp_path / "legacy.sqlite", load_changes(str(csv)))
    assert snap["items"][0]["plan_valued_write_count"] == 0
    assert snap["history_total"] == 0


def test_old_name_mismatch_stops_at_query(tmp_path):
    csv = tmp_path / "mismatch.csv"
    csv.write_text(
        "catalog_code,name,unit,price,expected_old_name\n"
        "A001,中华铅笔,支,1.50,错名称\n"
        "A002,绿茶包,盒,20.00,茶包\n",
        encoding="utf-8",
    )
    prepare(tmp_path)
    code, report = run_assistant(tmp_path, csv=csv)
    items = by_code(report)
    assert items["A001"]["status"] == "old_name_mismatch"
    assert any("A001" in b for b in report["blockers"])
    # A002 still proceeds; A001 must be untouched.
    snap = {i["code"]: i for i in inspect(tmp_path / "legacy.sqlite", load_changes(str(csv)))["items"]}
    assert snap["A001"]["plan_valued_write_count"] == 0
    assert snap["A001"]["current"]["name"] == "铅笔"
    assert snap["A002"]["plan_matches_current"]


def test_not_found_code_stops(tmp_path):
    csv = tmp_path / "nf.csv"
    csv.write_text(
        "catalog_code,name,unit,price,expected_old_name\n"
        "ZZZZ,不存在,支,1.00,无\n",
        encoding="utf-8",
    )
    prepare(tmp_path)
    code, report = run_assistant(tmp_path, csv=csv)
    assert by_code(report)["ZZZZ"]["status"] == "not_found"
    snap = inspect(tmp_path / "legacy.sqlite", load_changes(str(csv)))
    assert snap["history_total"] == 0


def test_conflict_requeries_and_does_not_overwrite(tmp_path):
    prepare(tmp_path, conflict_before_save_once=True)
    code, report = run_assistant(tmp_path)
    a1 = by_code(report)["A001"]
    assert a1["status"] == "conflict"
    assert "保存冲突" in a1["save_feedback"]
    assert a1["requery_values"]["name"] == "铅笔-外部修改"
    assert a1["requery_values"]["revision"] == 2
    snap = {i["code"]: i for i in db_state(tmp_path)["items"]}
    assert snap["A001"]["plan_valued_write_count"] == 0
    assert snap["A001"]["current"]["name"] == "铅笔-外部修改"


def test_unknown_popup_preserved_without_keys(tmp_path):
    prepare(tmp_path, unknown_prompt_once=True)
    code, report = run_assistant(tmp_path)
    a1 = by_code(report)["A001"]
    assert a1["status"] == "unknown_popup"
    assert a1["site_preserved"] is True
    assert a1.get("no_keys_after_stop") is True
    assert "E97" in a1["detail"] or "未知弹窗" in a1["detail"]
    # No further item was attempted after the unknown site.
    assert by_code(report)["A002"]["status"] == "not_attempted"
    # The recorded key sequence stops at the price submission: no Enter, B, S
    # or X was sent after the popup appeared.
    sent = [s["sent"] for s in a1["actions"][0]["screens"]]
    assert sent[-1] == "1.50"  # last key is the price, never a save/ack
    assert "s" not in sent and "b" not in sent and "x" not in sent
    # Nothing was written for A001.
    snap = {i["code"]: i for i in db_state(tmp_path)["items"]}
    assert snap["A001"]["plan_valued_write_count"] == 0
    # The last recorded screen still shows the popup (site preserved).
    last_screen = a1["actions"][0]["screens"][-1]["screen"]
    assert "异常弹窗" in last_screen


def test_save_rejected_reports_feedback(tmp_path):
    prepare(tmp_path, reject_save_once=True)
    code, report = run_assistant(tmp_path)
    a1 = by_code(report)["A001"]
    assert a1["status"] == "rejected"
    assert "拒绝" in a1["save_feedback"]
    snap = {i["code"]: i for i in db_state(tmp_path)["items"]}
    assert snap["A001"]["plan_valued_write_count"] == 0
    # Batch continued to the next item.
    assert by_code(report)["A002"]["status"] == "confirmed"


def test_drop_after_save_readback_confirms_current(tmp_path):
    prepare(tmp_path, drop_after_save_once=True)
    code, report = run_assistant(tmp_path)
    a1 = by_code(report)["A001"]
    assert a1["status"] == "confirmed_current"
    assert a1["crash_readback_values"]["name"] == "中华铅笔"
    assert a1["needs_followup"] is False
    # The report must not claim exactly-once.
    assert "不证明恰好一次写入" in a1["detail"]
    snap = {i["code"]: i for i in db_state(tmp_path)["items"]}
    assert snap["A001"]["plan_matches_current"]


def test_query_unavailable_retries_then_succeeds(tmp_path):
    prepare(tmp_path, query_unavailable_once=True)
    code, report = run_assistant(tmp_path)
    a1 = by_code(report)["A001"]
    assert a1["status"] == "confirmed"
    assert a1["prequery"]["attempts"] == 2


def test_plan_only_makes_no_edits(tmp_path):
    prepare(tmp_path)
    code, report = run_assistant(tmp_path, extra=["--plan-only"])
    assert code == 0
    assert report["approved"] is False
    snap = db_state(tmp_path) if (tmp_path / "legacy.sqlite").exists() else {"history_total": 0}
    assert snap["history_total"] == 0


def test_invalid_csv_never_launches_terminal(tmp_path):
    csv = tmp_path / "bad.csv"
    csv.write_text(
        "catalog_code,name,unit,price,expected_old_name\n"
        "A001,中华铅笔,支,-1,铅笔\n",
        encoding="utf-8",
    )
    prepare(tmp_path)
    code = cli_main([
        "--csv", str(csv),
        "--db", str(tmp_path / "legacy.sqlite"),
        "--faults", str(tmp_path / "faults-run.json"),
        "--report", str(tmp_path / "report.json"),
        "--yes",
    ])
    assert code == 2
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["validation_errors"]
    assert not (tmp_path / "legacy.sqlite").exists()  # never launched the app


def test_resume_only_processes_unconfirmed(tmp_path):
    prepare(tmp_path, conflict_before_save_once=True)
    _, first = run_assistant(tmp_path)
    assert by_code(first)["A001"]["status"] == "conflict"
    assert by_code(first)["A002"]["status"] == "confirmed"
    # Second run resumes from the first report on the same DB.
    report2 = tmp_path / "resume-report.json"
    cli_main([
        "--csv", str(CSV),
        "--db", str(tmp_path / "legacy.sqlite"),
        "--faults", str(tmp_path / "faults-run.json"),
        "--app", str(ROOT / "fixtures" / "legacy_app.py"),
        "--report", str(report2),
        "--yes", "--settle", "0.15",
        "--resume", str(tmp_path / "report.json"),
    ])
    second = json.loads(report2.read_text(encoding="utf-8"))
    assert second["resume_codes"] == ["A001"]
    # A001 now has an unexpected old name, so it stops at query rather than overwriting.
    assert by_code(second)["A001"]["status"] == "old_name_mismatch"
