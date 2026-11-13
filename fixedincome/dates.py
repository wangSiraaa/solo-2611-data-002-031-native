"""日期与营业日规则。

- 仅使用标准库 datetime，避免任何外部日期约定；
- 日历 weekday 编号遵循 fixtures：Monday=0 ... Sunday=6；
- 计息边界与付款日严格分开：边界用“未调整”的日历日期，付款日再做修正后续调整。
"""

from __future__ import annotations

import calendar as _calendar
import json
from dataclasses import dataclass
from datetime import date, timedelta

DAYS_IN_YEAR = 365.0  # ACT/365 Fixed：分母恒为 365，不处理闰年例外


def parse_date(value: str) -> date:
    """解析 ISO 日期字符串，拒绝非日期输入。"""
    if not isinstance(value, str):
        raise ValueError(f"日期必须是 ISO 字符串，收到 {value!r}")
    return date.fromisoformat(value)


def add_months(base: date, months: int, end_of_month: bool = False) -> date:
    """返回 base 加 months 个月后的“未调整”日历日期。

    end_of_month=True 时遵循月底约定：若基准日本身是当月最后一个营业日历日
    （自然月末），则结果也调整为目标月的自然月末；目标月天数不足时同样取月末。
    月份数必须为正。
    """
    if months <= 0:
        raise ValueError(f"月份增量必须为正整数，收到 {months}")
    total = base.month - 1 + months
    year = base.year + total // 12
    month = total % 12 + 1
    last_day = _calendar.monthrange(year, month)[1]
    if end_of_month:
        base_last_day = _calendar.monthrange(base.year, base.month)[1]
        if base.day == base_last_day:
            return date(year, month, last_day)
    return date(year, month, min(base.day, last_day))


def year_fraction(start: date, end: date) -> float:
    """ACT/365 Fixed 年份分数：实际日历天数 / 365。"""
    return (end - start).days / DAYS_IN_YEAR


@dataclass(frozen=True)
class BusinessCalendar:
    """周末 + 固定假日表的单市场营业日日历。"""

    weekend_weekdays: tuple[int, ...]
    holidays: frozenset[date]
    adjustment: str = "modified_following"

    def __init__(
        self,
        weekend_weekdays: tuple[int, ...],
        holidays: frozenset[date],
        adjustment: str = "modified_following",
    ) -> None:
        if adjustment != "modified_following":
            raise ValueError(f"只支持 modified_following，收到 {adjustment!r}")
        object.__setattr__(self, "weekend_weekdays", weekend_weekdays)
        object.__setattr__(self, "holidays", holidays)
        object.__setattr__(self, "adjustment", adjustment)

    @classmethod
    def from_dict(cls, raw: dict) -> "BusinessCalendar":
        adjustment = raw.get("payment_adjustment", "modified_following")
        numbering = raw.get("weekday_numbering", "Monday=0")
        if numbering != "Monday=0":
            raise ValueError(f"只支持 Monday=0 的 weekday 编号，收到 {numbering!r}")
        weekend = tuple(int(d) for d in raw.get("weekend_weekdays", (5, 6)))
        holidays = frozenset(parse_date(h) for h in raw.get("holidays", ()))
        return cls(weekend_weekdays=weekend, holidays=holidays, adjustment=adjustment)

    @classmethod
    def from_json_file(cls, path: str) -> "BusinessCalendar":
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    def is_business_day(self, day: date) -> bool:
        return day.weekday() not in self.weekend_weekdays and day not in self.holidays

    def modify_following(self, day: date) -> date:
        """修正后续（Modified Following）。

        遇非营业日顺延到下一个营业日；若顺延跨月，则改为向前回退到当月最后一个
        营业日。计息边界不使用本函数，这里只用于付款日。
        """
        adjusted = day
        while not self.is_business_day(adjusted):
            adjusted = adjusted + timedelta(days=1)
        if adjusted.month != day.month:
            adjusted = day
            while not self.is_business_day(adjusted):
                adjusted = adjusted - timedelta(days=1)
        return adjusted

    def to_summary(self) -> dict:
        return {
            "weekend_weekdays": list(self.weekend_weekdays),
            "weekday_numbering": "Monday=0",
            "holidays": sorted(d.isoformat() for d in self.holidays),
            "payment_adjustment": self.adjustment,
        }
