"""评价层：真值截止时刻与预测时可见范围严格分开。

- 真值只取 evaluation_as_of 时刻可见的最高修订；此时仍不可见或存在同修订冲突
  的目标日，明确列为"未评价原因"，绝不悄悄剔除后只报平均分；
- 没有成功数值预测的行同样保留并归类；
- 误差按 门店 × 模型 × 步数 汇总，并给出门店级与总体计数。
- 评价列直接合并回逐折预测帧，滞后/来源证据列原样保留。
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .config import Plan
from .status import (
    EVAL_DONE,
    EVAL_SKIPPED_PREDICTION,
    EVAL_SKIPPED_TRUTH_CONFLICT,
    EVAL_SKIPPED_TRUTH_MISSING,
    PRED_OK,
)
from .visibility import sales_snapshot


def evaluate_predictions(
    predictions: pd.DataFrame,
    sales: pd.DataFrame,
    plan: Plan,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    eval_ts = pd.Timestamp(plan.evaluation_as_of)
    truth_snap = sales_snapshot(sales, eval_ts)
    tv = truth_snap.visible

    truth_by_key: dict[tuple[str, Any], dict[str, Any]] = {
        (row.series_id, row.business_date): {
            "sales": float(row.sales),
            "record_id": row.record_id,
            "revision": int(row.revision),
            "source_line": int(row.source_line),
            "available_at": pd.Timestamp(row.available_at).isoformat(),
        }
        for row in tv.itertuples()
    }
    conflict_keys: set[tuple[str, Any]] = set()
    for c in truth_snap.conflicts:
        sid, _, dstr = c.key.partition(":")
        conflict_keys.add((sid, pd.Timestamp(dstr).date()))

    eval_columns = {
        "actual_sales": [],
        "abs_error": [],
        "squared_error": [],
        "absolute_percent_error": [],
        "evaluation_status": [],
        "not_evaluated_reason": [],
        "truth_record_id": [],
        "truth_revision": [],
        "truth_source_line": [],
        "truth_available_at": [],
    }

    for r in predictions.itertuples(index=False):
        key = (r.series_id, r.target_date)
        vals = {k: None for k in eval_columns}

        if r.prediction_status != PRED_OK:
            vals["evaluation_status"] = EVAL_SKIPPED_PREDICTION
            vals["not_evaluated_reason"] = (
                f"预测侧无数值结果（{r.prediction_status}），不参与误差平均"
            )
        elif key in conflict_keys:
            vals["evaluation_status"] = EVAL_SKIPPED_TRUTH_CONFLICT
            vals["not_evaluated_reason"] = "真值在截止时刻存在同修订冲突，无法确定唯一真值"
        elif key not in truth_by_key:
            vals["evaluation_status"] = EVAL_SKIPPED_TRUTH_MISSING
            vals["not_evaluated_reason"] = (
                "目标日在真值截止时刻仍无可见观测（缺测不等于零，不按零评价）"
            )
        else:
            truth = truth_by_key[key]
            yhat = float(r.predicted_sales)
            y = float(truth["sales"])
            err = yhat - y
            vals.update(
                actual_sales=y,
                abs_error=abs(err),
                squared_error=err * err,
                absolute_percent_error=(abs(err) / abs(y) if y != 0 else np.nan),
                evaluation_status=EVAL_DONE,
                truth_record_id=truth["record_id"],
                truth_revision=truth["revision"],
                truth_source_line=truth["source_line"],
                truth_available_at=truth["available_at"],
            )
        for k in eval_columns:
            eval_columns[k].append(vals[k])

    evaluated = predictions.copy()
    for col, values in eval_columns.items():
        evaluated[col] = values

    # ---------------- 汇总：门店 × 模型 × 步数，不丢任何未评价行 ---------------- #
    done = evaluated[evaluated["evaluation_status"] == EVAL_DONE].copy()
    group_cols = ["series_id", "model_id", "step"]
    if done.empty:
        metrics = pd.DataFrame(
            columns=group_cols
            + ["n_evaluated", "mae", "rmse", "mape", "n_not_evaluated"]
        )
    else:
        agg = (
            done.groupby(group_cols, as_index=False)
            .agg(
                n_evaluated=("abs_error", "size"),
                mae=("abs_error", "mean"),
                rmse=("squared_error", lambda s: float(np.sqrt(np.mean(s)))),
                mape=("absolute_percent_error", "mean"),
            )
        )
        not_eval_counts = (
            evaluated[evaluated["evaluation_status"] != EVAL_DONE]
            .groupby(group_cols)
            .size()
            .rename("n_not_evaluated")
            .reset_index()
        )
        metrics = agg.merge(not_eval_counts, on=group_cols, how="left")
        metrics["n_not_evaluated"] = metrics["n_not_evaluated"].fillna(0).astype(int)

    # 未评价原因清单（逐行，可追溯到具体 origin/series/step）。
    reasons = (
        evaluated[evaluated["evaluation_status"] != EVAL_DONE][
            [
                "origin",
                "series_id",
                "model_id",
                "step",
                "target_date",
                "evaluation_status",
                "not_evaluated_reason",
                "prediction_status",
            ]
        ]
        .copy()
    )
    reasons["origin"] = reasons["origin"].map(lambda v: pd.Timestamp(v).isoformat())
    reasons = reasons.sort_values(["series_id", "model_id", "origin", "step"]).reset_index(drop=True)
    return evaluated, metrics.sort_values(group_cols).reset_index(drop=True), reasons
