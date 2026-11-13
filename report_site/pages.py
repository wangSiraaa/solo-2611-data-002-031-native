"""Render the two page variants.

Both variants share form names, control names and option display labels.
Action strings, hidden-field names/order, machine values, submit-button
values and decorative extras differ, matching catalog.variant_info().
"""

from __future__ import annotations

from .catalog import (SEMANTIC_FORM_NAMES,
                      month_options, org_options, section_options,
                      type_options, variant_info)
from .htmlbuild import document, tag


def _hidden(fields: list[tuple[str, str]]) -> str:
    return "".join(tag("input", {"type": "hidden", "name": n, "value": v}, void=True)
                   for n, v in fields)


def _select(name: str, options: list[tuple[str, str, str]], *,
            selected: str | None = None, disabled_index: int | None = None) -> str:
    out = []
    for i, item in enumerate(options):
        machine, label = item[0], item[1]
        opts = {"value": machine}
        if machine == selected:
            opts["selected"] = "selected"
        if disabled_index is not None and i == disabled_index:
            opts["disabled"] = "disabled"
        out.append(tag("option", opts, text=label))
    return tag("select", {"name": name}, children=out)


def _checkboxes(name: str, values: list[tuple[str, str, bool]]) -> str:
    out = []
    for machine, label, disabled in values:
        opts = {"type": "checkbox", "name": name, "value": machine}
        if disabled:
            opts["disabled"] = "disabled"
        out.append(tag("label", children=[
            tag("input", opts, void=True), " " + label]))
    return "".join(out)


def _decorative_form(variant: str) -> str:
    """Unrelated form present only in variant B."""
    if variant != "b":
        return ""
    return (
        tag("form", {"name": "newsletter", "action": "misc/prefs", "method": "POST"},
            children=[tag("h3", text="站点通知（无关表单）"),
                      _hidden([("ref", "u-991"), ("nonce", "no-cache-7")]),
                      tag("input", {"type": "text", "name": "email",
                                    "value": "ops@example.invalid"}, void=True),
                      tag("input", {"type": "checkbox", "name": "weekly",
                                    "value": "y", "checked": "checked"}, void=True),
                      tag("input", {"type": "submit", "name": "newsletter_op",
                                    "value": "订阅预览"}, void=True)])
    )


def login_page(variant: str, csrf: str, *, notice: str = "",
               username: str = "", not_found_hint: bool = False) -> str:
    info = variant_info(variant)
    forms = []
    if variant == "b":
        forms.append(_decorative_form(variant))
    if variant == "b":
        extra = [("step", "auth-u"), ("mode", "legacy")]
    else:
        extra = [("forward_page", "org")]
    form = tag("form", {"name": SEMANTIC_FORM_NAMES["login"], "method": "POST",
                        "action": info["login_action"]}, children=[
        tag("h2", text="报表门户登录"),
        tag("p", {"class": "hint"}, text="演示账号 ops / demo-pass"),
        _hidden([(info["csrf_field"], csrf)] + extra),
        tag("p", children=["账号 ", tag("input", {"type": "text", "name": "username",
                                                   "value": username}, void=True)]),
        tag("p", children=["口令 ", tag("input", {"type": "password", "name": "password"},
                                        void=True)]),
        tag("input", {"type": "submit", "name": info["submit_name"],
                      "value": info["submit_value"]["login"]}, void=True),
    ])
    forms.append(form)
    parts = [tag("h1", text="遗留报表站")]
    if notice:
        parts.append(tag("p", {"class": "warn", "id": "notice"}, text=notice))
    if not_found_hint:
        parts.append(tag("p", {"class": "hint"},
                         text="原页面不可用；可在此重新登录后继续。"))
    parts.extend(forms)
    return document("登录 - 报表站", parts,
                    layout="variant_b" if variant == "b" else "default")


def org_page(variant: str, csrf: str, *, username: str = "ops") -> str:
    info = variant_info(variant)
    forms = []
    if variant == "b":
        forms.append(_decorative_form(variant))
    opts = org_options(variant)
    if variant == "b":
        before = [(info["csrf_field"], csrf), ("session_tag", "u-" + username)]
        after = [("scope", "all-visible")]
    else:
        before = [(info["csrf_field"], csrf)]
        after = []
    children = [tag("h2", text="选择机构"), _hidden(before)]
    if variant == "b":
        children.append(tag("p", {"class": "hint"}, text="会话标签由服务端附加"))
    children.append(tag("p", children=["机构 ", _select("org", opts)]))
    if variant != "b":
        children.append(tag("label", children=[
            tag("input", {"type": "text", "name": "quick_filter", "value": ""}, void=True),
            " 快速筛选（可选，不影响提交）"]))
    children.append(_hidden(after))
    children.append(tag("input", {"type": "submit", "name": info["submit_name"],
                                  "value": info["submit_value"]["org"]}, void=True))
    forms.append(tag("form", {"name": SEMANTIC_FORM_NAMES["org"], "method": "POST",
                              "action": info["org_action"]}, children=children))
    return document("选择机构", [tag("h1", text="遗留报表站"), *forms],
                    layout="variant_b" if variant == "b" else "default")


