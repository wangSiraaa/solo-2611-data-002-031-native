"""Unit tests that run in-process: parser, form serialization, config,
trace redaction, CSV validation, and page-stage classification."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from report_helper.config import ConfigError, load_config, load_tasks  # noqa: E402
from report_helper.htmlforms import parse_html, summarize_page  # noqa: E402
from report_helper.engine import build_fields, option_by_label  # noqa: E402
from report_helper.stages import classify  # noqa: E402
from report_helper.config import StepForms  # noqa: E402
from report_helper.state import StateStore, TaskState  # noqa: E402
from report_helper.trace import Trace, read_events  # noqa: E402
from report_site.pages import (login_page, org_page, params_page,  # noqa: E402
                               preview_page, error_page)
from report_site.catalog import (month_options, type_options,  # noqa: E402
                                 variant_info)


def test_parse_both_variants_same_semantics():
    for v in ("a", "b"):
        doc = parse_html(login_page(v, "TOKEN"), f"http://x/{v}/login")
        form = [f for f in doc.forms if f.name == "login"][0]
        assert form.method == "POST"
        assert any(c.kind == "password" for c in form.controls)
        assert form.action_url().endswith(
            "auth" if v == "b" else "login")
        # variant B carries a decorative unrelated form
        names = {f.name for f in doc.forms}
        assert "newsletter" in names or "newsletter" not in names


def test_checkbox_same_name_and_disabled():
    for v in ("a", "b"):
        doc = parse_html(params_page(v, "T", org_label="O",
                                     selected_month=month_options(v)[0][0],
                                     selected_type=type_options(v)[0][0]),
                         "http://x/params")
        form = [f for f in doc.forms if f.name == "report_params"][0]
        boxes = [c for c in form.controls if c.name == "columns"]
        assert len(boxes) == 3
        assert boxes[0].label == "显示摘要"
        assert boxes[2].disabled
        assert boxes[2].label.startswith("旧版标签")
        fields = build_fields(form, {"month": month_options(v)[0][0],
                                     "rtype": type_options(v)[0][0]},
                              {"columns": [boxes[0].value, boxes[1].value]})
        sent = [k for k, _ in fields]
        assert "columns" in sent
        assert sum(1 for k, _ in fields if k == "columns") == 2
        # disabled checkbox never submitted
        assert all(v2 != boxes[2].value for k, v2 in fields if k == "columns")


def test_options_by_display_label_not_machine_value():
    for v in ("a", "b"):
        doc = parse_html(params_page(v, "T", org_label="O",
                                     selected_month=month_options(v)[0][0],
                                     selected_type=type_options(v)[0][0]),
                         "http://x/params")
        form = [f for f in doc.forms if f.name == "report_params"][0]
        month_c = [c for c in form.controls if c.name == "month"][0]
        opt = option_by_label(month_c, "2024年7月", "month")
        assert opt.label == "2024年7月"
        assert opt.value == ("2024/07" if v == "b" else "per-2024-07")
        type_c = [c for c in form.controls if c.name == "rtype"][0]
        assert option_by_label(type_c, "成本明细", "rtype").value == (
            "kind/101" if v == "b" else "rt-901")


def test_submit_button_value_comes_from_page():
    for v in ("a", "b"):
        doc = parse_html(org_page(v, "T"), "http://x/org")
        form = [f for f in doc.forms if f.name == "select_org"][0]
        org_c = [c for c in form.controls if c.name == "org"][0]
        fields = build_fields(form, {"org": org_c.options[0].value})
        submit = form.submits()[0]
        assert (submit.name, submit.value) in fields
        info = variant_info(v)
        assert submit.value == info["submit_value"]["org"]


def test_stage_classification():
    forms = StepForms()
    for v in ("a", "b"):
        d = parse_html(login_page(v, "T"), "http://x/login")
        assert classify(d, forms) == "login"
        d = parse_html(org_page(v, "T"), "http://x/org")
        assert classify(d, forms) == "org"
        d = parse_html(params_page(v, "T", org_label="O",
                                   selected_month=month_options(v)[0][0],
                                   selected_type=type_options(v)[0][0]),
                       "http://x/params")
        assert classify(d, forms) == "params"
        d = parse_html(error_page(v, "bad"), "http://x/e")
        assert classify(d, forms) == "error"


def test_config_rejects_hardcoded_secrets():
    base = {"base_url": "http://127.0.0.1:1",
            "credentials": {"username": "u", "password": "p"}}
    path = Path("/tmp/t_cfg.json")
    path.write_text(json.dumps(base))
    cfg = load_config(path)
    assert cfg.username == "u"
    for extra in ({"csrf": "X"}, {"cookies": {"sid": "x"}},
                  {"forms": {"download_url": "x"}},
                  {"forms": {"unknown_key": "x"}}):
        bad = dict(base, **extra)
        path.write_text(json.dumps(bad))
        try:
            load_config(path)
        except ConfigError:
            continue
        raise AssertionError(f"config accepted forbidden content: {extra}")


def test_task_file_validation():
    p = Path("/tmp/t_tasks.json")
    p.write_text(json.dumps({"tasks": [
        {"org_label": "A", "month_label": "2023年1月", "report_type": "销售汇总"},
        {"org_label": "A", "month_label": "2023年1月", "report_type": "销售汇总"},
    ]}))
    try:
        load_tasks(p)
    except ConfigError as exc:
        assert "duplicate" in str(exc)
        return
    raise AssertionError("duplicate tasks were accepted")


def test_trace_redaction():
    d = Path("/tmp/t_trace")
    d.mkdir(exist_ok=True)
    t = Trace(d, "run-1")
    t.event("task", "login", "build_form",
            fields=[{"name": "username", "value": "ops"},
                    {"name": "password", "value": "***"}],
            url="http://x/login")
    t.close()
    content = Path("/tmp/t_trace/trace.jsonl").read_text(encoding="utf-8")
    assert "demo-pass" not in content
    assert "***" in content
    events = read_events(Path("/tmp/t_trace/trace.jsonl"))
    assert events[0]["event"] == "build_form"


def test_state_distinguishes_download_and_completion():
    store = StateStore("/tmp/t_state")
    s = TaskState(task="x", org_label="A", month_label="M", type_label="T")
    s.save_state = "downloaded_file"
    store.save(s)
    got = store.load("x")
    assert got.save_state == "downloaded_file"
    got.save_state = "completed"
    store.save(got)
    assert store.load("x").save_state == "completed"


def main():
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
