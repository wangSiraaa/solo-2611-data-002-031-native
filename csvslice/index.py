"""Persistent index: build, verify, and read.

The index is a SQLite database with two tables:

``meta``
    ``key TEXT PRIMARY KEY, value TEXT``.  One row per fact.  Besides the
    header registration it stores the source identity, the format version
    and the index's own checksum.

``records``
    ``record_no INTEGER PRIMARY KEY, start_off, end_off, term_len, fields``.
    One row per *data* record (the header is not a row here).

Offsets are absolute 0-based byte offsets into the source; ``end_off`` is
exclusive and includes the record terminator.  The index is always built
into a temporary file in the destination directory and moved into place
with :func:`os.replace` only after the whole scan (and checksum) succeeds,
so a failed rebuild leaves a previously good index untouched.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from urllib.parse import quote

from .errors import (
    ColumnCountError,
    EmptyInputError,
    IndexChecksumError,
    IndexIntegrityError,
    IndexValidationError,
    SourceMismatchError,
)
from .scanner import MAX_RECORD_BYTES, Scanner, ScanRecord

FORMAT_VERSION = "1"
CHECKSUM_KEY = "index_checksum"
BOM = b"\xef\xbb\xbf"
DEFAULT_CHUNK_BYTES = 1 << 20
_BATCH_ROWS = 4096
_HASH_READ_BYTES = 1 << 20

_REQUIRED_META = (
    "format_version",
    "source_size",
    "source_sha256",
    "bom_len",
    "header_start",
    "header_end",
    "header_term_len",
    "header_fields",
    "data_record_count",
    CHECKSUM_KEY,
)


@dataclass
class BuildResult:
    index_path: str
    source_path: str
    source_size: int
    source_sha256: str
    bom_len: int
    header_start: int
    header_end: int
    header_fields: int
    data_record_count: int
    chunk_bytes: int


@dataclass
class VerifyResult:
    index_path: str
    source_size: int
    source_sha256: str
    data_record_count: int
    header_fields: int
    source_checksum_checked: bool
    source_checksum_ok: bool | None


def _sqlite_ro_uri(path: str) -> str:
    return "file:" + quote(os.path.abspath(path), safe="/") + "?mode=ro"


def connect_readonly(path: str) -> sqlite3.Connection:
    """Open an existing index read-only; never create a missing file."""
    if not os.path.exists(path):
        raise IndexValidationError(f"index file not found: {path}")
    if not os.path.isfile(path):
        raise IndexValidationError(f"index path is not a file: {path}")
    try:
        conn = sqlite3.connect(_sqlite_ro_uri(path), uri=True)
    except sqlite3.Error as exc:  # pragma: no cover - platform dependent
        raise IndexValidationError(f"cannot open index {path}: {exc}") from exc
    try:
        conn.execute("PRAGMA query_only=ON")
    except sqlite3.Error:
        pass
    return conn


def read_meta(conn: sqlite3.Connection) -> dict[str, str]:
    return {row[0]: row[1] for row in conn.execute("SELECT key, value FROM meta")}


def _meta_int(meta: dict[str, str], key: str, *, default: int | None = None) -> int:
    if key not in meta:
        if default is not None:
            return default
        raise IndexValidationError(f"index is missing meta key {key!r}")
    try:
        return int(meta[key])
    except ValueError as exc:
        raise IndexValidationError(f"meta key {key!r} is not an integer") from exc


def compute_checksum(conn: sqlite3.Connection) -> str:
    """Canonical SHA256 over all metadata (minus the checksum) and rows."""
    hasher = hashlib.sha256()
    for key, value in conn.execute(
        "SELECT key, value FROM meta WHERE key <> ? ORDER BY key", (CHECKSUM_KEY,)
    ):
        hasher.update(b"meta\t" + key.encode("utf-8") + b"\t" + value.encode("utf-8") + b"\n")
    cursor = conn.execute(
        "SELECT record_no, start_off, end_off, term_len, fields "
        "FROM records ORDER BY record_no"
    )
    while True:
        rows = cursor.fetchmany(_BATCH_ROWS)
        if not rows:
            break
        for record_no, start, end, term_len, fields in rows:
            hasher.update(
                b"record\t"
                + str(record_no).encode()
                + b"\t"
                + str(start).encode()
                + b"\t"
                + str(end).encode()
                + b"\t"
                + str(term_len).encode()
                + b"\t"
                + str(fields).encode()
                + b"\n"
            )
    return hasher.hexdigest()


def _check_structure(meta: dict[str, str], conn: sqlite3.Connection) -> None:
    missing = [key for key in _REQUIRED_META if key not in meta]
    if missing:
        raise IndexValidationError(f"index is missing meta keys: {', '.join(missing)}")
    if meta["format_version"] != FORMAT_VERSION:
        raise IndexValidationError(
            f"unsupported index format version {meta['format_version']!r} "
            f"(this tool understands {FORMAT_VERSION!r})"
        )
    source_size = _meta_int(meta, "source_size")
    bom_len = _meta_int(meta, "bom_len")
    if bom_len not in (0, 3):
        raise IndexValidationError(f"invalid bom_len {bom_len}")
    header_start = _meta_int(meta, "header_start")
    header_end = _meta_int(meta, "header_end")
    header_term_len = _meta_int(meta, "header_term_len")
    header_fields = _meta_int(meta, "header_fields")
    data_count = _meta_int(meta, "data_record_count")
    if source_size < 0 or header_fields < 1:
        raise IndexValidationError("index metadata is out of range")
    if header_start != bom_len:
        raise IndexValidationError("header_start does not follow the BOM")
    if not (header_start <= header_end <= source_size):
        raise IndexValidationError("header range is outside the source")
    if header_term_len not in (0, 1, 2):
        raise IndexValidationError("invalid header_term_len")
    if header_term_len == 0 and header_end != source_size:
        raise IndexValidationError("unterminated header is not the last logical record")

    count = 0
    prev_end = header_end
    last_row = None
    cursor = conn.execute(
        "SELECT record_no, start_off, end_off, term_len, fields "
        "FROM records ORDER BY record_no"
    )
    while True:
        rows = cursor.fetchmany(_BATCH_ROWS)
        if not rows:
            break
        for record_no, start, end, term_len, fields in rows:
            if record_no != count:
                raise IndexValidationError(
                    f"records are not numbered consecutively (expected {count}, got {record_no})"
                )
            if start != prev_end:
                raise IndexValidationError(
                    f"record {record_no} is not contiguous with its predecessor"
                )
            if start < header_end or end < start or end > source_size:
                raise IndexValidationError(f"record {record_no} has an invalid byte range")
            if end - start > MAX_RECORD_BYTES:
                raise IndexValidationError(f"record {record_no} exceeds the 1 MiB limit")
            if term_len not in (0, 1, 2) or end - start < term_len:
                raise IndexValidationError(f"record {record_no} has an invalid terminator length")
            if term_len == 0 and end != source_size:
                raise IndexValidationError(f"unterminated record {record_no} is not at EOF")
            if fields != header_fields:
                raise IndexValidationError(
                    f"record {record_no} has {fields} field(s), header has {header_fields}"
                )
            prev_end = end
            last_row = (record_no, end, term_len)
            count += 1
    if count != data_count:
        raise IndexValidationError(
            f"data_record_count says {data_count}, but {count} record row(s) were found"
        )
    if count == 0:
        if header_end != source_size:
            raise IndexValidationError("header-only index does not end at EOF")
    else:
        assert last_row is not None
        if last_row[1] != source_size:
            raise IndexValidationError("last record does not end at EOF")


def verify_index_connection(conn: sqlite3.Connection, meta: dict[str, str] | None = None) -> dict[str, str]:
    """Verify checksum, SQLite integrity and structural invariants."""
    try:
        if meta is None:
            meta = read_meta(conn)
    except sqlite3.Error as exc:
        raise IndexValidationError(f"cannot read index metadata: {exc}") from exc
    try:
        rows = [row[0] for row in conn.execute("PRAGMA integrity_check")]
    except sqlite3.Error as exc:
        raise IndexValidationError(f"index is not a SQLite database: {exc}") from exc
    if rows != ["ok"]:
        raise IndexIntegrityError("SQLite integrity_check failed: " + "; ".join(map(str, rows)))
    if meta.get("format_version") != FORMAT_VERSION:
        raise IndexValidationError(
            f"unsupported index format version {meta.get('format_version')!r} "
            f"(this tool understands {FORMAT_VERSION!r})"
        )
    stored = meta.get(CHECKSUM_KEY)
    if stored is None:
        raise IndexValidationError("index has no self-checksum")
    try:
        actual = compute_checksum(conn)
    except sqlite3.Error as exc:
        raise IndexValidationError(f"cannot read index rows: {exc}") from exc
    if not _constant_compare(stored, actual):
        raise IndexChecksumError("index self-checksum does not match its contents")
    try:
        _check_structure(meta, conn)
    except sqlite3.Error as exc:
        raise IndexValidationError(f"index structure is unreadable: {exc}") from exc
    return meta


def _constant_compare(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(a, b)


def _sha256_file(path: str, chunk_bytes: int = _HASH_READ_BYTES) -> tuple[int, str]:
    hasher = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while True:
            data = f.read(chunk_bytes)
            if not data:
                break
            size += len(data)
            hasher.update(data)
    return size, hasher.hexdigest()


def check_source_identity(meta: dict[str, str], source_path: str) -> tuple[int, str]:
    """Compare the source against the index's stored size and SHA256."""
    expected_size = _meta_int(meta, "source_size")
    expected_hash = meta.get("source_sha256", "")
    try:
        stat_size = os.stat(source_path).st_size
    except OSError as exc:
        raise SourceMismatchError(f"cannot stat source file: {exc}") from exc
    if stat_size != expected_size:
        raise SourceMismatchError(
            f"size is {stat_size} bytes, index recorded {expected_size} bytes"
        )
    size, digest = _sha256_file(source_path)
    if size != expected_size:
        raise SourceMismatchError(
            f"file changed while hashing ({size} bytes read, expected {expected_size})"
        )
    if not _constant_compare(digest, expected_hash):
        raise SourceMismatchError("SHA256 of the source does not match the index")
    return size, digest


