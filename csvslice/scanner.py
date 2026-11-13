"""Streaming, byte-level CSV record scanner.

The scanner never holds a complete record in memory and never reads the
whole file: callers feed arbitrary byte chunks.  It tracks the grammar
across chunk boundaries (quoted fields, escaped double quotes, and CR/LF
terminators) and reports each logical record's absolute byte range.

Grammar (strict RFC-4180-like, but only LF and CRLF terminate records):

* fields are separated by ``,``
* a field may be enclosed in ``"..."``; ``""`` inside denotes a literal quote
* commas, CR, LF and arbitrary UTF-8 bytes are legal inside quotes
* a quote inside an unquoted field is an error
* anything other than ``,`` / CR / LF / EOF after a closing quote is an error
* an unterminated quoted field is an error, reported at the opening quote
* a completely empty line is a record with one empty field
* the final record may omit its terminator
* no logical record may exceed ``max_record_bytes`` (terminator included)

Offsets are absolute 0-based byte offsets into the file, counted from the
first byte (the BOM, when present, is *not* part of any logical record).
A record's ``end`` is exclusive and includes its terminator.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from .errors import CsvSyntaxError, RecordTooLargeError

MAX_RECORD_BYTES = 1 << 20  # 1 MiB, as required

# States of the field/record state machine.
_FIELD_START = 0
_UNQUOTED = 1
_QUOTED = 2
_QUOTE_IN_QUOTED = 3

_QUOTE = 0x22
_COMMA = 0x2C
_CR = 0x0D
_LF = 0x0A

# One C-level scan finds the next byte that can end/alter an unquoted field.
_UNQUOTED_SPECIAL = re.compile(rb'[,"\r\n]')


@dataclass(frozen=True)
class ScanRecord:
    """One logical record discovered by :class:`Scanner`."""

    start: int
    end: int  # exclusive, includes terminator when present
    term_len: int  # 0, 1 (LF or CR) or 2 (CRLF)
    fields: int
    is_header: bool
    logical_no: int  # 1-based logical position; header == 1
    record_no: int | None  # 0-based data record number; None for header


RecordCallback = Callable[[ScanRecord], None]


class Scanner:
    """Incrementally feed bytes, receive records through ``on_record``."""

    def __init__(
        self,
        on_record: RecordCallback,
        *,
        start_offset: int = 0,
        max_record_bytes: int = MAX_RECORD_BYTES,
    ) -> None:
        if max_record_bytes <= 0:
            raise ValueError("max_record_bytes must be positive")
        self.on_record = on_record
        self.max_record_bytes = max_record_bytes
        self.state = _FIELD_START
        self.fields = 0
        self.record_start = start_offset
        self.record_active = False
        self.quote_offset = -1
        self.logical_no = 0
        self.pending_cr = None  # absolute offset of a CR awaiting LF lookahead
        self.bytes_fed = 0
        self._finished = False

    # -- public API -----------------------------------------------------

    def feed(self, data: bytes, base_offset: int) -> None:
        """Scan ``data`` whose first byte lives at absolute ``base_offset``."""
        if self._finished:
            raise RuntimeError("feed() after finish()")
        if not isinstance(data, bytes):
            data = bytes(data)
        self.bytes_fed += len(data)
        n = len(data)
        i = 0
        while i < n:
            if self.pending_cr is not None:
                cr_offset = self.pending_cr
                if data[i] == _LF:
                    self.pending_cr = None
                    self._check_limit(base_offset + i + 1, cr_offset)
                    self._finish_record(cr_offset, 2, base_offset + i + 1)
                    i += 1
                    continue
                self.pending_cr = None
                self._check_limit(base_offset + i, cr_offset)
                self._finish_record(cr_offset, 1, base_offset + i)
                # fall through and scan data[i] as the next record's byte

            pos = base_offset + i
            state = self.state

            if state == _QUOTED:
                j = data.find(b'"', i)
                if j < 0:
                    self._check_limit(base_offset + n, pos)
                    i = n
                else:
                    self._check_limit(base_offset + j, pos)
                    self.state = _QUOTE_IN_QUOTED
                    i = j + 1
                continue

            if state == _UNQUOTED:
                m = _UNQUOTED_SPECIAL.search(data, i)
                if m is None:
                    self._check_limit(base_offset + n, pos)
                    i = n
                    continue
                end = base_offset + m.start()
                self._check_limit(end, pos)
                ch = data[m.start()]
                if ch == _COMMA:
                    self.fields += 1
                    self.state = _FIELD_START
                    i = m.start() + 1
                elif ch == _QUOTE:
                    self._raise_quote_in_unquoted(end)
                elif ch == _CR:
                    self.pending_cr = end
                    i = m.start() + 1
                else:  # LF
                    self._finish_record(end, 1, end + 1)
                    i = m.start() + 1
                continue

            # FIELD_START / QUOTE_IN_QUOTED examine a single byte.
            ch = data[i]
            if state == _FIELD_START:
                if ch == _QUOTE:
                    self.state = _QUOTED
                    self.quote_offset = pos
                    self.record_active = True
                    i += 1
                elif ch == _COMMA:
                    self.fields += 1
                    self.record_active = True
                    i += 1
                elif ch == _CR:
                    self.record_active = True
                    self.pending_cr = pos
                    i += 1
                elif ch == _LF:
                    self.record_active = True
                    self._finish_record(pos, 1, pos + 1)
                    i += 1
                else:
                    self.state = _UNQUOTED
                    self.record_active = True
                    i += 1
                self._check_limit(base_offset + i, pos)
                continue

            # _QUOTE_IN_QUOTED: a quote was just seen; decide what it meant.
            if ch == _QUOTE:
                self.state = _QUOTED  # escaped "" -> literal quote, stay quoted
                i += 1
            elif ch == _COMMA:
                self.fields += 1
                self.state = _FIELD_START
                i += 1
            elif ch == _CR:
                self.pending_cr = pos
                i += 1
            elif ch == _LF:
                self._finish_record(pos, 1, pos + 1)
                i += 1
            else:
                self._raise_junk_after_quote(pos)
            self._check_limit(base_offset + i, pos)
        # end while

    def finish(self, file_size: int) -> None:
        """Resolve EOF.  ``file_size`` is the total length of the source."""
        if self._finished:
            return
        self._finished = True
        if self.state == _QUOTED:
            self._raise_unclosed_quote()
        if self.pending_cr is not None:
            cr_offset = self.pending_cr
            self.pending_cr = None
            self._check_limit(cr_offset + 1, cr_offset)
            self._finish_record(cr_offset, 1, cr_offset + 1)
            return
        if self.record_active:
            self._check_limit(file_size, self.record_start)
            self._finish_record(file_size, 0, file_size)

    # -- internals -----------------------------------------------------

    def _finish_record(self, term_start: int, term_len: int, next_start: int) -> None:
        if self.state == _QUOTED:
            self._raise_unclosed_quote()
        record_end = term_start + term_len
        if record_end - self.record_start > self.max_record_bytes:
            logical = self.logical_no
            raise RecordTooLargeError(
                record_no=None if logical == 0 else logical - 1,
                is_header=(logical == 0),
                record_start=self.record_start,
                offset=record_end,
                limit=self.max_record_bytes,
            )
        fields = self.fields + 1  # the field in progress always counts
        logical = self.logical_no
        rec = ScanRecord(
            start=self.record_start,
            end=record_end,
            term_len=term_len,
            fields=fields,
            is_header=(logical == 0),
            logical_no=logical + 1,
            record_no=(None if logical == 0 else logical - 1),
        )
        self.on_record(rec)
        self.logical_no += 1
        self.state = _FIELD_START
        self.fields = 0
        self.record_active = False
        self.quote_offset = -1
        self.record_start = next_start

    def _check_limit(self, consumed_end: int, at: int) -> None:
        """Fail as soon as bytes consumed in the current record exceed the cap."""
        if not self.record_active:
            return
        hard_end = self.record_start + self.max_record_bytes
        if consumed_end > hard_end:
            logical = self.logical_no
            raise RecordTooLargeError(
                record_no=(None if logical == 0 else logical - 1),
                is_header=(logical == 0),
                record_start=self.record_start,
                offset=max(at, hard_end),
                limit=self.max_record_bytes,
            )

    def _error(self, message: str, offset: int, detail: str = "") -> None:
        logical = self.logical_no
        raise CsvSyntaxError(
            message=message,
            offset=offset,
            record_no=(None if logical == 0 else logical - 1),
            is_header=(logical == 0),
            logical_record_no=logical + 1,
            detail=detail,
        )

    def _raise_unclosed_quote(self) -> None:
        self._error(
            "unterminated quoted field",
            self.quote_offset,
            detail="field opened here has no closing quote before EOF",
        )

    def _raise_quote_in_unquoted(self, offset: int) -> None:
        self._error(
            "double quote inside an unquoted field",
            offset,
            detail="quote fields must start with a quote",
        )

    def _raise_junk_after_quote(self, offset: int) -> None:
        self._error(
            "unexpected character after a closing quote",
            offset,
            detail="expected comma, CR, LF or end of file",
        )
