"""End-to-end tests for the synthetic multi-currency ledger CLI.

Every correctness assertion reads the raw tables with independent SQL and
never calls ledger helper functions.  Concurrency tests exercise real
operating-system processes, not in-process threads, so the PostgreSQL
transaction/lock boundary is what is actually being tested.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from tests.helpers import (
    TEST_DSN,
    assert_raw_ledger_invariants,
    finish_cli,
    make_entry,
    post_args,
    query,
    reverse_args,
    run_cli,
    scalar,
    start_cli,
    wait_marker,
)


# ---------------------------------------------------------------------------
# Functional / semantic unit tests through the real CLI.
# ---------------------------------------------------------------------------
def test_multicurrency_entry_balances_each_currency(accounts):
    """A multi-currency entry posts only when every currency balances."""
    payload = make_entry(
        "mc-1", "2026-01-10",
        [
            ("cash-usd", "USD", 100, 0), ("revenue-usd", "USD", 0, 100),
            ("cash-eur", "EUR", 200, 0), ("revenue-eur", "EUR", 0, 200),
            ("cash-jpy", "JPY", 300, 0), ("revenue-jpy", "JPY", 0, 300),
        ],
    )
    rc, out, log = run_cli(post_args(payload))
    assert rc == 0, log
    assert out["line_count"] == 6
    assert_raw_ledger_invariants()

    # Raw per-currency entry totals each net to zero independently.
    diffs = {
        currency: int(diff)
        for currency, diff in query(
            """
            SELECT p.currency, (sum(p.debit_cents) - sum(p.credit_cents))::bigint
              FROM postings p JOIN entries e USING (entry_seq)
             WHERE e.entry_id = 'mc-1'
             GROUP BY p.currency
            """
        )
    }
    assert diffs == {"USD": 0, "EUR": 0, "JPY": 0}


def test_totals_equal_but_cross_currency_offset_is_rejected(accounts):
    """Equal overall debit/credit totals cannot hide a per-currency imbalance."""
    # Whole entry: 500 debit = 500 credit across all lines, but USD is +500
    # while EUR is -500, so each currency fails on its own.
    payload = make_entry(
        "cross-1", "2026-01-10",
        [
            ("cash-usd", "USD", 500, 0),
            ("revenue-usd", "USD", 0, 100),
            ("cash-eur", "EUR", 100, 0),
            ("revenue-eur", "EUR", 0, 500),
        ],
    )
    rc, out, _log = run_cli(post_args(payload))
    assert rc != 0
    assert out["ok"] is False
    assert out["error"] == "unbalanced"
    assert scalar("SELECT count(*) FROM entries WHERE entry_id='cross-1'") == 0
    assert scalar("SELECT count(*) FROM postings") == 0
    assert scalar("SELECT count(*) FROM requests") == 0


def test_same_currency_totals_cannot_net_other_currencies(accounts):
    """USD ledger and EUR ledger each need independent equality."""
    payload = make_entry(
        "cross-2", "2026-01-11",
        [
            ("cash-usd", "USD", 10, 0), ("cash-eur", "EUR", 0, 10),
        ],
    )
    rc, out, _ = run_cli(post_args(payload))
    assert rc != 0 and out["error"] == "unbalanced"
    assert scalar("SELECT count(*) FROM entries") == 0


def test_unknown_account_and_currency_mismatch_are_atomic(accounts):
    """A bad line fails the entire entry; no partial posting lands."""
    # Case 1: a balanced entry that references an undeclared account.
    payload = make_entry(
        "x-bad-unknown", "2026-01-12",
        [("cash-usd", "USD", 100, 0), ("revenue-usd", "USD", 0, 100),
         ("ghost-usd", "USD", 10, 0), ("cash-usd", "USD", 0, 10)],
    )
    rc, out, _ = run_cli(post_args(payload))
    assert rc != 0, out
    assert out["error"] == "unknown_account", out
    assert scalar("SELECT count(*) FROM entries") == 0
    assert scalar("SELECT count(*) FROM postings") == 0
    assert scalar("SELECT count(*) FROM requests") == 0

    # Case 2: a balanced entry whose declared currency differs from the
    # account's fixed currency.
    payload = make_entry(
        "x-bad-currency", "2026-01-12",
        [("cash-usd", "USD", 100, 0), ("revenue-usd", "USD", 0, 100),
         ("revenue-usd", "EUR", 0, 10), ("cash-eur", "EUR", 10, 0)],
    )
    rc, out, _ = run_cli(post_args(payload))
    assert rc != 0, out
    assert out["error"] == "account_currency_mismatch", out
    assert scalar("SELECT count(*) FROM entries") == 0
    assert scalar("SELECT count(*) FROM postings") == 0
    assert scalar("SELECT count(*) FROM requests") == 0


def test_identical_retry_returns_original_and_different_content_is_rejected(accounts):
    payload = make_entry(
        "idem-1", "2026-01-13",
        [("cash-usd", "USD", 77, 0), ("revenue-usd", "USD", 0, 77)],
    )
    rc1, out1, _ = run_cli(post_args(payload))
    rc2, out2, _ = run_cli(post_args(payload))
    assert rc1 == rc2 == 0
    assert out1 == out2
    assert scalar("SELECT count(*) FROM requests WHERE operation='post'") == 1

    changed = json.loads(json.dumps(payload))
    changed["lines"][0]["debit_cents"] = 78
    changed["lines"][1]["credit_cents"] = 78
    rc3, out3, _ = run_cli(post_args(changed))
    assert rc3 != 0
    assert out3["error"] == "entry_id_conflict"
    assert scalar("SELECT debit_cents FROM postings p JOIN entries e "
                 "USING (entry_seq) WHERE e.entry_id='idem-1' AND line_no=1") == 77

    # Key-order/whitespace-equivalent JSON is still the identical request.
    reordered = {
        "lines": [
            {"credit_cents": 0, "debit_cents": 77,
             "currency": "USD", "account_id": "cash-usd"},
            {"debit_cents": 0, "currency": "USD", "credit_cents": 77,
             "account_id": "revenue-usd"},
        ],
        "entry_date": "2026-01-13",
        "entry_id": "idem-1",
    }
    rc4, out4, _ = run_cli(post_args(reordered))
    assert rc4 == 0 and out4 == out1


def test_reverse_mirrors_and_is_single_use(accounts):
    payload = make_entry(
        "orig-1", "2026-01-14",
        [("cash-usd", "USD", 150, 0), ("revenue-usd", "USD", 0, 150),
         ("cash-eur", "EUR", 90, 0), ("revenue-eur", "EUR", 0, 90)],
    )
    assert run_cli(post_args(payload))[0] == 0
    rc, rev, _ = run_cli(reverse_args(
        {"entry_id": "rev-1", "reverses_entry_id": "orig-1",
         "entry_date": "2026-02-03"}
    ))
    assert rc == 0, rev
    assert rev["period"] == "2026-02"
    assert_raw_ledger_invariants()

    rc, out, _ = run_cli(reverse_args(
        {"entry_id": "rev-1-second", "reverses_entry_id": "orig-1",
         "entry_date": "2026-02-04"}
    ))
    assert rc != 0 and out["error"] == "already_reversed"
    rc, out, _ = run_cli(reverse_args(
        {"entry_id": "rev-of-rev", "reverses_entry_id": "rev-1",
         "entry_date": "2026-02-05"}
    ))
    assert rc != 0 and out["error"] == "not_reversible"

    # Exact retry of the successful reversal returns the original result.
    rc, again, _ = run_cli(reverse_args(
        {"entry_id": "rev-1", "reverses_entry_id": "orig-1",
         "entry_date": "2026-02-03"}
    ))
    assert rc == 0 and again == rev


def test_closed_original_reversed_in_open_period_preserves_history(accounts):
    payload = make_entry(
        "jan-orig", "2026-01-20",
        [("cash-usd", "USD", 400, 0), ("revenue-usd", "USD", 0, 400)],
    )
    assert run_cli(post_args(payload))[0] == 0
    rc, snap, _ = run_cli(["close", "--period", "2026-01"])
    assert rc == 0 and snap["entry_count"] == 1
    before = query(
        "SELECT period, snapshot_hash, entry_count, posting_count, snapshot::text "
        "FROM periods WHERE period='2026-01'"
    )
    before_rows = query(
        "SELECT account_id, currency, debit_cents, credit_cents, net_cents "
        "FROM period_snapshots WHERE period='2026-01' ORDER BY account_id"
    )

    # A later post in January must be rejected.
    rc, out, _ = run_cli(post_args(make_entry(
        "jan-late", "2026-01-31",
        [("cash-usd", "USD", 1, 0), ("revenue-usd", "USD", 0, 1)],
    )))
    assert rc != 0 and out["error"] == "period_closed"

    # Reversal lands in February.
    rc, rev, _ = run_cli(reverse_args(
        {"entry_id": "feb-rev-of-jan", "reverses_entry_id": "jan-orig",
         "entry_date": "2026-02-10"}
    ))
    assert rc == 0 and rev["period"] == "2026-02"

    after = query(
        "SELECT period, snapshot_hash, entry_count, posting_count, snapshot::text "
        "FROM periods WHERE period='2026-01'"
    )
    assert after == before
    assert query(
        "SELECT account_id, currency, debit_cents, credit_cents, net_cents "
        "FROM period_snapshots WHERE period='2026-01' ORDER BY account_id"
    ) == before_rows
    assert_raw_ledger_invariants()


def test_close_is_idempotent_and_snapshot_matches_raw(accounts):
    for idx in range(3):
        assert run_cli(post_args(make_entry(
            f"close-{idx}", f"2026-03-0{idx + 1}",
            [("cash-usd", "USD", 100 + idx, 0),
             ("revenue-usd", "USD", 0, 100 + idx)],
        )))[0] == 0
    rc1, first, _ = run_cli(["close", "--period", "2026-03"])
    rc2, second, _ = run_cli(["close", "--period", "2026-03"])
    assert rc1 == rc2 == 0
    assert first == second
    assert scalar("SELECT count(*) FROM periods WHERE period='2026-03'") == 1
    assert scalar(
        "SELECT count(*) FROM requests WHERE operation='close' "
        "AND closed_period='2026-03'"
    ) == 1
    assert_raw_ledger_invariants()


def test_append_only_enforced_inside_database(accounts):
    """Even raw SQL cannot mutate or delete committed ledger facts."""
    import psycopg

    assert run_cli(post_args(make_entry(
        "imm-1", "2026-05-01",
        [("cash-usd", "USD", 5, 0), ("revenue-usd", "USD", 0, 5)],
    )))[0] == 0
    assert run_cli(["close", "--period", "2026-05"])[0] == 0
    before = query("SELECT * FROM postings ORDER BY entry_seq, line_no")
    with psycopg.connect(TEST_DSN) as conn:
        for statement in [
            "UPDATE postings SET debit_cents = debit_cents",
            "DELETE FROM postings",
            "UPDATE entries SET entry_date = entry_date",
            "DELETE FROM periods",
            "UPDATE period_snapshots SET net_cents = net_cents",
            "UPDATE requests SET response_json = response_json",
        ]:
            with pytest.raises(psycopg.Error), conn.cursor() as cur:
                cur.execute(statement)
            conn.rollback()
    assert query("SELECT * FROM postings ORDER BY entry_seq, line_no") == before
    assert_raw_ledger_invariants()


def test_balance_rows_survive_rejected_posts(accounts):
    """A rejected entry leaves balances and the request log untouched."""
    before = query(
        "SELECT account_id, currency, debit_cents, credit_cents "
        "FROM account_balances ORDER BY account_id"
    )
    for payload, expected in [
        (make_entry("rej-unbalanced", "2026-11-01",
                    [("cash-usd", "USD", 1, 0),
                     ("revenue-usd", "USD", 0, 2)]), "unbalanced"),
        (make_entry("rej-unknown", "2026-11-01",
                    [("ghost", "USD", 1, 0),
                     ("revenue-usd", "USD", 0, 1)]), "unknown_account"),
        # Structurally invalid: both sides positive.
        ({"entry_id": "rej-shape", "entry_date": "2026-11-01",
          "lines": [{"account_id": "cash-usd", "currency": "USD",
                     "debit_cents": 1, "credit_cents": 1}]},
         "invalid_request"),
        # Negative amount.
        ({"entry_id": "rej-negative", "entry_date": "2026-11-01",
          "lines": [{"account_id": "cash-usd", "currency": "USD",
                     "debit_cents": -1, "credit_cents": 0}]},
         "invalid_request"),
    ]:
        rc, out, _ = run_cli(post_args(payload))
        assert rc != 0 and out["error"] == expected, out
    assert query(
        "SELECT account_id, currency, debit_cents, credit_cents "
        "FROM account_balances ORDER BY account_id"
    ) == before
    assert scalar("SELECT count(*) FROM requests") == 0
    assert scalar("SELECT count(*) FROM entries") == 0
    assert scalar("SELECT count(*) FROM postings") == 0


# ---------------------------------------------------------------------------
# Concurrency tests: real processes.
# ---------------------------------------------------------------------------
def test_same_entry_concurrent_retry_posts_once(accounts):
    payload = make_entry(
        "race-1", "2026-06-01",
        [("cash-usd", "USD", 321, 0), ("revenue-usd", "USD", 0, 321)],
    )
    start = threading.Barrier(4)
    results: list[tuple[int, dict | None, str]] = []
    lock = threading.Lock()

    def worker():
        start.wait()
        result = run_cli(post_args(payload))
        with lock:
            results.append(result)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(rc == 0 for rc, _o, _log in results), results
    first = results[0][1]
    assert all(out == first for _rc, out, _log in results), results
    assert scalar("SELECT count(*) FROM entries WHERE entry_id='race-1'") == 1
    assert scalar(
        "SELECT count(*) FROM requests WHERE operation='post'"
    ) == 1
    assert_raw_ledger_invariants()


def test_two_concurrent_reversals_yield_one_reversal(accounts):
    original = make_entry(
        "race-orig", "2026-06-02",
        [("cash-usd", "USD", 111, 0), ("revenue-usd", "USD", 0, 111),
         ("cash-eur", "EUR", 222, 0), ("revenue-eur", "EUR", 0, 222)],
    )
    assert run_cli(post_args(original))[0] == 0

    # Hold the advisory lock in process A long enough that B and C both queue
    # behind it, then release; exactly one may create a reversal.
    marker = Path("/tmp/ledger-test-rev-a-locked")
    marker.unlink(missing_ok=True)
    common = {"LEDGER_TEST_LOCK_MARKER": marker,
              "LEDGER_TEST_LOCK_HOLD_MS": 400}
    procs = {
        "a": start_cli(reverse_args(
            {"entry_id": "race-rev-a", "reverses_entry_id": "race-orig",
             "entry_date": "2026-06-10"}), common),
        "b": start_cli(reverse_args(
            {"entry_id": "race-rev-b", "reverses_entry_id": "race-orig",
             "entry_date": "2026-06-10"}), common),
        "c": start_cli(reverse_args(
            {"entry_id": "race-rev-c", "reverses_entry_id": "race-orig",
             "entry_date": "2026-06-10"}), common),
    }
    wait_marker(marker)
    outcomes = {name: finish_cli(proc) for name, proc in procs.items()}
    wins = [name for name, (rc, _o, _log) in outcomes.items() if rc == 0]
    losses = [name for name, (rc, out, _log) in outcomes.items() if rc != 0]
    assert len(wins) == 1, outcomes
    for name in losses:
        assert outcomes[name][1]["error"] == "already_reversed", outcomes[name]

    reversals = query(
        "SELECT entry_id FROM entries WHERE reverses_entry_id='race-orig'"
    )
    assert len(reversals) == 1 and reversals[0][0] == f"race-rev-{wins[0]}"
    assert_raw_ledger_invariants()


def test_close_and_last_entry_race_has_clear_boundary(accounts):
    """The final entry is either fully in the snapshot or rejected."""
    for idx in range(5):
        assert run_cli(post_args(make_entry(
            f"raceclose-{idx}", f"2026-07-0{idx + 1}",
            [("cash-usd", "USD", 10, 0), ("revenue-usd", "USD", 0, 10)],
        )))[0] == 0

    last = make_entry(
        "raceclose-last", "2026-07-31",
        [("cash-jpy", "JPY", 700, 0), ("revenue-jpy", "JPY", 0, 700)],
    )
    gate = Path("/tmp/ledger-test-close-gate")
    gate.unlink(missing_ok=True)
    close_marker = Path("/tmp/ledger-test-close-locked")
    close_marker.unlink(missing_ok=True)

    # Close holds the writer lock first, then the post waits behind it.  The
    # post must either land before the snapshot or be rejected, never land
    # after the snapshot was computed.
    close_proc = start_cli(
        ["close", "--period", "2026-07"],
        {"LEDGER_TEST_LOCK_MARKER": close_marker,
         "LEDGER_TEST_LOCK_HOLD_MS": 600},
    )
    wait_marker(close_marker)
    post_proc = start_cli(post_args(last), {"LEDGER_TEST_START_FILE": gate})
    # Release the post only after the close has captured the lock.
    gate.write_text("go\n", encoding="utf-8")

    close_rc, close_out, close_log = finish_cli(close_proc)
    post_rc, post_out, post_log = finish_cli(post_proc)
    assert close_rc == 0, close_log

    # The close acquired the writer lock first (marker proves it), so the
    # posting is strictly ordered after it and must be rejected.  A posting
    # that committed but was missing from the snapshot would violate this.
    assert post_rc != 0, post_out
    assert post_out["error"] == "period_closed", post_log
    assert scalar(
        "SELECT count(*) FROM entries WHERE entry_id='raceclose-last'"
    ) == 0
    assert close_out["entry_count"] == 5
    # The rejected JPY posting left no snapshot row: only the USD accounts
    # from the five earlier entries are present, and they still net to zero.
    assert query(
        "SELECT count(*) FROM period_snapshots WHERE period='2026-07'"
    ) == [(2,)]
    assert scalar(
        "SELECT count(*) FROM period_snapshots WHERE period='2026-07' "
        "AND currency='JPY'"
    ) == 0
    assert_raw_ledger_invariants()

    # And after close, a committed post for July is impossible either way.
    rc, out, _ = run_cli(post_args(make_entry(
        "raceclose-after", "2026-07-31",
        [("cash-usd", "USD", 5, 0), ("revenue-usd", "USD", 0, 5)],
    )))
    assert rc != 0 and out["error"] == "period_closed"


def test_post_race_where_post_wins_is_in_snapshot(accounts):
    """Deterministic ordering: post acquires first, then close includes it."""
    post_gate = Path("/tmp/ledger-test-post-first-gate")
    post_gate.unlink(missing_ok=True)
    lock_marker = Path("/tmp/ledger-test-post-first-locked")
    lock_marker.unlink(missing_ok=True)

    payload = make_entry(
        "post-first", "2026-08-31",
        [("cash-eur", "EUR", 990, 0), ("revenue-eur", "EUR", 0, 990)],
    )
    post_proc = start_cli(post_args(payload), {
        "LEDGER_TEST_START_FILE": post_gate,
        "LEDGER_TEST_LOCK_MARKER": lock_marker,
        "LEDGER_TEST_LOCK_HOLD_MS": 500,
    })
    post_gate.write_text("go\n", encoding="utf-8")
    wait_marker(lock_marker)
    # Close queues behind the post that already holds the lock.
    close_proc = start_cli(["close", "--period", "2026-08"])
    post_rc, _post_out, post_log = finish_cli(post_proc)
    close_rc, close_out, close_log = finish_cli(close_proc)
    assert post_rc == 0, post_log
    assert close_rc == 0, close_log
    assert close_out["entry_count"] == 1
    assert query(
        "SELECT debit_cents FROM period_snapshots WHERE period='2026-08' "
        "AND account_id='cash-eur'"
    ) == [(990,)]
    assert_raw_ledger_invariants()


# ---------------------------------------------------------------------------
# Crash tests: terminate after insert, before commit, and after commit but
# before the response is emitted.
# ---------------------------------------------------------------------------
def test_kill_after_insert_before_commit_leaves_nothing(accounts):
    marker = Path("/tmp/ledger-test-crash-before.marker")
    marker.unlink(missing_ok=True)
    payload = make_entry(
        "crash-before", "2026-09-01",
        [("cash-usd", "USD", 601, 0), ("revenue-usd", "USD", 0, 601)],
    )
    proc = start_cli(post_args(payload), {
        "LEDGER_CRASH_AT": "before_commit",
        "LEDGER_CRASH_MARKER": marker,
    })
    rc, _out, _log = finish_cli(proc)
    assert rc != 0  # SIGKILL is never a success exit
    assert wait_marker(marker) == "before_commit"
    assert scalar("SELECT count(*) FROM entries") == 0
    assert scalar("SELECT count(*) FROM postings") == 0
    assert scalar("SELECT count(*) FROM account_balances "
                  "WHERE debit_cents <> 0 OR credit_cents <> 0") == 0
    assert scalar("SELECT count(*) FROM requests") == 0

    # Retrying after the crash posts exactly once.
    rc, _out, log = run_cli(post_args(payload))
    assert rc == 0, log
    assert_raw_ledger_invariants()


def test_kill_after_commit_before_response_keeps_committed_effect(accounts):
    marker = Path("/tmp/ledger-test-crash-after.marker")
    marker.unlink(missing_ok=True)
    payload = make_entry(
        "crash-after", "2026-09-02",
        [("cash-usd", "USD", 602, 0), ("revenue-usd", "USD", 0, 602)],
    )
    proc = start_cli(post_args(payload), {
        "LEDGER_CRASH_AT": "after_commit",
        "LEDGER_CRASH_MARKER": marker,
    })
    rc, _out, _log = finish_cli(proc)
    assert rc != 0
    assert wait_marker(marker) == "after_commit"
    # The transaction is durable even though the caller saw no response.
    assert scalar("SELECT count(*) FROM entries WHERE entry_id='crash-after'") == 1
    assert scalar("SELECT count(*) FROM requests WHERE operation='post'") == 1
    assert scalar(
        "SELECT debit_cents FROM account_balances WHERE account_id='cash-usd'"
    ) == 602
    assert_raw_ledger_invariants()

    # The retry with identical content returns the original committed result.
    rc, out, log = run_cli(post_args(payload))
    assert rc == 0, log
    assert out["entry_id"] == "crash-after"
    assert scalar("SELECT count(*) FROM entries") == 1


def test_kill_before_commit_of_reversal_and_close(accounts):
    assert run_cli(post_args(make_entry(
        "crash-orig", "2026-09-03",
        [("cash-eur", "EUR", 33, 0), ("revenue-eur", "EUR", 0, 33)],
    )))[0] == 0

    for phase_entry, args, marker_name, checks in [
        ("rev-before", reverse_args(
            {"entry_id": "crash-rev", "reverses_entry_id": "crash-orig",
             "entry_date": "2026-09-04"}),
         "/tmp/ledger-test-rev-before.marker",
         "entries WHERE reverses_entry_id='crash-orig'"),
        ("close-before", ["close", "--period", "2026-09"],
         "/tmp/ledger-test-close-before.marker",
         "periods WHERE period='2026-09'"),
    ]:
        marker = Path(marker_name)
        marker.unlink(missing_ok=True)
        proc = start_cli(args, {"LEDGER_CRASH_AT": "before_commit",
                                "LEDGER_CRASH_MARKER": marker})
        rc, _out, _log = finish_cli(proc)
        assert rc != 0
        assert wait_marker(marker) == "before_commit"
        assert scalar(f"SELECT count(*) FROM {checks}") == 0, phase_entry
        assert scalar(
            "SELECT count(*) FROM requests WHERE operation IN "
            "('reverse','close')"
        ) == 0
    assert_raw_ledger_invariants()


# ---------------------------------------------------------------------------
# Auditing the audit itself.
# ---------------------------------------------------------------------------
def test_audit_detects_tampering(accounts):
    """The audit must fail when a stored balance is inconsistent with raw."""
    import psycopg

    assert run_cli(post_args(make_entry(
        "audit-1", "2026-10-01",
        [("cash-usd", "USD", 42, 0), ("revenue-usd", "USD", 0, 42)],
    )))[0] == 0
    rc, out, _ = run_cli(["audit"])
    assert rc == 0 and out["ok"] is True

    # balances are mutable by design (documented single-writer invariant), so
    # simulate corruption and confirm audit notices it.
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute(
            "UPDATE account_balances SET debit_cents = debit_cents + 1 "
            "WHERE account_id='cash-usd'"
        )
        conn.commit()
    rc, out, _ = run_cli(["audit"])
    assert out["ok"] is False
    assert out["account_balance_mismatches"], out


def test_concurrent_close_returns_identical_snapshot(accounts):
    for idx in range(2):
        assert run_cli(post_args(make_entry(
            f"cc-{idx}", f"2026-12-0{idx + 1}",
            [("cash-eur", "EUR", 60 + idx, 0),
             ("revenue-eur", "EUR", 0, 60 + idx)],
        )))[0] == 0
    start = threading.Barrier(3)
    results = []
    lock = threading.Lock()

    def worker():
        start.wait()
        result = run_cli(["close", "--period", "2026-12"])
        with lock:
            results.append(result)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(rc == 0 for rc, _o, _l in results), results
    payloads = [o for _rc, o, _l in results]
    assert all(p == payloads[0] for p in payloads), payloads
    assert scalar("SELECT count(*) FROM periods WHERE period='2026-12'") == 1
    assert scalar(
        "SELECT count(*) FROM requests WHERE operation='close' "
        "AND closed_period='2026-12'"
    ) == 1
    assert_raw_ledger_invariants()


def test_concurrent_identical_reverse_retry_creates_one_reversal(accounts):
    assert run_cli(post_args(make_entry(
        "same-rev-orig", "2026-12-10",
        [("cash-usd", "USD", 25, 0), ("revenue-usd", "USD", 0, 25)],
    )))[0] == 0
    request = {"entry_id": "same-rev", "reverses_entry_id": "same-rev-orig",
               "entry_date": "2026-12-11"}
    start = threading.Barrier(4)
    results = []
    lock = threading.Lock()

    def worker():
        start.wait()
        result = run_cli(reverse_args(request))
        with lock:
            results.append(result)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(rc == 0 for rc, _o, _l in results), results
    assert all(o == results[0][1] for _r, o, _l in results), results
    assert scalar(
        "SELECT count(*) FROM entries WHERE reverses_entry_id='same-rev-orig'"
    ) == 1
    assert scalar("SELECT count(*) FROM requests WHERE operation='reverse'") == 1
    assert_raw_ledger_invariants()


def test_reversal_date_is_independent_of_original_date(accounts):
    """The reversal's period is its own date; no ordering constraint is imposed."""
    assert run_cli(post_args(make_entry(
        "late-orig", "2026-12-20",
        [("cash-usd", "USD", 8, 0), ("revenue-usd", "USD", 0, 8)],
    )))[0] == 0
    rc, out, _ = run_cli(reverse_args(
        {"entry_id": "early-rev", "reverses_entry_id": "late-orig",
         "entry_date": "2026-11-19"}
    ))
    assert rc == 0, out
    assert out["period"] == "2026-11"
    assert scalar(
        "SELECT count(*) FROM entries WHERE reverses_entry_id='late-orig'"
    ) == 1
    assert_raw_ledger_invariants()


