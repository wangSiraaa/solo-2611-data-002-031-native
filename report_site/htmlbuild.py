"""Tiny HTML builder used by both site variants.

It intentionally produces plain, structurally-valid HTML that a generic
parser (stdlib html.parser in the helper) can walk without knowing this code.
"""

from __future__ import annotations

from html import escape


def attr(name: str, value: str | None) -> str:
    if value is None:
        return ""
    return f' {name}="{escape(str(value), quote=True)}"'


def tag(name: str, attrs: dict | None = None, *, text: str | None = None,
        children: list[str] | None = None, void: bool = False) -> str:
    a = "".join(attr(k, v) for k, v in (attrs or {}).items())
    if void:
        return f"<{name}{a}>"
    inner = ""
    if text is not None:
        inner = escape(text)
    if children:
        inner += "".join(children)
    return f"<{name}{a}>{inner}</{name}>"


def document(title: str, body_parts: list[str], *, extra_head: str = "",
             layout: str = "default") -> str:
    nav = ""
    if layout == "variant_b":
        nav = tag("div", {"class": "topnav"},
                  children=[tag("span", text="遗留报表中心"),
                            tag("span", {"class": "deco"}, text="环境: 合成 B")])
    body = "".join(body_parts)
    return (
        "<!DOCTYPE html>\n"
        f"<html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        f"<title>{escape(title)}</title>"
        "<style>body{font-family:sans-serif;margin:1.5em}"
        "table{border-collapse:collapse}td,th{border:1px solid #999;padding:3px 8px}"
        ".warn{color:#a00}.hint{color:#555}</style>"
        f"{extra_head}</head><body>{nav}{body}</body></html>"
    )
