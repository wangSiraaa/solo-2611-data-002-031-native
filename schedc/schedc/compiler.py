"""Compile local periodic rules into UTC plan items.

Every local date is enumerated on the calendar and resolved individually
through ``zoneinfo`` against the tzdata installed on this machine.  Nothing
is ever computed by adding fixed second offsets to a previous occurrence.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .models import (MAX_WINDOW_DAYS, CompileError, DateException, Exclusion,
                     Plan, Rule, UTC, canonical_rule, iso_z)

ONE_DAY = timedelta(days=1)


@dataclass
class CompileResult:
    window_start: datetime
    window_end: datetime
    plans: list[Plan]
    exclusions: list[Exclusion]


def resolve_local(tz: ZoneInfo, naive: datetime) -> list[tuple[datetime, int]]:
    """Real UTC occurrences of a naive local time, sorted by UTC instant.

    Returns 0 entries when the local time never happens (spring-forward
    gap), 1 entry for a normal time, 2 entries for an ambiguous local time
    (fall-back), earlier UTC first.  A local time is only accepted when a
    genuine UTC -> local round-trip reproduces it, never by merely
    attaching tzinfo.
    """
    found: list[tuple[datetime, int]] = []
    seen: set[datetime] = set()
    for fold in (0, 1):
        aware = naive.replace(tzinfo=tz, fold=fold)
        utc = aware.astimezone(UTC)
        if utc in seen:
            continue
        back = utc.astimezone(tz)
        if back.replace(tzinfo=None) == naive and back.fold == fold:
            seen.add(utc)
            found.append((utc, int(aware.utcoffset().total_seconds())))
    found.sort(key=lambda item: item[0])
    return found


def _last_day_of_month(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (date(year, month + 1, 1) - ONE_DAY).day


def _matches(rule: Rule, d: date) -> bool:
    f = rule.frequency
    if f.kind == "daily":
        return True
    if f.kind == "weekly":
        return d.isoweekday() in f.weekdays
    # monthly: a day number that the month does not have simply never
    # matches -- it is skipped, never shifted to the month end.
    if f.last_day:
        return d.day == _last_day_of_month(d.year, d.month)
    return d.day == f.month_day


def make_plan_id(rule: Rule, planned_date: date, utc_dt: datetime) -> str:
    """Stable id from canonical rule content + local date + actual UTC."""
    canon = json.dumps(canonical_rule(rule), sort_keys=True, separators=(",", ":"))
    blob = f"{canon}|{planned_date.isoformat()}|{iso_z(utc_dt)}"
    return "pln_" + hashlib.sha256(blob.encode()).hexdigest()[:24]


def validate_window(window_start: datetime, window_end: datetime) -> None:
    if window_end <= window_start:
        raise CompileError("window end must be after window start")
    if window_end - window_start > timedelta(days=MAX_WINDOW_DAYS):
        raise CompileError(
            f"compile window exceeds {MAX_WINDOW_DAYS} days: "
            f"{window_end - window_start}")


def compile_rules(rules: list[Rule], exceptions: list[DateException],
                  window_start: datetime, window_end: datetime) -> CompileResult:
    """Compile plans strictly inside the half-open window [start, end)."""
    validate_window(window_start, window_end)
    exc_by_rule: dict[str, dict[date, DateException]] = {}
    for e in exceptions:
        exc_by_rule.setdefault(e.rule_id, {})[e.local_date] = e

    plans: list[Plan] = []
    exclusions: list[Exclusion] = []

    for rule in rules:
        tz = ZoneInfo(rule.timezone)
        excs = exc_by_rule.get(rule.rule_id, {})
        # Local dates whose times could possibly land inside the UTC window
        # (offsets are well within +/-24h, so a 2 day margin is ample).
        lo = window_start.astimezone(tz).date() - timedelta(days=2)
        hi = window_end.astimezone(tz).date() + timedelta(days=2)

        # Exceptions dated outside the rule's effective range can never
        # produce a plan; record them when they fall in the visible window.
        for exc_date in sorted(excs):
            if lo <= exc_date <= hi and not (rule.start_date <= exc_date <= rule.end_date):
                exclusions.append(Exclusion(
                    rule.rule_id, exc_date, "exception_out_of_range",
                    f"exception date {exc_date} is outside effective range "
                    f"{rule.start_date}..{rule.end_date}"))

        d = max(rule.start_date, lo)
        last = min(rule.end_date, hi)
        while d <= last:
            exc = excs.get(d)
            if exc is not None and exc.action == "skip":
                exclusions.append(Exclusion(
                    rule.rule_id, d, "exception_skip",
                    f"exception skip on {d}"))
                d += ONE_DAY
                continue
            if exc is not None:  # replace: fires even off-pattern
                candidates = [(exc.replace_time, f"exception:replace:{d.isoformat()}")]
            elif _matches(rule, d):
                candidates = [(rule.local_time, None)]
            else:
                candidates = []

            for local_t, basis in candidates:
                naive = datetime.combine(d, local_t)
                occs = resolve_local(tz, naive)
                if not occs:
                    if rule.on_nonexistent == "error":
                        raise CompileError(
                            f"rule {rule.rule_id}: local time "
                            f"{naive.isoformat()} does not exist in "
                            f"{rule.timezone} (DST gap, policy=error)")
                    exclusions.append(Exclusion(
                        rule.rule_id, d, "nonexistent_local_time",
                        f"local time {local_t.strftime('%H:%M:%S')} does not "
                        f"exist in {rule.timezone} on {d} (policy=skip)"))
                    continue
                if len(occs) == 1:
                    chosen = [(occs[0], "none")]
                elif rule.on_repeated == "first":
                    chosen = [(occs[0], "first")]
                elif rule.on_repeated == "second":
                    chosen = [(occs[1], "second")]
                else:  # both
                    chosen = [(occs[0], "first"), (occs[1], "second")]
                for (utc_dt, offset), branch in chosen:
                    if window_start <= utc_dt < window_end:
                        plans.append(Plan(
                            plan_id=make_plan_id(rule, d, utc_dt),
                            rule_id=rule.rule_id,
                            planned_date=d,
                            local_dt=naive,
                            utc_dt=utc_dt,
                            offset_seconds=offset,
                            branch=branch,
                            exception_basis=basis))
            d += ONE_DAY

    plans.sort(key=lambda p: (p.utc_dt, p.rule_id, p.planned_date))
    exclusions.sort(key=lambda e: (e.rule_id, e.local_date, e.reason))
    return CompileResult(window_start, window_end, plans, exclusions)