# ---------------------------------------------------------------------------
# build


def build_index(
    source_path: str,
    index_path: str,
    *,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    max_record_bytes: int = MAX_RECORD_BYTES,
) -> BuildResult:
    """Stream ``source_path`` and publish a verified index at ``index_path``."""
    if chunk_bytes < 1:
        raise ValueError("chunk_bytes must be at least 1")
    if max_record_bytes < 1 or max_record_bytes > MAX_RECORD_BYTES:
        raise ValueError(f"max_record_bytes must be between 1 and {MAX_RECORD_BYTES} (1 MiB)")
    if os.path.abspath(source_path) == os.path.abspath(index_path):
        raise ValueError("index path must differ from the source path")
    if not os.path.isfile(source_path):
        raise FileNotFoundError(source_path)
    index_dir = os.path.dirname(os.path.abspath(index_path))
    if not os.path.isdir(index_dir):
        raise NotADirectoryError(index_dir)
    if os.path.exists(index_path) and os.path.isdir(index_path):
        raise IsADirectoryError(index_path)

    fd, tmp_path = tempfile.mkstemp(
        prefix=".csvslice-index-", suffix=".tmp", dir=index_dir
    )
    os.close(fd)
    os.unlink(tmp_path)  # let sqlite create it itself
    conn = sqlite3.connect(tmp_path)
    batch: list[tuple[int, int, int, int, int]] = []
    try:
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA locking_mode=EXCLUSIVE")
        conn.execute("PRAGMA mmap_size=0")
        conn.execute("PRAGMA cache_size=-4096")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID"
        )
        conn.execute(
            "CREATE TABLE records ("
            "record_no INTEGER PRIMARY KEY, "
            "start_off INTEGER NOT NULL, "
            "end_off INTEGER NOT NULL, "
            "term_len INTEGER NOT NULL, "
            "fields INTEGER NOT NULL)"
        )

        state: dict[str, object] = {
            "header_seen": False,
            "data_count": 0,
            "fields": None,
        }

        def flush() -> None:
            if batch:
                conn.executemany(
                    "INSERT INTO records VALUES (?, ?, ?, ?, ?)", batch
                )
                batch.clear()

        def on_record(rec: ScanRecord) -> None:
            if rec.is_header:
                state["header_seen"] = True
                state["fields"] = rec.fields
                header_meta.update(
                    {
                        "header_start": str(rec.start),
                        "header_end": str(rec.end),
                        "header_term_len": str(rec.term_len),
                        "header_fields": str(rec.fields),
                    }
                )
                return
            expected = state["fields"]
            if rec.fields != expected:
                raise ColumnCountError(
                    record_no=rec.record_no,
                    offset=rec.start,
                    expected=int(expected),
                    actual=rec.fields,
                )
            batch.append((rec.record_no, rec.start, rec.end, rec.term_len, rec.fields))
            state["data_count"] = int(state["data_count"]) + 1
            if len(batch) >= _BATCH_ROWS:
                flush()

        header_meta: dict[str, str] = {}
        hasher = hashlib.sha256()
        bytes_read = 0
        with open(source_path, "rb") as f:
            source_size = os.fstat(f.fileno()).st_size
            probe = f.read(3)
            if probe == BOM:
                bom_len = 3
                hasher.update(probe)
                bytes_read = 3
            else:
                bom_len = 0
            scanner = Scanner(
                on_record, start_offset=bom_len, max_record_bytes=max_record_bytes
            )
            if probe and probe != BOM:
                hasher.update(probe)
                bytes_read = len(probe)
                scanner.feed(probe, 0)
            while True:
                data = f.read(chunk_bytes)
                if not data:
                    break
                hasher.update(data)
                scanner.feed(data, bytes_read)
                bytes_read += len(data)
        scanner.finish(source_size)
        if bytes_read != source_size:
            raise SourceMismatchError(
                f"source changed while building ({bytes_read} bytes read, "
                f"initial size {source_size})"
            )
        if not state["header_seen"]:
            raise EmptyInputError(bom_len)
        flush()

        meta = {
            "format_version": FORMAT_VERSION,
            "source_size": str(source_size),
            "source_sha256": hasher.hexdigest(),
            "bom_len": str(bom_len),
            "data_record_count": str(state["data_count"]),
            **header_meta,
        }
        conn.executemany(
            "INSERT INTO meta VALUES (?, ?)", list(meta.items())
        )
        conn.commit()
        checksum = compute_checksum(conn)
        conn.execute(
            "INSERT INTO meta VALUES (?, ?)", (CHECKSUM_KEY, checksum)
        )
        conn.commit()
        conn.close()
        conn = None

        with open(tmp_path, "rb") as f:
            os.fsync(f.fileno())
        os.replace(tmp_path, index_path)
        _fsync_dir(index_dir)
    except BaseException:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return BuildResult(
        index_path=index_path,
        source_path=source_path,
        source_size=source_size,
        source_sha256=hasher.hexdigest(),
        bom_len=bom_len,
        header_start=int(header_meta["header_start"]),
        header_end=int(header_meta["header_end"]),
        header_fields=int(header_meta["header_fields"]),
        data_record_count=int(state["data_count"]),
        chunk_bytes=chunk_bytes,
    )


