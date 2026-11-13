"""逐折模型：季节朴素基线 + 滞后特征 Ridge 回归。

所有预处理（缺失填充、标准化）都在单个折的训练数据上估计，
禁止使用任何全局/未来统计量；多步采用递推（recursive），递推期只消费
历史观测和本折已经生成的预测值，绝不读取预测期真实销量。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler

from .status import (
    MODEL_NAIVE,
    MODEL_RIDGE,
    PRED_MODEL_FIT_FAILED,
    PRED_MODEL_INSUFFICIENT,
    PRED_NAIVE_UNAVAILABLE,
    PRED_OK,
)

MIN_TRAINING_ROWS = 7          # 少于 7 个完整监督样本不拟合
MIN_TARGET_STD = 1e-12         # 目标近乎常数时判定为不可学（而非静默产出常数模型冒充）


@dataclass
class FoldSeries:
    """一个 (origin, series) 折的训练窗序列（缺测保留 NaN，不补零）。"""

    series_id: str
    origin: pd.Timestamp
    train_start: date
    train_end: date
    target_dates: tuple[date, ...]
    sales_indexed: pd.Series                 # 日期 -> 销量（缺测为 NaN），含训练窗全部日历日
    calendar_open: dict[date, dict[str, Any]]  # 日期 -> {planned_open, available_at, source_line}
    source_by_date: dict[date, dict[str, Any]]  # 日期 -> 该最高修订来源信息
    conflicts: list                          # visibility.Conflict 列表（非空即阻断）


@dataclass
class FoldArtifact:
    """一份逐折证据：实际使用的来源、滞后、预处理参数与模型标识。"""

    series_id: str
    origin: str
    model_id: str
    status: str
    reason: str | None
    strategy: str
    train_start: str
    train_end: str
    target_dates: list[str]
    source_records: list[dict[str, Any]]
    calendar_sources: list[dict[str, Any]]
    preprocessing: dict[str, Any]
    model_params: dict[str, Any]
    conflicts: list[dict[str, Any]] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 季节朴素
# --------------------------------------------------------------------------- #
def seasonal_naive_forecast(fold: FoldSeries, seasonal_lag: int) -> tuple[list[dict[str, Any]], FoldArtifact]:
    """逐步直接取 t-seasonal_lag 的观测值；滞后缺测 => 明确不可预测，不补零。"""
    rows: list[dict[str, Any]] = []
    used_sources: list[dict[str, Any]] = []
    cal_sources: list[dict[str, Any]] = []

    for step, target in enumerate(fold.target_dates, start=1):
        lag_date = target - pd.Timedelta(days=seasonal_lag)
        lag_value = fold.sales_indexed.get(pd.Timestamp(lag_date))
        lag_available = lag_value is not None and not pd.isna(lag_value)
        lag_day = lag_date.date() if hasattr(lag_date, "date") else lag_date
        src = fold.source_by_date.get(lag_day)
        cal = fold.calendar_open.get(target)
        cal_sources.append(
            {
                "business_date": str(target),
                "planned_open": None if cal is None else cal["planned_open"],
                "available_at": None if cal is None else cal["available_at"],
                "source_line": None if cal is None else cal["source_line"],
            }
        )
        if lag_available:
            rows.append(
                {
                    "series_id": fold.series_id,
                    "target_date": target,
                    "step": step,
                    "model_id": MODEL_NAIVE,
                    "prediction_status": PRED_OK,
                    "predicted_sales": float(lag_value),
                    "unpredictable_reason": None,
                    "lag_values": {f"lag_{seasonal_lag}": float(lag_value)},
                    "lag_sources": {
                        f"lag_{seasonal_lag}": None if src is None else _source_ref(src, lag_day)
                    },
                    "calendar_open_at_target": None if cal is None else cal["planned_open"],
                }
            )
            if src is not None:
                used_sources.append(_source_ref(src, lag_day))
        else:
            rows.append(
                {
                    "series_id": fold.series_id,
                    "target_date": target,
                    "step": step,
                    "model_id": MODEL_NAIVE,
                    "prediction_status": PRED_NAIVE_UNAVAILABLE,
                    "predicted_sales": None,
                    "unpredictable_reason": (
                        f"季节滞后日 {lag_date} 在发起时刻无可见观测（缺测不等于零，拒绝猜测）"
                    ),
                    "lag_values": {f"lag_{seasonal_lag}": None},
                    "lag_sources": {f"lag_{seasonal_lag}": None},
                    "calendar_open_at_target": None if cal is None else cal["planned_open"],
                }
            )

    artifact = FoldArtifact(
        series_id=fold.series_id,
        origin=pd.Timestamp(fold.origin).isoformat(),
        model_id=MODEL_NAIVE,
        status=PRED_OK if all(r["prediction_status"] == PRED_OK for r in rows) else PRED_NAIVE_UNAVAILABLE,
        reason=None
        if all(r["prediction_status"] == PRED_OK for r in rows)
        else "至少一个目标日的季节滞后缺测，对应步骤标记为不可预测",
        strategy="seasonal_naive：每个目标日独立取同星期滞后观测，无拟合参数",
        train_start=str(fold.train_start),
        train_end=str(fold.train_end),
        target_dates=[str(d) for d in fold.target_dates],
        source_records=used_sources,
        calendar_sources=cal_sources,
        preprocessing={"imputation": "none", "scaling": "none", "estimated_on": "fold_train"},
        model_params={"seasonal_lag": seasonal_lag},
        conflicts=[],
    )
    return rows, artifact


# --------------------------------------------------------------------------- #
# 滞后特征 Ridge（真实逐折拟合 + 递推多步）
# --------------------------------------------------------------------------- #
def _build_training_frame(
    history: pd.Series,
    lags: tuple[int, ...],
    open_by_date: dict,
    target_start: date | None = None,
    target_end: date | None = None,
) -> pd.DataFrame:
    """构造监督样本；目标日限定在训练窗 [target_start, target_end]。

    序列本身可向前覆盖最大滞后天数，使训练窗早期样本能取到窗外真实滞后；
    但窗外日期只作滞后来源，不作为监督目标。滞后或目标缺失的样本自然剔除，不填目标。
    """
    records: list[dict[str, Any]] = []
    for d in history.index:
        if target_start is not None and d.date() < target_start:
            continue
        if target_end is not None and d.date() > target_end:
            continue
        y = history.loc[d]
        if pd.isna(y):
            continue
        feat: dict[str, Any] = {"target_date": d, "y": float(y)}
        usable = True
        for lag in lags:
            lag_date = d - pd.Timedelta(days=lag)
            val = history.get(pd.Timestamp(lag_date))
            if val is None or pd.isna(val):
                usable = False
                break
            feat[f"lag_{lag}"] = float(val)
        if not usable:
            continue
        cal = open_by_date.get(d.date() if hasattr(d, "date") else d)
        feat["planned_open"] = np.nan if cal is None else float(cal["planned_open"])
        records.append(feat)
    return pd.DataFrame(records)


def ridge_forecast(
    fold: FoldSeries,
    lags: tuple[int, ...],
    alpha: float,
) -> tuple[list[dict[str, Any]], FoldArtifact]:
    feature_names = [f"lag_{lag}" for lag in lags] + ["planned_open"]
    base_artifact = dict(
        series_id=fold.series_id,
        origin=pd.Timestamp(fold.origin).isoformat(),
        model_id=MODEL_RIDGE,
        train_start=str(fold.train_start),
        train_end=str(fold.train_end),
        target_dates=[str(d) for d in fold.target_dates],
        source_records=[
            _source_ref(src, d) for d, src in sorted(fold.source_by_date.items())
        ],
        calendar_sources=[
            {
                "business_date": str(d),
                "planned_open": cal["planned_open"],
                "available_at": cal["available_at"],
                "source_line": cal["source_line"],
            }
            for d, cal in sorted(fold.calendar_open.items())
        ],
        conflicts=[],
    )

    def _fail(status: str, reason: str, prep: dict[str, Any], params: dict[str, Any]) -> tuple[list[dict[str, Any]], FoldArtifact]:
        rows = [
            {
                "series_id": fold.series_id,
                "target_date": target,
                "step": step,
                "model_id": MODEL_RIDGE,
                "prediction_status": status,
                "predicted_sales": None,
                "unpredictable_reason": reason,
                "lag_values": {name: None for name in feature_names if name.startswith("lag_")},
                "lag_sources": {name: None for name in feature_names if name.startswith("lag_")},
                "calendar_open_at_target": (
                    None if fold.calendar_open.get(target) is None
                    else fold.calendar_open[target]["planned_open"]
                ),
            }
            for step, target in enumerate(fold.target_dates, start=1)
        ]
        artifact = FoldArtifact(
            status=status,
            reason=reason,
            strategy="recursive（拟合失败，未进入递推）",
            preprocessing=prep,
            model_params=params,
            **base_artifact,
        )
        return rows, artifact

    frame = _build_training_frame(
        fold.sales_indexed,
        lags,
        fold.calendar_open,
        target_start=fold.train_start,
        target_end=fold.train_end,
    )
    if len(frame) < MIN_TRAINING_ROWS:
        return _fail(
            PRED_MODEL_INSUFFICIENT,
            f"完整监督样本仅 {len(frame)} 行 < 最低要求 {MIN_TRAINING_ROWS} 行，拒绝拟合",
            {"imputation": "mean_imputer", "scaling": "StandardScaler", "estimated_on": "fold_train",
             "note": "未估计：样本不足"},
            {"kind": "ridge", "alpha": alpha, "lags": list(lags), "fitted": False},
        )
    if frame["y"].std(ddof=0) <= MIN_TARGET_STD:
        return _fail(
            PRED_MODEL_INSUFFICIENT,
            "训练目标在训练窗内近乎常数，判定为不可学，明确标记而非输出常数冒充预测",
            {"imputation": "mean_imputer", "scaling": "StandardScaler", "estimated_on": "fold_train",
             "note": "未估计：目标无方差"},
            {"kind": "ridge", "alpha": alpha, "lags": list(lags), "fitted": False},
        )

    X_train = frame[feature_names].to_numpy(dtype=float)
    y_train = frame["y"].to_numpy(dtype=float)

    # 逐折估计：均值填充与标准化参数只来自本折训练数据。
    with np.errstate(invalid="ignore"):
        col_means = np.nanmean(X_train, axis=0)
    if np.isnan(col_means).any():
        all_nan_cols = [feature_names[i] for i, m in enumerate(col_means) if np.isnan(m)]
        return _fail(
            PRED_MODEL_INSUFFICIENT,
            f"特征列在整个训练窗无任何可用值：{all_nan_cols}，无法估计填充参数",
            {"imputation": "mean_imputer", "scaling": "StandardScaler", "estimated_on": "fold_train",
             "note": "未估计：特征全缺"},
            {"kind": "ridge", "alpha": alpha, "lags": list(lags), "fitted": False},
        )
    fill_values = np.where(np.isnan(col_means), 0.0, col_means)
    X_filled = np.where(np.isnan(X_train), fill_values, X_train)
    scaler = StandardScaler(with_mean=True, with_std=True)
    X_scaled = scaler.fit_transform(X_filled)  # fit 仅接触本折训练数据

    try:
        model = Ridge(alpha=alpha, random_state=None)
        model.fit(X_scaled, y_train)
    except Exception as exc:  # 拟合失败显式上报，不吞异常、不输出零
        return _fail(
            PRED_MODEL_FIT_FAILED,
            f"Ridge 拟合抛出异常：{type(exc).__name__}: {exc}",
            {
                "imputation": "mean_imputer",
                "imputer_fill_values": {n: float(v) for n, v in zip(feature_names, fill_values)},
                "scaling": "StandardScaler",
                "scaler_mean": {n: float(v) for n, v in zip(feature_names, scaler.mean_)},
                "scaler_scale": {n: float(v) for n, v in zip(feature_names, scaler.scale_)},
                "estimated_on": "fold_train",
            },
            {"kind": "ridge", "alpha": alpha, "lags": list(lags), "fitted": False},
        )

    prep = {
        "imputation": "mean_imputer（按列均值，仅用本折训练数据估计）",
        "imputer_fill_values": {n: float(v) for n, v in zip(feature_names, fill_values)},
        "scaling": "StandardScaler（仅用本折训练数据 fit）",
        "scaler_mean": {n: float(v) for n, v in zip(feature_names, scaler.mean_)},
        "scaler_scale": {n: float(v) for n, v in zip(feature_names, scaler.scale_)},
        "estimated_on": "fold_train",
        "training_rows_used": int(len(frame)),
        "training_rows_dropped_for_missing_lag_or_target": int(
            int(
                fold.sales_indexed.loc[
                    pd.Timestamp(fold.train_start):pd.Timestamp(fold.train_end)
                ].notna().sum()
            )
            - len(frame)
        ),
    }
    params = {
        "kind": "ridge",
        "alpha": alpha,
        "lags": list(lags),
        "fitted": True,
        "coefficients": {n: float(c) for n, c in zip(feature_names, model.coef_)},
        "intercept": float(model.intercept_),
        "n_features_in": int(model.n_features_in_),
    }

    # ---------------- 递推多步：滞后只来自历史观测或已生成预测 ---------------- #
    history = fold.sales_indexed.copy()  # 含训练窗全部日期；缺测为 NaN
    rows: list[dict[str, Any]] = []
    for step, target in enumerate(fold.target_dates, start=1):
        lag_values: dict[str, float | None] = {}
        lag_sources: dict[str, Any] = {}
        feat_row: dict[str, float] = {}
        for lag, name in zip(lags, [f"lag_{l}" for l in lags]):
            lag_date = target - pd.Timedelta(days=lag)
            raw = history.get(pd.Timestamp(lag_date))
            if raw is None or pd.isna(raw):
                # 仅在滞后位置缺测时用本折训练均值填充，并如实记录原始缺测。
                lag_values[name] = None
                lag_sources[name] = {
                    "business_date": str(lag_date),
                    "record_id": None,
                    "revision": None,
                    "source_line": None,
                    "imputed": True,
                    "impute_fill": float(fill_values[feature_names.index(name)]),
                }
                feat_row[name] = float(fill_values[feature_names.index(name)])
            else:
                val = float(raw)
                lag_values[name] = val
                # source_by_date 以 datetime.date 为键（与 backtest 构造保持一致）
                src = fold.source_by_date.get(
                    lag_date.date() if hasattr(lag_date, "date") else lag_date
                )
                if src is not None:
                    ref = _source_ref(src, lag_date)
                else:
                    # 来自递推期内本模型已生成的预测（不是真实销量）。
                    ref = {
                        "business_date": str(lag_date),
                        "record_id": None,
                        "revision": None,
                        "source_line": None,
                        "imputed": False,
                        "origin": "recursive_forecast",
                    }
                ref["value_used"] = val
                lag_sources[name] = ref
                feat_row[name] = val

        cal = fold.calendar_open.get(target)
        if cal is None:
            open_val = float(fill_values[feature_names.index("planned_open")])
            open_imputed = True
        else:
            open_val = float(cal["planned_open"])
            open_imputed = False
        feat_row["planned_open"] = open_val

        x_raw = np.array([[feat_row[n] for n in feature_names]], dtype=float)
        x_scaled = scaler.transform(x_raw)
        yhat = float(model.predict(x_scaled)[0])

        rows.append(
            {
                "series_id": fold.series_id,
                "target_date": target,
                "step": step,
                "model_id": MODEL_RIDGE,
                "prediction_status": PRED_OK,
                "predicted_sales": yhat,
                "unpredictable_reason": None,
                "lag_values": lag_values,
                "lag_sources": lag_sources,
                "calendar_open_at_target": None if cal is None else cal["planned_open"],
                "calendar_open_imputed": open_imputed,
                "feature_vector_raw": {n: float(feat_row[n]) for n in feature_names},
                "feature_vector_scaled": {
                    n: float(v) for n, v in zip(feature_names, x_scaled[0])
                },
            }
        )
        # 递推：把本步预测写回历史，后续步骤的滞后可能引用它；真实销量永不参与。
        history[pd.Timestamp(target)] = yhat

    artifact = FoldArtifact(
        status=PRED_OK,
        reason=None,
        strategy=(
            "recursive：每步用同一 Ridge 与同一逐折预处理器，滞后缺测按训练均值填充；"
            "预测值写回历史供后续滞后使用，预测期真实销量零接触"
        ),
        preprocessing=prep,
        model_params=params,
        **base_artifact,
    )
    return rows, artifact


def _source_ref(src: dict[str, Any], lag_date: date) -> dict[str, Any]:
    return {
        "business_date": str(lag_date),
        "record_id": src.get("record_id"),
        "revision": None if src.get("revision") is None else int(src["revision"]),
        "source_line": None if src.get("source_line") is None else int(src["source_line"]),
        "available_at": src.get("available_at"),
        "sales_value": None if src.get("sales") is None or pd.isna(src.get("sales")) else float(src["sales"]),
        "imputed": False,
    }
