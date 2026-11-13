"""计息日程与逐笔现金流。

关键约定（需求明确要求，不能混淆）：
1. 计息边界（accrual start/end）是按月滚动得到的“未调整”日历日期；
2. 付款日（payment date）对期末边界另行应用修正后续调整；
3. 起息日到到期日必须是票息月数的整数倍，否则视为未声明的残缺期并拒绝，
   绝不偷偷补一期；
4. 日计数统一 ACT/365 Fixed，票息按当期实际天数计算。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .dates import BusinessCalendar, add_months, parse_date, year_fraction
from .errors import ContractValidationError, ScheduleError

SUPPORTED_CONTRACT_FIELDS = frozenset(
    {
        "contract_id",
        "start_date",
        "maturity_date",
        "coupon_months",
        "coupon_rate",
        "principal",
        "end_of_month",
    }
)
# 报价债券额外允许净价字段；出现浮息 / 分期还本等条款一律拒绝。
SUPPORTED_BOND_FIELDS = SUPPORTED_CONTRACT_FIELDS | {"quote_id", "clean_price"}
SUPPORTED_DEPOSIT_FIELDS = frozenset(
    {"quote_id", "start_date", "maturity_date", "simple_rate"}
)
UNSUPPORTED_KEYWORDS = (
    "float",
    "floating",
    "amort",
    "amortization",
    "installment",
    "margin",
    "index",
    "reset",
)


@dataclass(frozen=True)
class Period:
    """一个完整计息期：边界未调整，付款日已调整。"""

    index: int
    accrual_start: date
    accrual_end: date
    payment_date: date
    year_fraction: float


@dataclass(frozen=True)
class CashFlow:
    """一笔固定现金流。is_principal 仅在最后一期为 True（固定本金，到期一次还本）。"""

    period_index: int
    accrual_start: date
    accrual_end: date
    payment_date: date
    year_fraction: float
    coupon_amount: float
    principal_amount: float
    total_amount: float

    def to_dict(self) -> dict:
        return {
            "period_index": self.period_index,
            "accrual_start": self.accrual_start.isoformat(),
            "accrual_end": self.accrual_end.isoformat(),
            "payment_date": self.payment_date.isoformat(),
            "year_fraction": self.year_fraction,
            "coupon_amount": self.coupon_amount,
            "principal_amount": self.principal_amount,
            "total_amount": self.total_amount,
        }


def _require_supported_fields(raw: dict, allowed: frozenset, kind: str, identifier: str) -> None:
    extra = set(raw) - allowed
    if extra:
        raise ContractValidationError(
            f"{kind} {identifier} 含本工具不支持的字段: {sorted(extra)}",
            unsupported_fields=sorted(extra),
        )
    lowered = {key.lower() for key in raw}
    hits = sorted(key for key in lowered if any(word in key for word in UNSUPPORTED_KEYWORDS))
    if hits:
        raise ContractValidationError(
            f"{kind} {identifier} 含浮息/分期还本等不支持条款的字段: {hits}",
            unsupported_fields=hits,
        )


def parse_contract(raw: dict, kind: str = "contract") -> dict:
    """校验并解析合约类输入（目标合约或基准债券），返回带类型的字典。"""
    identifier = raw.get("contract_id") or raw.get("quote_id") or "<unknown>"
    allowed = SUPPORTED_BOND_FIELDS if kind == "bond" else SUPPORTED_CONTRACT_FIELDS
    _require_supported_fields(raw, allowed, kind, identifier)
    try:
        start = parse_date(raw["start_date"])
        maturity = parse_date(raw["maturity_date"])
        coupon_months = int(raw["coupon_months"])
        coupon_rate = float(Decimal(str(raw["coupon_rate"])))
        principal = float(Decimal(str(raw["principal"])))
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractValidationError(
            f"{kind} {identifier} 字段解析失败: {exc}", identifier=identifier
        ) from exc
    end_of_month = bool(raw.get("end_of_month", False))
    if maturity <= start:
        raise ContractValidationError(
            f"{kind} {identifier} 到期日必须晚于起息日",
            start_date=start.isoformat(),
            maturity_date=maturity.isoformat(),
        )
    if coupon_months <= 0:
        raise ContractValidationError(
            f"{kind} {identifier} coupon_months 必须为正整数",
            coupon_months=coupon_months,
        )
    if coupon_rate < 0:
        raise ContractValidationError(
            f"{kind} {identifier} 固定票息不能为负", coupon_rate=coupon_rate
        )
    if principal <= 0:
        raise ContractValidationError(
            f"{kind} {identifier} 本金必须为正", principal=principal
        )
    parsed = {
        "identifier": identifier,
        "start_date": start,
        "maturity_date": maturity,
        "coupon_months": coupon_months,
        "coupon_rate": coupon_rate,
        "principal": principal,
        "end_of_month": end_of_month,
    }
    if kind == "bond":
        try:
            parsed["clean_price"] = float(Decimal(str(raw["clean_price"])))
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractValidationError(
                f"债券 {identifier} 净价解析失败: {exc}", identifier=identifier
            ) from exc
    return parsed


def parse_deposit(raw: dict) -> dict:
    """校验并解析简单计息存款报价。"""
    identifier = raw.get("quote_id", "<unknown>")
    _require_supported_fields(raw, SUPPORTED_DEPOSIT_FIELDS, "deposit", identifier)
    try:
        start = parse_date(raw["start_date"])
        maturity = parse_date(raw["maturity_date"])
        simple_rate = float(Decimal(str(raw["simple_rate"])))
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractValidationError(
            f"存款 {identifier} 字段解析失败: {exc}", identifier=identifier
        ) from exc
    if maturity <= start:
        raise ContractValidationError(
            f"存款 {identifier} 到期日必须晚于起息日", identifier=identifier
        )
    return {
        "identifier": identifier,
        "start_date": start,
        "maturity_date": maturity,
        "simple_rate": simple_rate,
    }


def build_schedule(
    start: date,
    maturity: date,
    coupon_months: int,
    end_of_month: bool,
    calendar: BusinessCalendar,
) -> list[Period]:
    """生成完整计息期列表。

    按月滚动未调整边界，再对每个期末边界做修正后续得到付款日。
    若整段期限不是 coupon_months 的整数倍，抛出 ScheduleError（残缺期拒绝）。
    """
    boundaries: list[date] = [start]
    cursor = start
    # 上限保护：正常合约期数有限，超过 1200 期（100 年月付）视为异常输入。
    for _ in range(1200):
        cursor = add_months(cursor, coupon_months, end_of_month=end_of_month)
        if cursor == maturity:
            boundaries.append(cursor)
            break
        if cursor > maturity:
            # 规则边界越过到期日：存在未声明的残缺期。cursor 是越过点，
            # 不能把它当作合法边界补进日程。
            raise ScheduleError(
                "起息日到到期日不是票息月数的整数倍，存在未声明的残缺期；"
                "本工具不偷偷补一期，请显式修正合约期限",
                start_date=start.isoformat(),
                maturity_date=maturity.isoformat(),
                coupon_months=coupon_months,
                last_regular_boundary=boundaries[-1].isoformat(),
                overshoot_boundary=cursor.isoformat(),
            )
        boundaries.append(cursor)
    else:  # pragma: no cover - 仅在异常输入触发
        raise ScheduleError(
            "计息期数超过 1200 的保护上限，请检查合约期限与票息频率",
            coupon_months=coupon_months,
        )
    periods: list[Period] = []
    for idx, (period_start, period_end) in enumerate(
        zip(boundaries[:-1], boundaries[1:]), start=1
    ):
        periods.append(
            Period(
                index=idx,
                accrual_start=period_start,
                accrual_end=period_end,
                payment_date=calendar.modify_following(period_end),
                year_fraction=year_fraction(period_start, period_end),
            )
        )
    return periods


def build_cashflows(contract: dict, calendar: BusinessCalendar) -> list[CashFlow]:
    """根据合约生成逐笔现金流（固定票息 + 到期一次还本）。"""
    periods = build_schedule(
        contract["start_date"],
        contract["maturity_date"],
        contract["coupon_months"],
        contract["end_of_month"],
        calendar,
    )
    rate = contract["coupon_rate"]
    principal = contract["principal"]
    flows: list[CashFlow] = []
    for period in periods:
        coupon = principal * rate * period.year_fraction
        redemption = principal if period.index == len(periods) else 0.0
        flows.append(
            CashFlow(
                period_index=period.index,
                accrual_start=period.accrual_start,
                accrual_end=period.accrual_end,
                payment_date=period.payment_date,
                year_fraction=period.year_fraction,
                coupon_amount=coupon,
                principal_amount=redemption,
                total_amount=coupon + redemption,
            )
        )
    return flows


def accrued_interest(
    contract: dict, periods: list[Period], valuation_date: date
) -> tuple[float, Period | None]:
    """计算估值日应计利息，返回 (应计利息, 所在计息期)。

    - 估值日不晚于起息日，或全部付款日均已过：应计为 0；
    - 落在某个计息期 [accrual_start, accrual_end) 内时，按 ACT/365 实际天数计提；
    - 付款日当天按 plan 约定“视同已付”，即进入下一期，应计重新从 0 起算，
      这一点由区间判断（左闭右开）自然实现。
    """
    if valuation_date <= contract["start_date"]:
        return 0.0, None
    for period in periods:
        if period.accrual_start < valuation_date <= period.accrual_end:
            if valuation_date == period.accrual_end:
                # 期末边界当天：该期票息视同到期支付，不计应计。
                return 0.0, period
            frac = year_fraction(period.accrual_start, valuation_date)
            return contract["principal"] * contract["coupon_rate"] * frac, period
    return 0.0, None