def params_page(variant: str, csrf: str, *, org_label: str,
                selected_month: str, selected_type: str,
                selected_sections: list[str] | None = None) -> str:
    info = variant_info(variant)
    months = month_options(variant)
    types = type_options(variant)
    sections = section_options(variant)
    if variant == "b":
        first = [(info["csrf_field"], csrf), ("org_echo", org_label)]
        last = [("trail", "post-org"), ("org_echo", org_label)]
    else:
        first = [(info["csrf_field"], csrf)]
        last = [("origin", "preview-request")]
    children = [
        tag("h2", text=f"报表参数 - {org_label}"),
        tag("p", {"class": "hint"}, text="月份与报表类型均以显示名为准选择"),
        _hidden(first),
        tag("p", children=["月份 ", _select("month", months, selected=selected_month)]),
    ]
    if variant == "b":
        children.append(tag("p", {"class": "hint"}, text="（机器值随环境变化）"))
    children.append(tag("p", children=["报表类型 ",
                         _select("rtype", types, selected=selected_type)]))
    if variant != "b":
        children.append(tag("p", children=["快速筛选 ",
                          tag("input", {"type": "text", "name": "quick_filter",
                                        "value": ""}, void=True)]))
    children.append(tag("fieldset", children=[
        tag("legend", text="可选栏目"),
        _checkboxes("columns", sections),
    ]))
    children.append(_hidden(last))
    children.append(tag("input", {"type": "submit", "name": info["submit_name"],
                                  "value": info["submit_value"]["params"]}, void=True))
    forms = []
    if variant == "b":
        forms.append(_decorative_form(variant))
    forms.append(tag("form", {"name": SEMANTIC_FORM_NAMES["params"], "method": "POST",
                              "action": info["params_action"]}, children=children))
    return document("报表参数", [tag("h1", text="遗留报表站"), *forms],
                    layout="variant_b" if variant == "b" else "default")


def _preview_table(columns: list[str], rows: list[dict]) -> str:
    head = tag("tr", children=[tag("th", text=c) for c in columns])
    body = "".join(
        tag("tr", children=[tag("td", text=r["code"]), tag("td", text=r["name"]),
                            tag("td", text=r["amount"])])
        for r in rows
    )
    return tag("table", {"id": "preview-table"}, children=[head, body])


def preview_page(variant: str, csrf: str, *, org_label: str, month_label: str,
                 type_label: str, columns: list[str], rows: list[dict],
                 total: str, qid: str, dl_action: str, submit_name: str,
                 submit_value: str, extra_hidden: list[tuple[str, str]] | None = None) -> str:
    info = variant_info(variant)
    forms = []
    if variant == "b":
        forms.append(_decorative_form(variant))
    meta = tag("dl", children=[
        tag("dt", text="查询标识"), tag("dd", {"id": "query-id"}, text=qid),
        tag("dt", text="机构"), tag("dd", {"id": "preview-org"}, text=org_label),
        tag("dt", text="月份"), tag("dd", {"id": "preview-month"}, text=month_label),
        tag("dt", text="报表类型"), tag("dd", {"id": "preview-type"}, text=type_label),
        tag("dt", text="列名"), tag("dd", {"id": "preview-columns"},
                                    text=", ".join(columns)),
        tag("dt", text="行数"), tag("dd", {"id": "preview-count"}, text=str(len(rows))),
        tag("dt", text="金额合计"), tag("dd", {"id": "preview-total"}, text=total),
    ])
    table = _preview_table(columns, rows)
    form = tag("form", {"name": SEMANTIC_FORM_NAMES["download"], "method": "POST",
                        "action": dl_action}, children=[
        tag("h3", text="下载"),
        _hidden([(info["csrf_field"], csrf), ("report_id", qid)] + (extra_hidden or [])),
        tag("input", {"type": "submit", "name": submit_name,
                      "value": submit_value}, void=True),
    ])
    forms.append(tag("section", {"id": "preview-summary"},
                     children=[tag("h2", text="预览结果"), meta, table]))
    forms.append(form)
    return document("预览 - 报表站",
                    [tag("h1", text="遗留报表站"), *forms],
                    layout="variant_b" if variant == "b" else "default")


def error_page(variant: str, message: str, *, detail: str = "") -> str:
    parts = [tag("h1", text="请求处理失败"),
             tag("p", {"class": "warn", "id": "error-message"}, text=message)]
    if detail:
        parts.append(tag("pre", {"class": "detail"}, text=detail))
    parts.append(tag("p", text="请从报表选择重新进入。"))
    return document("错误 - 报表站", parts,
                    layout="variant_b" if variant == "b" else "default")
