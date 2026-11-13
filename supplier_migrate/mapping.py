"""CSV mapping parsing, validation and canonicalisation.

The mapping is a strict, whole-batch contract:

* header is exactly ``old_code,new_code``;
* every old code appears at most once (exact, case-sensitive comparison);
* every new code appears at most once (exact, case-sensitive comparison);
* every old code must exist in the supplier table at the moment the operation
  starts;
* every new code must either be one of the mapping's old codes (so two codes
  may swap / form cycles), or be currently unused.

Any violation rejects the whole batch and carries the CSV line number.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass, field
from typing import Iterable

HEADER = ["old_code", "new_code"]


class MappingRejected(Exception):
    """Whole mapping rejected; ``errors`` are machine-readable and line-tagged."""

    def __init__(self, errors: list[dict], code: str = "invalid_mapping"):
        self.errors = errors
        self.code = code
        super().__init__(f"mapping rejected: {code} ({len(errors)} error(s))")

    def as_dict(self, command: str, **extra) -> dict:
        payload = {
            "command": command,
            "status": "rejected",
            "code": self.code,
            "errors": self.errors,
        }
        payload.update(extra)
        return payload


@dataclass(frozen=True)
class Entry:
    line: int            # logical CSV record line (header = line 1)
    physical_line: int   # physical line reported by csv.reader
    old_code: str
    new_code: str

    @property
    def changed(self) -> bool:
        return self.old_code != self.new_code


@dataclass
class Mapping:
    entries: list[Entry]          # all rows, sorted by old code
    normalized: list[dict]        # canonical representation, sorted
    mapping_hash: str
    old_codes: set[str]
    new_codes: set[str]

    @property
    def changed_entries(self) -> list[Entry]:
        return [e for e in self.entries if e.changed]

    @property
    def identity_entries(self) -> list[Entry]:
        return [e for e in self.entries if not e.changed]

    def old_to_new(self) -> dict[str, str]:
        return {e.old_code: e.new_code for e in self.entries}


def _error(line: int, physical_line: int, reason: str, message: str,
           old_code: str | None = None, new_code: str | None = None) -> dict:
    out = {
        "line": line,
        "physical_line": physical_line,
        "reason": reason,
        "message": message,
    }
    if old_code is not None:
        out["old_code"] = old_code
    if new_code is not None:
        out["new_code"] = new_code
    return out


def parse_csv(path: str) -> tuple[list[Entry], list[dict]]:
    """Parse the CSV structure.  No database is consulted here."""

    errors: list[dict] = []
    entries: list[Entry] = []

    try:
        handle = open(path, "r", encoding="utf-8-sig", newline="")
    except UnicodeDecodeError as exc:
        raise MappingRejected([
            _error(1, 1, "encoding_error", f"mapping file is not valid UTF-8: {exc}")
        ])
    except OSError as exc:
        raise MappingRejected([
            _error(1, 1, "file_error", f"cannot read mapping file: {exc}")
        ])

    with handle:
        reader = csv.reader(handle)
        try:
            raw_header = next(reader)
        except StopIteration:
            raise MappingRejected([
                _error(1, 1, "missing_header",
                       f"empty mapping file; expected header {','.join(HEADER)}")
            ])

        header = [h.strip() for h in raw_header]
        if header != HEADER:
            raise MappingRejected([
                _error(1, reader.line_num, "wrong_header",
                       f"expected header {','.join(HEADER)}, got {raw_header!r}")
            ])

        logical_line = 1
        for row in reader:
            logical_line += 1
            physical = reader.line_num
            if len(row) == 0 or (len(row) == 1 and row[0].strip() == ""):
                continue  # tolerate blank lines
            if len(row) != 2:
                errors.append(_error(
                    logical_line, physical, "wrong_column_count",
                    f"expected 2 columns, got {len(row)}",))
                continue

            old, new = row[0], row[1]
            if "\x00" in old or "\x00" in new:
                errors.append(_error(
                    logical_line, physical, "nul_byte",
                    "NUL bytes are not valid in supplier codes", old, new))
                continue
            if old == "":
                errors.append(_error(
                    logical_line, physical, "empty_old_code",
                    "old_code must not be empty", old, new))
                continue
            if new == "":
                errors.append(_error(
                    logical_line, physical, "empty_new_code",
                    "new_code must not be empty", old, new))
                continue

            entries.append(Entry(logical_line, physical, old, new))

    return entries, errors


def analyze(entries: list[Entry], existing_codes: Iterable[str]) -> list[dict]:
    """Validate duplicates, source existence and target legality.

    ``existing_codes`` is the supplier table's *current* state, read by the
    caller under the operation's serialising lock (apply) or snapshot (plan).
    """

    existing = set(existing_codes)
    old_codes = {e.old_code for e in entries}
    errors: list[dict] = []

    first_old: dict[str, Entry] = {}
    first_new: dict[str, Entry] = {}
    for entry in entries:
        if entry.old_code in first_old:
            first = first_old[entry.old_code]
            errors.append(_error(
                entry.line, entry.physical_line, "duplicate_source",
                f"old_code {entry.old_code!r} already used on line {first.line}",
                entry.old_code, entry.new_code))
        else:
            first_old[entry.old_code] = entry

        if entry.new_code in first_new:
            first = first_new[entry.new_code]
            errors.append(_error(
                entry.line, entry.physical_line, "duplicate_target",
                f"new_code {entry.new_code!r} already used on line {first.line}",
                entry.old_code, entry.new_code))
        else:
            first_new[entry.new_code] = entry

        if entry.old_code not in existing:
            errors.append(_error(
                entry.line, entry.physical_line, "source_missing",
                f"old_code {entry.old_code!r} does not exist in suppliers",
                entry.old_code, entry.new_code))

        # A target is legal iff it is one of this mapping's old codes (this
        # keeps swaps/cycles legal) or it is currently unused.  A target that
        # exists but is not in the mapping would clobber an unrelated supplier.
        if entry.new_code not in old_codes and entry.new_code in existing:
            errors.append(_error(
                entry.line, entry.physical_line, "target_occupied",
                f"new_code {entry.new_code!r} is currently used by a supplier "
                "outside this mapping",
                entry.old_code, entry.new_code))

    return errors


def build_mapping(entries: list[Entry]) -> Mapping:
    ordered = sorted(entries, key=lambda e: (e.old_code, e.new_code))
    normalized = [{"old_code": e.old_code, "new_code": e.new_code} for e in ordered]
    canonical = json.dumps(normalized, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return Mapping(
        entries=ordered,
        normalized=normalized,
        mapping_hash=digest,
        old_codes={e.old_code for e in entries},
        new_codes={e.new_code for e in entries},
    )


def load_mapping(path: str, existing_codes: Iterable[str] | None) -> Mapping:
    """Parse + (optionally) validate against current codes.

    Passing ``existing_codes=None`` performs structural validation only; that
    is used by the offline ``verify`` command against historical mappings.
    """

    entries, errors = parse_csv(path)
    if errors:
        raise MappingRejected(errors)
    if existing_codes is not None:
        errors = analyze(entries, existing_codes)
        if errors:
            raise MappingRejected(errors)
    else:
        # duplicates are purely structural and always enforced.
        dup_errors = []
        seen_old: dict[str, Entry] = {}
        seen_new: dict[str, Entry] = {}
        for e in entries:
            if e.old_code in seen_old:
                first = seen_old[e.old_code]
                dup_errors.append(_error(
                    e.line, e.physical_line, "duplicate_source",
                    f"old_code {e.old_code!r} already used on line {first.line}",
                    e.old_code, e.new_code))
            else:
                seen_old[e.old_code] = e
            if e.new_code in seen_new:
                first = seen_new[e.new_code]
                dup_errors.append(_error(
                    e.line, e.physical_line, "duplicate_target",
                    f"new_code {e.new_code!r} already used on line {first.line}",
                    e.old_code, e.new_code))
            else:
                seen_new[e.new_code] = e
        if dup_errors:
            raise MappingRejected(dup_errors)
    return build_mapping(entries)
