"""Pure compiler tests: DST policies, calendars, exceptions, boundaries.

All expected UTC instants and offsets below are hand-computed from the
IANA rules for 2024/2026 (US DST: spring forward 2026-03-08 02:00->03:00,
fall back 2026-11-01 02:00->01:00; Asia/Shanghai is fixed +08:00).
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from schedc.cli import sample_document
from schedc.compiler import compile_rules, resolve_local
from schedc.models import (UTC, CompileError, iso_z, load_document,
                           parse_instant)


def mk_rule(**kw):
    base = {"rule_id": "r1", "timezone": "Etc/UTC", "local_time": "00:00:00",
            "start_date": "2026-01-01", "end_date": "2026-01-31",
            "frequency": {"type": "daily"},
            "on_nonexistent": "skip", "on_repeated": "first"}
    base.update(kw)
    return base


def compile1(rule, start, end, exceptions=None):
    rules, excs = load_document({"rules": [rule],
                                 "exceptions": exceptions or []})
    return compile_rules(rules, excs, parse_instant(start), parse_instant(end))


# ---------------------------------------------------------------- DST gaps

def test_ny_spring_gap_skip_records_exclusion():
    res = compile1(mk_rule(timezone="America/New_York", local_time="02:30:00",
                           start_date="2026-03-01", end_date="2026-03-15",
                           on_nonexistent="skip"),
                   "2026-03-01T00:00:00Z", "2026-03-16T00:00:00Z")
    assert len(res.plans) == 14                      # 15 days minus the gap
    assert [(x.local_date, x.reason) for x in res.exclusions] == [
        (date(2026, 3, 8), "nonexistent_local_time")]
    by_date = {p.planned_date: p for p in res.plans}
    assert date(2026, 3, 8) not in by_date
    # EST (-05:00) before the jump, EDT (-04:00) after it
    assert by_date[date(2026, 3, 7)].utc_dt == datetime(2026, 3, 7, 7, 30, tzinfo=UTC)
    assert by_date[date(2026, 3, 7)].offset_seconds == -5 * 3600
    assert by_date[date(2026, 3, 9)].utc_dt == datetime(2026, 3, 9, 6, 30, tzinfo=UTC)
    assert by_date[date(2026, 3, 9)].offset_seconds == -4 * 3600


def test_ny_spring_gap_error_aborts_compile():
    with pytest.raises(CompileError, match="does not exist"):
        compile1(mk_rule(timezone="America/New_York", local_time="02:30:00",
                         start_date="2026-03-01", end_date="2026-03-15",
                         on_nonexistent="error"),
                 "2026-03-01T00:00:00Z", "2026-03-16T00:00:00Z")


def test_local_time_existence_is_checked_not_assumed():
    # 2026-03-08 02:30 simply does not happen in New York.
    assert resolve_local(ZoneInfo("America/New_York"),
                         datetime(2026, 3, 8, 2, 30)) == []
    # ...while the same naive time is perfectly real in Shanghai.
    occs = resolve_local(ZoneInfo("Asia/Shanghai"), datetime(2026, 3, 8, 2, 30))
    assert len(occs) == 1


# ---------------------------------------------------------- DST repetitions

@pytest.mark.parametrize("policy,expected", [
    ("first",  [("2026-11-01T05:30:00Z", -4 * 3600, "first")]),
    ("second", [("2026-11-01T06:30:00Z", -5 * 3600, "second")]),
    ("both",   [("2026-11-01T05:30:00Z", -4 * 3600, "first"),
                ("2026-11-01T06:30:00Z", -5 * 3600, "second")]),
])
def test_ny_fall_back_three_policies(policy, expected):
    res = compile1(mk_rule(timezone="America/New_York", local_time="01:30:00",
                           start_date="2026-11-01", end_date="2026-11-01",
                           on_repeated=policy),
                   "2026-11-01T00:00:00Z", "2026-11-02T00:00:00Z")
    got = [(iso_z(p.utc_dt), p.offset_seconds, p.branch) for p in res.plans]
    assert got == expected


def test_both_does_not_duplicate_a_normal_time():
    res = compile1(mk_rule(timezone="Asia/Shanghai", local_time="09:00:00",
                           start_date="2026-06-01", end_date="2026-06-03",
                           on_repeated="both"),
                   "2026-06-01T00:00:00Z", "2026-06-04T00:00:00Z")
    assert len(res.plans) == 3
    assert all(p.branch == "none" for p in res.plans)


# ------------------------------------------------------------- plain zones

def test_shanghai_normal_day():
    res = compile1(mk_rule(timezone="Asia/Shanghai", local_time="09:00:00",
                           start_date="2026-06-15", end_date="2026-06-15"),
                   "2026-06-14T00:00:00Z", "2026-06-16T00:00:00Z")
    [p] = res.plans
    assert p.utc_dt == datetime(2026, 6, 15, 1, 0, tzinfo=UTC)
    assert p.offset_seconds == 8 * 3600
    assert p.branch == "none"


# ----------------------------------------------------------------- monthly

def test_leap_year_february_last_day():
    res = compile1(mk_rule(timezone="Asia/Shanghai", local_time="08:00:00",
                           start_date="2024-02-01", end_date="2024-02-29",
                           frequency={"type": "monthly", "day": "L"}),
                   "2024-02-01T00:00:00Z", "2024-03-01T00:00:00Z")
    assert [p.planned_date for p in res.plans] == [date(2024, 2, 29)]
    assert res.plans[0].utc_dt == datetime(2024, 2, 29, 0, 0, tzinfo=UTC)


def test_monthly_29_skipped_in_non_leap_february():
    res = compile1(mk_rule(timezone="Asia/Shanghai", local_time="08:00:00",
                           start_date="2025-02-01", end_date="2025-02-28",
                           frequency={"type": "monthly", "day": 29}),
                   "2025-02-01T00:00:00Z", "2025-03-01T00:00:00Z")
    assert res.plans == []


def test_monthly_29_hits_leap_day_and_is_not_shifted():
    res = compile1(mk_rule(timezone="Etc/UTC", local_time="12:00:00",
                           start_date="2024-02-01", end_date="2024-02-29",
                           frequency={"type": "monthly", "day": 29}),
                   "2024-02-01T00:00:00Z", "2024-03-01T00:00:00Z")
    assert [p.planned_date for p in res.plans] == [date(2024, 2, 29)]


def test_monthly_31_skips_short_months_without_moving():
    res = compile1(mk_rule(timezone="Etc/UTC", local_time="00:00:00",
                           start_date="2026-01-01", end_date="2026-12-31",
                           frequency={"type": "monthly", "day": 31}),
                   "2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z")
    assert sorted(p.planned_date for p in res.plans) == [
        date(2026, 1, 31), date(2026, 3, 31), date(2026, 5, 31),
        date(2026, 7, 31), date(2026, 8, 31), date(2026, 10, 31),
        date(2026, 12, 31)]
    # nothing was shifted onto e.g. April 30th
    assert not any(p.planned_date == date(2026, 4, 30) for p in res.plans)


def test_monthly_last_day_non_leap_february():
    res = compile1(mk_rule(timezone="Etc/UTC", local_time="00:00:00",
                           start_date="2026-02-01", end_date="2026-02-28",
                           frequency={"type": "monthly", "day": "L"}),
                   "2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z")
    assert [p.planned_date for p in res.plans] == [date(2026, 2, 28)]


# ------------------------------------------------------------------ weekly

def test_weekly_uses_iso_weekday_numbers():
    # 2026-03-01 is a Sunday (ISO 7).
    res = compile1(mk_rule(frequency={"type": "weekly", "weekdays": [7]},
                           start_date="2026-03-01", end_date="2026-03-31"),
                   "2026-03-01T00:00:00Z", "2026-04-01T00:00:00Z")
    assert sorted(p.planned_date for p in res.plans) == [
        date(2026, 3, 1), date(2026, 3, 8), date(2026, 3, 15),
        date(2026, 3, 22), date(2026, 3, 29)]


# --------------------------------------------------------------- exceptions

WEEKLY_MON = mk_rule(rule_id="r1", timezone="America/New_York",
                     local_time="09:00:00",
                     start_date="2026-03-01", end_date="2026-03-31",
                     frequency={"type": "weekly", "weekdays": [1]})
MAR_WINDOW = ("2026-03-01T00:00:00Z", "2026-04-01T00:00:00Z")


def test_exception_skip_overrides_base_pattern():
    res = compile1(WEEKLY_MON, *MAR_WINDOW,
                   exceptions=[{"rule_id": "r1", "date": "2026-03-09",
                                "action": "skip"}])
    assert sorted(p.planned_date for p in res.plans) == [
        date(2026, 3, 2), date(2026, 3, 16), date(2026, 3, 23),
        date(2026, 3, 30)]
    assert [(x.local_date, x.reason) for x in res.exclusions] == [
        (date(2026, 3, 9), "exception_skip")]


def test_exception_replace_adds_an_off_pattern_day():
    # 2026-03-11 is a Wednesday: no weekly plan without the exception.
    res = compile1(WEEKLY_MON, *MAR_WINDOW,
                   exceptions=[{"rule_id": "r1", "date": "2026-03-11",
                                "action": "replace", "replace_time": "15:00:00"}])
    wed = [p for p in res.plans if p.planned_date == date(2026, 3, 11)]
    assert len(wed) == 1
    assert wed[0].local_dt == datetime(2026, 3, 11, 15, 0)
    assert wed[0].utc_dt == datetime(2026, 3, 11, 19, 0, tzinfo=UTC)  # EDT
    assert wed[0].exception_basis == "exception:replace:2026-03-11"


def test_exception_replace_moves_an_on_pattern_time():
    res = compile1(WEEKLY_MON, *MAR_WINDOW,
                   exceptions=[{"rule_id": "r1", "date": "2026-03-16",
                                "action": "replace", "replace_time": "07:30:00"}])
    mon = [p for p in res.plans if p.planned_date == date(2026, 3, 16)]
    assert len(mon) == 1
    assert mon[0].utc_dt == datetime(2026, 3, 16, 11, 30, tzinfo=UTC)  # not 13:00


def test_exception_outside_effective_range_cannot_extend_it():
    res = compile1(WEEKLY_MON, *MAR_WINDOW,
                   exceptions=[{"rule_id": "r1", "date": "2026-04-01",
                                "action": "replace", "replace_time": "10:00:00"}])
    assert not any(p.planned_date == date(2026, 4, 1) for p in res.plans)
    assert [(x.local_date, x.reason) for x in res.exclusions] == [
        (date(2026, 4, 1), "exception_out_of_range")]


def test_exception_replace_into_the_gap_is_recorded():
    res = compile1(mk_rule(timezone="America/New_York", local_time="09:00:00",
                           start_date="2026-03-01", end_date="2026-03-15",
                           on_nonexistent="skip"),
                   "2026-03-01T00:00:00Z", "2026-03-16T00:00:00Z",
                   exceptions=[{"rule_id": "r1", "date": "2026-03-08",
                                "action": "replace", "replace_time": "02:30:00"}])
    assert not any(p.planned_date == date(2026, 3, 8) for p in res.plans)
    assert any(x.local_date == date(2026, 3, 8)
               and x.reason == "nonexistent_local_time"
               for x in res.exclusions)


# ----------------------------------------------------------------- window

def test_window_is_half_open_on_both_ends():
    rule = mk_rule(timezone="Etc/UTC", local_time="00:00:00",
                   start_date="2026-01-01", end_date="2026-01-03")
    res = compile1(rule, "2026-01-01T00:00:00Z", "2026-01-03T00:00:00Z")
    assert [p.planned_date for p in res.plans] == [date(2026, 1, 1),
                                                   date(2026, 1, 2)]
    res = compile1(rule, "2026-01-01T00:00:01Z", "2026-01-03T00:00:00Z")
    assert [p.planned_date for p in res.plans] == [date(2026, 1, 2)]
    res = compile1(rule, "2026-01-01T00:00:00Z", "2026-01-03T00:00:01Z")
    assert len(res.plans) == 3


def test_window_over_400_days_rejected():
    with pytest.raises(CompileError, match="400"):
        compile1(mk_rule(), "2026-01-01T00:00:00Z", "2027-02-06T00:00:00Z")


def test_window_of_exactly_400_days_accepted():
    res = compile1(mk_rule(end_date="2027-12-31"),
                   "2026-01-01T00:00:00Z", "2027-02-04T00:00:00Z")
    assert res.plans


def test_reversed_window_rejected():
    with pytest.raises(CompileError, match="after"):
        compile1(mk_rule(), "2026-02-01T00:00:00Z", "2026-01-01T00:00:00Z")


# ------------------------------------------------------------- determinism

def _sample_result():
    rules, excs = load_document(sample_document())
    return rules, excs, compile_rules(
        rules, excs, parse_instant("2026-01-01T00:00:00Z"),
        parse_instant("2027-01-01T00:00:00Z"))


def test_recompile_is_deterministic_and_rule_order_independent():
    rules, excs, r1 = _sample_result()
    r2 = compile_rules(list(reversed(rules)), excs,
                       parse_instant("2026-01-01T00:00:00Z"),
                       parse_instant("2027-01-01T00:00:00Z"))
    assert [p.plan_id for p in r1.plans] == [p.plan_id for p in r2.plans]
    assert r1.exclusions == r2.exclusions


def test_plan_uniqueness():
    _, _, res = _sample_result()
    ids = [p.plan_id for p in res.plans]
    keys = [(p.rule_id, p.planned_date, p.utc_dt) for p in res.plans]
    assert len(set(ids)) == len(ids)
    assert len(set(keys)) == len(keys)


def test_plans_sorted_by_utc_then_rule_then_date():
    _, _, res = _sample_result()
    keys = [(p.utc_dt, p.rule_id, p.planned_date) for p in res.plans]
    assert keys == sorted(keys)


def test_every_plan_roundtrips_local_to_utc_and_back():
    rules, _, res = _sample_result()
    zones = {r.rule_id: ZoneInfo(r.timezone) for r in rules}
    for p in res.plans:
        tz = zones[p.rule_id]
        occs = resolve_local(tz, p.local_dt)
        assert any(o[0] == p.utc_dt and o[1] == p.offset_seconds
                   for o in occs), p
        assert p.utc_dt.astimezone(tz).replace(tzinfo=None) == p.local_dt


def test_daily_rule_is_not_a_fixed_seconds_increment():
    # 01:30 in New York across the 2026 spring forward: the UTC gap
    # between Mar 8 and Mar 9 is 23h, not 24h.
    res = compile1(mk_rule(timezone="America/New_York", local_time="01:30:00",
                           start_date="2026-03-06", end_date="2026-03-10"),
                   "2026-03-01T00:00:00Z", "2026-03-16T00:00:00Z")
    utcs = [p.utc_dt for p in res.plans]
    deltas = {(b - a).total_seconds() for a, b in zip(utcs, utcs[1:])}
    assert 82800.0 in deltas          # 23 hours across the spring forward
    assert deltas != {86400.0}


# --------------------------------------------------------------- validation

def test_too_many_rules_rejected():
    doc = {"rules": [mk_rule(rule_id=f"r{i}") for i in range(101)]}
    with pytest.raises(CompileError, match="100"):
        load_document(doc)


def test_validation_errors():
    with pytest.raises(CompileError, match="timezone"):
        load_document({"rules": [mk_rule(timezone="Mars/Olympus")]})
    with pytest.raises(CompileError, match="on_repeated"):
        load_document({"rules": [mk_rule(on_repeated="third")]})
    with pytest.raises(CompileError, match="on_nonexistent"):
        load_document({"rules": [mk_rule(on_nonexistent="move")]})
    with pytest.raises(CompileError, match="duplicate rule_id"):
        load_document({"rules": [mk_rule(), mk_rule()]})
    with pytest.raises(CompileError, match="unknown rule_id"):
        load_document({"rules": [mk_rule()], "exceptions": [
            {"rule_id": "ghost", "date": "2026-01-02", "action": "skip"}]})
    with pytest.raises(CompileError, match="replace_time"):
        load_document({"rules": [mk_rule()], "exceptions": [
            {"rule_id": "r1", "date": "2026-01-02", "action": "replace"}]})
    with pytest.raises(CompileError, match="duplicate exception"):
        load_document({"rules": [mk_rule()], "exceptions": [
            {"rule_id": "r1", "date": "2026-01-02", "action": "skip"},
            {"rule_id": "r1", "date": "2026-01-02", "action": "skip"}]})
    with pytest.raises(CompileError, match="1..31"):
        load_document({"rules": [mk_rule(
            frequency={"type": "monthly", "day": 32})]})
    with pytest.raises(CompileError, match="ISO 1..7"):
        load_document({"rules": [mk_rule(
            frequency={"type": "weekly", "weekdays": [0]})]})
