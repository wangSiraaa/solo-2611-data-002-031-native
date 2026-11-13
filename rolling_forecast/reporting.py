"""输出层：CSV / JSON / 自包含静态 HTML + 可复现 manifest。"""
from __future__ import annotations

import hashlib
import json
import platform
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from . import __version__
from .config import Plan


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (pd.Timestamp, datetime)):
        return pd.Timestamp(obj).isoformat()
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if isinstance(obj, float) and pd.isna(obj):
        return None
    raise TypeError(f"不可序列化的类型：{type(obj)!r}")


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )


def _df_to_jsonable(df: pd.DataFrame) -> list[dict[str, Any]]:
    return json.loads(df.to_json(orient="records", force_ascii=False, date_format="iso"))


def _html_table(df: pd.DataFrame, float_digits: int = 3) -> str:
    if df.empty:
        return "<p><em>（无记录）</em></p>"
    view = df.copy()
    for col in view.select_dtypes(include=["float", "float64"]).columns:
        view[col] = view[col].map(lambda v: "" if pd.isna(v) else f"{v:.{float_digits}f}")
    return view.to_html(index=False, escape=True, border=0)


def write_outputs(
    out_dir: str,
    plan: Plan,
    sales_path: str,
    calendar_path: str,
    plan_path: str,
    predictions: pd.DataFrame,
    fold_evidence: list[dict[str, Any]],
    metrics: pd.DataFrame,
    reasons: pd.DataFrame,
) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # 1) 逐折预测（嵌套证据列以 JSON 字符串保留，CSV 仍可直接读）。
    pred_flat = predictions.copy()
    for col in ("lag_values", "lag_sources", "conflicts",
                "feature_vector_raw", "feature_vector_scaled"):
        if col in pred_flat.columns:
            pred_flat[col] = pred_flat[col].map(
                lambda v: "" if v is None or (isinstance(v, float) and pd.isna(v))
                else json.dumps(v, ensure_ascii=False, default=_json_default)
            )
    pred_path = out / "predictions.csv"
    pred_flat.to_csv(pred_path, index=False)

    # 2) 折级预处理/模型证据 JSON。
    evidence_path = out / "fold_evidence.json"
    _write_json(evidence_path, fold_evidence)

    # 3) 误差表与未评价原因。
    metrics_path = out / "metrics_by_series_step.csv"
    metrics.to_csv(metrics_path, index=False)
    reasons_path = out / "not_evaluated_reasons.csv"
    reasons.to_csv(reasons_path, index=False)

    # 4) 状态计数与计划快照。
    status_counts = (
        predictions.groupby(["model_id", "prediction_status"]).size()
        .reset_index(name="n")
    )
    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "tool_version": __version__,
        "plan": {
            "origins": [pd.Timestamp(o).isoformat() for o in plan.origins],
            "training_days": plan.training_days,
            "horizon_days": plan.horizon_days,
            "seasonal_lag": plan.seasonal_lag,
            "model": {
                "kind": plan.model.kind,
                "alpha": plan.model.alpha,
                "lags": list(plan.model.lags),
            },
            "evaluation_as_of": pd.Timestamp(plan.evaluation_as_of).isoformat(),
            "seed": plan.seed,
        },
        "n_prediction_rows": int(len(predictions)),
        "n_folds_evidence": int(len(fold_evidence)),
        "prediction_status_counts": _df_to_jsonable(status_counts),
        "n_not_evaluated_rows": int(len(reasons)),
        "metrics": _df_to_jsonable(metrics),
    }
    summary_path = out / "summary.json"
    _write_json(summary_path, summary)

    # 5) 可复现 manifest：输入指纹、环境、输出指纹。
    input_files = {
        "sales": Path(sales_path),
        "calendar": Path(calendar_path),
        "plan": Path(plan_path),
    }
    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "tool_version": __version__,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "inputs": {
            name: {
                "path": str(p.resolve()),
                "sha256": _sha256(p),
                "bytes": p.stat().st_size,
            }
            for name, p in input_files.items()
        },
        "outputs": {},
    }
    for p in (pred_path, evidence_path, metrics_path, reasons_path, summary_path):
        manifest["outputs"][p.name] = {"sha256": _sha256(p), "bytes": p.stat().st_size}
    _write_json(out / "manifest.json", manifest)

    # 6) 自包含静态 HTML（无外部资源/无 JS 依赖）。
    _write_html(out / "report.html", plan, predictions, metrics, reasons, status_counts)


def _write_html(
    path: Path,
    plan: Plan,
    predictions: pd.DataFrame,
    metrics: pd.DataFrame,
    reasons: pd.DataFrame,
    status_counts: pd.DataFrame,
) -> None:
    pred_view_cols = [
        "origin", "series_id", "model_id", "step", "target_date",
        "prediction_status", "predicted_sales", "unpredictable_reason",
        "actual_sales", "evaluation_status", "abs_error",
        "not_evaluated_reason",
    ]
    available_cols = [c for c in pred_view_cols if c in predictions.columns]
    pred_view = predictions[available_cols].copy()
    if "origin" in pred_view:
        pred_view["origin"] = pred_view["origin"].map(lambda v: pd.Timestamp(v).isoformat())

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>点-in-time 滚动预测报告</title>
<style>
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         margin: 2rem; color: #1f2933; }}
  h1, h2 {{ border-bottom: 1px solid #d3dce6; padding-bottom: .3rem; }}
  table {{ border-collapse: collapse; margin: 1rem 0; font-size: .9rem; }}
  th, td {{ border: 1px solid #c8d1dc; padding: .35rem .6rem; text-align: left; }}
  th {{ background: #f0f4f8; }}
  code {{ background: #f5f7fa; padding: .1rem .3rem; border-radius: 3px; }}
  .meta {{ background: #f8fafc; border: 1px solid #e1e7ef; padding: 1rem; border-radius: 6px; }}
</style>
</head>
<body>
<h1>点-in-time 滚动销量预测报告</h1>
<div class="meta">
<p>生成时刻（UTC）：{datetime.now(timezone.utc).isoformat()} ｜ 工具版本：{__version__}</p>
<p>发起时刻：{', '.join(pd.Timestamp(o).isoformat() for o in plan.origins)}<br>
训练窗：{plan.training_days} 天 ｜ 预测水平：{plan.horizon_days} 步 ｜ 季节滞后：{plan.seasonal_lag}<br>
模型：{plan.model.kind} alpha={plan.model.alpha} lags={list(plan.model.lags)} ｜
真值截止：{pd.Timestamp(plan.evaluation_as_of).isoformat()}</p>
</div>

<h2>1. 预测/评价状态计数</h2>
{_html_table(status_counts)}

<h2>2. 误差汇总（门店 × 模型 × 步数；未评价行不参与平均但单独计数）</h2>
{_html_table(metrics)}

<h2>3. 未评价原因清单（不允许悄悄丢样本）</h2>
{_html_table(reasons)}

<h2>4. 逐折预测明细（完整滞后/来源证据见 <code>predictions.csv</code> 与 <code>fold_evidence.json</code>）</h2>
{_html_table(pred_view)}
</body>
</html>
"""
    path.write_text(html, encoding="utf-8")
