"""Unit tests for the crash/retry decision path using a stub PTY client.

The fixture always commits before the simulated drop, so the "old values still
unchanged" branch cannot arise naturally.  These tests drive the orchestrator's
decision code directly with a stub client to prove the policy behaves.
"""
from __future__ import annotations

from types import SimpleNamespace

from terminal_assistant.client import EditOutcome, QueryOutcome
from terminal_assistant.orchestrator import Assistant
from terminal_assistant.protocol import ScreenState


def state(name, unit, price, rev) -> ScreenState:
    return ScreenState(
        lines=(),
        title="查询结果",
        record={"编码": "A001", "名称": name, "单位": unit, "价格": price, "修订号": str(rev)},
    )


def make_args(**over):
    defaults = dict(
        app="fixtures/legacy_app.py", db="x.sqlite", faults="f.json", cwd=None,
        raw_dir=None, report=None, retry_policy="safe-old-values",
        max_crash_retries=1, query_attempts=2, retry_delay=0.0, settle=0.15,
        read_timeout=10.0, csv="c.csv", plan_only=False, yes=True, resume=None,
    )
    defaults.update(over)
    return SimpleNamespace(**defaults)


class StubClient:
    def __init__(self, queries, edits=None):
        self.queries = list(queries)
        self.edits = list(edits or [])
        self.sent = []

    def query(self, code, prefix="query"):
        self.sent.append(("query", prefix, code))
        return self.queries.pop(0)

    def edit_and_save(self, plan, pre, prefix="edit"):
        self.sent.append(("edit", prefix))
        return self.edits.pop(0)


def build_assistant(policy, queries, edits=None):
    assistant = Assistant(make_args(retry_policy=policy))
    client = StubClient(queries, edits)
    session = SimpleNamespace()

    def open_session(label):
        return session, client

    assistant.open_session = open_session  # type: ignore[assignment]
    assistant.quit_client = lambda c: True  # type: ignore[assignment]
    assistant.close_session = lambda s: None  # type: ignore[assignment]
    return assistant, client


def entry_for():
    return {
        "code": "A001",
        "current": {"name": "铅笔", "unit": "支", "price": "1.20", "revision": 1},
        "status": "save_unconfirmed",
        "needs_followup": True,
    }


def row():
    return SimpleNamespace(code="A001", name="中华铅笔", unit="支", price_text="1.50")


def test_old_unchanged_with_manual_policy_declines_retry():
    assistant, client = build_assistant(
        "manual", [QueryOutcome("result", state("铅笔", "支", "1.20", 1))]
    )
    entry = entry_for()
    assistant.crash_readback(row(), entry)
    assert entry["status"] == "retry_declined"
    assert entry["needs_followup"] is True
    # No edit attempt was made.
    assert not any(item[0] == "edit" for item in client.sent)


def test_old_unchanged_then_retry_success_is_confirmed_after_retry():
    pre_ok = QueryOutcome("result", state("铅笔", "支", "1.20", 1))
    retry_pre = QueryOutcome("result", state("铅笔", "支", "1.20", 1))
    retry_readback = QueryOutcome("result", state("中华铅笔", "支", "1.50", 2))
    success = EditOutcome("success", state("中华铅笔", "支", "1.50", 1), at_main=True)
    assistant, client = build_assistant(
        "safe-old-values",
        [pre_ok, retry_pre, retry_readback],
        edits=[success],
    )
    entry = entry_for()
    assistant.crash_readback(row(), entry)
    assert entry["status"] == "confirmed_after_retry"
    assert entry["needs_followup"] is False
    assert entry["retry_readback"]["record"]["名称"] == "中华铅笔"
    # Retried exactly once.
    assert sum(1 for item in client.sent if item[0] == "edit") == 1


def test_retry_precondition_changed_becomes_conflict():
    pre_ok = QueryOutcome("result", state("铅笔", "支", "1.20", 1))
    # Between readback and retry, someone changed the value and revision.
    retry_pre = QueryOutcome("result", state("铅笔-外部", "支", "1.20", 2))
    assistant, client = build_assistant("safe-old-values", [pre_ok, retry_pre])
    entry = entry_for()
    assistant.crash_readback(row(), entry)
    assert entry["status"] == "conflict_after_disconnect"
    assert entry["needs_followup"] is True
    assert not any(item[0] == "edit" for item in client.sent)  # never saved


def test_readback_plan_match_confirms_current_without_retry():
    assistant, client = build_assistant(
        "safe-old-values", [QueryOutcome("result", state("中华铅笔", "支", "1.50", 2))]
    )
    entry = entry_for()
    assistant.crash_readback(row(), entry)
    assert entry["status"] == "confirmed_current"
    assert entry["needs_followup"] is False
    assert not any(item[0] == "edit" for item in client.sent)


def test_readback_unreadable_is_unknown_state():
    assistant, client = build_assistant("safe-old-values", [QueryOutcome("not_found")])
    entry = entry_for()
    assistant.crash_readback(row(), entry)
    assert entry["status"] == "unknown_state"
    assert entry["needs_followup"] is True
