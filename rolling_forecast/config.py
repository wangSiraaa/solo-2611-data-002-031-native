"""plan.json 的加载与校验：发起时刻、窗口、固定模型参数、评价真值截止。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _parse_ts(value: str, field: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"{field} 必须带时区（期望 UTC ISO8601）：{value!r}")
    return dt.astimezone(timezone.utc)


@dataclass(frozen=True)
class ModelSpec:
    """计划中固定的模型参数，运行期不允许搜索/调参。"""

    kind: str
    alpha: float
    lags: tuple[int, ...]


@dataclass(frozen=True)
class Plan:
    origins: tuple[datetime, ...]
    training_days: int
    horizon_days: int
    seasonal_lag: int
    model: ModelSpec
    evaluation_as_of: datetime
    seed: int
    target_date_rule: str

    @property
    def origin_dates(self) -> tuple[Any, ...]:
        """发起时刻对应的 UTC 日期（目标日为其后的下一个自然日起）。"""
        return tuple(o.date() for o in self.origins)


def load_plan(path: str | Path) -> Plan:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    required = [
        "origins",
        "training_days",
        "horizon_days",
        "seasonal_lag",
        "model",
        "evaluation_as_of",
        "seed",
        "target_date_rule",
    ]
    missing = [k for k in required if k not in raw]
    if missing:
        raise ValueError(f"plan.json 缺少字段：{missing}")

    origins = tuple(_parse_ts(o, "origins") for o in raw["origins"])
    if not origins:
        raise ValueError("plan.json 的 origins 不能为空")
    if any(origins[i] >= origins[i + 1] for i in range(len(origins) - 1)):
        raise ValueError("origins 必须严格升序")

    for key in ("training_days", "horizon_days", "seasonal_lag"):
        if not isinstance(raw[key], int) or raw[key] <= 0:
            raise ValueError(f"{key} 必须为正整数，实际：{raw[key]!r}")

    model_raw = raw["model"]
    if model_raw.get("kind") != "ridge":
        raise ValueError(f"仅支持 plan 指定的 ridge 模型，收到：{model_raw.get('kind')!r}")
    lags = tuple(model_raw.get("lags", []))
    if not lags or any((not isinstance(x, int)) or x <= 0 for x in lags):
        raise ValueError(f"model.lags 必须为正整数列表，实际：{lags!r}")
    if not isinstance(model_raw.get("alpha"), (int, float)) or model_raw["alpha"] < 0:
        raise ValueError("model.alpha 必须为非负数")

    return Plan(
        origins=origins,
        training_days=int(raw["training_days"]),
        horizon_days=int(raw["horizon_days"]),
        seasonal_lag=int(raw["seasonal_lag"]),
        model=ModelSpec(
            kind="ridge",
            alpha=float(model_raw["alpha"]),
            lags=lags,
        ),
        evaluation_as_of=_parse_ts(raw["evaluation_as_of"], "evaluation_as_of"),
        seed=int(raw["seed"]),
        target_date_rule=str(raw["target_date_rule"]),
    )
