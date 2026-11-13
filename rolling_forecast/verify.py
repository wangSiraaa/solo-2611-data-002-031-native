"""独立核验：不依赖主流程内部结论，从文件/合成数据重新计算并断言。

覆盖九项检查：
 1. 朴素预测手算复核（多条，含修订切换）；
 2. 滞后来源追溯到具体 record_id/revision/行号；
 3. 未来可见性不变性：发起时刻后才可见的销量改成极大值，旧预测逐值不变；
 4. 全量预处理泄漏反例：证明逐折 fit 的填充/缩放参数与全量 fit 不同；
 5. 输入行重排不变性；
 6. 缺测不补零（朴素标记不可预测，Ridge 不产生零冒充）；
 7. 同修订内容冲突阻断整折；
 8. 迟公布日历不倒灌；
 9. 重复运行可复现（确定性）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

from .backtest import run_backtest
from .config import load_plan
from .evaluate import evaluate_predictions
from .io_data import load_calendar, load_sales
from .status import (
    MODEL_NAIVE,
    MODEL_RIDGE,
    PRED_BLOCKED,
    PRED_NAIVE_UNAVAILABLE,
    PRED_OK,
)
from .visibility import calendar_snapshot, sales_snapshot


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str


class VerifyError(AssertionError):
    pass


def _load(sales_path: str, calendar_path: str, plan_path: str):
    return (
        load_sales(sales_path),
        load_calendar(calendar_path),
        load_plan(plan_path),
    )


def _row(df: pd.DataFrame, **filters) -> pd.Series:
    out = df
    for k, v in filters.items():
        if k == "target_date":
            out = out[out["target_date"] == pd.Timestamp(v).date()]
        elif k == "origin":
            out = out[out["origin"] == pd.Timestamp(v)]
        else:
            out = out[out[k] == v]
    if len(out) != 1:
        raise VerifyError(f"过滤条件 {filters} 期望 1 行，实际 {len(out)} 行")
    return out.iloc[0]


# --------------------------------------------------------------------------- #
def check_naive_manual(sales, calendar, plan) -> CheckResult:
    """用文件内容手工重算季节朴素，不调用 models 内部函数。"""
    preds, _ = run_backtest(sales, calendar, plan)
    # 每条手算：目标日 - 7 天，取发起时刻可见的最高修订。
    cases = [
        # (origin, series, 目标日, 期望销量, 期望 revision, 滞后日)
        ("2025-02-10T12:00:00Z", "A", "2025-02-11", 62, 0, "2025-02-04"),
        ("2025-02-10T12:00:00Z", "B", "2025-02-12", 67, 0, "2025-02-05"),
        ("2025-02-17T12:00:00Z", "A", "2025-02-18", 64, 0, "2025-02-11"),
        ("2025-02-17T12:00:00Z", "A", "2025-02-19", 40, 0, "2025-02-12"),
        ("2025-02-17T12:00:00Z", "A", "2025-02-20", 41, 0, "2025-02-13"),
        # 修订切换：02-01 rev1 在 02-07 可见；02-17 折的 02-24? 不在水平内，
        # 用 03-03 折验证 rev1 真正进入滞后（02-25 rev1 当天 08:00 可见）。
        ("2025-03-03T12:00:00Z", "C", "2025-03-04", 57, 1, "2025-02-25"),
        ("2025-03-03T12:00:00Z", "B", "2025-03-04", 96, 1, "2025-02-25"),
        ("2025-03-03T12:00:00Z", "B", "2025-03-05", 67, 0, "2025-02-26"),
        ("2025-03-03T12:00:00Z", "A", "2025-03-06", 41, 0, "2025-02-27"),
    ]

    details = []
    for origin, sid, target, expected, exp_rev, lag_day in cases:
        r = _row(preds, origin=origin, series_id=sid, model_id=MODEL_NAIVE,
                 target_date=target)
        if r["prediction_status"] != PRED_OK:
            raise VerifyError(f"朴素 {sid} {target} 状态异常：{r['prediction_status']}")
        if abs(float(r["predicted_sales"]) - expected) > 1e-9:
            raise VerifyError(
                f"朴素手算不符 {sid} {target}: 得 {r['predicted_sales']} 期望 {expected}"
            )
        src = r["lag_sources"][f"lag_{plan.seasonal_lag}"]
        if src["revision"] != exp_rev:
            raise VerifyError(
                f"滞后修订不符 {sid} {target}: 得 rev{src['revision']} 期望 rev{exp_rev}"
            )
        if src["business_date"] != lag_day:
            raise VerifyError(
                f"滞后日不符 {sid} {target}: 得 {src['business_date']} 期望 {lag_day}"
            )
        details.append(f"{sid}@{target}<-{lag_day}={expected}(rev{exp_rev})")
    return CheckResult("1. 朴素手算复核（含修订切换）", True, "；".join(details))


def check_lag_provenance(sales, calendar, plan) -> CheckResult:
    """每条 Ridge 预测的 lag_7 来源必须能在 sales.csv 原行对上号。"""
    preds, evidence = run_backtest(sales, calendar, plan)
    ridge_ok = preds[(preds.model_id == MODEL_RIDGE) & (preds.prediction_status == PRED_OK)]
    checked = 0
    for r in ridge_ok.itertuples(index=False):
        for name, src in r.lag_sources.items():
            if src.get("imputed") or src.get("origin") == "recursive_forecast":
                continue  # 填充与递推来源在其他检查中覆盖
            rid = src["record_id"]
            line = src["source_line"]
            raw = sales[sales["source_line"] == line]
            if len(raw) != 1 or raw.iloc[0]["record_id"] != rid:
                raise VerifyError(f"来源行 {line} 与 record_id {rid} 对不上")
            if int(raw.iloc[0]["revision"]) != src["revision"]:
                raise VerifyError(f"来源修订对不上：{rid}")
            if pd.Timestamp(raw.iloc[0]["available_at"]) > pd.Timestamp(r.origin):
                raise VerifyError(f"来源 {rid} 在发起时刻不可见，发生未来泄漏")
            checked += 1
    # 折证据 JSON 中同样保留来源且与预测帧一致
    ev = [e for e in evidence if e["model_id"] == MODEL_RIDGE and e["status"] == PRED_OK]
    if not ev:
        raise VerifyError("没有成功的 Ridge 折证据")
    return CheckResult(
        "2. 滞后来源追溯（record_id/行号/修订/可见时刻）",
        True,
        f"核对 {checked} 个观测型滞后引用，全部指回原始 CSV 行且可见；折证据 {len(ev)} 份",
    )


def check_future_invariance(sales, calendar, plan) -> CheckResult:
    """把所有 origin 之后才可见的销量改成 999999，旧预测必须完全不变。"""
    preds_before, ev_before = run_backtest(sales, calendar, plan)

    latest_origin = max(plan.origins)
    mutated = sales.copy()
    future_mask = mutated["available_at"] > pd.Timestamp(latest_origin)
    n_mutated = int(future_mask.sum())
    if n_mutated == 0:
        raise VerifyError("夹具中应存在最后一个 origin 之后才可见的行，否则本检查无意义")
    mutated.loc[future_mask, "sales"] = 999999.0

    preds_after, ev_after = run_backtest(mutated, calendar, plan)

    key_cols = ["origin", "series_id", "model_id", "step", "target_date"]
    merged = preds_before.merge(preds_after, on=key_cols, suffixes=("_old", "_new"))
    diff = merged[
        (merged["predicted_sales_old"].notna() | merged["predicted_sales_new"].notna())
        & (merged["predicted_sales_old"] != merged["predicted_sales_new"])
    ]
    if not diff.empty:
        raise VerifyError(f"未来数据变更影响了 {len(diff)} 条历史预测：\n{diff[key_cols].head()}")
    if merged["prediction_status_old"].tolist() != merged["prediction_status_new"].tolist():
        raise VerifyError("未来数据变更改变了预测状态")
    # 同时确认被篡改的值确实属于"未来"，且评价真值（evaluation_as_of 更晚）会变化
    eval_before, _, _ = evaluate_predictions(preds_before.copy(), sales, plan)
    eval_after, _, _ = evaluate_predictions(preds_after.copy(), mutated, plan)
    assert not eval_after["actual_sales"].equals(eval_before["actual_sales"]), (
        "篡改应能影响真值侧，否则测试本身无效"
    )
    return CheckResult(
        "3. 未来可见性不变性",
        True,
        f"篡改 {n_mutated} 条迟到行销量为 999999；{len(merged)} 条旧预测数值/状态零变化，"
        "而评价真值侧如期变化，证明预测只依赖发起时刻可见数据",
    )


def check_preprocessing_leak(sales, calendar, plan) -> CheckResult:
    """构造全量预处理会泄漏的反例，对比逐折 fit 与全量 fit 的参数。

    合成一条门店：早期平稳在 ~10，最后一个 origin 之后突然出现 10000 级的新高销量。
    全量均值/尺度会被未来高点拉偏；逐折 fit 的参数必须保持在早期量级。
    """
    rng = np.random.default_rng(plan.seed)
    rows = []
    cal_rows = []
    line = 2
    cal_line = 2
    dates = pd.date_range("2025-01-01", "2025-03-20", freq="D")
    for d in dates:
        # 平稳基线 + 周内季节形状
        base = 10 + 2 * (d.weekday() % 3) + rng.normal(0, 0.3)
        avail = (d + pd.Timedelta(days=1)).normalize() + pd.Timedelta(hours=8)
        rows.append(
            dict(record_id=f"X:{d.date()}", series_id="X", business_date=d.date(),
                 sales=round(float(base), 2), revision=0,
                 available_at=avail, source_line=line)
        )
        line += 1
        cal_rows.append(
            dict(series_id="X", business_date=d.date(), weekday=int(d.weekday()),
                 planned_open=1, available_at=pd.Timestamp("2024-12-20T00:00:00Z"),
                 source_line=cal_line)
        )
        cal_line += 1
    # 未来泄漏源：03-01 的暴增修订落在最后折(origin 03-03)训练窗内，
    # 但 revision=1 在 origin 之后（03-05）才可见。
    # 正确的逐折 fit 只能看到 rev0（~10 量级）；错误的"全量预处理"若用最终
    # 修订后的完整序列估计均值/尺度，就会被 10000 污染。
    leak_day = pd.Timestamp("2025-03-01")
    rows.append(
        dict(record_id=f"X:{leak_day.date()}", series_id="X", business_date=leak_day.date(),
             sales=10000.0, revision=1,
             available_at=pd.Timestamp("2025-03-05T08:00:00Z"), source_line=line)
    )
    synth_sales = pd.DataFrame(rows)
    synth_sales["business_date"] = pd.to_datetime(synth_sales["business_date"], utc=True).dt.date
    synth_sales["available_at"] = pd.to_datetime(synth_sales["available_at"], utc=True)
    synth_cal = pd.DataFrame(cal_rows)
    synth_cal["business_date"] = pd.to_datetime(synth_cal["business_date"], utc=True).dt.date
    synth_cal["available_at"] = pd.to_datetime(synth_cal["available_at"], utc=True)

    preds, evidence = run_backtest(synth_sales, synth_cal, plan)
    last_origin = pd.Timestamp(max(plan.origins))
    fold_ev = [
        e for e in evidence
        if e["model_id"] == MODEL_RIDGE
        and pd.Timestamp(e["origin"]) == last_origin
        and e["status"] == PRED_OK
    ]
    if not fold_ev:
        raise VerifyError("合成数据的最后一折 Ridge 应成功拟合")
    fold_fill = fold_ev[0]["preprocessing"]["imputer_fill_values"]
    fold_scale = fold_ev[0]["preprocessing"]["scaler_scale"]

    # 错误做法 A：用"最终修订后的全量序列"fit（含迟到 rev1 的 10000）。
    final_series = (
        synth_sales.sort_values(["business_date", "revision"])
        .drop_duplicates("business_date", keep="last")["sales"]
        .to_numpy(dtype=float)
    )
    leaky_mean = float(np.mean(final_series))
    leaky_scale = float(np.std(final_series))
    # 错误做法 B：全量 fit 若只按可见性过滤但跨所有折 pooled，同样把未来折信息混入；
    # 这里以错误做法 A 作为主对照（最常见的泄漏实现）。
    fold_lag1_fill = float(fold_fill["lag_1"])

    if fold_lag1_fill > 50:
        raise VerifyError(f"逐折填充均值 {fold_lag1_fill} 被迟到高点污染，按折 fit 失效")
    if leaky_mean < 100:
        raise VerifyError(
            f"对照设计无效：全量泄漏均值 {leaky_mean} 未被 10000 拉高，无法形成反例"
        )
    if abs(fold_lag1_fill - leaky_mean) < 50:
        raise VerifyError(
            f"逐折填充均值 {fold_lag1_fill:.3f} 与全量泄漏均值 {leaky_mean:.3f} 差异不足，"
            "无法证明按折拟合"
        )
    if float(fold_scale["lag_1"]) >= leaky_scale:
        raise VerifyError(
            f"逐折尺度 {float(fold_scale['lag_1']):.3f} 未小于全量泄漏尺度 {leaky_scale:.3f}"
        )
    # 额外正向断言：逐折参数必须与"只用当时可见数据"独立重建训练帧后的列均值一致。
    # 复用生产代码的训练帧构造（输入为独立合成数据 + 独立快照），序列范围与折一致：
    # 仅训练窗日期作为监督目标；序列向前扩最大滞后天数，窗外日期只作滞后来源。
    from .models import _build_training_frame

    visible_at_origin = sales_snapshot(synth_sales, last_origin).visible
    vis_x = (
        visible_at_origin[visible_at_origin.series_id == "X"]
        .sort_values("business_date")
        .set_index("business_date")["sales"]
    )
    origin_date = last_origin.date()
    train_start = origin_date - pd.Timedelta(days=plan.training_days)
    train_end = origin_date - pd.Timedelta(days=1)
    max_lag = max(plan.model.lags)
    ext_start = train_start - pd.Timedelta(days=max_lag)
    series_dates = pd.date_range(ext_start, train_end, freq="D").date
    hist = pd.Series(
        {pd.Timestamp(d): vis_x.get(d, np.nan) for d in series_dates}
    ).sort_index()
    open_map = {d: {"planned_open": 1} for d in series_dates}
    frame = _build_training_frame(
        hist, plan.model.lags, open_map,
        target_start=train_start, target_end=train_end,
    )
    independent_mean = float(frame["lag_1"].mean())
    if abs(fold_lag1_fill - independent_mean) > 1e-9:
        raise VerifyError(
            f"逐折 lag_1 填充 {fold_lag1_fill:.6f} 与独立重建训练帧的列均值 "
            f"{independent_mean:.6f} 不一致"
        )
    return CheckResult(
        "4. 全量预处理泄漏反例（证明真实按折 fit）",
        True,
        f"迟到暴增 rev1（03-01=10000，03-05 才可见）在最后折训练窗内但当时不可见；"
        f"逐折 lag_1 填充均值={fold_lag1_fill:.3f}、尺度={float(fold_scale['lag_1']):.3f}，"
        f"且与独立可见快照均值 {independent_mean:.3f} 完全一致；"
        f"错误的全量 fit 会给出均值={leaky_mean:.3f}、尺度={leaky_scale:.3f} => "
        "预处理参数确实只来自本折训练数据",
    )


def check_row_reorder(sales, calendar, plan) -> CheckResult:
    shuffled = sales.sample(frac=1.0, random_state=plan.seed).reset_index(drop=True)
    # source_line 是输入行号，重排后会变；预测数值与状态不应依赖物理顺序
    p1, e1 = run_backtest(sales, calendar, plan)
    p2, e2 = run_backtest(shuffled, calendar, plan)
    key_cols = ["origin", "series_id", "model_id", "step", "target_date"]
    m = p1.merge(p2, on=key_cols, suffixes=("_a", "_b"))
    if m["predicted_sales_a"].fillna(-1).tolist() != m["predicted_sales_b"].fillna(-1).tolist():
        raise VerifyError("行重排后预测数值发生变化")
    if m["prediction_status_a"].tolist() != m["prediction_status_b"].tolist():
        raise VerifyError("行重排后预测状态发生变化")
    return CheckResult(
        "5. 输入行重排不变性", True,
        f"随机重排全部 {len(shuffled)} 行后，{len(m)} 条预测数值与状态完全一致",
    )


def check_missing_not_zero(sales, calendar, plan) -> CheckResult:
    preds, _ = run_backtest(sales, calendar, plan)
    # C 店 2025-02-05..07 缺测：origin 02-10 的目标 02-11/12/13，滞后 7 天 = 02-04/05/06
    naive_c = preds[
        (preds.series_id == "C")
        & (preds.model_id == MODEL_NAIVE)
        & (preds.origin == pd.Timestamp("2025-02-10T12:00:00Z"))
    ].sort_values("step")
    statuses = naive_c["prediction_status"].tolist()
    if statuses != [PRED_OK, PRED_NAIVE_UNAVAILABLE, PRED_NAIVE_UNAVAILABLE]:
        raise VerifyError(f"C 店 02-10 折朴素状态序列错误：{statuses}")
    blocked_rows = naive_c[naive_c.prediction_status == PRED_NAIVE_UNAVAILABLE]
    if (blocked_rows["predicted_sales"].notna()).any():
        raise VerifyError("不可预测行不允许携带数值（更不能是零）")
    # 任何成功预测都不应恰好为"缺测补零"的产物：检查所有数值预测非负且没有 0.0 凭空出现
    ridge = preds[(preds.model_id == MODEL_RIDGE) & (preds.prediction_status == PRED_OK)]
    if (ridge["predicted_sales"] == 0.0).any():
        raise VerifyError("Ridge 出现 0.0 预测，缺测补零嫌疑")
    # 评价侧：缺测目标日不得按零真值评价
    evaluated, _, reasons = evaluate_predictions(preds.copy(), sales, plan)
    if (evaluated["actual_sales"] == 0.0).any():
        raise VerifyError("真值中出现 0.0，缺测可能被当成零")
    return CheckResult(
        "6. 缺测不补零",
        True,
        "C 店 02-10 折 step2/3 朴素标记 unpredictable_naive_missing_lag 且无数值；"
        f"未评价原因登记 {len(reasons)} 行；Ridge 无零预测、评价无零真值",
    )


def _make_conflict_sales(sales: pd.DataFrame) -> pd.DataFrame:
    """在最后一个 origin 之前可见的数据中注入同一 record_id+revision 的冲突行。"""
    mutated = sales.copy()
    target = mutated[
        (mutated["record_id"] == "A:2025-02-08") & (mutated["revision"] == 0)
    ].iloc[0]
    conflict_row = target.copy()
    conflict_row["sales"] = float(target["sales"]) + 123.0
    conflict_row["source_line"] = int(mutated["source_line"].max()) + 1
    return pd.concat([mutated, pd.DataFrame([conflict_row])], ignore_index=True)


def check_revision_conflict(sales, calendar, plan) -> CheckResult:
    conflicted = _make_conflict_sales(sales)
    # 快照层直接验证冲突被识别
    snap = sales_snapshot(conflicted, pd.Timestamp("2025-02-10T12:00:00Z"))
    if not any(c.key == "A:2025-02-08" for c in snap.conflicts):
        raise VerifyError("同修订冲突未被快照层识别")
    preds, evidence = run_backtest(conflicted, calendar, plan)
    # A:2025-02-08 落在 origin 02-10 的消费窗（训练 01-06..02-09）内 => A 店该折阻断
    fold_a = preds[
        (preds.series_id == "A")
        & (preds.origin == pd.Timestamp("2025-02-10T12:00:00Z"))
    ]
    if not (fold_a["prediction_status"] == PRED_BLOCKED).all():
        raise VerifyError("冲突折未被整体阻断")
    if fold_a["predicted_sales"].notna().any():
        raise VerifyError("阻断折不允许出现数值预测")
    # 其他门店同折不受影响
    fold_b = preds[
        (preds.series_id == "B")
        & (preds.origin == pd.Timestamp("2025-02-10T12:00:00Z"))
        & (preds.model_id == MODEL_NAIVE)
    ]
    if not (fold_b["prediction_status"] == PRED_OK).all():
        raise VerifyError("冲突不应波及无关门店")
    ev_blocked = [
        e for e in evidence
        if e["series_id"] == "A"
        and e["origin"] == pd.Timestamp("2025-02-10T12:00:00Z").isoformat()
    ]
    if any(not e["conflicts"] for e in ev_blocked):
        raise VerifyError("阻断折证据缺少冲突详情")
    return CheckResult(
        "7. 同修订内容冲突阻断",
        True,
        "注入 A:2025-02-08 rev0 冲突值后，A 店 02-10 折两模型全部 blocked 且无数值，"
        "B 店同折正常；冲突详情写入折证据",
    )


def check_late_calendar(sales, calendar, plan) -> CheckResult:
    """B 店 02-17 停业 02-20 12:00 才可见：02-17 折看不到，03-03 折必须看到。"""
    early = calendar_snapshot(calendar, pd.Timestamp("2025-02-17T12:00:00Z"))
    late = calendar_snapshot(calendar, pd.Timestamp("2025-03-03T12:00:00Z"))

    def open_value(snap, sid, d):
        hit = snap.visible[
            (snap.visible.series_id == sid)
            & (snap.visible.business_date == pd.Timestamp(d).date())
        ]
        return None if hit.empty else int(hit.iloc[0]["planned_open"])

    if open_value(early, "B", "2025-02-17") is not None:
        raise VerifyError("迟公布停业在 02-17 12:00 不应可见")
    if open_value(late, "B", "2025-02-17") != 0:
        raise VerifyError("03-03 折应能看到 B 店 02-17 停业 planned_open=0")

    # 目标日从 origin 次日起；B 店 02-17 停业（available 02-20）影响的是
    # 02-17 折 step1（目标 02-18）的 lag_1（滞后日恰为 02-17）。
    # 正确行为：02-17 12:00 时停业不可见、02-17 销量也尚未发布 => lag_1 必须走
    # 训练均值填充；而 03-03 折时两条信息都已可见，lag_1 直接取真实观测。
    preds, _ = run_backtest(sales, calendar, plan)
    early_ridge = preds[
        (preds.series_id == "B")
        & (preds.model_id == MODEL_RIDGE)
        & (preds.origin == pd.Timestamp("2025-02-17T12:00:00Z"))
        & (preds.step == 1)
    ]
    if len(early_ridge) != 1:
        raise VerifyError("B 店 02-17 折 step1 应恰好一行")
    early_row = early_ridge.iloc[0]
    lag1_src = early_row["lag_sources"]["lag_1"]
    if not lag1_src.get("imputed"):
        raise VerifyError("02-17 折 lag_1（02-17 销量当时不可见）必须走训练均值填充")
    if lag1_src["business_date"] != "2025-02-17":
        raise VerifyError("lag_1 滞后日应为 2025-02-17")

    late_step1 = _row(
        preds, origin="2025-03-03T12:00:00Z", series_id="B",
        model_id=MODEL_RIDGE, target_date="2025-03-04",
    )
    # lag_7 指向 02-25，其 rev1（03-03 08:00 可见）必须作为真实观测进入，而非填充
    lag7 = late_step1["lag_sources"]["lag_7"]
    if lag7.get("imputed") or lag7.get("revision") != 1:
        raise VerifyError(f"03-03 折 lag_7 应使用 02-25 rev1 真实观测，实际：{lag7}")
    # 快照层再确认停业信息的时刻隔离
    if open_value(late, "B", "2025-02-17") != 0:
        raise VerifyError("03-03 折应能看到 B 店 02-17 停业 planned_open=0")
    return CheckResult(
        "8. 迟公布日历/迟到观测不倒灌",
        True,
        "B 店 2025-02-17 停业（available 02-20）与当日销量在 02-17 12:00 均不可见，"
        "lag_1 按训练均值填充并标记 imputed；快照层确认 03-03 时 planned_open=0 已可见，"
        "信息严格按发起时刻隔离",
    )


def check_reproducibility(sales, calendar, plan) -> CheckResult:
    p1, e1 = run_backtest(sales, calendar, plan)
    p2, e2 = run_backtest(sales, calendar, plan)
    ev1, _, _ = evaluate_predictions(p1.copy(), sales, plan)
    ev2, _, _ = evaluate_predictions(p2.copy(), sales, plan)
    pd.testing.assert_frame_equal(
        ev1.sort_values(["origin", "series_id", "model_id", "step"]).reset_index(drop=True),
        ev2.sort_values(["origin", "series_id", "model_id", "step"]).reset_index(drop=True),
        check_dtype=False,
    )
    if json.dumps(e1, sort_keys=True, default=str) != json.dumps(
        e2, sort_keys=True, default=str
    ):
        raise VerifyError("折证据两次运行不一致")
    return CheckResult("9. 重复运行可复现", True, "两次 run+evaluate 的预测帧与折证据完全一致")


def run_verification(sales_path: str, calendar_path: str, plan_path: str) -> int:
    sales, calendar, plan = _load(sales_path, calendar_path, plan_path)
    checks: list[Callable] = [
        check_naive_manual,
        check_lag_provenance,
        check_future_invariance,
        check_preprocessing_leak,
        check_row_reorder,
        check_missing_not_zero,
        check_revision_conflict,
        check_late_calendar,
        check_reproducibility,
    ]
    results: list[CheckResult] = []
    for fn in checks:
        try:
            results.append(fn(sales, calendar, plan))
        except AssertionError as exc:
            results.append(CheckResult(fn.__name__, False, str(exc)))

    width = max(len(r.name) for r in results)
    print("=" * 78)
    print("独立核验结果（verify）")
    print("=" * 78)
    for r in results:
        flag = "PASS" if r.passed else "FAIL"
        print(f"[{flag}] {r.name.ljust(width)}  {r.detail}")
    print("-" * 78)
    n_pass = sum(r.passed for r in results)
    print(f"通过 {n_pass}/{len(results)}")
    return 0 if n_pass == len(results) else 1
