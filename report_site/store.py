"""SQLite persistence for the synthetic site.

Only the site imports this module.  The helper talks to the site exclusively
over HTTP; it never opens this database.  Ground truth is exported separately
from a deterministic domain generator so that verification does not trust
the same code path that renders the pages.
"""

from __future__ import annotations

import csv
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .domain import MONTHS, ORGS, TYPES, amount_columns, generate_rows, summarize

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS sessions(
    sid TEXT PRIMARY KEY,
    csrf TEXT NOT NULL,
    org_key TEXT,
    org_label TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_tokens(
    token TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS previews(
    handle TEXT PRIMARY KEY,
    sid TEXT NOT NULL,
    qid TEXT NOT NULL,
    org_key TEXT NOT NULL,
    org_label TEXT NOT NULL,
    month_key TEXT NOT NULL,
    month_label TEXT NOT NULL,
    type_key TEXT NOT NULL,
    type_label TEXT NOT NULL,
    columns_json TEXT NOT NULL,
    rows_json TEXT NOT NULL,
    total TEXT NOT NULL,
    dl_token TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    downloaded_at TEXT
);
CREATE TABLE IF NOT EXISTS queries(
    qid TEXT PRIMARY KEY,
    handle TEXT NOT NULL,
    org_key TEXT NOT NULL,
    month_key TEXT NOT NULL,
    type_key TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    total TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS request_log(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    phase TEXT NOT NULL,
    status INTEGER NOT NULL,
    session_present INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    qid TEXT,
    redirect TEXT,
    content_type TEXT,
    bytes INTEGER
);
CREATE TABLE IF NOT EXISTS truth_export(
    org_key TEXT NOT NULL,
    org_label TEXT NOT NULL,
    month_key TEXT NOT NULL,
    month_label TEXT NOT NULL,
    type_key TEXT NOT NULL,
    type_label TEXT NOT NULL,
    columns TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    total TEXT NOT NULL,
    PRIMARY KEY (org_key, month_key, type_key)
);
CREATE INDEX IF NOT EXISTS idx_log_qid ON request_log(qid);
CREATE INDEX IF NOT EXISTS idx_log_phase ON request_log(phase);
"""


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_db(db_path: str | Path) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        with conn:
            conn.execute("DELETE FROM sessions")
            conn.execute("DELETE FROM pending_tokens")
            conn.execute("DELETE FROM previews")
            conn.execute("DELETE FROM queries")
            seed_truth(conn)
    finally:
        conn.close()


def seed_truth(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM truth_export")
    month_labels = dict(MONTHS)
    insert = (
        "INSERT INTO truth_export(org_key, org_label, month_key, month_label,"
        " type_key, type_label, columns, row_count, total) VALUES(?,?,?,?,?,?,?,?,?)"
    )
    for org_key, org_label in ORGS:
        for month_key, _ in MONTHS:
            for type_key, type_label in TYPES:
                rows = generate_rows(org_key, month_key, type_key)
                spec = summarize(rows)
                conn.execute(insert, (
                    org_key, org_label, month_key, month_labels[month_key],
                    type_key, type_label, "|".join(amount_columns(type_key)),
                    spec["count"], spec["total"],
                ))


def export_truth_csv(conn: sqlite3.Connection, out_path: str | Path) -> int:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows = conn.execute(
        "SELECT * FROM truth_export ORDER BY org_key, month_key, type_key"
    ).fetchall()
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["org_key", "org_label", "month_key", "month_label",
                         "type_key", "type_label", "columns", "row_count", "total"])
        for r in rows:
            writer.writerow([r["org_key"], r["org_label"], r["month_key"],
                             r["month_label"], r["type_key"], r["type_label"],
                             r["columns"], r["row_count"], r["total"]])
    return len(rows)


# ---------------------------------------------------------------- sessions
def create_pending(conn: sqlite3.Connection, ttl: int = 900) -> str:
    token = secrets.token_urlsafe(24)
    now = now_utc()
    with conn:
        conn.execute("DELETE FROM pending_tokens WHERE expires_at < ?", (fmt(now),))
        conn.execute(
            "INSERT INTO pending_tokens(token, created_at, expires_at) VALUES(?,?,?)",
            (token, fmt(now), fmt(now + timedelta(seconds=ttl))),
        )
    return token


def consume_pending(conn: sqlite3.Connection, token: str) -> bool:
    row = conn.execute("SELECT * FROM pending_tokens WHERE token=?", (token,)).fetchone()
    if row is None:
        return False
    expired = row["expires_at"] < fmt(now_utc())
    with conn:
        conn.execute("DELETE FROM pending_tokens WHERE token=?", (token,))
    return not expired


def create_session(conn: sqlite3.Connection, ttl: int) -> tuple[str, str]:
    sid = secrets.token_hex(24)
    csrf = secrets.token_urlsafe(24)
    now = now_utc()
    with conn:
        conn.execute(
            "INSERT INTO sessions(sid, csrf, created_at, expires_at) VALUES(?,?,?,?)",
            (sid, csrf, fmt(now), fmt(now + timedelta(seconds=ttl))),
        )
    return sid, csrf


def fetch_session(conn: sqlite3.Connection, sid: str | None):
    if not sid:
        return None
    row = conn.execute("SELECT * FROM sessions WHERE sid=?", (sid,)).fetchone()
    if row is None:
        return None
    if row["expires_at"] < fmt(now_utc()):
        with conn:
            conn.execute("DELETE FROM sessions WHERE sid=?", (sid,))
        return None
    return row


def rotate_csrf(conn: sqlite3.Connection, sid: str, ttl: int = 900) -> str:
    csrf = secrets.token_urlsafe(24)
    with conn:
        conn.execute("UPDATE sessions SET csrf=?, expires_at=? WHERE sid=?",
                     (csrf, fmt(now_utc() + timedelta(seconds=ttl)), sid))
    return csrf


def set_session_org(conn: sqlite3.Connection, sid: str, key: str, label: str) -> None:
    with conn:
        conn.execute("UPDATE sessions SET org_key=?, org_label=? WHERE sid=?",
                     (key, label, sid))


def delete_session(conn: sqlite3.Connection, sid: str | None) -> None:
    if sid:
        with conn:
            conn.execute("DELETE FROM sessions WHERE sid=?", (sid,))


# ---------------------------------------------------------------- previews
def create_preview(conn: sqlite3.Connection, sid: str, org_key: str, org_label: str,
                   month_key: str, month_label: str, type_key: str, type_label: str,
                   rows, columns: list[str], total: str) -> dict:
    handle = secrets.token_urlsafe(12)
    qid = "Q-" + secrets.token_hex(7).upper()
    dl_token = secrets.token_urlsafe(18)
    created = fmt(now_utc())
    rows_json = json.dumps([
        {"code": r.code, "name": r.name, "amount": r.amount} for r in rows
    ], ensure_ascii=False)
    with conn:
        conn.execute(
            "INSERT INTO previews(handle, sid, qid, org_key, org_label, month_key,"
            " month_label, type_key, type_label, columns_json, rows_json, total,"
            " dl_token, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (handle, sid, qid, org_key, org_label, month_key, month_label,
             type_key, type_label, json.dumps(columns, ensure_ascii=False),
             rows_json, total, dl_token, created),
        )
        conn.execute(
            "INSERT INTO queries(qid, handle, org_key, month_key, type_key,"
            " row_count, total, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (qid, handle, org_key, month_key, type_key, len(rows), total, created),
        )
    return {"handle": handle, "qid": qid, "dl_token": dl_token,
            "columns": columns, "rows": rows, "total": total}


def get_preview(conn: sqlite3.Connection, handle: str):
    return conn.execute("SELECT * FROM previews WHERE handle=?", (handle,)).fetchone()


def get_preview_different_from(conn: sqlite3.Connection, preview):
    """Another preview with a different (org, month, type) identity.

    Used by the mismatch fault so the CSV's identity visibly differs from the
    preview the user just saw.
    """
    return conn.execute(
        "SELECT * FROM previews WHERE org_key != ? OR month_key != ? OR type_key != ?"
        " ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (preview["org_key"], preview["month_key"], preview["type_key"]),
    ).fetchone()


def mark_preview_downloaded(conn: sqlite3.Connection, handle: str) -> None:
    with conn:
        conn.execute("UPDATE previews SET downloaded_at=? WHERE handle=?",
                     (fmt(now_utc()), handle))


# ---------------------------------------------------------------- logging
def log_request(conn: sqlite3.Connection, *, method: str, path: str, phase: str,
                status: int, session_present: bool, note: str = "",
                qid: str | None = None, redirect: str | None = None,
                content_type: str | None = None, size: int | None = None) -> None:
    with conn:
        conn.execute(
            "INSERT INTO request_log(ts, method, path, phase, status,"
            " session_present, note, qid, redirect, content_type, bytes)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (fmt(now_utc()), method, path, phase, status, int(session_present),
             note, qid, redirect, content_type, size),
        )
