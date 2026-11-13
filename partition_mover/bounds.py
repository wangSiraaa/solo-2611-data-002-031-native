"""UTC month ranges and parsing of PostgreSQL range-partition bound expressions.

The tool never infers a partition's coverage from its name.  It parses the
actual bound expression stored in ``pg_class.relpartbound`` (deparse obtained
via ``pg_get_expr``).  The reading session sets ``TIME ZONE 'UTC'`` so
``timestamptz`` bounds deparse with a ``+00`` offset, but the parser accepts
any numeric offset and compares absolute instants, so correctness does not
depend on the session setting.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


class BoundParseError(ValueError):
    """A partition bound expression could not be parsed."""


class _Infinity:
    """Sentinel for MINVALUE / MAXVALUE range bound endpoints."""

    __slots__ = ("sign",)

    def __init__(self, sign: int):
        self.sign = sign

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return "MAXVALUE" if self.sign > 0 else "MINVALUE"

    def __eq__(self, other):
        return isinstance(other, _Infinity) and self.sign == other.sign

    def __hash__(self):
        return hash(self.sign)

    def __lt__(self, other):
        if isinstance(other, _Infinity):
            return self.sign < other.sign
        return self.sign < 0

    def __le__(self, other):
        return self == other or self < other

    def __gt__(self, other):
        return not self.__le__(other)

    def __ge__(self, other):
        return not self.__lt__(other)


MINVALUE = _Infinity(-1)
MAXVALUE = _Infinity(1)


@dataclass(frozen=True)
class Bound:
    """Parsed partition bound.  ``lower``/``upper`` are aware datetimes or
    the MINVALUE/MAXVALUE sentinels; the range is left-closed, right-open."""

    is_default: bool
    lower: object = None
    upper: object = None
    raw: str = ""

    def contains(self, ts: datetime) -> bool:
        """Half-open membership test: lower <= ts < upper."""
        return not self.is_default and self.lower <= ts < self.upper

    def overlaps(self, start: datetime, end: datetime) -> bool:
        """True if [lower, upper) and [start, end) share at least one instant."""
        return not self.is_default and self.lower < end and start < self.upper


_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")


def month_range(month: str) -> tuple[datetime, datetime]:
    """UTC left-closed right-open range [first of month, first of next month).

    The half-open form means the microsecond-precision last instant of the
    month (e.g. 2026-01-31 23:59:59.999999Z) is naturally included without
    ever computing it, and the next month's midnight is naturally excluded.
    """
    m = _MONTH_RE.fullmatch(month.strip())
    if not m:
        raise ValueError(f"invalid month {month!r}: expected 'YYYY-MM'")
    year, mon = int(m.group(1)), int(m.group(2))
    if not 1 <= mon <= 12:
        raise ValueError(f"invalid month {month!r}: month must be 01..12")
    start = datetime(year, mon, 1, tzinfo=timezone.utc)
    ny, nm = (year + 1, 1) if mon == 12 else (year, mon + 1)
    return start, datetime(ny, nm, 1, tzinfo=timezone.utc)


_TS_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[T ]"
    r"(\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)"
    r"([+-]\d{2})(?::?(\d{2}))?$"
)
_QUOTED_RE = re.compile(r"^'(.*)'(?:::[a-zA-Z][a-zA-Z ]*)?$", re.S)


def parse_ts_literal(token: str) -> datetime:
    """Parse a quoted timestamptz/timestamp literal as deparsed by pg_get_expr.

    Examples accepted: '2025-12-01 00:00:00+00', '2026-01-31 23:59:59.999999+00',
    the same with a cast suffix, and ISO 'T' separators.
    """
    token = token.strip()
    m = _QUOTED_RE.fullmatch(token)
    if not m:
        raise BoundParseError(f"cannot parse bound value: {token!r}")
    body = m.group(1)
    mm = _TS_RE.fullmatch(body)
    if not mm:
        raise BoundParseError(f"cannot parse timestamp literal: {body!r}")
    date_part, time_part, tz_hours, tz_minutes = mm.groups()
    sign = -1 if tz_hours.startswith("-") else 1
    offset = timedelta(hours=int(tz_hours), minutes=sign * int(tz_minutes or 0))
    naive = datetime.fromisoformat(f"{date_part} {time_part}")
    return naive.replace(tzinfo=timezone(offset))


def parse_bound_value(token: str):
    """Parse one endpoint of a FOR VALUES bound: MINVALUE/MAXVALUE or a literal."""
    token = token.strip()
    if token.upper() == "MINVALUE":
        return MINVALUE
    if token.upper() == "MAXVALUE":
        return MAXVALUE
    return parse_ts_literal(token)


_RANGE_RE = re.compile(r"^FOR VALUES FROM \((.+)\) TO \((.+)\)$", re.S)


def parse_partition_bound(expr: str | None) -> Bound:
    """Parse pg_get_expr(relpartbound) output for a single-column range key.

    The DEFAULT partition deparses as the literal string ``DEFAULT``.
    """
    if expr is None:
        raise BoundParseError("NULL partition bound expression")
    raw = expr
    expr = expr.strip()
    if expr.upper() == "DEFAULT":
        return Bound(is_default=True, raw=raw)
    m = _RANGE_RE.fullmatch(expr)
    if not m:
        raise BoundParseError(f"unsupported partition bound: {raw!r}")
    lower = parse_bound_value(m.group(1))
    upper = parse_bound_value(m.group(2))
    return Bound(is_default=False, lower=lower, upper=upper, raw=raw)


def sql_ts_literal(dt: datetime, with_tz: bool = True) -> str:
    """Render an aware datetime as a SQL literal string, normalized to UTC.

    ``with_tz=False`` is used for ``timestamp without time zone`` keys, where
    the UTC wall clock is rendered without an offset.
    """
    dt = dt.astimezone(timezone.utc)
    if with_tz:
        return dt.isoformat(sep=" ")
    return dt.strftime("%Y-%m-%d %H:%M:%S")
