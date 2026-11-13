"""Variant-specific values for the synthetic report site.

Both variants expose the *same semantics*:
  form names, control names and visible option labels are identical.
They differ in:
  * action strings and page locations (all relative URL forms exercise
    different resolution rules),
  * hidden-field names and ordering,
  * every select/radio/checkbox machine value,
  * submit-button names and values,
  * extra unrelated forms/controls.
The helper is expected to resolve all of these from the HTML at run time.
"""

from __future__ import annotations

from .domain import MONTHS, ORGS, TYPES

SEMANTIC_FORM_NAMES = {
    "login": "login",
    "org": "select_org",
    "params": "report_params",
    "download": "download",
}

SECTION_LABELS = ["显示摘要", "显示备注"]
SECTION_DISABLED_LABEL = "旧版标签（不可用）"


def variant_info(variant: str) -> dict:
    if variant not in ("a", "b"):
        raise ValueError(f"unknown variant: {variant}")
    is_b = variant == "b"
    if is_b:
        return {
            "variant": variant,
            "is_b": True,
            "csrf_field": "csrf_guard",
            "submit_name": "op",
            "login_page": "/portal/index.cgi",
            "login_action": "auth",           # relative -> /portal/auth
            "org_page": "/portal/org",
            "org_action": "org",              # relative -> /portal/org
            "params_page": "/portal/setup",
            "params_action": "setup",         # relative -> /portal/setup
            "download_action": "/portal/dl",  # root-absolute
            "month_value": lambda y, m: f"{y}/{m:02d}",
            "type_value": lambda i: f"kind/{100 + i}",
            "org_value": lambda i: f"tenant/{i}",
            "section_value": lambda i: f"cb-{21 + i}",
            "submit_value": {
                "login": "auth-77", "org": "next-22",
                "params": "preview-33", "download": "dl-44",
            },
        }
    return {
        "variant": variant,
        "is_b": False,
        "csrf_field": "_csrf",
        "submit_name": "submitter",
        "login_page": "/login",
        "login_action": "login",
        "org_page": "/select-org",
        "org_action": "select-org",
        "params_page": "/params",
        "params_action": "params",
        "download_action": "download",
        "month_value": lambda y, m: f"per-{y}-{m:02d}",
        "type_value": lambda i: f"rt-{900 + i}",
        "org_value": lambda i: f"org-{i}",
        "section_value": lambda i: f"sec-{i}",
        "submit_value": {
            "login": "do-login", "org": "do-org",
            "params": "do-preview", "download": "do-download",
        },
    }


def resolved_actions(variant: str) -> dict:
    """Resolve the relative/absolute action strings the way a browser would."""
    import urllib.parse

    info = variant_info(variant)
    pages = {"login": info["login_page"], "org": info["org_page"],
             "params": info["params_page"], "download": info["params_page"]}
    out = {}
    for key, rel in (("login", info["login_action"]), ("org", info["org_action"]),
                     ("params", info["params_action"]),
                     ("download", info["download_action"])):
        out[key] = urllib.parse.urljoin(pages[key], rel).split("?")[0]
    return out


def org_options(variant: str) -> list[tuple[str, str, str]]:
    info = variant_info(variant)
    return [(info["org_value"](i), label, key) for i, (key, label) in enumerate(ORGS)]


def type_options(variant: str) -> list[tuple[str, str, str]]:
    info = variant_info(variant)
    return [(info["type_value"](i), label, key) for i, (label, key) in enumerate(
        [(lbl, key) for key, lbl in TYPES])]


def month_options(variant: str) -> list[tuple[str, str, str]]:
    info = variant_info(variant)
    out = []
    for key, label in MONTHS:
        y, m = int(key[:4]), int(key[5:])
        out.append((info["month_value"](y, m), label, key))
    return out


def section_options(variant: str) -> list[tuple[str, str, bool]]:
    info = variant_info(variant)
    return [
        (info["section_value"](0), SECTION_LABELS[0], False),
        (info["section_value"](1), SECTION_LABELS[1], False),
        (info["section_value"](2), SECTION_DISABLED_LABEL, True),
    ]


def lookup(values: list[tuple], machine: str):
    for item in values:
        if item[0] == machine:
            return item
    return None