def test_historical_snapshot_unchanged_by_later_reversal(accounts):
    assert run_cli(post_args(make_entry(
        "hist-1", "2026-05-05",
        [("cash-usd", "USD", 300, 0), ("revenue-usd", "USD", 0, 300)],
    )))[0] == 0
    assert run_cli(["close", "--period", "2026-05"])[0] == 0
    jan_before = query("SELECT * FROM periods WHERE period='2026-05'")
    rows_before = query("SELECT * FROM period_snapshots WHERE period='2026-05'")

    assert run_cli(reverse_args(
        {"entry_id": "hist-rev", "reverses_entry_id": "hist-1",
         "entry_date": "2026-06-05"}
    ))[0] == 0
    assert run_cli(["close", "--period", "2026-06"])[0] == 0

    assert query("SELECT * FROM periods WHERE period='2026-05'") == jan_before
    assert query(
        "SELECT * FROM period_snapshots WHERE period='2026-05'"
    ) == rows_before

    # May and June snapshots differ, but original + reversal nets to zero.
    snap_may = {
        (a, c): (d, cr, n) for a, c, d, cr, n in query(
            "SELECT account_id, currency, debit_cents, credit_cents, net_cents "
            "FROM period_snapshots WHERE period='2026-05'"
        )
    }
    snap_jun = {
        (a, c): (d, cr, n) for a, c, d, cr, n in query(
            "SELECT account_id, currency, debit_cents, credit_cents, net_cents "
            "FROM period_snapshots WHERE period='2026-06'"
        )
    }
    combined = {}
    for key in set(snap_may) | set(snap_jun):
        d1, c1, _ = snap_may.get(key, (0, 0, 0))
        d2, c2, _ = snap_jun.get(key, (0, 0, 0))
        combined[key] = (d1 + d2, c1 + c2, (d1 - c1) + (d2 - c2))
    assert all(net == 0 for _d, _c, net in combined.values()), combined
    assert_raw_ledger_invariants()


