"""折现曲线与自举校准。

曲线表示
--------
支点（pillar）取各校准工具的到期付款日，曲线变量为连续复利零息利率 r(t)，
折现因子 DF(t) = exp(-r(t) * t)，t 为距估值日的 ACT/365 年份分数。
支点之间对零息利率做线性插值；首支点之前按首支点利率平坦外推（t=0 时 DF=1）；
末支点之外不做外推，直接报范围外诊断。

校准方法
--------
按到期日排序后逐个自举：
- 简单计息存款：DF(T) = 1 / (1 + r * ACT/365(T))，解析得到支点利率；
- 固定息债券：其净价 + 估值日应计 = 全价，全价等于剩余现金流按当前曲线
  （含本债券到期支点处的未知利率 r*）折现之和；r* 用 Brent 求根（scipy.brentq）。
负利率是合法解：折现因子允许大于 1，求解区间对称覆盖负利率，且不施加 DF<=1 约束。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
from scipy.optimize import brentq

from .dates import BusinessCalendar, year_fraction
from .errors import CalibrationError, OutOfRangeError
from .schedule import CashFlow, accrued_interest, build_cashflows, build_schedule

# 数值容差（也在 README 与报告中声明）
SOLVER_XTOL = 1e-12
SOLVER_RTOL = 8.881784197001252e-16  # scipy brentq 的 4*eps 机器精度下限
SOLVER_BRACKET = (-0.5, 1.0)  # 连续复利零息利率求根区间，覆盖负利率
RESIDUAL_TOL = 1e-8  # 校准后重新报价的残差容忍（货币单位，按每 100 本金）
PRICE_TOL = 1e-9  # 价格恒等核对容忍


@dataclass(frozen=True)
class DiscountCurve:
    """支点零息利率曲线（线性插值、前段平坦、末段拒绝外推）。"""

    valuation_date: date
    pillar_dates: tuple[date, ...]
    pillar_zero_rates: tuple[float, ...]
    day_count: str = "ACT/365"

    def _time(self, day: date) -> float:
        return year_fraction(self.valuation_date, day)

    def zero_rate(self, day: date) -> float:
        if day <= self.valuation_date:
            return float(self.pillar_zero_rates[0])
        times = [self._time(d) for d in self.pillar_dates]
        rates = list(self.pillar_zero_rates)
        t = self._time(day)
        if t <= times[0]:
            return rates[0]
        if t > times[-1] + 1e-15:
            raise OutOfRangeError(
                f"日期 {day.isoformat()} 超出曲线支持范围 "
                f"(截至 {self.pillar_dates[-1].isoformat()})，拒绝外推定价",
                requested_date=day.isoformat(),
                supported_to=self.pillar_dates[-1].isoformat(),
            )
        return float(np.interp(t, times, rates))

    def discount_factor(self, day: date) -> float:
        if day <= self.valuation_date:
            return 1.0
        t = self._time(day)
        return float(np.exp(-self.zero_rate(day) * t))

    @property
    def supported_to(self) -> date:
        return self.pillar_dates[-1]

    def to_summary(self) -> dict:
        return {
            "valuation_date": self.valuation_date.isoformat(),
            "day_count": self.day_count,
            "interpolation": "linear on continuously-compounded zero rates",
            "short_end": "flat extrapolation to the first pillar",
            "long_end": "no extrapolation beyond the last pillar (out-of-range error)",
            "solver": {
                "method": "scipy.optimize.brentq",
                "bracket": list(SOLVER_BRACKET),
                "xtol": SOLVER_XTOL,
                "rtol": SOLVER_RTOL,
            },
            "pillars": [
                {
                    "date": d.isoformat(),
                    "year_fraction": self._time(d),
                    "zero_rate": r,
                    "discount_factor": float(
                        np.exp(-r * self._time(d)) if d > self.valuation_date else 1.0
                    ),
                }
                for d, r in zip(self.pillar_dates, self.pillar_zero_rates)
            ],
        }


def _bond_dirty_value(
    cashflows: list[CashFlow],
    valuation_date: date,
    curve: DiscountCurve,
) -> float:
    """剩余现金流（付款日严格晚于估值日）按曲线折现求和。"""
    total = 0.0
    for cf in cashflows:
        if cf.payment_date <= valuation_date:
            continue  # 已付现金流不再计入
        total += cf.total_amount * curve.discount_factor(cf.payment_date)
    return total


def _curve_with_trial_pillar(
    base: DiscountCurve, valuation_date: date, new_date: date, trial_rate: float
) -> DiscountCurve:
    """在已自举支点后追加一个候选支点，构造试算曲线（支点保持日期升序）。"""
    dates = list(base.pillar_dates) + [new_date]
    rates = list(base.pillar_zero_rates) + [trial_rate]
    order = np.argsort([year_fraction(valuation_date, d) for d in dates])
    dates = [dates[i] for i in order]
    rates = [rates[i] for i in order]
    return DiscountCurve(valuation_date, tuple(dates), tuple(rates))


@dataclass
class InstrumentResidual:
    quote_id: str
    instrument_type: str
    maturity_date: date
    target_quote: float
    model_quote: float
    residual: float

    def to_dict(self) -> dict:
        return {
            "quote_id": self.quote_id,
            "instrument_type": self.instrument_type,
            "maturity_date": self.maturity_date.isoformat(),
            "target_quote": self.target_quote,
            "model_quote": self.model_quote,
            "residual": self.residual,
            "abs_residual": abs(self.residual),
        }


@dataclass
class CalibrationResult:
    curve: DiscountCurve
    residuals: list[InstrumentResidual]
    instrument_count: int
    deposit_count: int
    bond_count: int

    def to_summary(self) -> dict:
        return {
            "instrument_count": self.instrument_count,
            "deposit_count": self.deposit_count,
            "bond_count": self.bond_count,
            "residual_tolerance": RESIDUAL_TOL,
            "max_abs_residual": max((abs(r.residual) for r in self.residuals), default=0.0),
            "residuals": [r.to_dict() for r in self.residuals],
        }


def bootstrap_curve(
    valuation_date: date,
    deposits: list[dict],
    bonds: list[dict],
    calendar: BusinessCalendar,
) -> CalibrationResult:
    """按存款 + 债券净价自举折现曲线。

    任何无法得到可信解的情形都抛 CalibrationError，带具体诊断；
    绝不会返回“用某个初值算出来”的曲线。
    """
    if not deposits and not bonds:
        raise CalibrationError("没有任何可校准工具，无法构建折现曲线")

    pillars: list[tuple[date, float]] = []  # (支点日期, 连续复利零息利率)
    residuals: list[InstrumentResidual] = []
    seen_pillar_dates: set[date] = set()

    def add_pillar(day: date, rate: float, quote_id: str) -> None:
        if day in seen_pillar_dates:
            raise CalibrationError(
                f"工具 {quote_id} 的到期支点 {day.isoformat()} 与其它工具重复，"
                "同一支点存在两个相互冲突的报价，无法自举",
                duplicate_pillar=day.isoformat(),
            )
        if day <= valuation_date:
            raise CalibrationError(
                f"工具 {quote_id} 的到期日不晚于估值日，无可用于校准的未来现金流",
                quote_id=quote_id,
            )
        seen_pillar_dates.add(day)
        pillars.append((day, rate))

    # 先按到期日排序，存款与债券统一按到期日进入自举顺序。
    deposit_items = sorted(deposits, key=lambda d: d["maturity_date"])
    bond_items = sorted(bonds, key=lambda b: b["maturity_date"])

    for dep in deposit_items:
        if dep["start_date"] != valuation_date:
            raise CalibrationError(
                f"存款 {dep['identifier']} 起息日 {dep['start_date'].isoformat()} "
                f"不是估值日 {valuation_date.isoformat()}；远期起息存款需要已知的远端"
                "折现因子，本工具不支持静默处理",
                quote_id=dep["identifier"],
            )
        yf = year_fraction(valuation_date, dep["maturity_date"])
        df = 1.0 / (1.0 + dep["simple_rate"] * yf)  # 负利率下 DF 可大于 1，合法
        rate = -np.log(df) / yf
        add_pillar(dep["maturity_date"], float(rate), dep["identifier"])
        residuals.append(
            InstrumentResidual(
                quote_id=dep["identifier"],
                instrument_type="deposit",
                maturity_date=dep["maturity_date"],
                target_quote=dep["simple_rate"],
                model_quote=(1.0 / df - 1.0) / yf,
                residual=0.0,  # 解析校准，重报残差恒为 0
            )
        )

    for bond in bond_items:
        periods = build_schedule(
            bond["start_date"],
            bond["maturity_date"],
            bond["coupon_months"],
            bond["end_of_month"],
            calendar,
        )
        cashflows = build_cashflows(bond, calendar)
        future_flows = [cf for cf in cashflows if cf.payment_date > valuation_date]
        if not future_flows:
            raise CalibrationError(
                f"债券 {bond['identifier']} 在估值日之后没有任何现金流，无法用于校准",
                quote_id=bond["identifier"],
            )
        accrued, _ = accrued_interest(bond, periods, valuation_date)
        target_dirty = bond["clean_price"] + accrued
        maturity_payment = future_flows[-1].payment_date
        if maturity_payment <= valuation_date:
            raise CalibrationError(
                f"债券 {bond['identifier']} 到期付款日不晚于估值日",
                quote_id=bond["identifier"],
            )
        # 重复支点必须在求根之前识别：同一到期日存在两个冲突报价时，
        # 数学上没有单一利率能同时满足，不能靠“无解”掩盖真正的诊断。
        if maturity_payment in seen_pillar_dates:
            raise CalibrationError(
                f"债券 {bond['identifier']} 的到期支点 {maturity_payment.isoformat()} "
                "与其它工具重复，同一支点存在两个相互冲突的报价，无法自举",
                quote_id=bond["identifier"],
                duplicate_pillar=maturity_payment.isoformat(),
            )

        def objective(trial_rate: float) -> float:
            base = DiscountCurve(
                valuation_date,
                tuple(d for d, _ in pillars),
                tuple(r for _, r in pillars),
            )
            trial_curve = _curve_with_trial_pillar(
                base, valuation_date, maturity_payment, trial_rate
            )
            return _bond_dirty_value(cashflows, valuation_date, trial_curve) - target_dirty

        low, high = SOLVER_BRACKET
        f_low, f_high = objective(low), objective(high)
        if not np.isfinite(f_low) or not np.isfinite(f_high) or f_low * f_high > 0:
            raise CalibrationError(
                f"债券 {bond['identifier']} 在求根区间 [{low}, {high}] 内无解"
                "（两端残差同号或非有限值），报价与其它工具不存在一致的折现曲线",
                quote_id=bond["identifier"],
                bracket=[low, high],
                residual_at_bracket=[float(f_low), float(f_high)],
                target_dirty_price=target_dirty,
            )
        root = brentq(objective, low, high, xtol=SOLVER_XTOL, rtol=SOLVER_RTOL)
        # 不以求根器返回成功作为完整验证：显式重算残差并核对容忍。
        check_curve = _curve_with_trial_pillar(
            DiscountCurve(
                valuation_date,
                tuple(d for d, _ in pillars),
                tuple(r for _, r in pillars),
            ),
            valuation_date,
            maturity_payment,
            root,
        )
        model_dirty = _bond_dirty_value(cashflows, valuation_date, check_curve)
        if abs(model_dirty - target_dirty) > RESIDUAL_TOL:
            raise CalibrationError(
                f"债券 {bond['identifier']} 求根后残差 {model_dirty - target_dirty:.3e} "
                f"超过容忍 {RESIDUAL_TOL:.0e}，校准不可信",
                quote_id=bond["identifier"],
                residual=model_dirty - target_dirty,
            )
        add_pillar(maturity_payment, float(root), bond["identifier"])
        model_clean = model_dirty - accrued
        residuals.append(
            InstrumentResidual(
                quote_id=bond["identifier"],
                instrument_type="bond",
                maturity_date=maturity_payment,
                target_quote=bond["clean_price"],
                model_quote=model_clean,
                residual=model_clean - bond["clean_price"],
            )
        )

    pillars.sort(key=lambda item: item[0])
    curve = DiscountCurve(
        valuation_date,
        tuple(d for d, _ in pillars),
        tuple(r for _, r in pillars),
    )

    # 全量重新报价核对：用最终曲线重估全部工具，残差必须落在容忍内。
    verified: list[InstrumentResidual] = []
    residual_map = {r.quote_id: r for r in residuals}
    for dep in deposit_items:
        yf = year_fraction(valuation_date, dep["maturity_date"])
        df = curve.discount_factor(dep["maturity_date"])
        model_rate = (1.0 / df - 1.0) / yf
        diff = model_rate - dep["simple_rate"]
        if abs(diff) > RESIDUAL_TOL:
            raise CalibrationError(
                f"存款 {dep['identifier']} 全量重报残差 {diff:.3e} 超容忍",
                quote_id=dep["identifier"],
                residual=diff,
            )
        r = residual_map[dep["identifier"]]
        verified.append(
            InstrumentResidual(r.quote_id, r.instrument_type, r.maturity_date,
                               r.target_quote, model_rate, diff)
        )
    for bond in bond_items:
        periods = build_schedule(
            bond["start_date"], bond["maturity_date"],
            bond["coupon_months"], bond["end_of_month"], calendar,
        )
        cashflows = build_cashflows(bond, calendar)
        accrued, _ = accrued_interest(bond, periods, valuation_date)
        model_dirty = _bond_dirty_value(cashflows, valuation_date, curve)
        model_clean = model_dirty - accrued
        diff = model_clean - bond["clean_price"]
        if abs(diff) > RESIDUAL_TOL:
            raise CalibrationError(
                f"债券 {bond['identifier']} 全量重报残差 {diff:.3e} 超过容忍 "
                f"{RESIDUAL_TOL:.0e}",
                quote_id=bond["identifier"],
                residual=diff,
            )
        verified.append(
            InstrumentResidual(
                bond["identifier"], "bond", cashflows[-1].payment_date,
                bond["clean_price"], model_clean, diff,
            )
        )

    verified.sort(key=lambda r: r.maturity_date)
    return CalibrationResult(
        curve=curve,
        residuals=verified,
        instrument_count=len(deposits) + len(bonds),
        deposit_count=len(deposits),
        bond_count=len(bonds),
    )
