"""Reference CSV record parser used only by the test suite.

This is a deliberately independent implementation: it decodes the whole
input into memory and walks it with explicit indices, rather than a
re-usable streaming state machine.  Tests compare its record boundaries and
field values against the production scanner and extraction output.

The CSV dialect is the same one the task requires:

* UTF-8, optional BOM at byte 0
* first logical record is the header
* ``,`` separates fields
* fields may be quoted; ``""`` inside quotes is a literal quote
* LF and CRLF terminate records; CR inside quotes is ordinary content
* a fully empty line is one field (empty)
* the last record may lack a terminator
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

BOM = b"\xef\xbb\xbf"


@dataclass(frozen=True)
class RefRecord:
    start: int
    end: int  # exclusive, includes terminator
    term: bytes
    fields: list[str]
    raw: bytes


def split_records(data: bytes) -> tuple[int, list[RefRecord]]:
    """Return ``(bom_len, records)``; raises ``ValueError`` on malformed input."""
    bom = 0
    if data.startswith(BOM):
        bom = 3
    records: list[RefRecord] = []
    n = len(data)
    i = bom
    rec_start = i
    fields: list[str] = []
    field = bytearray()
    state = "field_start"

    def end_field() -> None:
        fields.append(field.decode("utf-8"))
        field.clear()

    def end_record(term_start: int, term: bytes, next_start: int) -> None:
        nonlocal rec_start, fields
        end_field()
        raw = data[rec_start:next_start]
        records.append(
            RefRecord(
                start=rec_start,
                end=next_start,
                term=term,
                fields=list(fields),
                raw=raw,
            )
        )
        fields = []
        rec_start = next_start

    while i < n:
        ch = data[i : i + 1]
        if state == "field_start":
            if ch == b'"':
                state = "quoted"
                i += 1
            elif ch == b",":
                fields.append("")
                i += 1
            elif ch == b"\r":
                if i + 1 < n and data[i + 1 : i + 2] == b"\n":
                    end_record(i, b"\r\n", i + 2)
                    i += 2
                else:
                    end_record(i, b"\r", i + 1)
                    i += 1
            elif ch == b"\n":
                end_record(i, b"\n", i + 1)
                i += 1
            else:
                state = "unquoted"
                field += ch
                i += 1
        elif state == "unquoted":
            if ch == b'"':
                raise ValueError(f"quote in unquoted field at {i}")
            elif ch == b",":
                end_field()
                state = "field_start"
                i += 1
            elif ch == b"\r":
                if i + 1 < n and data[i + 1 : i + 2] == b"\n":
                    end_record(i, b"\r\n", i + 2)
                    i += 2
                else:
                    end_record(i, b"\r", i + 1)
                    i += 1
                state = "field_start"
            elif ch == b"\n":
                end_record(i, b"\n", i + 1)
                state = "field_start"
                i += 1
            else:
                field += ch
                i += 1
        elif state == "quoted":
            if ch == b'"':
                state = "quote_in_quoted"
                i += 1
            else:
                field += ch
                i += 1
        elif state == "quote_in_quoted":
            if ch == b'"':
                field += b'"'
                state = "quoted"
                i += 1
            elif ch == b",":
                end_field()
                state = "field_start"
                i += 1
            elif ch == b"\r":
                if i + 1 < n and data[i + 1 : i + 2] == b"\n":
                    end_record(i, b"\r\n", i + 2)
                    i += 2
                else:
                    end_record(i, b"\r", i + 1)
                    i += 1
                state = "field_start"
            elif ch == b"\n":
                end_record(i, b"\n", i + 1)
                state = "field_start"
                i += 1
            else:
                raise ValueError(f"junk after closing quote at {i}")
        else:  # pragma: no cover
            raise AssertionError(state)

    if state == "quoted":
        raise ValueError("unterminated quote")
    if rec_start < n or state in ("field_start", "unquoted", "quote_in_quoted"):
        # A final record with no terminator.  An empty file (n == 0) has no
        # active record, because rec_start == n and no bytes were seen.
        if rec_start < n:
            end_record(n, b"", n)
    return bom, records


def header_and_data(data: bytes) -> tuple[list[str], list[RefRecord]]:
    _, records = split_records(data)
    if not records:
        raise ValueError("no header")
    return records[0].fields, records[1:]


def record_bytes(data: bytes, ref: RefRecord) -> bytes:
    return data[ref.start : ref.end]


def parse_with_stdlib(raw: bytes) -> list[list[str]]:
    """Parse one raw record with the stdlib as a second opinion.

    The trailing terminator is removed so a lone ``\\r`` is not treated as
    part of the record; this keeps the comparison independent of the
    dialect's own terminator handling.
    """
    text = raw.decode("utf-8")
    if text.endswith("\r\n"):
        text = text[:-2]
    elif text.endswith("\n") or text.endswith("\r"):
        text = text[:-1]
    return list(csv.reader(io.StringIO(text, newline="")))
