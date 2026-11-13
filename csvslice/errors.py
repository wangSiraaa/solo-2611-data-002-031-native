"""Error types for the csvslice indexer.

All user-facing failures derive from :class:`CsvSliceError` so the CLI can
render them uniformly and return a non-zero exit status.
"""

from __future__ import annotations

from dataclasses import dataclass


class CsvSliceError(Exception):
    """Base class for expected, user-facing failures."""


class EmptyInputError(CsvSliceError):
    """The source has no header record (empty, or only a BOM)."""

    def __init__(self, bom_len: int):
        self.bom_len = bom_len
        super().__init__(
            "source contains no header record"
            + (" (only a UTF-8 BOM was found)" if bom_len else " (file is empty)")
        )


@dataclass
class CsvSyntaxError(CsvSliceError):
    """A CSV grammar violation at a byte offset.

    ``record_no`` is the zero-based *data* record number.  It is ``None``
    when the failure is in the header.  ``offset`` is the absolute byte
    offset used to locate the problem; for an unclosed quote that is the
    opening quote, for the other quote errors it is the offending byte.
    """

    message: str
    offset: int
    record_no: int | None = None
    is_header: bool = False
    logical_record_no: int = 1
    detail: str = ""

    def __post_init__(self) -> None:
        # Dataclass does not call Exception.__init__, so build the message.
        where = "header" if self.is_header else f"data record {self.record_no}"
        text = f"{where} (logical record {self.logical_record_no}), byte offset {self.offset}: {self.message}"
        if self.detail:
            text += f" ({self.detail})"
        Exception.__init__(self, text)


@dataclass
class ColumnCountError(CsvSliceError):
    """A data record has a different number of fields than the header."""

    record_no: int
    offset: int
    expected: int
    actual: int

    def __post_init__(self) -> None:
        Exception.__init__(
            self,
            f"data record {self.record_no} at byte offset {self.offset} "
            f"has {self.actual} field(s), header has {self.expected}; "
            "index was not published",
        )


@dataclass
class RecordTooLargeError(CsvSliceError):
    """A logical record grew beyond the configured hard limit."""

    record_no: int | None
    is_header: bool
    record_start: int
    offset: int
    limit: int

    def __post_init__(self) -> None:
        where = "header" if self.is_header else f"data record {self.record_no}"
        Exception.__init__(
            self,
            f"{where} starting at byte offset {self.record_start} exceeds "
            f"the {self.limit}-byte logical-record limit at byte offset {self.offset}",
        )


class IndexValidationError(CsvSliceError):
    """The index file is missing data, corrupt, or of an unknown version."""


class IndexChecksumError(IndexValidationError):
    """The stored index self-checksum does not match the index contents."""


class IndexIntegrityError(IndexValidationError):
    """SQLite's own integrity_check did not report ``ok``."""


class SourceMismatchError(CsvSliceError):
    """The source file no longer matches the identity stored in the index."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"source file does not match index: {reason}")


class ExtractRangeError(CsvSliceError):
    """A requested record interval is malformed or out of range."""


class OutputConflictError(CsvSliceError):
    """The requested output path would overwrite the source or index."""
