"""CSV 加载与基础校验。

加载阶段不做任何"跨时间"假设：
- 缺失的 sales 行保持缺失（绝不补零）；
- available_at 原样解析，资格判断全部在快照阶段完成；
- 保留输入文件的原始行号，便于来源证据追溯。
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

SALES_COLUMNS = [
    "record_id",
    "series_id",
    "business_date",
    "sales",
    "revision",
    "available_at",
]
CALENDAR_COLUMNS = [
    "series_id",
    "business_date",
    "weekday",
    "planned_open",
    "available_at",
]


def _parse_ts_utc(series: pd.Series, field: str) -> pd.Series:
    parsed = pd.to_datetime(series, utc=True, errors="raise", format="ISO8601")
    if parsed.isna().any():
        raise ValueError(f"字段 {field} 存在无法解析的时间戳")
    return parsed


def load_sales(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"record_id": str, "series_id": str})
    missing = [c for c in SALES_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"sales.csv 缺少列：{missing}")
    df = df[SALES_COLUMNS].copy()
    df["source_line"] = df.index + 2  # CSV 行号（含表头）
    df["business_date"] = pd.to_datetime(df["business_date"], utc=True, errors="raise").dt.date
    df["available_at"] = _parse_ts_utc(df["available_at"], "available_at")
    df["revision"] = pd.to_numeric(df["revision"], errors="raise").astype("int64")
    df["sales"] = pd.to_numeric(df["sales"], errors="raise").astype(float)
    if df["record_id"].isna().any() or df["series_id"].isna().any():
        raise ValueError("record_id / series_id 不允许为空")
    # 同一物理行的自洽性：record_id 约定为 series:date，若不匹配属于脏数据，直接拒绝。
    rid_sid = df["record_id"].str.split(":", n=1).str[0]
    if not (rid_sid == df["series_id"]).all():
        raise ValueError("存在 record_id 前缀与 series_id 不一致的行")
    return df


def load_calendar(path: str | Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"series_id": str})
    missing = [c for c in CALENDAR_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"calendar.csv 缺少列：{missing}")
    df = df[CALENDAR_COLUMNS].copy()
    df["source_line"] = df.index + 2
    df["business_date"] = pd.to_datetime(df["business_date"], utc=True, errors="raise").dt.date
    df["available_at"] = _parse_ts_utc(df["available_at"], "available_at")
    df["weekday"] = pd.to_numeric(df["weekday"], errors="raise").astype("int64")
    df["planned_open"] = pd.to_numeric(df["planned_open"], errors="raise").astype("int64")
    if not df["planned_open"].isin([0, 1]).all():
        raise ValueError("planned_open 只允许 0/1")
    if not df["weekday"].between(0, 6).all():
        raise ValueError("weekday 必须在 0..6")
    return df


def utc_ts(value: str) -> datetime:
    """供核验脚本使用的 UTC 时间解析。"""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
