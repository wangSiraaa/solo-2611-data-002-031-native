"""Task configuration loading, validation and derived scope."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .urls import USER_AGENT, Scope, URLIdentityError, parse_identity


class ConfigError(ValueError):
    pass


@dataclass
class OriginConfig:
    origin: str
    path_prefixes: list[str]
    seeds: list[str]
    concurrency: int
    min_interval: float


@dataclass
class Limits:
    max_depth: int = 4
    page_budget: int = 500
    max_redirects: int = 5
    per_origin_concurrency: int = 2
    min_request_interval: float = 0.25
    max_body_bytes: int = 262_144
    timeout_seconds: float = 10.0
    max_attempts: int = 3
    retry_backoff_seconds: list[float] = field(default_factory=lambda: [0.5, 2.0, 5.0])
    robots_max_bytes: int = 524_288
    robots_max_attempts: int = 3
    metrics_every: int = 25
    max_runtime_seconds: float | None = None


@dataclass
class TaskConfig:
    path: Path
    database: Path
    origins: list[OriginConfig]
    limits: Limits
    user_agent: str
    scope: Scope
    raw: dict[str, Any]

    @property
    def origin_values(self) -> list[str]:
        return [o.origin for o in self.origins]

    @property
    def scope_origins(self) -> set[str]:
        return {o.origin for o in self.origins}

    def origin_config(self, origin: str) -> OriginConfig:
        for o in self.origins:
            if o.origin == origin:
                return o
        raise KeyError(origin)

    def effective_concurrency(self, origin: str) -> int:
        return self.origin_config(origin).concurrency

    def effective_interval(self, origin: str) -> float:
        return self.origin_config(origin).min_interval

    def all_seeds(self) -> list[str]:
        result: list[str] = []
        for o in self.origins:
            result.extend(o.seeds)
        return result


def _origin_of(url: str) -> str:
    return parse_identity(url).origin


def _normalise_prefix(prefix: str) -> str:
    if not prefix.startswith("/"):
        raise ConfigError(f"path prefix must start with '/': {prefix!r}")
    if "?" in prefix or "#" in prefix:
        raise ConfigError(f"path prefix must not contain query/fragment: {prefix!r}")
    return prefix


def load_config(path: str | Path) -> TaskConfig:
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config is not valid JSON: {exc}") from exc

    limits_data = data.get("limits", {})
    defaults = Limits()
    try:
        limits = Limits(
            max_depth=int(limits_data.get("max_depth", defaults.max_depth)),
            page_budget=int(limits_data.get("page_budget", defaults.page_budget)),
            max_redirects=int(limits_data.get("max_redirects", defaults.max_redirects)),
            per_origin_concurrency=int(
                limits_data.get("per_origin_concurrency", defaults.per_origin_concurrency)
            ),
            min_request_interval=float(
                limits_data.get("min_request_interval", defaults.min_request_interval)
            ),
            max_body_bytes=int(limits_data.get("max_body_bytes", defaults.max_body_bytes)),
            timeout_seconds=float(
                limits_data.get("timeout_seconds", defaults.timeout_seconds)
            ),
            max_attempts=int(limits_data.get("max_attempts", defaults.max_attempts)),
            retry_backoff_seconds=[
                float(v) for v in limits_data.get(
                    "retry_backoff_seconds", defaults.retry_backoff_seconds
                )
            ],
            robots_max_bytes=int(
                limits_data.get("robots_max_bytes", defaults.robots_max_bytes)
            ),
            robots_max_attempts=int(
                limits_data.get("robots_max_attempts", defaults.robots_max_attempts)
            ),
            metrics_every=int(limits_data.get("metrics_every", defaults.metrics_every)),
            max_runtime_seconds=(
                float(limits_data["max_runtime_seconds"])
                if limits_data.get("max_runtime_seconds") is not None
                else None
            ),
        )
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"invalid limits: {exc}") from exc

    if limits.max_depth < 0:
        raise ConfigError("limits.max_depth must be >= 0")
    if limits.page_budget < 1:
        raise ConfigError("limits.page_budget must be >= 1")
    if limits.max_redirects < 0:
        raise ConfigError("limits.max_redirects must be >= 0")
    if limits.per_origin_concurrency < 1:
        raise ConfigError("limits.per_origin_concurrency must be >= 1")
    if limits.min_request_interval < 0:
        raise ConfigError("limits.min_request_interval must be >= 0")
    if limits.max_body_bytes < 1:
        raise ConfigError("limits.max_body_bytes must be >= 1")
    if limits.timeout_seconds <= 0:
        raise ConfigError("limits.timeout_seconds must be > 0")
    if limits.max_attempts < 1:
        raise ConfigError("limits.max_attempts must be >= 1")
    if not limits.retry_backoff_seconds:
        limits.retry_backoff_seconds = [0.5]

    database = Path(data.get("database", ".runtime/crawl.db"))
    if not database.is_absolute():
        database = (path.parent / database).resolve()

    user_agent = str(data.get("user_agent") or USER_AGENT).strip()
    if not user_agent:
        raise ConfigError("user_agent must not be empty")
    if "\n" in user_agent or "\r" in user_agent:
        raise ConfigError("user_agent must be a single line")

    origin_entries = data.get("origins")
    if not isinstance(origin_entries, list) or not origin_entries:
        raise ConfigError("origins must be a non-empty list")
    seen: set[str] = set()
    origins: list[OriginConfig] = []
    scope_entries: list[tuple[str, str]] = []
    for item in origin_entries:
        if not isinstance(item, dict):
            raise ConfigError("each origin entry must be an object")
        origin = str(item.get("origin", "")).rstrip("/")
        if not origin:
            raise ConfigError("origin entry needs 'origin'")
        try:
            ident = parse_identity(origin + "/")
        except URLIdentityError as exc:
            raise ConfigError(f"invalid origin {origin!r}: {exc}") from exc
        if ident.origin != origin:
            raise ConfigError(
                f"origin {origin!r} is not in canonical form; use {ident.origin!r}"
            )
        if origin in seen:
            raise ConfigError(f"duplicate origin {origin!r}")
        seen.add(origin)
        prefixes = item.get("path_prefixes", ["/"])
        if not isinstance(prefixes, list) or not prefixes:
            raise ConfigError(f"path_prefixes for {origin} must be a non-empty list")
        normalised = [_normalise_prefix(str(p)) for p in prefixes]
        seeds_raw = item.get("seeds", [])
        if not isinstance(seeds_raw, list):
            raise ConfigError(f"seeds for {origin} must be a list")
        seeds: list[str] = []
        for seed in seeds_raw:
            seed = str(seed)
            absolute = seed if "://" in seed else origin + seed
            try:
                seed_ident = parse_identity(absolute)
            except URLIdentityError as exc:
                raise ConfigError(f"invalid seed {seed!r}: {exc}") from exc
            if seed_ident.origin != origin:
                raise ConfigError(
                    f"seed {seed!r} is on {seed_ident.origin}, not {origin}"
                )
            if not any(seed_ident.path.startswith(p) for p in normalised):
                raise ConfigError(
                    f"seed {seed!r} is outside the path prefixes of {origin}"
                )
            seeds.append(absolute.split("#", 1)[0])
        concurrency = int(item.get("concurrency", limits.per_origin_concurrency))
        if concurrency < 1:
            raise ConfigError(f"concurrency for {origin} must be >= 1")
        interval = float(
            item.get("min_request_interval", limits.min_request_interval)
        )
        if interval < 0:
            raise ConfigError(f"min_request_interval for {origin} must be >= 0")
        origins.append(
            OriginConfig(
                origin=origin,
                path_prefixes=normalised,
                seeds=seeds,
                concurrency=concurrency,
                min_interval=interval,
            )
        )
        for prefix in normalised:
            scope_entries.append((origin, prefix))

    return TaskConfig(
        path=path,
        database=database,
        origins=origins,
        limits=limits,
        user_agent=user_agent,
        scope=Scope(scope_entries),
        raw=data,
    )
