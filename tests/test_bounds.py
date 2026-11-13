"""Unit tests for UTC month ranges, bound parsing and boundary attribution."""
from datetime import datetime, timedelta, timezone

import pytest

from partition_mover.bounds import (
    MAXVALUE,
    MINVALUE,
    BoundParseError,
    month_range,
    parse_partition_bound,
    parse_ts_literal,
    sql_ts_literal,
)

UTC = timezone.utc


class TestMonthRange:
    def test_regular_month(self):
        start, end = month_range("2026-01")
        assert start == datetime(2026, 1, 1, tzinfo=UTC)
        assert end == datetime(2026, 2, 1, tzinfo=UTC)

    def test_december_rolls_year(self):
        start, end = month_range("2026-12")
        assert start == datetime(2026, 12, 1, tzinfo=UTC)
        assert end == datetime(2027, 1, 1, tzinfo=UTC)

    @pytest.mark.parametrize("bad", ["2026-1", "2026-13", "2026-00", "jan-2026", "", "2026/01"])
    def test_invalid(self, bad):
        with pytest.raises(ValueError):
            month_range(bad)


class TestBoundaryAttribution:
    """The UTC half-open range must place the month start, the next month's
    midnight and the microsecond-precision month end correctly."""

    def test_half_open_membership(self):
        start, end = month_range("2026-01")
        bound = parse_partition_bound(
            "FOR VALUES FROM ('2026-01-01 00:00:00+00') TO ('2026-02-01 00:00:00+00')"
        )
        assert bound.contains(start)                                  # month start belongs in
        assert bound.contains(end - timedelta(microseconds=1))        # ...59.999999 belongs in
        assert bound.contains(datetime(2026, 1, 31, 23, 59, 59, 999999, UTC))
        assert not bound.contains(end)                                # next month midnight is out
        assert not bound.contains(start - timedelta(microseconds=1))  # previous instant is out


class TestParsePartitionBound:
    def test_default(self):
        bound = parse_partition_bound("DEFAULT")
        assert bound.is_default

    def test_range_with_plus00_offset(self):
        bound = parse_partition_bound(
            "FOR VALUES FROM ('2025-12-01 00:00:00+00') TO ('2026-01-01 00:00:00+00')"
        )
        assert not bound.is_default
        assert bound.lower == datetime(2025, 12, 1, tzinfo=UTC)
        assert bound.upper == datetime(2026, 1, 1, tzinfo=UTC)

    def test_minvalue_maxvalue(self):
        bound = parse_partition_bound("FOR VALUES FROM (MINVALUE) TO (MAXVALUE)")
        assert bound.lower is MINVALUE
        assert bound.upper is MAXVALUE
        assert bound.contains(datetime(1970, 1, 1, tzinfo=UTC))
        assert bound.contains(datetime(9999, 1, 1, tzinfo=UTC))

    def test_offset_with_minutes_and_fractional_seconds(self):
        ts = parse_ts_literal("'2026-01-31 23:59:59.999999+05:30'")
        assert ts.utcoffset() == timedelta(hours=5, minutes=30)
        assert ts.microsecond == 999999

    def test_cast_suffix_tolerated(self):
        ts = parse_ts_literal("'2026-01-01 00:00:00+00'::timestamp with time zone")
        assert ts == datetime(2026, 1, 1, tzinfo=UTC)

    def test_absolute_instant_comparison_across_offsets(self):
        # The same instant written against a non-UTC offset must compare equal.
        bound = parse_partition_bound(
            "FOR VALUES FROM ('2025-12-01 08:00:00+08') TO ('2026-01-01 08:00:00+08')"
        )
        assert bound.lower == datetime(2025, 12, 1, tzinfo=UTC)

    def test_garbage_rejected(self):
        with pytest.raises(BoundParseError):
            parse_partition_bound("FOR VALUES IN (1, 2, 3)")

    def test_overlaps(self):
        jan = parse_partition_bound(
            "FOR VALUES FROM ('2026-01-01 00:00:00+00') TO ('2026-02-01 00:00:00+00')"
        )
        start, end = month_range("2026-01")
        assert jan.overlaps(start, end)  # identical ranges overlap
        assert not jan.overlaps(end, month_range("2026-03")[1])  # touching is not overlapping
        dec = parse_partition_bound(
            "FOR VALUES FROM ('2025-12-01 00:00:00+00') TO ('2026-01-01 00:00:00+00')"
        )
        assert not dec.overlaps(start, end)  # adjacent December does not overlap January


class TestSqlLiteral:
    def test_timestamptz_normalized_to_utc(self):
        dt = datetime(2026, 1, 1, 8, 0, tzinfo=timezone(timedelta(hours=8)))
        assert sql_ts_literal(dt, with_tz=True) == "2026-01-01 00:00:00+00:00"

    def test_timestamp_without_tz(self):
        assert sql_ts_literal(datetime(2026, 2, 1, tzinfo=UTC), with_tz=False) == "2026-02-01 00:00:00"
