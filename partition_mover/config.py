"""Loading and validation of the migration target configuration (target.json)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .bounds import month_range
from .errors import ConfigError

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

_REQUIRED_KEYS = {"table", "month", "timezone", "new_partition"}
_OPTIONAL_KEYS = {"lock_timeout_ms", "statement_timeout_ms", "max_attempts", "retry_backoff_ms"}


def _split_qname(value, field: str) -> tuple[str, str]:
    if not isinstance(value, str):
        raise ConfigError(f"{field}: expected a string, got {value!r}")
    parts = value.split(".")
    if len(parts) != 2 or not all(_IDENT_RE.match(p) for p in parts):
        raise ConfigError(
            f"{field}: expected a schema-qualified identifier 'schema.name' "
            f"with simple identifier parts, got {value!r}"
        )
    return parts[0], parts[1]


def _positive_int(data: dict, key: str, default: int) -> int:
    value = data.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{key}: expected a positive integer, got {value!r}")
    return value


@dataclass(frozen=True)
class Config:
    table_schema: str
    table_name: str
    month: str                       # 'YYYY-MM'
    new_schema: str
    new_name: str
    lock_timeout_ms: int = 500       # max wait for the migration lock, per attempt
    statement_timeout_ms: int = 30_000  # safety cap per migration statement
    max_attempts: int = 3            # bounded retries on lock contention
    retry_backoff_ms: int = 250      # linear backoff base between attempts

    @property
    def table_qname(self) -> str:
        return f"{self.table_schema}.{self.table_name}"

    @property
    def new_qname(self) -> str:
        return f"{self.new_schema}.{self.new_name}"

    @property
    def month_range(self):
        return month_range(self.month)


def load_config(path: str | Path) -> Config:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("config must be a JSON object")

    unknown = set(data) - _REQUIRED_KEYS - _OPTIONAL_KEYS
    if unknown:
        raise ConfigError(f"unknown config keys: {sorted(unknown)}")
    missing = _REQUIRED_KEYS - set(data)
    if missing:
        raise ConfigError(f"missing config keys: {sorted(missing)}")

    table_schema, table_name = _split_qname(data["table"], "table")
    new_schema, new_name = _split_qname(data["new_partition"], "new_partition")

    timezone = data["timezone"]
    if not isinstance(timezone, str) or timezone.upper() != "UTC":
        raise ConfigError(
            f"timezone: this tool only supports 'UTC' (the partition months are "
            f"defined as UTC ranges), got {timezone!r}"
        )

    month = data["month"]
    if not isinstance(month, str):
        raise ConfigError(f"month: expected a string 'YYYY-MM', got {month!r}")
    try:
        month_range(month)
    except ValueError as exc:
        raise ConfigError(f"month: {exc}") from exc

    if (table_schema, table_name) == (new_schema, new_name):
        raise ConfigError("new_partition must differ from the parent table")

    return Config(
        table_schema=table_schema,
        table_name=table_name,
        month=month,
        new_schema=new_schema,
        new_name=new_name,
        lock_timeout_ms=_positive_int(data, "lock_timeout_ms", 500),
        statement_timeout_ms=_positive_int(data, "statement_timeout_ms", 30_000),
        max_attempts=_positive_int(data, "max_attempts", 3),
        retry_backoff_ms=_positive_int(data, "retry_backoff_ms", 250),
    )