def test_accounts_load_is_idempotent_and_rejects_currency_change(accounts, tmp_path):
    csv_path = tmp_path / "accounts.csv"
    csv_path.write_text(
        "account_id,currency\ncash-usd,USD\nnew-eur,EUR\n", encoding="utf-8"
    )
    rc, out, _ = run_cli(["load-accounts", str(csv_path)])
    assert rc == 0 and out["inserted"] == 1, out
    rc, out, _ = run_cli(["load-accounts", str(csv_path)])
    assert rc == 0 and out["inserted"] == 0, out

    conflict_csv = tmp_path / "conflict.csv"
    conflict_csv.write_text(
        "account_id,currency\ncash-usd,EUR\n", encoding="utf-8"
    )
    rc, out, _ = run_cli(["load-accounts", str(conflict_csv)])
    assert rc != 0 and out["error"] == "accounts_conflict", out
    assert scalar("SELECT currency FROM accounts WHERE account_id='cash-usd'") == "USD"
    assert scalar("SELECT count(*) FROM accounts WHERE account_id='new-eur'") == 1


def test_close_then_reverse_same_period_is_rejected(accounts):
    assert run_cli(post_args(make_entry(
        "same-period-orig", "2026-04-10",
        [("cash-usd", "USD", 14, 0), ("revenue-usd", "USD", 0, 14)],
    )))[0] == 0
    assert run_cli(["close", "--period", "2026-04"])[0] == 0
    rc, out, _ = run_cli(reverse_args(
        {"entry_id": "same-period-rev", "reverses_entry_id": "same-period-orig",
         "entry_date": "2026-04-11"}
    ))
    assert rc != 0 and out["error"] == "period_closed", out
    assert scalar(
        "SELECT count(*) FROM entries WHERE entry_id='same-period-rev'"
    ) == 0
    assert_raw_ledger_invariants()
