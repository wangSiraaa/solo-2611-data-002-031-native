"""Data model, validation and canonicalisation for schedc.

A *rule* describes a local periodic schedule in one IANA timezone.  All
instants are handled as timezone-aware UTC datetimes; all local times are
naive datetimes that are only ever interpreted through ``zoneinfo`` using
the tzdata installed on this machine.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc

MAX_RULES = 100
MAX_WINDOW_DAYS = 400

NONEXISTENT_POLICIES = ("skip", "error")
REPEATED_POLICIES = ("first", "second", "both")
EXCEPTION_ACTIONS = ("skip", "replace")

RULE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class CompileError(Exception):
    """Validation failure, or a DST ``error`` policy aborting compilation."""


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Frequency:
    kind: str                       # "daily" | "weekly" | "monthly"
    weekdays: tuple = ()            # ISO 1..7 (Mon..Sun), weekly only
    month_day: int | None = None    # 1..31, monthly only
    last_day: bool = False          # monthly "L" only


@dataclass(frozen=True)
class Rule:
    rule_id: str
    timezone: str
    local_time: time
    start_date: date                # inclusive
    end_date: date                  # inclusive
    frequency: Frequency
    on_nonexistent: str             # "skip" | "error"
    on_repeated: str                # "first" | "second" | "both"


@dataclass(frozen=True)
class DateException:
    rule_id: str
    local_date: date
    action: str                     # "skip" | "replace"
    replace_time: time | None = None


@dataclass(frozen=True)
class Plan:
    plan_id: str
    rule_id: str
    planned_date: date              # original planned local date
    local_dt: datetime              # final local time (naive)
    utc_dt: datetime                # actual instant (aware, UTC)
    offset_seconds: int
    branch: str                     # "none" | "first" | "second"
    exception_basis: str | None     # why an exception produced this plan


@dataclass(frozen=True)
class Exclusion:
    rule_id: str
    local_date: date
    reason: str                     # nonexistent_local_time | exception_skip | exception_out_of_range
    detail: str


# --------------------------------------------------------------------------
# Parsing / validation
# --------------------------------------------------------------------------

def _parse_time(value, field: str) -> time:
    if not isinstance(value, str) or not TIME_RE.match(value):
        raise CompileError(f"{field}: expected HH:MM:SS, got {value!r}")
    h, m, s = (int(p) for p in value.split(":"))
    if h > 23 or m > 59 or s > 59:
        raise CompileError(f"{field}: invalid time {value!r}")
    return time(h, m, s)


def _parse_date(value, field: str) -> date:
    if not isinstance(value, str) or not DATE_RE.match(value):
        raise CompileError(f"{field}: expected YYYY-MM-DD, got {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise CompileError(f"{field}: invalid date {value!r}") from None


def _parse_frequency(value, field: str) -> Frequency:
    if not isinstance(value, dict):
        raise CompileError(f"{field}: frequency must be an object")
    kind = value.get("type")
    if kind == "daily":
        return Frequency("daily")
    if kind == "weekly":
        wd = value.get("weekdays")
        if not isinstance(wd, list) or not wd or any(type(w) is not int for w in wd):
            raise CompileError(f"{field}: weekly frequency needs a non-empty integer array 'weekdays'")
        if any(w < 1 or w > 7 for w in wd):
            raise CompileError(f"{field}: weekdays must be ISO 1..7 (Mon..Sun)")
        if len(set(wd)) != len(wd):
            raise CompileError(f"{field}: duplicate weekdays")
        return Frequency("weekly", weekdays=tuple(sorted(set(wd))))
    if kind == "monthly":
        day = value.get("day")
        if day == "L":
            return Frequency("monthly", last_day=True)
        if type(day) is not int or not (1 <= day <= 31):
            raise CompileError(f"{field}: monthly day must be 1..31 or \"L\"")
        return Frequency("monthly", month_day=day)
    raise CompileError(f"{field}: unknown frequency type {kind!r}")


def parse_rule(d) -> Rule:
    if not isinstance(d, dict):
        raise CompileError("rule must be an object")
    rule_id = d.get("rule_id")
    if not isinstance(rule_id, str) or not RULE_ID_RE.match(rule_id):
        raise CompileError(f"invalid rule_id {rule_id!r}")
    tzname = d.get("timezone")
    if not isinstance(tzname, str) or not tzname:
        raise CompileError(f"rule {rule_id}: timezone must be an IANA name")
    try:
        ZoneInfo(tzname)
    except (ZoneInfoNotFoundError, ValueError):
        raise CompileError(f"rule {rule_id}: unknown IANA timezone {tzname!r}") from None
    local_time = _parse_time(d.get("local_time"), f"rule {rule_id} local_time")
    start = _parse_date(d.get("start_date"), f"rule {rule_id} start_date")
    end = _parse_date(d.get("end_date"), f"rule {rule_id} end_date")
    if end < start:
        raise CompileError(f"rule {rule_id}: end_date {end} before start_date {start}")
    freq = _parse_frequency(d.get("frequency"), f"rule {rule_id}")
    on_n = d.get("on_nonexistent")
    if on_n not in NONEXISTENT_POLICIES:
        raise CompileError(f"rule {rule_id}: on_nonexistent must be one of {NONEXISTENT_POLICIES}")
    on_r = d.get("on_repeated")
    if on_r not in REPEATED_POLICIES:
        raise CompileError(f"rule {rule_id}: on_repeated must be one of {REPEATED_POLICIES}")
    return Rule(rule_id=rule_id, timezone=tzname, local_time=local_time,
                start_date=start, end_date=end, frequency=freq,
                on_nonexistent=on_n, on_repeated=on_r)


def parse_exception(d) -> DateException:
    if not isinstance(d, dict):
        raise CompileError("exception must be an object")
    rule_id = d.get("rule_id")
    if not isinstance(rule_id, str) or not RULE_ID_RE.match(rule_id):
        raise CompileError(f"exception: invalid rule_id {rule_id!r}")
    local_date = _parse_date(d.get("date"), f"exception {rule_id} date")
    action = d.get("action")
    if action not in EXCEPTION_ACTIONS:
        raise CompileError(f"exception {rule_id}@{local_date}: action must be one of {EXCEPTION_ACTIONS}")
    replace_time = d.get("replace_time")
    if action == "replace":
        if replace_time is None:
            raise CompileError(f"exception {rule_id}@{local_date}: replace needs replace_time")
        replace_time = _parse_time(replace_time, f"exception {rule_id}@{local_date} replace_time")
    elif replace_time is not None:
        raise CompileError(f"exception {rule_id}@{local_date}: skip must not carry replace_time")
    return DateException(rule_id=rule_id, local_date=local_date, action=action,
                         replace_time=replace_time)


def load_document(obj) -> tuple[list[Rule], list[DateException]]:
    """Parse and validate a whole rules document (rules + exceptions)."""
    if not isinstance(obj, dict):
        raise CompileError("rules document must be a JSON object")
    raw_rules = obj.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise CompileError("rules document needs a non-empty 'rules' array")
    rules = [parse_rule(r) for r in raw_rules]
    if len(rules) > MAX_RULES:
        raise CompileError(f"too many rules: {len(rules)} > {MAX_RULES}")
    ids = [r.rule_id for r in rules]
    if len(set(ids)) != len(ids):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise CompileError(f"duplicate rule_id(s): {', '.join(dupes)}")
    raw_exc = obj.get("exceptions", [])
    if not isinstance(raw_exc, list):
        raise CompileError("'exceptions' must be an array")
    exceptions = [parse_exception(e) for e in raw_exc]
    known = set(ids)
    seen = set()
    for e in exceptions:
        if e.rule_id not in known:
            raise CompileError(f"exception references unknown rule_id {e.rule_id!r}")
        key = (e.rule_id, e.local_date)
        if key in seen:
            raise CompileError(f"duplicate exception for {e.rule_id} on {e.local_date}")
        seen.add(key)
    return rules, exceptions


def parse_instant(value: str) -> datetime:
    """Parse an ISO-8601 instant with offset; normalise to UTC."""
    if not isinstance(value, str):
        raise CompileError(f"instant must be a string, got {value!r}")
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        raise CompileError(f"invalid instant {value!r}") from None
    if dt.tzinfo is None:
        raise CompileError(f"instant {value!r} must carry a UTC offset (e.g. trailing Z)")
    dt = dt.astimezone(UTC)
    if dt.microsecond:
        raise CompileError(f"instant {value!r} must have whole-second precision")
    return dt


# --------------------------------------------------------------------------
# Canonicalisation (stable hashing / plan ids)
# --------------------------------------------------------------------------

def iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def canonical_rule(rule: Rule) -> dict:
    f = rule.frequency
    if f.kind == "daily":
        freq = {"type": "daily"}
    elif f.kind == "weekly":
        freq = {"type": "weekly", "weekdays": list(f.weekdays)}
    else:
        freq = {"type": "monthly", "day": "L" if f.last_day else f.month_day}
    return {
        "rule_id": rule.rule_id,
        "timezone": rule.timezone,
        "local_time": rule.local_time.strftime("%H:%M:%S"),
        "start_date": rule.start_date.isoformat(),
        "end_date": rule.end_date.isoformat(),
        "frequency": freq,
        "on_nonexistent": rule.on_nonexistent,
        "on_repeated": rule.on_repeated,
    }


def canonical_exception(exc: DateException) -> dict:
    return {
        "rule_id": exc.rule_id,
        "date": exc.local_date.isoformat(),
        "action": exc.action,
        "replace_time": exc.replace_time.strftime("%H:%M:%S") if exc.replace_time else None,
    }


def content_hash(rules: list[Rule], exceptions: list[DateException],
                 window_start: datetime, window_end: datetime) -> str:
    """Stable hash over canonical rules + exceptions + compile window."""
    doc = {
        "rules": sorted((canonical_rule(r) for r in rules), key=lambda d: d["rule_id"]),
        "exceptions": sorted((canonical_exception(e) for e in exceptions),
                             key=lambda d: (d["rule_id"], d["date"])),
        "window_start": iso_z(window_start),
        "window_end": iso_z(window_end),
    }
    blob = json.dumps(doc, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()
