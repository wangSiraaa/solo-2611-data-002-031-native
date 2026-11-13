"""Page-stage detection from structure (never status code or URL alone)."""

from __future__ import annotations

from .htmlforms import FormParser, norm_space


def classify(doc: FormParser, forms_cfg) -> str:
    """Return login | org | params | preview | download | error | unknown."""
    names = {f.name for f in doc.forms if f.name}
    headings = " ".join(doc.headings)
    paras = " ".join(p.get("text", "") for p in doc.paragraphs)

    if forms_cfg.login in names and _has_password(doc, forms_cfg.login):
        return "login"
    if forms_cfg.params in names and _named_control(doc, forms_cfg.params,
                                                    forms_cfg.month_control):
        return "params"
    if forms_cfg.org in names and _named_control(doc, forms_cfg.org,
                                                 forms_cfg.org_control):
        return "org"
    if forms_cfg.download in names and _has_query_identity(doc):
        return "preview"
    if "请求处理失败" in headings or "请求处理失败" in paras:
        return "error"
    if forms_cfg.download in names:
        return "download"
    return "unknown"


def _named_control(doc: FormParser, form_name: str, control_name: str) -> bool:
    for f in doc.forms:
        if f.name == form_name and any(c.name == control_name for c in f.controls):
            return True
    return False


def _has_password(doc: FormParser, form_name: str) -> bool:
    for f in doc.forms:
        if f.name != form_name:
            continue
        for c in f.controls:
            if c.kind == "password" and not c.disabled:
                return True
    return False


def _has_query_identity(doc: FormParser) -> bool:
    for term, value in doc.dl_pairs:
        if "查询" in term and value:
            return True
    return False
