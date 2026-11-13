"""SQLite persistence for the blind annotation service.

All state transitions are serialized with ``BEGIN IMMEDIATE`` transactions so
that concurrent submissions and concurrent arbitration attempts cannot create
conflicting final records.  Reads use deferred transactions to obtain a
consistent snapshot across the several queries that build a response.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .fixtures import TaskFixtures, canonical_json

SCHEMA_VERSION = "1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    seq     INTEGER PRIMARY KEY,
    item_id TEXT NOT NULL UNIQUE,
    text    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS labels (
    seq      INTEGER PRIMARY KEY,
    label_id TEXT NOT NULL UNIQUE,
    name     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id      TEXT PRIMARY KEY,
    role         TEXT NOT NULL CHECK (role IN ('annotator', 'arbiter')),
    token_sha256 TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS assignments (
    item_id TEXT NOT NULL REFERENCES items(item_id),
    user_id TEXT NOT NULL REFERENCES users(user_id),
    PRIMARY KEY (item_id, user_id)
);

CREATE TABLE IF NOT EXISTS submissions (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     TEXT NOT NULL REFERENCES items(item_id),
    author_id   TEXT NOT NULL REFERENCES users(user_id),
    labels_json TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    UNIQUE (item_id, author_id)
);

CREATE TABLE IF NOT EXISTS matches (
    seq              INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id          TEXT NOT NULL UNIQUE REFERENCES items(item_id),
    submission_a_seq INTEGER NOT NULL REFERENCES submissions(seq),
    submission_b_seq INTEGER NOT NULL REFERENCES submissions(seq),
    author_a_id      TEXT NOT NULL REFERENCES users(user_id),
    author_b_id      TEXT NOT NULL REFERENCES users(user_id),
    labels_json      TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    CHECK (submission_a_seq < submission_b_seq)
);

CREATE TABLE IF NOT EXISTS arbitrations (
    seq              INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id          TEXT NOT NULL UNIQUE REFERENCES items(item_id),
    submission_a_seq INTEGER NOT NULL REFERENCES submissions(seq),
    submission_b_seq INTEGER NOT NULL REFERENCES submissions(seq),
    author_a_id      TEXT NOT NULL REFERENCES users(user_id),
    author_b_id      TEXT NOT NULL REFERENCES users(user_id),
    labels_json      TEXT NOT NULL,
    arbiter_id       TEXT NOT NULL REFERENCES users(user_id),
    created_at       TEXT NOT NULL,
    CHECK (submission_a_seq < submission_b_seq)
);

CREATE INDEX IF NOT EXISTS idx_submissions_item ON submissions(item_id);
CREATE INDEX IF NOT EXISTS idx_assignments_item ON assignments(item_id);
"""


class StoreError(Exception):
    """Raised when an existing database cannot be safely reused."""


def token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _parse_label_list(raw: str) -> list[str]:
    """Parse a stored canonical label set, rejecting non-canonical content."""
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StoreError(f"corrupt labels_json: {exc}") from exc
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        raise StoreError("corrupt labels_json: expected a list of strings")
    if value != sorted(set(value)):
        raise StoreError("corrupt labels_json: not a canonical unique sorted set")
    return value


