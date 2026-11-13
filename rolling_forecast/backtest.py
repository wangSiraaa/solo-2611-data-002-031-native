"""逐折编排：对每个 (origin, series) 构造点-in-time 折并运行两个模型。

折的生命周期：
  1. 按发起时刻构造 sales / calendar 可见快照（迟到数据不可见）；
  2. 计算该折真正消费的日期窗口（训练窗 + 最大滞后 + 预测目标日）；
  3. 窗口内存在同修订冲突 => 整折阻断，两个模型都只产出 blocked 行；
  4. 否则在同一份折数据上分别运行季节朴素与 Ridge，预处理逐折估计。
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date
from typing import Any

import pandas as pd

from .config import Plan
from .models import (
    FoldSeries,
    FoldArtifact,
    ridge_forecast,
    seasonal_naive_forecast,
)
from .status import MODEL_NAIVE, MODEL_RIDGE, PRED_BLOCKED
from .visibility import (
    Snapshot,
    calendar_snapshot,
    conflicts_in_window,
    sales_snapshot,
)


def _date_range(start: date, end: date) -> list[date]:
    return [d.date() for d in pd.date_range(start=start, end=end, freq="D")]


def _serialize_conflicts(conflicts: list) -> list[dict[str, Any]]:
    return [
        {
            "key": c.key,
            "revision": c.revision,
            "conflicting_values": list(c.values),
            "source_lines": list(c.source_lines),
            "available_at": list(c.available_at),
        }
        for c in conflicts
    ]


def _blocked_rows(
    series_id: str,
    target_dates: tuple[date, ...],
    model_id: str,
    conflicts: list,
) -> list[dict[str, Any]]:
    return [
        {
            "series_id": series_id,
            "target_date": target,
            "step": step,
            "model_id": model_id,
            "prediction_status": PRED_BLOCKED,
            "predicted_sales": None,
            "unpredictable_reason": "同修订内容冲突，该折按规则阻断（不任选一行、不猜测）",
            "lag_values": {},
            "lag_sources": {},
            "calendar_open_at_target": None,
            "conflicts": _serialize_conflicts(conflicts),
        }
        for step, target in enumerate(target_dates, start=1)
    ]


def _build_fold_series(
    series_id: str,
    origin: pd.Timestamp,
    plan: Plan,
    snap_sales: Snapshot,
    snap_cal: Snapshot,
    conflicts: list,
) -> FoldSeries:
    origin_date = origin.date()
    train_start = origin_date - pd.Timedelta(days=plan.training_days)
    train_end = origin_date - pd.Timedelta(days=1)
    target_dates = tuple(
        origin_date + pd.Timedelta(days=k) for k in range(1, plan.horizon_days + 1)
    )

    max_lag = max(max(plan.model.lags), plan.seasonal_lag)
    consumed_start = train_start - pd.Timedelta(days=max_lag)
    consumed_end = target_dates[-1]

    sv = snap_sales.visible
    sv_series = sv[sv["series_id"] == series_id]
    sales_map = {row.business_date: float(row.sales) for row in sv_series.itertuples()}
    # 来源证据只保留该折真正可能消费的记录（消费窗内），不把无关历史写入证据。
    source_by_date = {
        row.business_date: {
            "record_id": row.record_id,
            "revision": int(row.revision),
            "source_line": int(row.source_line),
            "available_at": pd.Timestamp(row.available_at).isoformat(),
            "sales": float(row.sales),
        }
        for row in sv_series.itertuples()
        if consumed_start <= row.business_date <= consumed_end
    }
    # 序列覆盖完整消费窗（训练窗向前扩最大滞后）：训练帧早期样本的窗外滞后可取真实
    # 观测；预测期日期不进入序列（递推时才逐步写回预测值）。缺测显式 NaN，绝不补零。
    series_dates = _date_range(consumed_start, train_end)
    sales_indexed = pd.Series(
        {pd.Timestamp(d): sales_map.get(d, float("nan")) for d in series_dates},
        name="sales",
    ).sort_index()

    cv = snap_cal.visible
    cv_series = cv[cv["series_id"] == series_id]
    calendar_open: dict[date, dict[str, Any]] = {}
    for row in cv_series.itertuples():
        if consumed_start <= row.business_date <= consumed_end:
            calendar_open[row.business_date] = {
                "planned_open": int(row.planned_open),
                "available_at": pd.Timestamp(row.available_at).isoformat(),
                "source_line": int(row.source_line),
            }

    return FoldSeries(
        series_id=series_id,
        origin=origin,
        train_start=train_start,
        train_end=train_end,
        target_dates=target_dates,
        sales_indexed=sales_indexed,
        calendar_open=calendar_open,
        source_by_date=source_by_date,
        conflicts=conflicts,
    )


def run_backtest(
    sales: pd.DataFrame,
    calendar: pd.DataFrame,
    plan: Plan,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    all_rows: list[dict[str, Any]] = []
    fold_evidence: list[dict[str, Any]] = []
    series_ids = sorted(sales["series_id"].unique().tolist())
    max_lag = max(max(plan.model.lags), plan.seasonal_lag)

    for origin in plan.origins:
        origin_ts = pd.Timestamp(origin)
        snap_sales = sales_snapshot(sales, origin_ts)
        snap_cal = calendar_snapshot(calendar, origin_ts)
        origin_date = origin_ts.date()
        train_start = origin_date - pd.Timedelta(days=plan.training_days)
        target_dates = tuple(
            origin_date + pd.Timedelta(days=k) for k in range(1, plan.horizon_days + 1)
        )
        consumed_start = train_start - pd.Timedelta(days=max_lag)
        consumed_end = target_dates[-1]

        for series_id in series_ids:
            sales_conflicts = conflicts_in_window(
                snap_sales, series_id, consumed_start, consumed_end
            )
            cal_conflicts = conflicts_in_window(
                snap_cal, series_id, consumed_start, consumed_end
            )
            conflicts = sales_conflicts + cal_conflicts

            if conflicts:
                # 整折阻断：两个模型都不给数值，只给带证据的 blocked 行。
                for model_id in (MODEL_NAIVE, MODEL_RIDGE):
                    blocked = _blocked_rows(series_id, target_dates, model_id, conflicts)
                    for r in blocked:
                        r["origin"] = origin_ts.isoformat()
                    all_rows.extend(blocked)
                    fold_evidence.append(
                        asdict(
                            FoldArtifact(
                                series_id=series_id,
                                origin=origin_ts.isoformat(),
                                model_id=model_id,
                                status=PRED_BLOCKED,
                                reason="窗口内同修订内容冲突，整折阻断",
                                strategy="未运行（阻断）",
                                train_start=str(train_start),
                                train_end=str(origin_date - pd.Timedelta(days=1)),
                                target_dates=[str(d) for d in target_dates],
                                source_records=[],
                                calendar_sources=[],
                                preprocessing={"estimated_on": "fold_train", "note": "未估计：折阻断"},
                                model_params={"fitted": False},
                                conflicts=_serialize_conflicts(conflicts),
                            )
                        )
                    )
                continue

            fold = _build_fold_series(
                series_id, origin_ts, plan, snap_sales, snap_cal, conflicts
            )

            naive_rows, naive_artifact = seasonal_naive_forecast(fold, plan.seasonal_lag)
            ridge_rows, ridge_artifact = ridge_forecast(
                fold, plan.model.lags, plan.model.alpha
            )
            for rows, artifact in (
                (naive_rows, naive_artifact),
                (ridge_rows, ridge_artifact),
            ):
                for r in rows:
                    r["origin"] = origin_ts.isoformat()
                    r["train_start"] = str(fold.train_start)
                    r["train_end"] = str(fold.train_end)
                fold_evidence.append(asdict(artifact))
            all_rows.extend(naive_rows)
            all_rows.extend(ridge_rows)

    predictions = pd.DataFrame(all_rows)
    predictions["origin"] = pd.to_datetime(predictions["origin"], utc=True)
    predictions["target_date"] = pd.to_datetime(predictions.target_date.astype(str), utc=True).dt.date
    predictions = predictions.sort_values(
        ["origin", "series_id", "model_id", "step"]
    ).reset_index(drop=True)
    return predictions, fold_evidence
