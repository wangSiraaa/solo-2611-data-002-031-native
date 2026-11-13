"""日期算术与修正后续（含月底跨假日）小例。"""

from __future__ import annotations

from datetime import date

import pytest

from fixedincome.dates import BusinessCalendar, add_months, year_fraction


def test_add_months_plain_and_eom():
    # 非月底约定：日对日，目标月天数不足时取月末
    assert add_months(date(2025, 1, 31), 1, end_of_month=False) == date(2025, 2, 28)
    # 月底约定：1/31（月末）+ 1 个月 -> 2/28（月末）
    assert add_months(date(2025, 1, 31), 1, end_of_month=True) == date(2025, 2, 28)
    # 月中日 + 月底标志：基准日不是月末，仍按日对日
    assert add_months(date(2025, 2, 14), 6, end_of_month=True) == date(2025, 8, 14)
    # 半年滚动
    assert add_months(date(2025, 1, 31), 6, end_of_month=True) == date(2025, 7, 31)
    assert add_months(date(2025, 7, 31), 6, end_of_month=True) == date(2026, 1, 31)
    # 跨年
    assert add_months(date(2026, 7, 31), 6, end_of_month=True) == date(2027, 1, 31)


def test_add_months_rejects_nonpositive():
    with pytest.raises(ValueError):
        add_months(date(2025, 1, 1), 0)


def test_year_fraction_act365():
    assert year_fraction(date(2025, 1, 31), date(2025, 7, 31)) == 181 / 365.0
    # 闰年也用 365 固定分母
    assert year_fraction(date(2024, 1, 1), date(2025, 1, 1)) == 366 / 365.0


def test_modified_following_weekend_within_month(calendar):
    # 2025-02-01 是周六，顺延到 2025-02-05（02-03/04 为假日），同月
    assert calendar.modify_following(date(2025, 2, 1)) == date(2025, 2, 5)


def test_modified_following_weekend_cross_month_rolls_back(calendar):
    # 2027-01-31 是周日且为假日；顺延会进入 2 月，按规则回退到 1 月最后一个营业日
    adjusted = calendar.modify_following(date(2027, 1, 31))
    assert adjusted == date(2027, 1, 29)
    assert adjusted.month == 1
    assert calendar.is_business_day(adjusted)


def test_modified_following_holiday_friday(calendar):
    # 2025-05-01（周四，假日）-> 顺延到 2025-05-05（周一，05-02 也是假日）
    assert calendar.modify_following(date(2025, 5, 1)) == date(2025, 5, 5)


def test_business_day_predicate(calendar):
    assert calendar.is_business_day(date(2025, 2, 5))
    assert not calendar.is_business_day(date(2025, 2, 1))  # 周六
    assert not calendar.is_business_day(date(2025, 5, 1))  # 假日


def test_calendar_rejects_unknown_adjustment():
    with pytest.raises(ValueError):
        BusinessCalendar(
            weekend_weekdays=(5, 6),
            holidays=frozenset(),
            adjustment="following",
        )
