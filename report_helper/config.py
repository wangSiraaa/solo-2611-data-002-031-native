"""Configuration loading for the report helper.

A config describes the workflow by *semantics*:
  * form name and control name for each step
  * target options by their visible label, never by machine value
  * page-shape hints (which form/heading identifies a stage)
No CSRF values, download URLs, session cookies or machine option values may
appear in config.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(ValueError):
    pass


@dataclass
class StepForms:
    login: str = "login"
    org: str = "select_org"
    params: str = "report_params"
    download: str = "download"
    username_control: str = "username"
    password_control: str = "password"
    org_control: str = "org"
    month_control: str = "month"
    type_control: str = "rtype"
    section_control: str | None = None
    section_labels: list[str] = field(default_factory=list)


@dataclass
class SiteConfig:
    base_url: str
    login_url: str = "/"
    username: str = ""
    password: str = ""
    forms: StepForms = field(default_factory=StepForms)
    retry_limit: int = 2
    timeout: float = 20.0
    request_delay: float = 0.0

    def __post_init__(self):
        self.base_url = self.base_url.rstrip("/")
        if not self.login_url:
            self.login_url = "/"


@dataclass
class ReportTask:
    name: str
    org_label: str
    month_label: str
    type_label: str


def _require(mapping: dict, key: str, where: str):
    if key not in mapping:
        raise ConfigError(f"{where}: missing required key '{key}'")
    return mapping[key]


def load_config(path: str | Path) -> SiteConfig:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ConfigError("config root must be an object")
    base = _require(raw, "base_url", "config")
    if not isinstance(base, str) or not base.startswith(("http://", "https://")):
        raise ConfigError("config.base_url must be an absolute http(s) URL")
    forms_raw = raw.get("forms", {})
    if not isinstance(forms_raw, dict):
        raise ConfigError("config.forms must be an object")
    known = set(StepForms.__dataclass_fields__)
    unknown = set(forms_raw) - known
    if unknown:
        raise ConfigError("unknown form keys: " + ", ".join(sorted(unknown)))
    forms = StepForms(**forms_raw)
    creds = raw.get("credentials", {})
    if not isinstance(creds, dict):
        raise ConfigError("config.credentials must be an object")
    cfg = SiteConfig(
        base_url=base,
        login_url=raw.get("login_url", "/"),
        username=creds.get("username", raw.get("username", "")),
        password=creds.get("password", raw.get("password", "")),
        forms=forms,
        retry_limit=int(raw.get("retry_limit", 2)),
        timeout=float(raw.get("timeout", 20.0)),
        request_delay=float(raw.get("request_delay", 0.0)),
    )
    if not cfg.username or not cfg.password:
        raise ConfigError("config.credentials.username/password are required")
    if cfg.retry_limit < 0 or cfg.retry_limit > 10:
        raise ConfigError("config.retry_limit must be between 0 and 10")
    _reject_forbidden(raw)
    return cfg


def _reject_forbidden(raw: dict) -> None:
    """Guardrail: machine values / session artifacts must not be pre-baked."""
    forbidden_keys = {"csrf", "csrf_field", "csrf_value", "cookie", "cookies",
                      "download_url", "machine_value", "machine_values",
                      "sid", "session_id", "hidden_values"}
    stack = [("root", raw)]
    while stack:
        where, node = stack.pop()
        if isinstance(node, dict):
            for k, v in node.items():
                if k in forbidden_keys:
                    raise ConfigError(
                        f"{where}: key '{k}' is not allowed in config — resolve it "
                        "from the live page instead")
                stack.append((f"{where}.{k}", v))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                stack.append((f"{where}[{i}]", v))


def load_tasks(path: str | Path) -> list[ReportTask]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    items = raw.get("tasks") if isinstance(raw, dict) else raw
    if not isinstance(items, list) or not items:
        raise ConfigError("task file must contain a non-empty 'tasks' list")
    out = []
    seen = set()
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ConfigError(f"tasks[{i}]: must be an object")
        for key in ("org_label", "month_label", "report_type"):
            if key not in item:
                raise ConfigError(f"tasks[{i}]: missing '{key}'")
        name = item.get("name") or f"task-{i+1}"
        task = ReportTask(name=name, org_label=str(item["org_label"]).strip(),
                          month_label=str(item["month_label"]).strip(),
                          type_label=str(item["report_type"]).strip())
        key = (task.org_label, task.month_label, task.type_label)
        if key in seen:
            raise ConfigError(f"tasks[{i}]: duplicate task for {key}")
        seen.add(key)
        out.append(task)
    return out
