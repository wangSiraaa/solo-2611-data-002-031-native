"""Orchestration: validate CSV, pre-query via PTY, plan approval, edit, readback.

The assistant never opens the application database.  It observes only the
emulated screen and sends line-terminated keys through the PTY.
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import uuid

from .client import TerminalClient, record_query, _quit_main
from .csvplan import ChangeRow, PlanError, load_changes
from .reconcile import classify_post_disconnect, classify_post_save
from .terminal import TerminalSession

# Final dispositions that need no further work.
CONFIRMED_STATUSES = {
    "confirmed",
    "confirmed_current",
    "confirmed_after_retry",
    "unchanged",
}


@dataclass
class CurrentSnapshot:
    code: str
    kind: str
    record: dict[str, str] = field(default_factory=dict)
    revision: int | None = None
    detail: str = ""
    attempts: int = 0
    actions: list[dict] = field(default_factory=list)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def price_str(value: str) -> Decimal | None:
    try:
        parsed = Decimal(value.strip())
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def diff_fields(row: ChangeRow, current: dict[str, str]) -> dict:
    diff = {}
    for key, planned in (("name", row.name), ("unit", row.unit), ("price", row.price_text)):
        old = current.get(key, "")
        if key == "price":
            old_dec = price_str(old)
            new_dec = row.price
            changed = old_dec is None or old_dec != new_dec
        else:
            changed = old != planned
        diff[key] = {"old": old, "new": planned, "changed": changed}
    return diff


class Assistant:
    def __init__(self, args) -> None:
        self.args = args
        self.app_argv = [
            sys.executable,
            str(Path(args.app)),
            "--db",
            str(Path(args.db)),
            "--faults",
            str(Path(args.faults)),
        ]
        self.cwd = args.cwd
        self.run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
        self.write_root = Path(args.raw_dir) if args.raw_dir else None
        if self.write_root:
            self.write_root.mkdir(parents=True, exist_ok=True)
        self.sessions: list[TerminalSession] = []
        self.report_items: list[dict] = []
        self.parked_session: TerminalSession | None = None
        self.session_counter = 0
        self._dumped: set[int] = set()

    # ------------------------------------------------------------------ session
    def open_session(self, label: str):
        self.session_counter += 1
        session = TerminalSession(
            self.app_argv,
            cwd=self.cwd,
            cols=80,
            rows=24,
            settle_seconds=self.args.settle,
            read_timeout=self.args.read_timeout,
        )
        session._label = label  # type: ignore[attr-defined]
        session._raw_name = f"{self.session_counter:02d}-{label}-{uuid.uuid4().hex[:8]}.ansi"  # type: ignore[attr-defined]
        client = TerminalClient(session)
        obs = client.start()
        if obs.eof or obs.timed_out or not obs.state.is_main_menu:
            self._dump_raw(session)
            session.terminate()
            raise RuntimeError(f"无法进入主菜单（{label}）: eof={obs.eof} timeout={obs.timed_out}")
        self.sessions.append(session)
        return session, client

    def close_session(self, session: TerminalSession) -> None:
        self._dump_raw(session)
        session.terminate()

    def _dump_raw(self, session: TerminalSession) -> None:
        if not self.write_root:
            return
        name = getattr(session, "_raw_name", None)
        if not name or id(session) in self._dumped:
            return
        path = self.write_root / name
        try:
            path.write_bytes(bytes(session.raw_log))
            self._dumped.add(id(session))
        except OSError:
            pass

    def quit_client(self, client: TerminalClient) -> bool:
        """Exit via visible keys only.  Never touch an unknown popup/edit page."""
        state = client.state
        if state.unknown_popup:
            self.parked_session = client.session
            return False
        steps = 0
        while steps < 4:
            steps += 1
            state = client.state
            if state.is_main_menu:
                return _quit_main(client)
            if state.is_query_result:
                obs = client._send_and_observe("b", lambda s: s.is_main_menu, "quit:b")
                if obs.eof or not obs.state.is_main_menu:
                    return False
                continue
            if any(h in state.row22 for h in ("回车返回",)):
                ok, _ = client.ack_to_main("quit:ack")
                if not ok:
                    return False
                continue
            return False
        return False

    # ----------------------------------------------------------------- phase 1
    def prequery_item(self, row: ChangeRow) -> CurrentSnapshot:
        attempts = 0
        max_attempts = self.args.query_attempts
        actions: list[dict] = []
        last = CurrentSnapshot(code=row.code, kind="query_failed", detail="未开始")
        while attempts < max_attempts:
            attempts += 1
            session, client = self.open_session(f"prequery-{row.code}-{attempts}")
            last.attempts = attempts
            outcome = client.query(row.code, f"prequery{attempts}")
            actions = [a.as_dict() for a in client.actions]
            if outcome.kind == "result":
                state = outcome.state
                last = CurrentSnapshot(
                    code=row.code,
                    kind="result",
                    record=state.values or {},
                    revision=state.revision,
                    detail="已读取查询结果",
                    attempts=attempts,
                    actions=actions,
                )
                self.quit_client(client)
                self.close_session(session)
                return last
            if outcome.kind == "unavailable" and attempts < max_attempts:
                self.quit_client(client)
                self.close_session(session)
                time.sleep(self.args.retry_delay)
                continue
            if outcome.kind == "disconnected" and attempts < max_attempts:
                self.close_session(session)
                time.sleep(self.args.retry_delay)
                continue
            last = CurrentSnapshot(
                code=row.code,
                kind=outcome.kind,
                detail=outcome.detail,
                attempts=attempts,
                actions=actions,
            )
            self.quit_client(client)
            self.close_session(session)
            return last
        return last

    # ----------------------------------------------------------------- phase 2
    def build_plan(self, rows: list[ChangeRow], snapshots: dict[str, CurrentSnapshot]) -> tuple[list[dict], list[str]]:
        entries: list[dict] = []
        blockers: list[str] = []
        for row in rows:
            snap = snapshots[row.code]
            if snap.kind != "result":
                labels = {
                    "not_found": f"编码 {row.code} 查询不到（停在查询，不编辑）",
                    "unavailable": f"编码 {row.code} 查询暂不可用（不编辑）",
                    "disconnected": f"编码 {row.code} 查询时终端断开（不编辑）",
                    "timeout": f"编码 {row.code} 查询超时（不编辑）",
                    "unexpected": f"编码 {row.code} 查询出现未知屏幕（不编辑）",
                }
                blockers.append(labels.get(snap.kind, f"编码 {row.code} 查询失败: {snap.detail}"))
                entries.append({
                    "code": row.code,
                    "line_no": row.line_no,
                    "kind": snap.kind,
                    "status": snap.kind,
                    "detail": snap.detail,
                    "needs_followup": True,
                    "plan": {"name": row.name, "unit": row.unit, "price": row.price_text},
                })
                continue
            old_name = snap.record.get("name", "")
            name_matches = old_name == row.expected_old_name
            diff = diff_fields(row, snap.record)
            if not name_matches:
                blockers.append(
                    f"编码 {row.code} 预期旧名称不符: CSV={row.expected_old_name!r} 实际={old_name!r}（停在查询，不编辑）"
                )
            entry = {
                "code": row.code,
                "line_no": row.line_no,
                "expected_old_name": row.expected_old_name,
                "current": {
                    "name": snap.record.get("name"),
                    "unit": snap.record.get("unit"),
                    "price": snap.record.get("price"),
                    "revision": snap.revision,
                },
                "plan": {"name": row.name, "unit": row.unit, "price": row.price_text},
                "old_name_matches": name_matches,
                "diff": diff,
                "has_changes": any(v["changed"] for v in diff.values()),
                "prequery": {
                    "kind": snap.kind,
                    "attempts": snap.attempts,
                    "detail": snap.detail,
                    "actions": snap.actions,
                },
                "status": "pending",
                "needs_followup": True,
            }
            if not name_matches:
                entry["status"] = "old_name_mismatch"
                entry["detail"] = "预期旧名称与实际不一致；停在查询，未进入编辑"
            elif not entry["has_changes"]:
                entry["status"] = "unchanged"
                entry["detail"] = "当前值已等于计划值，无变化记录不保存"
                entry["needs_followup"] = False
            entries.append(entry)
        return entries, blockers

    # ----------------------------------------------------------------- phase 3
    def process_item(self, row: ChangeRow, entry: dict) -> dict:
        """Edit one record, then always read back after leaving the editor."""
        pre = {
            "name": entry["current"]["name"],
            "unit": entry["current"]["unit"],
            "price": entry["current"]["price"],
            "revision": entry["current"]["revision"],
        }
        attempts: list[dict] = []
        session, client = self.open_session(f"edit-{row.code}")
        try:
            # The edit page is reached from a query result.  Query first in the
            # same session and re-check the preconditions against this screen.
            q = client.query(row.code, f"edit-{row.code}-prequery")
            entry["edit_prequery"] = record_query(q)
            if q.kind != "result":
                entry["status"] = "unexpected" if q.kind not in ("not_found", "unavailable", "disconnected", "timeout") else q.kind
                entry["detail"] = f"编辑前查询未成功: {q.kind} {q.detail}；未进入编辑"
                entry["needs_followup"] = True
                self.quit_client(client)
                self.close_session(session)
                return entry
            cur = q.state
            cur_vals = cur.values or {}
            pre_matches = (
                cur_vals.get("name") == pre["name"]
                and cur_vals.get("unit") == pre["unit"]
                and price_str(cur_vals.get("price", "")) == price_str(pre["price"])
                and cur.revision == pre["revision"]
            )
            if not pre_matches:
                entry["status"] = "changed_before_edit"
                entry["detail"] = (
                    f"编辑前查询与计划基线不一致: 当前={cur.values} 修订号={cur.revision}，"
                    f"基线={pre}；未进入编辑，未覆盖写入"
                )
                entry["needs_followup"] = True
                entry["requery_values"] = {
                    "name": cur_vals.get("name"),
                    "unit": cur_vals.get("unit"),
                    "price": cur_vals.get("price"),
                    "revision": cur.revision,
                }
                self.quit_client(client)
                self.close_session(session)
                return entry

            outcome = client.edit_and_save(row, pre, f"edit-{row.code}")
            attempts.append({
                "attempt": 1,
                "kind": outcome.kind,
                "feedback": outcome.feedback,
                "detail": outcome.detail,
                "at_main": outcome.at_main,
                "screens": [a.as_dict() for a in outcome.steps],
            })
            entry["actions"] = attempts
            entry["save_feedback"] = outcome.feedback
            if outcome.kind in ("unknown_popup", "unexpected"):
                # Preserve the site: no further key, no B, no Enter, no signal.
                entry["screens"] = [a.as_dict() for a in outcome.steps]
                entry["no_keys_after_stop"] = True
                entry["status"] = "unknown_popup" if outcome.kind == "unknown_popup" else "stopped_unknown_screen"
                entry["detail"] = outcome.detail
                entry["needs_followup"] = True
                entry["site_preserved"] = True
                entry["halt_batch"] = True
                self.parked_session = session
                return entry
            if outcome.kind == "conflict":
                entry["status"] = "conflict"
                entry["detail"] = outcome.detail
                entry["needs_followup"] = True
                self._requery_for_report(client, row, entry, label="conflict-requery")
                self.quit_client(client)
                self.close_session(session)
                return entry
            if outcome.kind == "rejected":
                entry["status"] = "rejected"
                entry["detail"] = outcome.detail
                entry["needs_followup"] = True
                self.quit_client(client)
                self.close_session(session)
                return entry
            if outcome.kind == "field_rejected":
                entry["status"] = "field_rejected"
                entry["detail"] = outcome.detail
                entry["needs_followup"] = True
                self.quit_client(client)
                self.close_session(session)
                return entry
            if outcome.kind == "disconnected":
                entry["status"] = "save_unconfirmed"
                entry["detail"] = outcome.detail
                entry["needs_followup"] = True
                entry["disconnected"] = True
                # PTY is gone; park-by-loss (no further keys possible).
                self.parked_session = session
                return entry
            if outcome.kind == "timeout":
                entry["status"] = "stopped_no_feedback"
                entry["detail"] = outcome.detail
                entry["needs_followup"] = True
                entry["halt_batch"] = True
                self.parked_session = session
                return entry
            if outcome.kind == "aborted":
                entry["status"] = "aborted"
                entry["detail"] = outcome.detail
                entry["needs_followup"] = True
                self.quit_client(client)
                self.close_session(session)
                return entry
            if outcome.kind == "success":
                # Exit is already done (ack); re-query readback as required.
                before = len(client.actions)
                readback = client.query(row.code, "readback")
                entry["readback_actions"] = [a.as_dict() for a in client.actions[before:]]
                entry["readback"] = record_query(readback)
                if readback.kind == "result":
                    self._classify_readback_after_success(entry, row, pre, readback.state)
                else:
                    entry["status"] = "readback_unconfirmed"
                    entry["detail"] = f"保存成功提示后回读失败: {readback.kind} {readback.detail}"
                # Extra audit snapshot of the readback screen, then quit.
                self.quit_client(client)
                self.close_session(session)
                return entry
            entry["status"] = "stopped_unknown_screen"
            entry["detail"] = outcome.detail
            entry["needs_followup"] = True
            entry["halt_batch"] = True
            self.parked_session = session
            return entry
        except Exception as exc:
            entry["status"] = "stopped_error"
            entry["detail"] = f"自动化异常: {exc!r}"
            entry["needs_followup"] = True
            entry["halt_batch"] = True
            self.parked_session = session
            return entry

    def _requery_for_report(self, client, row, entry, label: str) -> None:
        outcome = client.query(row.code, label)
        entry["requery"] = record_query(outcome)
        if outcome.kind == "result":
            state = outcome.state
            values = state.values or {}
            entry["requery_values"] = {
                "name": values.get("name"),
                "unit": values.get("unit"),
                "price": values.get("price"),
                "revision": state.revision,
            }
            entry["detail"] = (entry.get("detail") or "") + "；已重新查询并记录现值，未覆盖写入"
        else:
            entry["detail"] = (entry.get("detail") or "") + f"；重新查询未成功: {outcome.kind} {outcome.detail}"

    def _classify_readback_after_success(self, entry, row, pre, state) -> None:
        values = state.values or {}
        entry["readback_values"] = {
            "name": values.get("name"),
            "unit": values.get("unit"),
            "price": values.get("price"),
            "revision": state.revision,
        }
        plan = {"name": row.name, "unit": row.unit, "price": row.price_text}
        status, detail = classify_post_save(pre, plan, values, state.revision)
        entry["status"] = status
        entry["detail"] = detail
        entry["needs_followup"] = status != "confirmed"

    # ------------------------------------------------------------ crash readback
    def crash_readback(self, row: ChangeRow, entry: dict) -> dict:
        """Restart a fresh terminal and read back the code after a lost success notice."""
        pre = dict(entry["current"])
        outcomes: list[dict] = []
        session, client = self.open_session(f"recover-{row.code}")
        outcome = client.query(row.code, "recover")
        outcomes.append(record_query(outcome))
        entry["crash_readback"] = outcomes
        if outcome.kind != "result":
            entry["status"] = "unknown_state"
            entry["detail"] = f"断线后按编码回读未取得可靠状态: {outcome.kind} {outcome.detail}"
            entry["needs_followup"] = True
            self.quit_client(client)
            self.close_session(session)
            return entry

        state = outcome.state
        values = state.values or {}
        entry["crash_readback_values"] = {
            "name": values.get("name"),
            "unit": values.get("unit"),
            "price": values.get("price"),
            "revision": state.revision,
        }
        plan = {"name": row.name, "unit": row.unit, "price": row.price_text}
        status, detail = classify_post_disconnect(pre, plan, values, state.revision)
        entry["detail"] = detail
        if status == "confirmed_current":
            entry["status"] = "confirmed_current"
            entry["needs_followup"] = False
            self.quit_client(client)
            self.close_session(session)
            return entry
        if status == "old_unchanged":
            entry["old_values_unchanged"] = True
            if self.args.retry_policy == "safe-old-values":
                entry["detail"] = detail + "；按 safe-old-values 策略允许重试"
                entry["retry_after_crash"] = self._retry_after_crash(row, entry, pre)
                self.quit_client(client)
                self.close_session(session)
                return entry
            entry["status"] = "retry_declined"
            entry["detail"] = detail + "；当前策略不允许重试，待人工处理"
            entry["needs_followup"] = True
            self.quit_client(client)
            self.close_session(session)
            return entry
        entry["status"] = "conflict_after_disconnect"
        entry["needs_followup"] = True
        self.quit_client(client)
        self.close_session(session)
        return entry

    def _retry_after_crash(self, row: ChangeRow, entry: dict, pre: dict) -> dict:
        attempts: list[dict] = []
        for attempt in range(1, self.args.max_crash_retries + 1):
            session, client = self.open_session(f"retry-{row.code}-{attempt}")
            q = client.query(row.code, f"retry{attempt}-pre")
            attempts.append({"attempt": attempt, "pre_query": record_query(q)})
            if q.kind != "result":
                attempts[-1]["kind"] = q.kind
                self.quit_client(client)
                self.close_session(session)
                entry["status"] = "unknown_state"
                entry["detail"] = f"重试前查询失败: {q.kind} {q.detail}"
                entry["needs_followup"] = True
                return {"attempts": attempts}
            # Re-check the retry precondition against the fresh screen.
            st = q.state
            vals = st.values or {}
            old_match = (
                vals.get("name") == pre["name"]
                and vals.get("unit") == pre["unit"]
                and price_str(vals.get("price", "")) == price_str(pre["price"])
            )
            rev_same = st.revision == pre["revision"]
            if not (old_match and rev_same):
                attempts[-1]["kind"] = "precondition_changed"
                entry["status"] = "conflict_after_disconnect"
                entry["detail"] = (
                    "重试前再次查询发现值或修订号已变化 → 冲突；未覆盖写入"
                )
                entry["needs_followup"] = True
                self.quit_client(client)
                self.close_session(session)
                return {"attempts": attempts}
            outcome = client.edit_and_save(row, pre, f"retry{attempt}")
            attempts[-1].update({
                "kind": outcome.kind,
                "feedback": outcome.feedback,
                "detail": outcome.detail,
                "screens": [a.as_dict() for a in outcome.steps],
            })
            if outcome.kind == "success":
                readback = client.query(row.code, f"retry{attempt}-readback")
                attempts[-1]["readback"] = record_query(readback)
                entry["retry_readback"] = record_query(readback)
                if readback.kind == "result":
                    self._classify_readback_after_success(entry, row, pre, readback.state)
                    if entry["status"] == "confirmed":
                        entry["status"] = "confirmed_after_retry"
                        entry["detail"] = "断线后旧值未变，按策略重试并退出编辑后回读一致，完成该条"
                        entry["needs_followup"] = False
                else:
                    entry["status"] = "readback_unconfirmed"
                    entry["detail"] = f"重试保存后回读失败: {readback.kind}"
                    entry["needs_followup"] = True
                self.quit_client(client)
                self.close_session(session)
                return {"attempts": attempts}
            if outcome.kind in ("conflict", "rejected", "field_rejected", "aborted"):
                entry["status"] = outcome.kind
                entry["detail"] = f"断线后重试得到 {outcome.kind}: {outcome.detail}"
                entry["needs_followup"] = True
                if outcome.kind == "conflict":
                    self._requery_for_report(client, row, entry, "retry-conflict-requery")
                self.quit_client(client)
                self.close_session(session)
                return {"attempts": attempts}
            if outcome.kind in ("unknown_popup", "unexpected", "timeout"):
                entry["status"] = "unknown_popup" if outcome.kind == "unknown_popup" else "stopped_unknown_screen"
                entry["detail"] = f"断线后重试: {outcome.detail}"
                entry["needs_followup"] = True
                entry["site_preserved"] = True
                entry["halt_batch"] = True
                self.parked_session = session
                return {"attempts": attempts}
            if outcome.kind == "disconnected":
                entry["status"] = "save_unconfirmed"
                entry["detail"] = "断线后重试再次在保存反馈前断开；结果未知，未再重试"
                entry["needs_followup"] = True
                self.parked_session = session
                return {"attempts": attempts}
        return {"attempts": attempts}

    # ---------------------------------------------------------------- run flow
    def run(self) -> int:
        report = {
            "schema": "legacy-pty-assistant/v1",
            "run_id": self.run_id,
            "started_at": now_iso(),
            "argv": sys.argv,
            "app_argv": self.app_argv,
            "csv": str(Path(self.args.csv).resolve()),
            "db": str(Path(self.args.db).resolve()),
            "faults": str(Path(self.args.faults).resolve()),
            "retry_policy_after_disconnect": self.args.retry_policy,
            "exactly_once_disclaimer": (
                "回读只能证明当前值/修订号，不能证明恰好一次写入；本报告不声称 exactly-once。"
            ),
            "items": [],
            "blockers": [],
        }
        try:
            rows, digest = load_changes(self.args.csv)
        except PlanError as exc:
            report["validation_errors"] = exc.errors
            self._write_report(report)
            print("CSV 校验失败，未连接终端：")
            for err in exc.errors:
                print(f"  - {err}")
            return 2
        report["csv_sha256"] = digest

        # Continuation: only work on items not already confirmed.
        resume_codes: set[str] | None = None
        if self.args.resume:
            resume_codes = self._load_resume(self.args.resume, report, digest)
            if resume_codes is not None:
                rows = [r for r in rows if r.code in resume_codes]
                report["resume_from"] = str(Path(self.args.resume).resolve())
                report["resume_codes"] = sorted(resume_codes)
                if not rows:
                    print("续办：没有未确认项需要处理。")
                    report["finished_at"] = now_iso()
                    self._write_report(report)
                    return 0

        print(f"校验通过：{len(rows)} 条变更（CSV sha256={digest[:16]}…）")
        print("预查询终端现值（只读，不进入编辑）…")
        snapshots: dict[str, CurrentSnapshot] = {}
        for row in rows:
            snap = self.prequery_item(row)
            snapshots[row.code] = snap
            label = "读取成功" if snap.kind == "result" else snap.kind
            print(f"  - {row.code}: {label}（尝试 {snap.attempts} 次）")

        entries, blockers = self.build_plan(rows, snapshots)
        # Freeze the approved plan so later edit/readback mutations do not
        # rewrite what the user actually approved.
        report["plan"] = json.loads(json.dumps(entries, ensure_ascii=False))
        report["blockers"] = blockers
        self._print_plan(entries, blockers)

        # Confirmation gate: edits only proceed after explicit approval.
        if self.args.plan_only:
            report["approved"] = False
            report["items"] = json.loads(json.dumps(entries, ensure_ascii=False))
            report["counts"] = self._counts(entries)
            report["finished_at"] = now_iso()
            self._write_report(report)
            print("已生成计划（--plan-only）；未进入编辑。")
            return 0
        if not self._confirm(blockers):
            report["approved"] = False
            report["finished_at"] = now_iso()
            self._write_report(report)
            print("未获得确认；未进入编辑。")
            return 3
        report["approved"] = True
        report["approved_at"] = now_iso()

        # Phase 3: edits with readback, per item.
        item_report_map = {e["code"]: e for e in entries}
        for row in rows:
            entry = item_report_map[row.code]
            if entry["status"] != "pending":
                continue
            print(f"编辑 {row.code} …")
            self.process_item(row, entry)
            print(f"  -> {entry['status']}: {entry.get('detail', '')}")
            if entry.get("halt_batch"):
                print("  已停止后续条目：未知现场/无反馈需保留，未再发送按键。")
                break

        # Items skipped because the batch halted keep an explicit disposition.
        for entry in entries:
            if entry["status"] == "pending":
                entry["status"] = "not_attempted"
                entry["detail"] = "批处理因前一条未知现场/无反馈而停止；本条未发送任何按键"
                entry["needs_followup"] = True

        # For post-save disconnects, restart terminal and read back by code.
        for row in rows:
            entry = item_report_map[row.code]
            if entry.get("status") == "save_unconfirmed" and entry.get("disconnected"):
                print(f"恢复 {row.code}: 重启终端按编码回读 …")
                self.crash_readback(row, entry)
                print(f"  -> {entry['status']}: {entry.get('detail', '')}")

        report["items"] = entries
        report["finished_at"] = now_iso()
        report["counts"] = self._counts(entries)
        self._write_report(report)
        self._print_summary(entries)
        return 0

    def _load_resume(self, path: str, report: dict, digest: str) -> set[str] | None:
        try:
            prior = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            report["resume_error"] = f"无法读取续办报告: {exc}"
            return None
        if prior.get("csv_sha256") and prior["csv_sha256"] != digest:
            report["resume_error"] = "续办报告与当前 CSV 的 sha256 不一致，拒绝续办"
            print("续办报告与当前 CSV 不一致；拒绝续办。")
            return None
        done = {
            item.get("code")
            for item in prior.get("items", [])
            if item.get("status") in CONFIRMED_STATUSES
        }
        all_codes = {item.get("code") for item in prior.get("items", [])}
        return all_codes - done

    def _confirm(self, blockers: list[str]) -> bool:
        if self.args.yes:
            print("已通过 --yes 确认计划（演示/自动化模式）。")
            return True
        if not sys.stdin.isatty():
            print("非交互环境且未提供 --yes；拒绝进入编辑。")
            return False
        try:
            answer = input("确认执行以上计划？输入 yes 进入编辑: ").strip().lower()
        except EOFError:
            return False
        return answer == "yes"

    def _print_plan(self, entries: list[dict], blockers: list[str]) -> None:
        print("\n计划与逐字段差异：")
        for e in entries:
            print(f"[{e['code']}] 行 {e['line_no']}  预期旧名称={e.get('expected_old_name')!r}")
            if e["status"] in ("not_found", "unavailable", "disconnected", "timeout", "unexpected", "query_failed"):
                print(f"  查询状态: {e['status']} — {e.get('detail', '')}")
                continue
            print(f"  当前: 名称={e['current']['name']!r} 单位={e['current']['unit']!r} 价格={e['current']['price']!r} 修订号={e['current']['revision']}")
            print(f"  计划: 名称={e['plan']['name']!r} 单位={e['plan']['unit']!r} 价格={e['plan']['price']!r}")
            for key, label in (("name", "名称"), ("unit", "单位"), ("price", "价格")):
                d = e["diff"][key]
                mark = "变更" if d["changed"] else "不变"
                print(f"    {label}: {d['old']!r} -> {d['new']!r} [{mark}]")
            if e["status"] != "pending":
                print(f"  处置: {e['status']} — {e.get('detail', '')}")
        if blockers:
            print("\n预期旧名称不符 / 查询不可用清单：")
            for b in blockers:
                print(f"  - {b}")
        else:
            print("\n预期旧名称不符清单：无")

    def _print_summary(self, entries: list[dict]) -> None:
        print("\n最终处置：")
        for e in entries:
            rb = e.get("readback_values") or e.get("crash_readback_values") or e.get("requery_values")
            print(f"  [{e['code']}] {e['status']} — {e.get('detail', '')}")
            if e.get("save_feedback"):
                print(f"    保存反馈: {e['save_feedback']}")
            if rb:
                print(f"    回读: {rb}")
        print("\n注意：回读只证明当前值/修订号，不证明恰好一次写入。")

    def _counts(self, entries: list[dict]) -> dict:
        counts: dict[str, int] = {}
        for e in entries:
            counts[e["status"]] = counts.get(e["status"], 0) + 1
        counts["needs_followup"] = sum(1 for e in entries if e.get("needs_followup"))
        return counts

    def _write_report(self, report: dict) -> None:
        if not self.args.report:
            return
        path = Path(self.args.report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"报告: {path}")

    def shutdown(self) -> None:
        parked = self.parked_session
        for session in self.sessions:
            self._dump_raw(session)
            try:
                if session is parked and session.is_alive():
                    # Preserve the live site untouched; do not signal it either.
                    continue
                if session.is_alive():
                    # Only SIGTERM; never send application keys at shutdown.
                    session.terminate()
            except Exception:
                pass
