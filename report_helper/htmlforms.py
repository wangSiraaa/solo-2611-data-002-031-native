"""Minimal HTML form parser for the helper.

Parses forms and controls from page *structure*.  It never assumes a CSRF
field name, action spelling, option machine value or submit-button value;
those are all discovered per page at run time.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser


def norm_space(text: str) -> str:
    return " ".join(text.replace("\xa0", " ").split())


@dataclass
class Option:
    value: str
    label: str
    selected: bool = False
    disabled: bool = False


@dataclass
class Control:
    tag: str
    kind: str                 # text/password/hidden/select/checkbox/submit/radio/other
    name: str | None
    value: str | None = None
    options: list[Option] = field(default_factory=list)
    disabled: bool = False
    checked: bool = False
    label: str = ""           # display label for checkbox/radio options
    dom_id: str | None = None

    @property
    def submittable(self) -> bool:
        return not self.disabled and self.kind != "other"


@dataclass
class Form:
    index: int
    name: str | None
    action_raw: str
    method: str
    page_url: str
    controls: list[Control] = field(default_factory=list)

    def action_url(self) -> str:
        # Browser rules: empty action => same page; else resolved relative to
        # the request URL.  (We deliberately ignore <base>.)
        return urllib.parse.urljoin(self.page_url, self.action_raw or "")

    def named(self, name: str) -> list[Control]:
        return [c for c in self.controls
                if c.name == name and c.kind not in ("submit",)]

    def submits(self) -> list[Control]:
        return [c for c in self.controls if c.kind == "submit" and not c.disabled]


class _Doc(HTMLParser):
    """State machine that collects forms, controls, headings, and dl pairs."""

    def __init__(self, page_url: str):
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.forms: list[Form] = []
        self.title = ""
        self.headings: list[str] = []
        self.dl_pairs: list[tuple[str, str]] = []

        self._form: Form | None = None
        self._select: Control | None = None
        self._option: dict | None = None
        self._labels: list[dict] = []
        self._title_buf: list[str] = []
        self._in_title = False

        # generic text capture: ("heading", None) or ("dlterm", None) etc.
        self._text_key: str | None = None
        self._text_buf: list[str] = []
        self._dl_term: str | None = None

        # paragraphs, tables (for preview/error/notice semantics)
        self.paragraphs: list[dict] = []
        self._para: dict | None = None
        self.tables: list[dict] = []
        self._table: dict | None = None
        self._row: list[dict] | None = None

    # ------------------------------------------------------------ helpers
    def _open_text(self, key: str):
        self._text_key = key
        self._text_buf = []

    def _close_text(self) -> str:
        text = norm_space("".join(self._text_buf))
        self._text_key = None
        self._text_buf = []
        return text

    # -------------------------------------------------------------- events
    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v if v is not None else "") for k, v in attrs}
        if tag == "form":
            self._form = Form(len(self.forms), a.get("name"),
                              a.get("action", ""),
                              (a.get("method") or "GET").upper(),
                              self.page_url)
            self.forms.append(self._form)
        elif tag == "title":
            self._in_title = True
        elif tag in ("h1", "h2", "h3"):
            self._open_text("heading")
        elif tag == "label":
            self._labels.append({"for": a.get("for") or "", "control": None,
                                 "text": []})
        elif tag == "dt":
            self._open_text("dlterm")
        elif tag == "dd":
            self._open_text("dlvalue")
        elif tag == "p":
            self._para = {"tag": tag, "class": a.get("class", ""),
                          "id": a.get("id", ""), "text": []}
            self.paragraphs.append(self._para)
            self._open_text("para")
        elif tag in ("td", "th") and self._row is not None:
            self._open_text("cell")
        elif tag == "input" and self._form is not None:
            self._on_input(a)
        elif tag == "select" and self._form is not None:
            self._select = Control("select", "select", a.get("name"),
                                   disabled=("disabled" in a))
            self._form.controls.append(self._select)
        elif tag == "option" and self._select is not None:
            self._option = {"value": a.get("value"),
                            "selected": "selected" in a,
                            "disabled": "disabled" in a, "text": []}
        elif tag == "table":
            self._table = {"id": a.get("id", ""), "rows": []}
            self.tables.append(self._table)
        elif tag == "tr" and self._table is not None:
            self._row = []
            self._table["rows"].append(self._row)
        elif tag == "textarea" and self._form is not None:
            self._form.controls.append(
                Control("textarea", "other", a.get("name"),
                        disabled=("disabled" in a)))

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None
            return
        if tag == "title":
            self._in_title = False
            self.title = norm_space("".join(self._title_buf))
            return
        if tag in ("h1", "h2", "h3"):
            text = self._close_text()
            if text:
                self.headings.append(text)
            return
        if tag == "label":
            if self._labels:
                frame = self._labels.pop()
                self._on_label_close(frame)
            return
        if tag == "dt":
            self._dl_term = self._close_text()
            return
        if tag == "dd":
            value = self._close_text()
            if self._dl_term is not None:
                self.dl_pairs.append((self._dl_term, value))
                self._dl_term = None
            return
        if tag == "p":
            if self._para is not None:
                self._para["text"] = self._close_text()
            self._para = None
            return
        if tag in ("td", "th"):
            text = self._close_text()
            if self._row is not None:
                self._row.append({"header": tag == "th", "text": text})
            return
        if tag == "tr":
            self._row = None
            return
        if tag == "table":
            self._table = None
            return
        if tag == "select":
            self._select = None
            return
        if tag == "option":
            if self._select is not None and self._option is not None:
                text = norm_space("".join(self._option["text"]))
                value = self._option["value"]
                if value is None:
                    value = text
                self._select.options.append(
                    Option(value, text, self._option["selected"],
                           self._option["disabled"]))
                self._option = None

    def handle_data(self, data):
        if self._option is not None:
            self._option["text"].append(data)
        if self._text_key is not None:
            self._text_buf.append(data)
        if self._in_title:
            self._title_buf.append(data)
        if self._labels:
            self._labels[-1]["text"].append(data)

    # -------------------------------------------------------------- inputs
    def _on_input(self, a: dict):
        kind = (a.get("type") or "text").lower()
        if kind not in {"text", "password", "hidden", "checkbox", "submit", "radio"}:
            kind = "other"
        c = Control("input", kind, a.get("name"), a.get("value"),
                    disabled=("disabled" in a), checked=("checked" in a),
                    dom_id=a.get("id") or None)
        if kind in ("checkbox", "radio") and self._labels:
            self._labels[-1]["control"] = c
        self._form.controls.append(c)

    def _on_label_close(self, frame: dict):
        text = norm_space("".join(frame["text"]))
        target = frame["control"] or self._find_by_for(frame["for"])
        if target is not None and text and not target.label:
            target.label = text

    def _find_by_for(self, for_id: str) -> Control | None:
        if not for_id or self._form is None:
            return None
        for c in self._form.controls:
            if c.dom_id == for_id:
                return c
        return None


def parse_html(html: str, page_url: str) -> _Doc:
    doc = _Doc(page_url)
    doc.feed(html)
    doc.close()
    return doc


FormParser = _Doc


def summarize_page(doc: _Doc) -> dict:
    """Structural summary with sensitive values hidden."""
    forms = []
    for f in doc.forms:
        controls = []
        for c in f.controls:
            entry = {"tag": c.tag, "kind": c.kind, "name": c.name,
                     "disabled": c.disabled, "checked": c.checked}
            if c.kind == "password":
                entry["value"] = "***"
            elif c.kind == "hidden":
                entry["value"] = "<hidden>"
            else:
                entry["value"] = c.value
            if c.options:
                entry["options"] = [
                    {"label": o.label,
                     "value": "***" if c.kind == "hidden" else o.value,
                     "selected": o.selected, "disabled": o.disabled}
                    for o in c.options
                ]
            if c.label:
                entry["label"] = c.label
            controls.append(entry)
        forms.append({"name": f.name, "method": f.method, "action": f.action_raw,
                      "controls": controls})
    return {"title": doc.title, "headings": doc.headings, "forms": forms}
