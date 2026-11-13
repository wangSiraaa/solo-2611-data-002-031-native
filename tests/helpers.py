"""Shared test helpers: CLI process control and independent raw-SQL checks."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import psycopg

REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_DSN = os.environ.get(
    "LEDGER_TEST_DSN",
    "host=/tmp/ledger-pg-sock port=55432 dbname=ledger_itest",
)

ACCOUNTS = [
    {"account_id": "cash-usd", "currency": "USD"},
    {"account_id": "revenue-usd", "currency": "USD"},
    {"account_id": "receivable-usd", "currency": "USD"},
    {"account_id": "suspense-usd", "currency": "USD"},
    {"account_id": "cash-eur", "currency": "EUR"},
    {"account_id": "revenue-eur", "currency": "EUR"},
    {"account_id": "payable-eur", "currency": "EUR"},
    {"account_id": "cash-jpy", "currency": "JPY"},
    {"account_id": "revenue-jpy", "currency": "JPY"},
]

# Environment variables understood by ledger.ledger, kept out of parent runs.
SEAM_VARS = (
    "LEDGER_DSN",
    "LEDGER_CRASH_AT",
    "LEDGER_CRASH_MARKER",
    "LEDGER_TEST_LOCK_HOLD_MS",
    "LEDGER_TEST_LOCK_MARKER",
    "LEDGER_TEST_PRE_LOCK_MS",
    "LEDGER_TEST_START_FILE",
)


def cli_env(extra: dict[str, Any] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    for key in SEAM_VARS:
        env.pop(key, None)
    env["LEDGER_DSN"] = TEST_DSN
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


def post_args(payload: dict[str, Any]) -> list[str]:
    return ["post", "--entry-json",
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"))]


def reverse_args(payload: dict[str, Any]) -> list[str]:
    return ["reverse", "--reversal-json",
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"))]


def run_cli(
    args: list[str],
    extra_env: dict[str, Any] | None = None,
    timeout: float = 60,
) -> tuple[int, dict[str, Any] | None, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "ledger", *args],
        check=False,
        cwd=str(REPO_ROOT),
        env=cli_env(extra_env),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    parsed = json.loads(proc.stdout) if proc.stdout.strip() else None
    return proc.returncode, parsed, proc.stdout + proc.stderr


def start_cli(
    args: list[str], extra_env: dict[str, Any] | None = None
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "ledger", *args],
        cwd=str(REPO_ROOT),
        env=cli_env(extra_env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def finish_cli(
    proc: subprocess.Popen[str], timeout: float = 90
) -> tuple[int, dict[str, Any] | None, str]:
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        raise AssertionError(
            f"CLI process timed out; stdout={stdout!r} stderr={stderr!r}"
        )
    parsed = json.loads(stdout) if stdout.strip() else None
    return proc.returncode, parsed, stdout + stderr


def wait_marker(path: Path, timeout: float = 15) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size > 0:
            return path.read_text(encoding="utf-8").strip()
        time.sleep(0.02)
    raise AssertionError(f"marker {path} was not written within {timeout}s")


def query(sql_text: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """Read raw rows directly, bypassing every ledger code path."""
    with psycopg.connect(TEST_DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(sql_text, params)
        return cur.fetchall()


def scalar(sql_text: str, params: tuple[Any, ...] = ()) -> Any:
    rows = query(sql_text, params)
    return rows[0][0] if rows else None


def make_entry(
    entry_id: str, entry_date: str, lines: list[tuple[str, str, int, int]]
) -> dict[str, Any]:
    return {
        "entry_id": entry_id,
        "entry_date": entry_date,
        "lines": [
            {"account_id": account_id, "currency": currency,
             "debit_cents": debit, "credit_cents": credit}
            for account_id, currency, debit, credit in lines
        ],
    }


def assert_raw_ledger_invariants() -> None:
    """Independent raw-SQL verification of every stated invariant.

    Deliberately reads only accounts/account_balances/entries/postings/
    periods/period_snapshots/requests, never ledger helper functions.
    """
    # 1. Cumulative balances equal raw posting sums, including untouched
    #    accounts whose balances must remain zero.
    rows = query(
        """
        SELECT b.account_id, b.currency, b.debit_cents, b.credit_cents,
               coalesce(r.debit_cents, 0), coalesce(r.credit_cents, 0)
          FROM account_balances b
          LEFT JOIN (
                SELECT account_id, currency,
                       sum(debit_cents)::bigint AS debit_cents,
                       sum(credit_cents)::bigint AS credit_cents
                  FROM postings GROUP BY account_id, currency
          ) r USING (account_id, currency)
         ORDER BY b.account_id, b.currency
        """
    )
    assert rows, "accounts fixture did not seed balance rows"
    for account_id, currency, d, c, rd, rc in rows:
        assert (d, c) == (rd, rc), (
            f"balance mismatch for {account_id}/{currency}: "
            f"stored=({d},{c}) raw=({rd},{rc})"
        )

    # 2. Every account currency nets to zero across the whole ledger.
    diffs = {c: int(d) for c, d in query(
        """
        SELECT currency, (sum(debit_cents) - sum(credit_cents))::bigint
          FROM postings GROUP BY currency
        """
    )}
    for (currency,) in query("SELECT DISTINCT currency FROM accounts"):
        assert diffs.get(currency, 0) == 0, (
            f"ledger currency {currency} has diff {diffs.get(currency)}"
        )

    # 3. Each entry balances independently in every currency it touches.
    unbalanced = query(
        """
        SELECT e.entry_id, p.currency,
               sum(p.debit_cents)::bigint, sum(p.credit_cents)::bigint
          FROM entries e JOIN postings p USING (entry_seq)
         GROUP BY e.entry_id, p.currency
        HAVING sum(p.debit_cents) <> sum(p.credit_cents)
        """
    )
    assert not unbalanced, f"unbalanced entries: {unbalanced}"

    # 4. Close snapshot equals a fresh raw aggregation of its period.
    stored = query(
        """
        SELECT period, account_id, currency, debit_cents, credit_cents, net_cents
          FROM period_snapshots
         ORDER BY period, account_id, currency
        """
    )
    expected = query(
        """
        SELECT e.period, p.account_id, p.currency,
               sum(p.debit_cents)::bigint,
               sum(p.credit_cents)::bigint,
               (sum(p.debit_cents) - sum(p.credit_cents))::bigint
          FROM entries e
          JOIN postings p USING (entry_seq)
          JOIN periods pe ON pe.period = e.period
         GROUP BY e.period, p.account_id, p.currency
         ORDER BY e.period, p.account_id, p.currency
        """
    )
    assert stored == expected, (
        f"snapshot mismatch: stored={stored} expected={expected}"
    )

    # 5. One committed request per entry effect and one per close.
    n_entries = int(scalar("SELECT count(*) FROM entries"))
    n_effect_requests = int(scalar(
        "SELECT count(*) FROM requests WHERE operation IN ('post','reverse')"
    ))
    n_periods = int(scalar("SELECT count(*) FROM periods"))
    n_close_requests = int(scalar(
        "SELECT count(*) FROM requests WHERE operation = 'close'"
    ))
    assert n_entries == n_effect_requests, (
        f"entries={n_entries} effect requests={n_effect_requests}"
    )
    assert n_periods == n_close_requests, (
        f"periods={n_periods} close requests={n_close_requests}"
    )
    assert int(scalar(
        """
        SELECT count(*) FROM entries e
         WHERE NOT EXISTS (
             SELECT 1 FROM requests r
              WHERE r.entry_seq = e.entry_seq
                AND r.operation IN ('post','reverse'))
        """
    )) == 0

    # 6. Each reversal mirrors its original line-for-line and original plus
    #    reversal has exactly zero net effect per account/currency.
    reversals = query(
        """
        SELECT orig.entry_id, rev.entry_id, orig.entry_seq, rev.entry_seq
          FROM entries rev
          JOIN entries orig ON orig.entry_id = rev.reverses_entry_id
        """
    )
    for orig_id, rev_id, orig_seq, rev_seq in reversals:
        orig_lines = query(
            """
            SELECT account_id, currency, debit_cents, credit_cents
              FROM postings WHERE entry_seq = %s ORDER BY line_no
            """,
            (orig_seq,),
        )
        rev_lines = query(
            """
            SELECT account_id, currency, debit_cents, credit_cents
              FROM postings WHERE entry_seq = %s ORDER BY line_no
            """,
            (rev_seq,),
        )
        assert len(orig_lines) == len(rev_lines), (
            f"reversal {rev_id} line count differs from {orig_id}"
        )
        for o, r in zip(orig_lines, rev_lines):
            assert o[0] == r[0] and o[1] == r[1], (
                f"reversal {rev_id} line account/currency differs: {o} vs {r}"
            )
            assert o[2] == r[3] and o[3] == r[2], (
                f"reversal {rev_id} is not debit/credit mirror: {o} vs {r}"
            )
        net = query(
            """
            SELECT account_id, currency,
                   (sum(debit_cents) - sum(credit_cents))::bigint
              FROM postings WHERE entry_seq IN (%s, %s)
             GROUP BY account_id, currency
            """,
            (orig_seq, rev_seq),
        )
        assert all(int(n) == 0 for _a, _c, n in net), (
            f"original {orig_id} + reversal {rev_id} has nonzero net: {net}"
        )

    # 7. Reversal relationships are type-correct and unique.
    assert int(scalar(
        """
        SELECT count(*) FROM entries rev
          JOIN entries orig ON orig.entry_id = rev.reverses_entry_id
         WHERE rev.entry_type <> 'reversal'
            OR orig.entry_type <> 'journal'
        """
    )) == 0
    assert int(scalar(
        """
        SELECT count(*) FROM (
            SELECT reverses_entry_id FROM entries
             WHERE reverses_entry_id IS NOT NULL
             GROUP BY reverses_entry_id HAVING count(*) > 1
        ) x
        """
    )) == 0