class Store:
    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # -- connection / transaction plumbing ---------------------------------

    def _new_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.path),
            timeout=15.0,
            isolation_level=None,  # explicit BEGIN/COMMIT below
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 15000")
        return conn

    @contextmanager
    def read_txn(self) -> Iterator[sqlite3.Connection]:
        conn = self._new_conn()
        try:
            conn.execute("BEGIN")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
        finally:
            conn.close()

    @contextmanager
    def write_txn(self) -> Iterator[sqlite3.Connection]:
        conn = self._new_conn()
        try:
            # BEGIN IMMEDIATE takes the write lock up-front.  Combined with
            # busy_timeout this serializes competing writers; the loser then
            # reads the winner's committed state and returns a replay or a
            # conflict instead of writing a second, divergent record.
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
        finally:
            conn.close()

    # -- start-up ----------------------------------------------------------

    def initialize(self, fixtures: TaskFixtures) -> None:
        conn = self._new_conn()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            conn.execute("BEGIN IMMEDIATE")
            try:
                seeded = conn.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'"
                ).fetchone()
                if seeded is None:
                    self._seed(conn, fixtures)
                else:
                    self._verify_fixture_pins(conn, fixtures)
                self._verify_chain(conn, fixtures.label_ids)
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    def _seed(self, conn: sqlite3.Connection, fixtures: TaskFixtures) -> None:
        for table in (
            "items",
            "labels",
            "users",
            "assignments",
            "submissions",
            "matches",
            "arbitrations",
            "meta",
        ):
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if count:
                raise StoreError(
                    f"database has {count} existing row(s) in {table!r} but no "
                    "service metadata; refusing to mix fixture data into an "
                    "unknown database"
                )

        for seq, item_id in enumerate(fixtures.item_order, start=1):
            conn.execute(
                "INSERT INTO items (seq, item_id, text) VALUES (?, ?, ?)",
                (seq, item_id, fixtures.items[item_id].text),
            )
        for seq, label_id in enumerate(sorted(fixtures.label_ids), start=1):
            conn.execute(
                "INSERT INTO labels (seq, label_id, name) VALUES (?, ?, ?)",
                (seq, label_id, fixtures.label_names[label_id]),
            )
        for user in sorted(fixtures.identities.values(), key=lambda u: u["id"]):
            conn.execute(
                "INSERT INTO users (user_id, role, token_sha256) VALUES (?, ?, ?)",
                (user["id"], user["role"], token_sha256(user["token"])),
            )
        for item_id, authors in fixtures.item_assignments.items():
            for author_id in authors:
                conn.execute(
                    "INSERT INTO assignments (item_id, user_id) VALUES (?, ?)",
                    (item_id, author_id),
                )
        now = fixtures_now()
        meta_rows = {
            "schema_version": SCHEMA_VERSION,
            "items_sha256": fixtures.items_sha256,
            "labels_sha256": fixtures.labels_sha256,
            "identities_sha256": fixtures.identities_sha256,
            "created_at": now,
        }
        for key, value in meta_rows.items():
            conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (key, value))

    def _verify_fixture_pins(
        self, conn: sqlite3.Connection, fixtures: TaskFixtures
    ) -> None:
        stored = {row["key"]: row["value"] for row in conn.execute("SELECT * FROM meta")}
        expected = {
            "schema_version": SCHEMA_VERSION,
            "items_sha256": fixtures.items_sha256,
            "labels_sha256": fixtures.labels_sha256,
            "identities_sha256": fixtures.identities_sha256,
        }
        for key, want in expected.items():
            got = stored.get(key)
            if got != want:
                raise StoreError(
                    f"fixture input {key} changed (stored {got!r}, current {want!r}); "
                    "this service does not migrate vocabulary or item versions. "
                    "Use a new --db path for changed fixtures."
                )

        actual_items = [
            (row["seq"], row["item_id"], row["text"])
            for row in conn.execute("SELECT seq, item_id, text FROM items ORDER BY seq")
        ]
        expected_items = [
            (seq, item_id, fixtures.items[item_id].text)
            for seq, item_id in enumerate(fixtures.item_order, start=1)
        ]
        if actual_items != expected_items:
            raise StoreError("stored items do not match items.jsonl; refusing to start")

        actual_labels = [
            (row["seq"], row["label_id"], row["name"])
            for row in conn.execute(
                "SELECT seq, label_id, name FROM labels ORDER BY seq"
            )
        ]
        expected_labels = [
            (seq, label_id, fixtures.label_names[label_id])
            for seq, label_id in enumerate(sorted(fixtures.label_ids), start=1)
        ]
        if actual_labels != expected_labels:
            raise StoreError(
                "stored label vocabulary does not match labels.json; refusing to start"
            )

        actual_users = {
            row["user_id"]: (row["role"], row["token_sha256"])
            for row in conn.execute("SELECT user_id, role, token_sha256 FROM users")
        }
        expected_users = {
            user["id"]: (user["role"], token_sha256(user["token"]))
            for user in fixtures.identities.values()
        }
        if actual_users != expected_users:
            raise StoreError(
                "stored identities/tokens do not match identities.json; refusing to start"
            )

        actual_assignments = {
            (row["item_id"], row["user_id"])
            for row in conn.execute("SELECT item_id, user_id FROM assignments")
        }
        expected_assignments = {
            (item_id, author_id)
            for item_id, authors in fixtures.item_assignments.items()
            for author_id in authors
        }
        if actual_assignments != expected_assignments:
            raise StoreError("stored item assignments do not match fixtures")

    def _verify_chain(self, conn: sqlite3.Connection, label_ids: frozenset[str]) -> None:
        """Audit the immutable submission -> final-result chain.

        This runs on every start so that a restart proves the stored chain is
        internally consistent: every final is bound to two real, immutable,
        assigned submissions, and no item carries two conflicting finals.
        """

        def fail(message: str) -> None:
            raise StoreError(f"stored final-result chain is inconsistent: {message}")

        assigned = {
            (row["item_id"], row["user_id"])
            for row in conn.execute("SELECT item_id, user_id FROM assignments")
        }
        role = {
            row["user_id"]: row["role"]
            for row in conn.execute("SELECT user_id, role FROM users")
        }

        submissions = {
            row["seq"]: row for row in conn.execute("SELECT * FROM submissions")
        }
        for seq, row in submissions.items():
            labels = _parse_label_list(row["labels_json"])
            if not set(labels) <= label_ids:
                fail(f"submission {seq} contains unknown labels")
            if role.get(row["author_id"]) != "annotator":
                fail(f"submission {seq} author is not an annotator")
            if (row["item_id"], row["author_id"]) not in assigned:
                fail(f"submission {seq} author is not assigned to the item")
            if canonical_json(labels) != row["labels_json"]:
                fail(f"submission {seq} labels are not canonical")

        items_with_final: dict[str, str] = {}

        for row in conn.execute("SELECT * FROM matches"):
            item_id = row["item_id"]
            if item_id in items_with_final:
                fail(f"item {item_id} has multiple final records")
            items_with_final[item_id] = "agreement"
            a = submissions.get(row["submission_a_seq"])
            b = submissions.get(row["submission_b_seq"])
            if a is None or b is None:
                fail(f"match {row['seq']} references a missing submission")
            if row["submission_a_seq"] >= row["submission_b_seq"]:
                fail(f"match {row['seq']} bindings are not ordered")
            if a["item_id"] != item_id or b["item_id"] != item_id:
                fail(f"match {row['seq']} binds submissions from another item")
            if row["author_a_id"] != a["author_id"] or row["author_b_id"] != b["author_id"]:
                fail(f"match {row['seq']} author snapshot does not match submissions")
            labels = _parse_label_list(row["labels_json"])
            if labels != _parse_label_list(a["labels_json"]) or labels != _parse_label_list(
                b["labels_json"]
            ):
                fail(f"match {row['seq']} labels are not the agreed submission labels")
            if row["created_at"] < max(a["created_at"], b["created_at"]):
                fail(f"match {row['seq']} predates one of its submissions")
            item_authors = {u for (i, u) in assigned if i == item_id}
            if {a["author_id"], b["author_id"]} != item_authors:
                fail(f"match {row['seq']} does not cover both assigned annotators")

        for row in conn.execute("SELECT * FROM arbitrations"):
            item_id = row["item_id"]
            if item_id in items_with_final:
                fail(f"item {item_id} has multiple final records")
            items_with_final[item_id] = "arbitration"
            a = submissions.get(row["submission_a_seq"])
            b = submissions.get(row["submission_b_seq"])
            if a is None or b is None:
                fail(f"arbitration {row['seq']} references a missing submission")
            if row["submission_a_seq"] >= row["submission_b_seq"]:
                fail(f"arbitration {row['seq']} bindings are not ordered")
            if a["item_id"] != item_id or b["item_id"] != item_id:
                fail(f"arbitration {row['seq']} binds submissions from another item")
            if row["author_a_id"] != a["author_id"] or row["author_b_id"] != b["author_id"]:
                fail(f"arbitration {row['seq']} author snapshot does not match submissions")
            labels = _parse_label_list(row["labels_json"])
            if not set(labels) <= label_ids:
                fail(f"arbitration {row['seq']} contains unknown labels")
            if _parse_label_list(a["labels_json"]) == _parse_label_list(b["labels_json"]):
                fail(f"arbitration {row['seq']} exists for an agreed submission pair")
            if role.get(row["arbiter_id"]) != "arbiter":
                fail(f"arbitration {row['seq']} was not made by an arbiter")
            if row["created_at"] < max(a["created_at"], b["created_at"]):
                fail(f"arbitration {row['seq']} predates one of its submissions")
            item_authors = {u for (i, u) in assigned if i == item_id}
            if {a["author_id"], b["author_id"]} != item_authors:
                fail(f"arbitration {row['seq']} does not cover both assigned annotators")


def fixtures_now() -> str:
    from datetime import datetime, timezone

    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
