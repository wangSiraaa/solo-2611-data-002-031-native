"""SQLite staging area.

Everything parsed from the XML stream lands here first.  The staging
database is a *scratch* file: it is deleted after each import attempt
and is never part of the published version.  Keeping it on disk (with a
tiny page cache) is what allows multi-hundred-MiB inputs while the
process RSS stays small.

Identity rules enforced here (violations raise :class:`DocumentError`,
i.e. the whole input fails):

* batch ids are globally unique  -> PRIMARY KEY on ``batches``
* customer ids unique per batch  -> UNIQUE(batch_id, customer_id)
* order ids unique per batch     -> UNIQUE(batch_id, order_id)

Rows with a NULL id are records whose id was missing; they are rejected
at record level and claim no identity (SQLite UNIQUE allows many NULLs).
"""

from __future__ import annotations

import os
import sqlite3

from .errors import DocumentError

SCHEMA = """
CREATE TABLE meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
CREATE TABLE batches (
    batch_id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL
);
CREATE TABLE customers (
    key INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    customer_id TEXT,          -- NULL when the id attribute was missing
    name TEXT,
    valid INTEGER NOT NULL,    -- 1 = usable, 0 = rejected at parse time
    seq INTEGER NOT NULL       -- document occurrence number
);
CREATE UNIQUE INDEX customers_identity ON customers(batch_id, customer_id);
CREATE TABLE orders (
    key INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    order_id TEXT,             -- NULL when the id attribute was missing
    customer_id TEXT,
    valid INTEGER NOT NULL,    -- 1 = publishable (so far), 0 = quarantined
    reported INTEGER NOT NULL DEFAULT 0,  -- 1 once a reject row exists
    reason TEXT,
    seq INTEGER NOT NULL
);
CREATE UNIQUE INDEX orders_identity ON orders(batch_id, order_id);
CREATE TABLE lines (
    order_key INTEGER NOT NULL,
    line_no INTEGER NOT NULL,
    sku TEXT,
    qty TEXT,                  -- canonical decimal text; exact, unbounded
    unit_cents TEXT
);
CREATE INDEX lines_order ON lines(order_key, line_no);
CREATE TABLE rejects (
    key INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,        -- 'customer' | 'order'
    batch_id TEXT,
    record_id TEXT,            -- customer/order id (may be NULL)
    seq INTEGER,               -- document occurrence number
    reason TEXT NOT NULL,
    detail TEXT
);
"""

_FLUSH_LINES_EVERY = 8192
_FLUSH_REJECTS_EVERY = 1024


class Staging:
    def __init__(self, path: str):
        if os.path.exists(path):
            os.remove(path)
        self.db = sqlite3.connect(path)
        # Scratch database: no journal, no fsync, tiny cache.  If the
        # process dies the file is simply rebuilt on the next run.
        self.db.execute("PRAGMA journal_mode=OFF")
        self.db.execute("PRAGMA synchronous=OFF")
        self.db.execute("PRAGMA cache_size=-2048")  # 2 MiB page cache
        self.db.executescript(SCHEMA)
        self.db.execute("BEGIN")
        self._lines_buf: list[tuple] = []
        self._rejects_buf: list[tuple] = []
        self._batch_seq = 0

    # -- batches ---------------------------------------------------------
    def insert_batch(self, batch_id: str) -> None:
        self._batch_seq += 1
        try:
            self.db.execute(
                "INSERT INTO batches(batch_id, seq) VALUES (?, ?)",
                (batch_id, self._batch_seq),
            )
        except sqlite3.IntegrityError:
            raise DocumentError(f"duplicate batch id {batch_id!r}") from None

    # -- customers / orders ----------------------------------------------
    def add_customer(self, batch_id, customer_id, name, valid, seq) -> None:
        try:
            self.db.execute(
                "INSERT INTO customers(batch_id, customer_id, name, valid, seq)"
                " VALUES (?,?,?,?,?)",
                (batch_id, customer_id, name, 1 if valid else 0, seq),
            )
        except sqlite3.IntegrityError:
            raise DocumentError(
                f"duplicate customer id {customer_id!r} in batch {batch_id!r}"
            ) from None

    def add_order(self, batch_id, order_id, customer_id, valid, seq, reason) -> int:
        """Stage an order; returns its staging key (rowid)."""
        try:
            cur = self.db.execute(
                "INSERT INTO orders(batch_id, order_id, customer_id, valid,"
                " reported, reason, seq) VALUES (?,?,?,?,?,?,?)",
                (
                    batch_id,
                    order_id,
                    customer_id,
                    1 if valid else 0,
                    0 if valid else 1,  # invalid-at-parse rows are reported now
                    reason,
                    seq,
                ),
            )
        except sqlite3.IntegrityError:
            raise DocumentError(
                f"duplicate order id {order_id!r} in batch {batch_id!r}"
            ) from None
        return int(cur.lastrowid)

    # -- lines / rejects (buffered, no uniqueness needed) -----------------
    def add_lines(self, order_key: int, lines: list[tuple]) -> None:
        self._lines_buf.extend(
            (order_key, i + 1, sku, str(qty), str(unit))
            for i, (sku, qty, unit) in enumerate(lines)
        )
        if len(self._lines_buf) >= _FLUSH_LINES_EVERY:
            self._flush_lines()

    def add_reject(self, kind, batch_id, record_id, seq, reason, detail=None) -> None:
        self._rejects_buf.append((kind, batch_id, record_id, seq, reason, detail))
        if len(self._rejects_buf) >= _FLUSH_REJECTS_EVERY:
            self._flush_rejects()

    # -- flushing ----------------------------------------------------------
    def _flush_lines(self) -> None:
        if self._lines_buf:
            self.db.executemany(
                "INSERT INTO lines(order_key, line_no, sku, qty, unit_cents)"
                " VALUES (?,?,?,?,?)",
                self._lines_buf,
            )
            self._lines_buf.clear()

    def _flush_rejects(self) -> None:
        if self._rejects_buf:
            self.db.executemany(
                "INSERT INTO rejects(kind, batch_id, record_id, seq, reason, detail)"
                " VALUES (?,?,?,?,?,?)",
                self._rejects_buf,
            )
            self._rejects_buf.clear()

    def flush(self) -> None:
        self._flush_lines()
        self._flush_rejects()
        self.db.commit()

    def close(self) -> None:
        try:
            self.db.close()
        except Exception:
            pass
