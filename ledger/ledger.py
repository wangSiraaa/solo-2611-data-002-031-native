"""Local multi-currency synthetic ledger CLI.

Single-package implementation using the standard library plus psycopg.

Safety model
------------
* Every mutating command uses one PostgreSQL transaction.  It takes the same
  transaction-scoped advisory lock, then commits postings, cumulative
  balances, entry/relationship rows and the request record together.  A
  failure at any point rolls back all of it.
* The same lock serializes post/reverse/close.  Therefore a close and a
  posting have a total order: a posting either commits before the close and is
  included in the snapshot, or it observes the closed period and is rejected.
* The SHA-256 digest of canonical JSON is the idempotency key.  It is stored
  in the same transaction as the effect it describes.
* Database triggers/constraints are a backstop: ledger facts are append-only,
  postings must balance per currency, and a closed period rejects new entries.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import signal
import sys
import time
from datetime import date
from typing import Any
from urllib.parse import quote

try:
    import psycopg
except ImportError as exc:  # pragma: no cover - dependency error path
    sys.stderr.write(
        "psycopg is required: python -m pip install 'psycopg[binary]'\n"
    )
    raise SystemExit(2) from exc


ADVISORY_LOCK_KEY = 0x1ED6E5  # stable 64-bit namespace for this ledger
PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
CURRENCY_RE = re.compile(r"^[A-Z][A-Z0-9._-]{0,31}$")
MAX_CENTS = 2**63 - 1  # PostgreSQL bigint range
MIGRATIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations"
)


class LedgerError(Exception):
    """A deterministic user-facing rejection, reported as JSON."""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def as_dict(self) -> dict[str, Any]:
        out = {"ok": False, "error": self.code, "message": self.message}
        out.update(self.extra)
        return out


# ---------------------------------------------------------------------------
# Canonical encoding, parsing, validation.
# ---------------------------------------------------------------------------
def canonical_json(value: Any) -> str:
    """Stable encoding used for hashing and for persisted request copies."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    )


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_date(value: Any, field: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise LedgerError("invalid_request", f"{field} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise LedgerError("invalid_request", f"{field} is not a valid date") from exc


def period_of(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def validate_period(value: Any, field: str = "period") -> str:
    if not isinstance(value, str) or not PERIOD_RE.fullmatch(value):
        raise LedgerError("invalid_request", f"{field} must be YYYY-MM")
    return value


def _require_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LedgerError("invalid_request", f"{field} must be an integer")
    if value < 0:
        raise LedgerError("invalid_request", f"{field} must be non-negative")
    if value > MAX_CENTS:
        raise LedgerError("invalid_request", f"{field} exceeds bigint range")
    return value


def _require_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise LedgerError("invalid_request", f"{field} must be a non-empty string")
    if len(value) > 200 or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise LedgerError("invalid_request", f"{field} contains invalid characters")
    return value


def _check_unknown_keys(obj: dict[str, Any], allowed: set[str], field: str) -> None:
    extra = set(obj) - allowed
    if extra:
        raise LedgerError(
            "invalid_request",
            f"{field} has unknown keys: {','.join(sorted(extra))}",
        )


def normalize_journal(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise LedgerError("invalid_request", "entry must be a JSON object")
    _check_unknown_keys(raw, {"entry_id", "entry_date", "lines"}, "entry")
    entry_id = _require_id(raw.get("entry_id"), "entry_id")
    entry_date = parse_date(raw.get("entry_date"), "entry_date")
    lines_raw = raw.get("lines")
    if not isinstance(lines_raw, list) or not lines_raw:
        raise LedgerError("invalid_request", "lines must be a non-empty array")
    lines: list[dict[str, Any]] = []
    for idx, line in enumerate(lines_raw):
        where = f"lines[{idx}]"
        if not isinstance(line, dict):
            raise LedgerError("invalid_request", f"{where} must be an object")
        _check_unknown_keys(
            line, {"account_id", "currency", "debit_cents", "credit_cents"}, where
        )
        account_id = _require_id(line.get("account_id"), f"{where}.account_id")
        currency = line.get("currency")
        if not isinstance(currency, str) or not CURRENCY_RE.fullmatch(currency):
            raise LedgerError(
                "invalid_request",
                f"{where}.currency must be an uppercase currency code",
            )
        debit = _require_int(line.get("debit_cents"), f"{where}.debit_cents")
        credit = _require_int(line.get("credit_cents"), f"{where}.credit_cents")
        if not ((debit > 0) != (credit > 0)):
            raise LedgerError(
                "invalid_request",
                f"{where} must have exactly one of debit_cents/credit_cents > 0",
            )
        lines.append(
            {"account_id": account_id, "currency": currency,
             "debit_cents": debit, "credit_cents": credit}
        )
    normalized = {
        "entry_id": entry_id,
        "entry_date": entry_date.isoformat(),
        "lines": lines,
    }
    check_currency_balance(normalized, field="entry")
    return normalized


def check_currency_balance(entry: dict[str, Any], *, field: str) -> None:
    totals: dict[str, list[int]] = {}
    for line in entry["lines"]:
        debit, credit = totals.setdefault(line["currency"], [0, 0])
        debit += line["debit_cents"]
        credit += line["credit_cents"]
        totals[line["currency"]] = [debit, credit]
    for currency, (debit, credit) in totals.items():
        if debit != credit:
            raise LedgerError(
                "unbalanced",
                f"{field} is not balanced in currency {currency}",
                currency=currency,
                debit_cents=debit,
                credit_cents=credit,
            )


def normalize_reversal(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise LedgerError("invalid_request", "reversal must be a JSON object")
    _check_unknown_keys(
        raw, {"entry_id", "reverses_entry_id", "entry_date"}, "reversal"
    )
    entry_id = _require_id(raw.get("entry_id"), "entry_id")
    original = _require_id(raw.get("reverses_entry_id"), "reverses_entry_id")
    entry_date = parse_date(raw.get("entry_date"), "entry_date")
    if entry_id == original:
        raise LedgerError(
            "invalid_request", "reversal entry_id must differ from the original"
        )
    return {
        "entry_id": entry_id,
        "reverses_entry_id": original,
        "entry_date": entry_date.isoformat(),
    }


def normalize_close(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise LedgerError("invalid_request", "close request must be a JSON object")
    _check_unknown_keys(raw, {"period"}, "close")
    return {"period": validate_period(raw.get("period"))}


def digest_of(request: Any) -> str:
    return sha256_hex(canonical_json(request))


# ---------------------------------------------------------------------------
# Database connection and migrations.
# ---------------------------------------------------------------------------
def default_dsn() -> str:
    if os.environ.get("LEDGER_DSN"):
        return os.environ["LEDGER_DSN"]
    socket_dir = os.environ.get("LEDGER_PGHOST", "/tmp/ledger-pg-sock")
    port = os.environ.get("LEDGER_PGPORT", "55432")
    db = os.environ.get("LEDGER_PGDATABASE", "taskdb")
    user = os.environ.get("LEDGER_PGUSER", "")
    dsn = f"host={quote(socket_dir)} port={port} dbname={quote(db)}"
    if user:
        dsn += f" user={quote(user)}"
    return dsn


def connect(dsn: str | None = None, *, autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(dsn or default_dsn(), autocommit=autocommit)


def migrate(dsn: str | None = None, *, reset: bool = False) -> dict[str, Any]:
    """Apply SQL migrations idempotently, recording each version once."""
    with connect(dsn) as conn:
        with conn.cursor() as cur:
            if reset:
                cur.execute("DROP SCHEMA public CASCADE")
                cur.execute("CREATE SCHEMA public")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version     text PRIMARY KEY,
                    applied_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
                    checksum    text NOT NULL
                )
                """
            )
            applied = {
                row[0] for row in cur.execute(
                    "SELECT version FROM schema_migrations"
                ).fetchall()
            }
            newly = []
            for name in sorted(os.listdir(MIGRATIONS_DIR)):
                if not name.endswith(".sql"):
                    continue
                path = os.path.join(MIGRATIONS_DIR, name)
                with open(path, "r", encoding="utf-8") as fh:
                    sql = fh.read()
                checksum = sha256_hex(sql)
                if name in applied:
                    cur.execute(
                        "SELECT checksum FROM schema_migrations WHERE version = %s",
                        (name,),
                    )
                    stored = cur.fetchone()[0]
                    if stored != checksum:
                        raise LedgerError(
                            "migration_checksum_mismatch",
                            f"{name} was applied with a different checksum",
                        )
                    continue
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                    (name, checksum),
                )
                newly.append(name)
        conn.commit()
    return {"ok": True, "operation": "init-db", "applied": newly,
            "migrations_dir": MIGRATIONS_DIR}


# ---------------------------------------------------------------------------
# Accounts.
# ---------------------------------------------------------------------------
def load_accounts_file(path: str) -> list[dict[str, str]]:
    """Read CSV declaring account_id and currency; currency is fixed per id."""
    rows: list[dict[str, str]] = []
    seen: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            headers = [h.strip() for h in (reader.fieldnames or [])]
            if "account_id" not in headers or "currency" not in headers:
                raise LedgerError(
                    "invalid_accounts",
                    "CSV header must declare account_id and currency",
                )
            for lineno, row in enumerate(reader, start=2):
                account_id = (row.get("account_id") or "").strip()
                currency = (row.get("currency") or "").strip()
                if not account_id or not CURRENCY_RE.fullmatch(currency):
                    raise LedgerError(
                        "invalid_accounts",
                        f"CSV line {lineno} has invalid account_id/currency",
                    )
                if len(account_id) > 200 or any(
                    ord(c) < 32 or ord(c) == 127 for c in account_id
                ):
                    raise LedgerError(
                        "invalid_accounts",
                        f"CSV line {lineno} has invalid account_id",
                    )
                if account_id in seen:
                    raise LedgerError(
                        "invalid_accounts",
                        f"CSV line {lineno} duplicates account_id {account_id}",
                    )
                seen[account_id] = currency
                rows.append({"account_id": account_id, "currency": currency})
    except OSError as exc:
        raise LedgerError("invalid_accounts", f"cannot read {path}: {exc}") from exc
    if not rows:
        raise LedgerError("invalid_accounts", "accounts CSV has no data rows")
    return rows


def load_accounts(conn: psycopg.Connection, rows: list[dict[str, str]]) -> dict[str, Any]:
    """Insert accounts and zero balances atomically.

    Accounts are immutable, so re-loading identical declarations is a no-op;
    a conflicting currency for an existing id is rejected.
    """
    conflicts = []
    inserted = 0
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_LOCK_KEY,))
        for row in rows:
            cur.execute(
                """
                INSERT INTO accounts (account_id, currency)
                VALUES (%s, %s)
                ON CONFLICT (account_id) DO NOTHING
                RETURNING account_id
                """,
                (row["account_id"], row["currency"]),
            )
            if cur.fetchone():
                cur.execute(
                    """
                    INSERT INTO account_balances (account_id, currency)
                    VALUES (%s, %s) ON CONFLICT DO NOTHING
                    """,
                    (row["account_id"], row["currency"]),
                )
                inserted += 1
                continue
            cur.execute(
                "SELECT currency FROM accounts WHERE account_id = %s",
                (row["account_id"],),
            )
            existing = cur.fetchone()[0]
            if existing != row["currency"]:
                conflicts.append(
                    {"account_id": row["account_id"],
                     "declared": row["currency"], "existing": existing}
                )
        if conflicts:
            raise LedgerError(
                "accounts_conflict",
                "account currency is immutable and differs from declaration",
                conflicts=conflicts,
            )
        conn.commit()
    return {"ok": True, "operation": "load-accounts", "inserted": inserted,
            "unchanged": len(rows) - inserted}


# ---------------------------------------------------------------------------
# Shared write helpers.
# ---------------------------------------------------------------------------
def _advisory_lock(cur: psycopg.Cursor) -> None:
    # Serializes all writers, so close and post have a total commit order.
    start_file = os.environ.get("LEDGER_TEST_START_FILE")
    if start_file:
        # Test-only seam: hold start until the harness releases the file.
        deadline = time.monotonic() + 60
        while not os.path.exists(start_file):
            if time.monotonic() > deadline:
                raise LedgerError(
                    "test_seam_timeout", "start file was not released"
                )
            time.sleep(0.005)
    pre_ms = os.environ.get("LEDGER_TEST_PRE_LOCK_MS")
    if pre_ms:
        # Test-only seam: stagger lock acquisition deterministically.
        time.sleep(float(pre_ms) / 1000.0)
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_LOCK_KEY,))
    marker = os.environ.get("LEDGER_TEST_LOCK_MARKER")
    if marker:
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write("locked\n")
    hold_ms = os.environ.get("LEDGER_TEST_LOCK_HOLD_MS")
    if hold_ms:
        # Test-only seam: widen the lock window so races are observable.
        time.sleep(float(hold_ms) / 1000.0)


def _crash_point(phase: str) -> None:
    """Test-only process-termination seam.

    LEDGER_CRASH_AT=before_commit kills the process with all writes uncommitted;
    LEDGER_CRASH_AT=after_commit kills it after the transaction committed but
    before the success response is emitted.  Production never sets this.
    """
    if os.environ.get("LEDGER_CRASH_AT") != phase:
        return
    marker = os.environ.get("LEDGER_CRASH_MARKER")
    if marker:
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(phase + "\n")
    os.kill(os.getpid(), signal.SIGKILL)


ENTRY_COLUMNS = (
    "entry_seq", "entry_id", "entry_type", "entry_date", "period",
    "canonical_content", "content_hash", "request_sha256", "request_json",
    "result_json", "reverses_entry_id", "created_at",
)


def _fetch_entry(cur: psycopg.Cursor, entry_id: str) -> dict[str, Any] | None:
    cur.execute(
        f"SELECT {', '.join(ENTRY_COLUMNS)} FROM entries WHERE entry_id = %s",
        (entry_id,),
    )
    row = cur.fetchone()
    return dict(zip(ENTRY_COLUMNS, row)) if row else None


PERIOD_COLUMNS = (
    "period", "snapshot_id", "snapshot_hash", "entry_count", "posting_count",
    "snapshot", "request_sha256", "request_json", "result_json", "closed_at",
)


def _fetch_period(cur: psycopg.Cursor, period: str) -> dict[str, Any] | None:
    cur.execute(
        f"SELECT {', '.join(PERIOD_COLUMNS)} FROM periods WHERE period = %s",
        (period,),
    )
    row = cur.fetchone()
    return dict(zip(PERIOD_COLUMNS, row)) if row else None


def _json_field(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _entry_period_closed(cur: psycopg.Cursor, period: str) -> dict[str, Any] | None:
    return _fetch_period(cur, period)


def _validate_accounts(cur: psycopg.Cursor, lines: list[dict[str, Any]]) -> None:
    """Reject unknown accounts / wrong declared currency before any insert."""
    for account_id, currency in sorted(
        {(ln["account_id"], ln["currency"]) for ln in lines}
    ):
        cur.execute(
            "SELECT currency FROM accounts WHERE account_id = %s",
            (account_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise LedgerError(
                "unknown_account", f"account {account_id} is not declared",
                account_id=account_id,
            )
        if row[0] != currency:
            raise LedgerError(
                "account_currency_mismatch",
                f"entry declares {currency} for account {account_id}, "
                f"whose fixed currency is {row[0]}",
                account_id=account_id, declared_currency=currency,
                account_currency=row[0],
            )


def _insert_lines_and_balances(
    cur: psycopg.Cursor,
    entry_seq: int,
    lines: list[dict[str, Any]],
    *,
    reverse: bool,
) -> None:
    """Insert posting lines and apply the same signed amounts to balances."""
    for line_no, line in enumerate(lines, start=1):
        debit = line["credit_cents"] if reverse else line["debit_cents"]
        credit = line["debit_cents"] if reverse else line["credit_cents"]
        cur.execute(
            """
            INSERT INTO postings
                (entry_seq, line_no, account_id, currency,
                 debit_cents, credit_cents)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (entry_seq, line_no, line["account_id"], line["currency"],
             debit, credit),
        )
        cur.execute(
            """
            UPDATE account_balances
               SET debit_cents  = debit_cents  + %s,
                   credit_cents = credit_cents + %s
             WHERE account_id = %s AND currency = %s
            """,
            (debit, credit, line["account_id"], line["currency"]),
        )
        if cur.rowcount != 1:  # defensive: accounts loader always seeds rows
            raise LedgerError(
                "unknown_account",
                f"no balance row for {line['account_id']} ({line['currency']})",
            )


def _next_entry_seq(cur: psycopg.Cursor) -> int:
    cur.execute("SELECT nextval(pg_get_serial_sequence('entries', 'entry_seq'))")
    return cur.fetchone()[0]


def _insert_request(
    cur: psycopg.Cursor,
    *,
    request_sha: str,
    operation: str,
    target_id: str,
    request: dict[str, Any],
    result: dict[str, Any],
    entry_seq: int | None = None,
    closed_period: str | None = None,
) -> None:
    cur.execute(
        """
        INSERT INTO requests
            (request_sha256, operation, target_id, request_json,
             response_json, entry_seq, closed_period)
        VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)
        """,
        (request_sha, operation, target_id, canonical_json(request),
         canonical_json(result), entry_seq, closed_period),
    )


# ---------------------------------------------------------------------------
# post.
# ---------------------------------------------------------------------------
def post_entry(conn: psycopg.Connection, raw: Any) -> dict[str, Any]:
    entry = normalize_journal(raw)
    request_sha = digest_of(entry)
    entry_id = entry["entry_id"]
    period = period_of(date.fromisoformat(entry["entry_date"]))

    with conn.cursor() as cur:
        _advisory_lock(cur)

        existing = _fetch_entry(cur, entry_id)
        if existing is not None:
            content = _journal_content(entry)
            if existing["content_hash"] == sha256_hex(content):
                return _json_field(existing["result_json"])
            raise LedgerError(
                "entry_id_conflict",
                f"entry_id {entry_id} already exists with different content",
                entry_id=entry_id,
            )

        closed = _entry_period_closed(cur, period)
        if closed is not None:
            raise LedgerError(
                "period_closed", f"period {period} is closed",
                period=period, snapshot_id=closed["snapshot_id"],
            )

        _validate_accounts(cur, entry["lines"])

        content = _journal_content(entry)
        content_hash = sha256_hex(content)
        entry_seq = _next_entry_seq(cur)
        result = {
            "ok": True,
            "operation": "post",
            "entry_id": entry_id,
            "entry_type": "journal",
            "entry_date": entry["entry_date"],
            "period": period,
            "entry_seq": entry_seq,
            "content_hash": content_hash,
            "line_count": len(entry["lines"]),
        }

        cur.execute(
            """
            INSERT INTO entries
                (entry_seq, entry_id, entry_type, entry_date, period,
                 canonical_content, content_hash, request_sha256,
                 request_json, result_json, reverses_entry_id)
            OVERRIDING SYSTEM VALUE
            VALUES (%s, %s, 'journal', %s, %s, %s, %s, %s, %s::jsonb,
                    %s::jsonb, NULL)
            """,
            (entry_seq, entry_id, entry["entry_date"], period, content,
             content_hash, request_sha, canonical_json(entry),
             canonical_json(result)),
        )
        _insert_lines_and_balances(cur, entry_seq, entry["lines"], reverse=False)
        _insert_request(
            cur, request_sha=request_sha, operation="post", target_id=entry_id,
            request=entry, result=result, entry_seq=entry_seq,
        )
        _crash_point("before_commit")
        conn.commit()
        _crash_point("after_commit")
    return result


def _journal_content(entry: dict[str, Any]) -> str:
    return canonical_json(
        {
            "entry_type": "journal",
            "entry_id": entry["entry_id"],
            "entry_date": entry["entry_date"],
            "lines": entry["lines"],
        }
    )


# ---------------------------------------------------------------------------
# reverse.
# ---------------------------------------------------------------------------
def reverse_entry(conn: psycopg.Connection, raw: Any) -> dict[str, Any]:
    request = normalize_reversal(raw)
    request_sha = digest_of(request)
    entry_id = request["entry_id"]
    original_id = request["reverses_entry_id"]
    period = period_of(date.fromisoformat(request["entry_date"]))

    with conn.cursor() as cur:
        _advisory_lock(cur)

        existing = _fetch_entry(cur, entry_id)
        if existing is not None:
            if existing["content_hash"] == sha256_hex(_reverse_content(request)):
                return _json_field(existing["result_json"])
            raise LedgerError(
                "entry_id_conflict",
                f"entry_id {entry_id} already exists with different content",
                entry_id=entry_id,
            )

        original = _fetch_entry(cur, original_id)
        if original is None:
            raise LedgerError(
                "not_found", f"original entry {original_id} not found",
                original_entry_id=original_id,
            )
        if original["entry_type"] != "journal":
            raise LedgerError(
                "not_reversible",
                f"entry {original_id} is itself a reversal and cannot be reversed",
                original_entry_id=original_id,
            )

        # A reversal may target an original in a closed period, but the
        # reversal's own accounting period must still be open.
        closed = _entry_period_closed(cur, period)
        if closed is not None:
            raise LedgerError(
                "period_closed", f"period {period} is closed",
                period=period, snapshot_id=closed["snapshot_id"],
            )

        cur.execute(
            "SELECT 1 FROM entries WHERE reverses_entry_id = %s",
            (original_id,),
        )
        if cur.fetchone():
            raise LedgerError(
                "already_reversed",
                f"entry {original_id} has already been successfully reversed",
                original_entry_id=original_id,
            )

        cur.execute(
            """
            SELECT account_id, currency, debit_cents, credit_cents
              FROM postings WHERE entry_seq = %s ORDER BY line_no
            """,
            (original["entry_seq"],),
        )
        original_lines = [
            {"account_id": r[0], "currency": r[1],
             "debit_cents": r[2], "credit_cents": r[3]}
            for r in cur.fetchall()
        ]

        content = _reverse_content(request)
        content_hash = sha256_hex(content)
        entry_seq = _next_entry_seq(cur)
        result = {
            "ok": True,
            "operation": "reverse",
            "entry_id": entry_id,
            "entry_type": "reversal",
            "reverses_entry_id": original_id,
            "entry_date": request["entry_date"],
            "period": period,
            "entry_seq": entry_seq,
            "content_hash": content_hash,
            "line_count": len(original_lines),
        }
        cur.execute(
            """
            INSERT INTO entries
                (entry_seq, entry_id, entry_type, entry_date, period,
                 canonical_content, content_hash, request_sha256,
                 request_json, result_json, reverses_entry_id)
            OVERRIDING SYSTEM VALUE
            VALUES (%s, %s, 'reversal', %s, %s, %s, %s, %s, %s::jsonb,
                    %s::jsonb, %s)
            """,
            (entry_seq, entry_id, request["entry_date"], period, content,
             content_hash, request_sha, canonical_json(request),
             canonical_json(result), original_id),
        )
        _insert_lines_and_balances(cur, entry_seq, original_lines, reverse=True)
        _insert_request(
            cur, request_sha=request_sha, operation="reverse", target_id=entry_id,
            request=request, result=result, entry_seq=entry_seq,
        )
        _crash_point("before_commit")
        conn.commit()
        _crash_point("after_commit")
    return result


def _reverse_content(request: dict[str, Any]) -> str:
    return canonical_json(
        {
            "entry_type": "reversal",
            "entry_id": request["entry_id"],
            "reverses_entry_id": request["reverses_entry_id"],
            "entry_date": request["entry_date"],
        }
    )


# ---------------------------------------------------------------------------
# close.
# ---------------------------------------------------------------------------
def close_period(conn: psycopg.Connection, raw: Any) -> dict[str, Any]:
    request = normalize_close(raw)
    request_sha = digest_of(request)
    period = request["period"]

    with conn.cursor() as cur:
        _advisory_lock(cur)
        # Backstop for writers that bypass this CLI: block concurrent inserts
        # into entries/postings while the close aggregate is read and the
        # snapshot row is written.
        cur.execute("LOCK TABLE entries, postings IN SHARE MODE")

        existing = _fetch_period(cur, period)
        if existing is not None:
            return _json_field(existing["result_json"])

        cur.execute(
            """
            SELECT p.account_id, p.currency,
                   sum(p.debit_cents)::bigint  AS debit_cents,
                   sum(p.credit_cents)::bigint AS credit_cents,
                   (sum(p.debit_cents) - sum(p.credit_cents))::bigint AS net_cents
              FROM entries e
              JOIN postings p USING (entry_seq)
             WHERE e.period = %s
             GROUP BY p.account_id, p.currency
             ORDER BY p.account_id, p.currency
            """,
            (period,),
        )
        rows = cur.fetchall()
        cur.execute(
            "SELECT count(*) FROM entries WHERE period = %s", (period,)
        )
        entry_count = cur.fetchone()[0]
        cur.execute(
            """
            SELECT count(*) FROM postings p JOIN entries e USING (entry_seq)
             WHERE e.period = %s
            """,
            (period,),
        )
        posting_count = cur.fetchone()[0]

        # A non-empty period must balance to zero in every currency.  This is
        # guaranteed by per-entry checks, but close re-verifies the aggregate.
        totals: dict[str, int] = {}
        for _acct, currency, debit, credit, _net in rows:
            totals[currency] = totals.get(currency, 0) + (int(debit) - int(credit))
        bad = {c: d for c, d in totals.items() if d != 0}
        if bad:
            raise LedgerError(
                "unbalanced_period",
                f"period {period} does not net to zero per currency",
                currencies=bad,
            )

        accounts = [
            {"account_id": acct, "currency": currency,
             "debit_cents": int(debit), "credit_cents": int(credit),
             "net_cents": int(net)}
            for acct, currency, debit, credit, net in rows
        ]
        cur.execute("SELECT nextval('period_snapshot_id_seq')")
        snapshot_id = cur.fetchone()[0]
        cur.execute("SELECT clock_timestamp()")
        closed_at = cur.fetchone()[0]
        snapshot = {
            "period": period,
            "snapshot_id": snapshot_id,
            "entry_count": entry_count,
            "posting_count": posting_count,
            "accounts": accounts,
        }
        snapshot_hash = sha256_hex(canonical_json(snapshot))
        result = {
            "ok": True,
            "operation": "close",
            "period": period,
            "snapshot_id": snapshot_id,
            "snapshot_hash": snapshot_hash,
            "entry_count": entry_count,
            "posting_count": posting_count,
            "closed_at": closed_at.isoformat(),
            "accounts": accounts,
        }

        cur.execute(
            """
            INSERT INTO periods
                (period, snapshot_id, snapshot_hash, entry_count,
                 posting_count, snapshot, request_sha256, request_json,
                 result_json, closed_at)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb,
                    %s::jsonb, %s)
            """,
            (period, snapshot_id, snapshot_hash, entry_count, posting_count,
             canonical_json(snapshot), request_sha, canonical_json(request),
             canonical_json(result), closed_at),
        )
        for account in accounts:
            cur.execute(
                """
                INSERT INTO period_snapshots
                    (period, account_id, currency, debit_cents,
                     credit_cents, net_cents)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (period, account["account_id"], account["currency"],
                 account["debit_cents"], account["credit_cents"],
                 account["net_cents"]),
            )
        _insert_request(
            cur, request_sha=request_sha, operation="close", target_id=period,
            request=request, result=result, closed_period=period,
        )
        _crash_point("before_commit")
        conn.commit()
        _crash_point("after_commit")
    return result


# ---------------------------------------------------------------------------
# Read-only operations.
# ---------------------------------------------------------------------------
def trial_balance(conn: psycopg.Connection, *, period: str | None = None) -> dict[str, Any]:
    with conn.cursor() as cur:
        if period is None:
            source = "all committed postings"
            cur.execute(
                """
                SELECT account_id, currency, sum(debit_cents)::bigint,
                       sum(credit_cents)::bigint,
                       (sum(debit_cents) - sum(credit_cents))::bigint
                  FROM postings GROUP BY account_id, currency
                 ORDER BY account_id, currency
                """
            )
        else:
            validate_period(period)
            source = f"postings whose entry_date is in {period}"
            cur.execute(
                """
                SELECT p.account_id, p.currency,
                       sum(p.debit_cents)::bigint,
                       sum(p.credit_cents)::bigint,
                       (sum(p.debit_cents) - sum(p.credit_cents))::bigint
                  FROM postings p JOIN entries e USING (entry_seq)
                 WHERE e.period = %s
                 GROUP BY p.account_id, p.currency
                 ORDER BY p.account_id, p.currency
                """,
                (period,),
            )
        rows = cur.fetchall()

    currency_totals: dict[str, dict[str, int]] = {}
    accounts = []
    for account_id, currency, debit, credit, net in rows:
        accounts.append(
            {"account_id": account_id, "currency": currency,
             "debit_cents": int(debit), "credit_cents": int(credit),
             "net_cents": int(net)}
        )
        bucket = currency_totals.setdefault(
            currency, {"debit_cents": 0, "credit_cents": 0, "net_cents": 0}
        )
        bucket["debit_cents"] += int(debit)
        bucket["credit_cents"] += int(credit)
        bucket["net_cents"] += int(net)
    return {
        "ok": True, "operation": "trial-balance", "source": source,
        "period": period, "accounts": accounts, "currencies": currency_totals,
    }


def audit(conn: psycopg.Connection) -> dict[str, Any]:
    """Verify stored state against raw postings with independent SQL."""
    with conn.cursor() as cur:
        # 1. Cumulative balances vs raw posting sums.
        cur.execute(
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
        balance_mismatches = [
            {"account_id": a, "currency": c,
             "stored_debit": int(d), "stored_credit": int(cr),
             "raw_debit": int(rd), "raw_credit": int(rc)}
            for a, c, d, cr, rd, rc in cur.fetchall()
            if (d, cr) != (rd, rc)
        ]
        cur.execute(
            """
            SELECT p.account_id, p.currency
              FROM postings p
             GROUP BY p.account_id, p.currency
            HAVING NOT EXISTS (
                SELECT 1 FROM account_balances b
                 WHERE b.account_id = p.account_id
                   AND b.currency = p.currency)
            """
        )
        balances_missing = [f"{a}|{c}" for a, c in cur.fetchall()]

        # 2. Whole ledger must net to zero in every currency.
        cur.execute(
            """
            SELECT p.currency, (sum(p.debit_cents) - sum(p.credit_cents))::bigint
              FROM postings p GROUP BY p.currency ORDER BY p.currency
            """
        )
        currency_diffs = [
            {"currency": c, "diff_cents": int(d)} for c, d in cur.fetchall()
        ]

        # 2b. Posting currency must equal the account's fixed currency.
        cur.execute(
            """
            SELECT DISTINCT p.account_id, p.currency, a.currency
              FROM postings p JOIN accounts a USING (account_id)
             WHERE p.currency <> a.currency
            """
        )
        posting_currency_problems = [
            {"account_id": acct, "posting_currency": c, "account_currency": a}
            for acct, c, a in cur.fetchall()
        ]

        # 3. Every entry must balance in every currency it touches.
        cur.execute(
            """
            SELECT e.entry_id, p.currency,
                   sum(p.debit_cents)::bigint,
                   sum(p.credit_cents)::bigint
              FROM entries e JOIN postings p USING (entry_seq)
             GROUP BY e.entry_id, p.currency
            HAVING sum(p.debit_cents) <> sum(p.credit_cents)
            ORDER BY e.entry_id, p.currency
            """
        )
        unbalanced_entries = [
            {"entry_id": eid, "currency": c,
             "debit_cents": int(d), "credit_cents": int(cr)}
            for eid, c, d, cr in cur.fetchall()
        ]

        # 4. Entries without any posting, and content/digest integrity.
        cur.execute(
            """
            SELECT e.entry_id FROM entries e
             WHERE NOT EXISTS (
                SELECT 1 FROM postings p WHERE p.entry_seq = e.entry_seq)
            """
        )
        empty_entries = [r[0] for r in cur.fetchall()]
        cur.execute(
            f"SELECT {', '.join(ENTRY_COLUMNS)} FROM entries ORDER BY entry_seq"
        )
        entry_rows = [dict(zip(ENTRY_COLUMNS, r)) for r in cur.fetchall()]
        content_problems = []
        for e in entry_rows:
            problems = []
            if sha256_hex(e["canonical_content"]) != e["content_hash"]:
                problems.append("content_hash")
            request = _json_field(e["request_json"])
            if digest_of(request) != e["request_sha256"]:
                problems.append("request_sha256")
            if problems:
                content_problems.append(
                    {"entry_id": e["entry_id"], "problems": problems}
                )

        # 5. Request records: one committed request per effect, with digest and
        #    response matching the stored ledger response.
        cur.execute(
            """
            SELECT r.request_sha256, r.operation, r.target_id, r.request_json,
                   r.response_json, r.entry_seq, r.closed_period,
                   e.entry_id, e.entry_type, e.entry_seq, e.result_json
              FROM requests r
              LEFT JOIN entries e ON e.entry_seq = r.entry_seq
             ORDER BY r.created_at
            """
        )
        request_problems = []
        for sha, op, target, req_json, resp_json, e_seq, closed, e_id, e_type, \
                entry_seq, e_result in cur.fetchall():
            req = _json_field(req_json)
            resp = _json_field(resp_json)
            problems = []
            if digest_of(req) != sha:
                problems.append("request_digest")
            if op in ("post", "reverse"):
                if e_seq is None or e_id is None:
                    problems.append("missing_entry_effect")
                elif (e_type == "journal") != (op == "post"):
                    problems.append("operation_type_mismatch")
                elif _json_field(e_result) != resp:
                    problems.append("response_entry_mismatch")
            if problems:
                request_problems.append(
                    {"request_sha256": sha, "operation": op,
                     "problems": problems}
                )

        # 6. Periods: hashes, counts, per-account snapshots and request rows.
        cur.execute(
            f"""
            SELECT pe.{', pe.'.join(PERIOD_COLUMNS)}
              FROM periods pe ORDER BY pe.period
            """
        )
        period_rows = [dict(zip(PERIOD_COLUMNS, r)) for r in cur.fetchall()]
        snapshot_problems = []
        snapshot_summaries = []
        for pe in period_rows:
            period = pe["period"]
            snap = _json_field(pe["snapshot"])
            problems = []
            if sha256_hex(canonical_json(snap)) != pe["snapshot_hash"]:
                problems.append("snapshot_hash")
            cur.execute(
                "SELECT count(*) FROM entries WHERE period = %s", (period,)
            )
            raw_entries = cur.fetchone()[0]
            cur.execute(
                """
                SELECT count(*) FROM postings p JOIN entries e USING (entry_seq)
                 WHERE e.period = %s
                """,
                (period,),
            )
            raw_postings = cur.fetchone()[0]
            if raw_entries != pe["entry_count"]:
                problems.append("entry_count")
            if raw_postings != pe["posting_count"]:
                problems.append("posting_count")
            cur.execute(
                """
                SELECT account_id, currency, debit_cents, credit_cents, net_cents
                  FROM period_snapshots WHERE period = %s
                 ORDER BY account_id, currency
                """,
                (period,),
            )
            stored = [
                {"account_id": a, "currency": c, "debit_cents": int(d),
                 "credit_cents": int(cr), "net_cents": int(n)}
                for a, c, d, cr, n in cur.fetchall()
            ]
            if stored != snap.get("accounts"):
                problems.append("snapshot_account_rows")
            cur.execute(
                """
                SELECT p.account_id, p.currency,
                       sum(p.debit_cents)::bigint,
                       sum(p.credit_cents)::bigint,
                       (sum(p.debit_cents) - sum(p.credit_cents))::bigint
                  FROM postings p JOIN entries e USING (entry_seq)
                 WHERE e.period = %s
                 GROUP BY p.account_id, p.currency
                 ORDER BY p.account_id, p.currency
                """,
                (period,),
            )
            raw = [
                {"account_id": a, "currency": c, "debit_cents": int(d),
                 "credit_cents": int(cr), "net_cents": int(n)}
                for a, c, d, cr, n in cur.fetchall()
            ]
            if stored != raw:
                problems.append("snapshot_vs_raw")
            cur.execute(
                """
                SELECT count(*) FROM requests
                 WHERE operation = 'close' AND closed_period = %s
                """,
                (period,),
            )
            if cur.fetchone()[0] != 1:
                problems.append("close_request")
            if digest_of(_json_field(pe["request_json"])) != pe["request_sha256"]:
                problems.append("close_request_digest")
            if _json_field(pe["result_json"]) != {
                "ok": True,
                "operation": "close",
                "period": period,
                "snapshot_id": pe["snapshot_id"],
                "snapshot_hash": pe["snapshot_hash"],
                "entry_count": pe["entry_count"],
                "posting_count": pe["posting_count"],
                "closed_at": pe["closed_at"].isoformat(),
                "accounts": snap.get("accounts"),
            }:
                problems.append("close_result_shape")
            if problems:
                snapshot_problems.append({"period": period, "problems": problems})
            snapshot_summaries.append(
                {"period": period, "snapshot_id": pe["snapshot_id"],
                 "snapshot_hash": pe["snapshot_hash"],
                 "entry_count": pe["entry_count"],
                 "posting_count": pe["posting_count"]}
            )

        # 7. Reversal relationships: exactly one live reversal, mirrored lines
        #    (same line count and swapped debit/credit), and zero net effect
        #    per account/currency for original + reversal.
        cur.execute(
            """
            SELECT orig.entry_id, orig.entry_seq, orig.entry_type,
                   orig.period, rev.entry_id, rev.entry_seq, rev.entry_type,
                   rev.period
              FROM entries rev
              JOIN entries orig ON orig.entry_id = rev.reverses_entry_id
             ORDER BY rev.entry_seq
            """
        )
        reversal_rows = cur.fetchall()
        reversal_checks = []
        for orig_id, orig_seq, orig_type, orig_period, rev_id, rev_seq, \
                rev_type, rev_period in reversal_rows:
            problems = []
            if orig_type != "journal" or rev_type != "reversal":
                problems.append("entry_type")
            cur.execute(
                """
                SELECT account_id, currency, debit_cents, credit_cents
                  FROM postings WHERE entry_seq = %s ORDER BY line_no
                """,
                (orig_seq,),
            )
            orig_lines = cur.fetchall()
            cur.execute(
                """
                SELECT account_id, currency, debit_cents, credit_cents
                  FROM postings WHERE entry_seq = %s ORDER BY line_no
                """,
                (rev_seq,),
            )
            rev_lines = cur.fetchall()
            if len(orig_lines) != len(rev_lines):
                problems.append("line_count")
            else:
                for o, r in zip(orig_lines, rev_lines):
                    if (o[0], o[1], o[2], o[3]) != (r[0], r[1], r[3], r[2]):
                        problems.append("line_mirror")
                        break
            cur.execute(
                """
                SELECT p.account_id, p.currency,
                       sum(p.debit_cents)::bigint,
                       sum(p.credit_cents)::bigint,
                       (sum(p.debit_cents) - sum(p.credit_cents))::bigint
                  FROM postings p
                 WHERE p.entry_seq IN (%s, %s)
                 GROUP BY p.account_id, p.currency
                """,
                (orig_seq, rev_seq),
            )
            nonzero = [
                {"account_id": a, "currency": c, "net_cents": int(n)}
                for a, c, _d, _cr, n in cur.fetchall() if int(n) != 0
            ]
            if nonzero:
                problems.append("nonzero_net_effect")
            if problems:
                reversal_checks.append({
                    "original_entry_id": orig_id,
                    "reversal_entry_id": rev_id,
                    "problems": problems,
                    "nonzero_net_accounts": nonzero,
                    "original_period": orig_period,
                    "reversal_period": rev_period,
                })

        cur.execute("SELECT count(*) FROM entries")
        entry_total = cur.fetchone()[0]

    ok = (
        not balance_mismatches
        and not balances_missing
        and all(c["diff_cents"] == 0 for c in currency_diffs)
        and not posting_currency_problems
        and not unbalanced_entries
        and not empty_entries
        and not content_problems
        and not request_problems
        and not snapshot_problems
        and not reversal_checks
    )
    return {
        "ok": ok,
        "operation": "audit",
        "entry_count": int(entry_total),
        "account_balance_mismatches": balance_mismatches,
        "balances_missing": balances_missing,
        "currency_diffs": currency_diffs,
        "posting_currency_problems": posting_currency_problems,
        "unbalanced_entries": unbalanced_entries,
        "empty_entries": empty_entries,
        "content_problems": content_problems,
        "request_problems": request_problems,
        "snapshot_problems": snapshot_problems,
        "snapshots": snapshot_summaries,
        "reversal_checks": reversal_checks,
    }


def show_entry(conn: psycopg.Connection, entry_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        found = _fetch_entry(cur, entry_id)
        if found is None:
            raise LedgerError("not_found", f"entry {entry_id} not found")
        cur.execute(
            """
            SELECT line_no, account_id, currency, debit_cents, credit_cents
              FROM postings WHERE entry_seq = %s ORDER BY line_no
            """,
            (found["entry_seq"],),
        )
        lines = [
            {"line_no": n, "account_id": a, "currency": c,
             "debit_cents": int(d), "credit_cents": int(cr)}
            for n, a, c, d, cr in cur.fetchall()
        ]
    return {
        "ok": True,
        "entry_id": found["entry_id"],
        "entry_type": found["entry_type"],
        "entry_date": found["entry_date"].isoformat(),
        "period": found["period"],
        "reverses_entry_id": found["reverses_entry_id"],
        "content_hash": found["content_hash"],
        "request_sha256": found["request_sha256"],
        "created_at": found["created_at"].isoformat(),
        "lines": lines,
    }


# ---------------------------------------------------------------------------
# Synthetic data generation (no real-world data).
# ---------------------------------------------------------------------------
DEMO_ACCOUNTS = [
    {"account_id": "cash-usd", "currency": "USD"},
    {"account_id": "cash-eur", "currency": "EUR"},
    {"account_id": "cash-jpy", "currency": "JPY"},
    {"account_id": "revenue-usd", "currency": "USD"},
    {"account_id": "revenue-eur", "currency": "EUR"},
    {"account_id": "revenue-jpy", "currency": "JPY"},
    {"account_id": "receivable-usd", "currency": "USD"},
    {"account_id": "payable-eur", "currency": "EUR"},
    {"account_id": "suspense-usd", "currency": "USD"},
]


def generate_demo(period: str, out_dir: str) -> dict[str, Any]:
    """Write a deterministic synthetic accounts CSV and entry JSON files."""
    validate_period(period)
    os.makedirs(os.path.join(out_dir, "entries"), exist_ok=True)
    accounts_path = os.path.join(out_dir, "accounts.csv")
    with open(accounts_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["account_id", "currency"])
        writer.writeheader()
        writer.writerows(DEMO_ACCOUNTS)

    sample = [
        ("demo-001", "USD", "cash-usd", "revenue-usd", 120000),
        ("demo-002", "EUR", "cash-eur", "revenue-eur", 90000),
        ("demo-003", "JPY", "cash-jpy", "revenue-jpy", 500000),
        ("demo-004", "USD", "receivable-usd", "revenue-usd", 75000),
        ("demo-005", "EUR", "cash-eur", "payable-eur", 33000),
        ("demo-006", "USD", "cash-usd", "suspense-usd", 41000),
    ]
    written = []
    for idx, (entry_id, currency, debit_acct, credit_acct, cents) in enumerate(
        sample, start=1
    ):
        day = (idx - 1) % 28 + 1
        entry = {
            "entry_id": entry_id,
            "entry_date": f"{period}-{day:02d}",
            "lines": [
                {"account_id": debit_acct, "currency": currency,
                 "debit_cents": cents, "credit_cents": 0},
                {"account_id": credit_acct, "currency": currency,
                 "debit_cents": 0, "credit_cents": cents},
            ],
        }
        path = os.path.join(out_dir, "entries", f"{entry_id}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(entry, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        written.append(path)

    multi = {
        "entry_id": "demo-multi",
        "entry_date": f"{period}-15",
        "lines": [
            {"account_id": "cash-usd", "currency": "USD",
             "debit_cents": 50000, "credit_cents": 0},
            {"account_id": "revenue-usd", "currency": "USD",
             "debit_cents": 0, "credit_cents": 50000},
            {"account_id": "cash-eur", "currency": "EUR",
             "debit_cents": 30000, "credit_cents": 0},
            {"account_id": "revenue-eur", "currency": "EUR",
             "debit_cents": 0, "credit_cents": 30000},
            {"account_id": "cash-jpy", "currency": "JPY",
             "debit_cents": 100000, "credit_cents": 0},
            {"account_id": "revenue-jpy", "currency": "JPY",
             "debit_cents": 0, "credit_cents": 100000},
        ],
    }
    multi_path = os.path.join(out_dir, "entries", "demo-multi.json")
    with open(multi_path, "w", encoding="utf-8") as fh:
        json.dump(multi, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    written.append(multi_path)
    return {"ok": True, "operation": "demo-data", "period": period,
            "accounts_csv": accounts_path, "entry_files": written}


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------
def _read_json_arg(path: str | None, inline: str | None, what: str) -> Any:
    if path and inline:
        raise LedgerError(
            "invalid_request",
            f"provide either --{what}-file or --{what}-json, not both",
        )
    if path:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except OSError as exc:
            raise LedgerError(
                "invalid_request", f"cannot read {path}: {exc}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise LedgerError(
                "invalid_request", f"{path} is not valid JSON: {exc}"
            ) from exc
    if inline is not None:
        try:
            return json.loads(inline)
        except json.JSONDecodeError as exc:
            raise LedgerError(
                "invalid_request", f"--{what}-json is invalid JSON: {exc}"
            ) from exc
    raise LedgerError(
        "invalid_request", f"missing --{what}-file or --{what}-json"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ledger",
        description="Synthetic multi-currency ledger with month-end close.",
    )
    parser.add_argument(
        "--dsn", default=None,
        help="PostgreSQL DSN; defaults to LEDGER_DSN or the local socket",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="apply migrations (idempotent)")
    p_reset = sub.add_parser("reset-db", help="drop schema and reapply migrations")
    p_reset.add_argument("--yes", action="store_true", required=True,
                         help="required; destroys all ledger data in the DB")

    p_load = sub.add_parser("load-accounts", help="load accounts CSV")
    p_load.add_argument("csv_path")

    p_post = sub.add_parser("post", help="post a journal entry")
    p_post.add_argument("--entry-file")
    p_post.add_argument("--entry-json")

    p_rev = sub.add_parser("reverse", help="reverse an original journal entry")
    p_rev.add_argument("--reversal-file")
    p_rev.add_argument("--reversal-json")

    p_close = sub.add_parser("close", help="close a period and snapshot it")
    p_close.add_argument("--period", required=True)

    p_tb = sub.add_parser("trial-balance", help="read a trial balance")
    p_tb.add_argument("--period")

    sub.add_parser("audit", help="verify every invariant against raw rows")

    p_show = sub.add_parser("show-entry", help="read one entry")
    p_show.add_argument("entry_id")

    p_demo = sub.add_parser("demo-data", help="write synthetic CSV/JSON samples")
    p_demo.add_argument("--period", required=True)
    p_demo.add_argument("--out-dir", default="demo")

    p_sql = sub.add_parser("sql", help="run a read-only SQL query")
    p_sql.add_argument("query")

    return parser


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    dsn = args.dsn
    try:
        if args.command == "init-db":
            out = migrate(dsn)
        elif args.command == "reset-db":
            out = migrate(dsn, reset=True)
        elif args.command == "load-accounts":
            with connect(dsn) as conn:
                out = load_accounts(conn, load_accounts_file(args.csv_path))
        elif args.command == "post":
            raw = _read_json_arg(args.entry_file, args.entry_json, "entry")
            with connect(dsn) as conn:
                out = post_entry(conn, raw)
        elif args.command == "reverse":
            raw = _read_json_arg(
                args.reversal_file, args.reversal_json, "reversal"
            )
            with connect(dsn) as conn:
                out = reverse_entry(conn, raw)
        elif args.command == "close":
            with connect(dsn) as conn:
                out = close_period(conn, {"period": args.period})
        elif args.command == "trial-balance":
            with connect(dsn) as conn:
                out = trial_balance(conn, period=args.period)
        elif args.command == "audit":
            with connect(dsn) as conn:
                out = audit(conn)
        elif args.command == "show-entry":
            with connect(dsn) as conn:
                out = show_entry(conn, args.entry_id)
        elif args.command == "demo-data":
            out = generate_demo(args.period, args.out_dir)
        elif args.command == "sql":
            with connect(dsn) as conn:
                # Read-only transaction: this helper cannot mutate ledger facts.
                conn.read_only = True
                with conn.cursor() as cur:
                    cur.execute(args.query)
                    rows = cur.fetchall()
                    names = [d.name for d in (cur.description or [])]
                conn.rollback()
            out = {"ok": True, "columns": names,
                   "rows": [list(r) for r in rows]}
        else:  # pragma: no cover
            parser.error("unknown command")
    except LedgerError as exc:
        print(json.dumps(exc.as_dict(), ensure_ascii=False, indent=2))
        return 1
    except psycopg.OperationalError as exc:
        print(json.dumps(
            {"ok": False, "error": "connection_error",
             "message": str(exc).strip()}, ensure_ascii=False, indent=2))
        return 2
    except psycopg.Error as exc:
        # Database backstops (triggers, constraints) surface as JSON.
        diag = getattr(exc, "diag", None)
        code = getattr(diag, "sqlstate", None) or "database_error"
        print(json.dumps(
            {"ok": False, "error": "database_error", "sqlstate": code,
             "message": getattr(exc, "message", None) or str(exc).strip()},
            ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
