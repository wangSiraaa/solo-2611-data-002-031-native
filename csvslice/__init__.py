"""csvslice: index large offline CSV files by logical record and slice them.

Public entry points are the four CLI commands ``generate``, ``build``,
``extract`` and ``verify``; the main building blocks are also importable.
"""

from __future__ import annotations

from .errors import (
    ColumnCountError,
    CsvSliceError,
    CsvSyntaxError,
    EmptyInputError,
    ExtractRangeError,
    IndexChecksumError,
    IndexIntegrityError,
    IndexValidationError,
    OutputConflictError,
    RecordTooLargeError,
    SourceMismatchError,
)
from .extract import ExtractResult, extract_ranges, parse_ranges
from .index import (
    DEFAULT_CHUNK_BYTES,
    FORMAT_VERSION,
    MAX_RECORD_BYTES,
    BuildResult,
    IndexReader,
    VerifyResult,
    build_index,
    verify_index,
)
from .scanner import Scanner, ScanRecord

__all__ = [
    "BuildResult",
    "ColumnCountError",
    "CsvSliceError",
    "CsvSyntaxError",
    "DEFAULT_CHUNK_BYTES",
    "EmptyInputError",
    "ExtractRangeError",
    "ExtractResult",
    "FORMAT_VERSION",
    "IndexChecksumError",
    "IndexIntegrityError",
    "IndexReader",
    "IndexValidationError",
    "MAX_RECORD_BYTES",
    "OutputConflictError",
    "RecordTooLargeError",
    "ScanRecord",
    "Scanner",
    "SourceMismatchError",
    "VerifyResult",
    "build_index",
    "extract_ranges",
    "parse_ranges",
    "verify_index",
]

__version__ = "1.0.0"