def _fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# verify / read


def verify_index(index_path: str, source_path: str | None = None) -> VerifyResult:
    conn = connect_readonly(index_path)
    try:
        meta = verify_index_connection(conn)
        source_checked = False
        source_ok: bool | None = None
        if source_path is not None:
            check_source_identity(meta, source_path)
            source_checked = True
            source_ok = True
        return VerifyResult(
            index_path=index_path,
            source_size=_meta_int(meta, "source_size"),
            source_sha256=meta["source_sha256"],
            data_record_count=_meta_int(meta, "data_record_count"),
            header_fields=_meta_int(meta, "header_fields"),
            source_checksum_checked=source_checked,
            source_checksum_ok=source_ok,
        )
    finally:
        conn.close()


class IndexReader:
    """Read-only, verified access to a published index."""

    def __init__(self, index_path: str, *, verify: bool = True):
        self.index_path = index_path
        self.conn = connect_readonly(index_path)
        try:
            if verify:
                self.meta = verify_index_connection(self.conn)
            else:
                self.meta = read_meta(self.conn)
        except BaseException:
            self.conn.close()
            raise
        self.source_size = _meta_int(self.meta, "source_size")
        self.source_sha256 = self.meta.get("source_sha256", "")
        self.bom_len = _meta_int(self.meta, "bom_len")
        self.header_start = _meta_int(self.meta, "header_start")
        self.header_end = _meta_int(self.meta, "header_end")
        self.header_term_len = _meta_int(self.meta, "header_term_len")
        self.header_fields = _meta_int(self.meta, "header_fields")
        self.data_record_count = _meta_int(self.meta, "data_record_count")

    def __enter__(self) -> "IndexReader":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def header_range(self) -> tuple[int, int, int]:
        return self.header_start, self.header_end, self.header_term_len

    def record(self, record_no: int) -> tuple[int, int, int, int]:
        row = self.conn.execute(
            "SELECT start_off, end_off, term_len, fields FROM records WHERE record_no = ?",
            (record_no,),
        ).fetchone()
        if row is None:
            raise IndexError(f"data record {record_no} is not in the index")
        return int(row[0]), int(row[1]), int(row[2]), int(row[3])

    def iter_records(self, start_record: int, stop_record: int):
        """Yield ``(record_no, start_off, end_off, term_len)`` in order."""
        cursor = self.conn.execute(
            "SELECT record_no, start_off, end_off, term_len FROM records "
            "WHERE record_no >= ? AND record_no < ? ORDER BY record_no",
            (start_record, stop_record),
        )
        while True:
            rows = cursor.fetchmany(256)
            if not rows:
                break
            for row in rows:
                yield int(row[0]), int(row[1]), int(row[2]), int(row[3])

    def iter_all_records(self):
        yield from self.iter_records(0, self.data_record_count)
