"""Workflow engine with per-task context, recovery, and content validation."""

from __future__ import annotations

import csv
import hashlib
import io
import re
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import ReportTask, SiteConfig
from .htmlforms import Control, Form, FormParser, Option, parse_html, summarize_page
from .httpx import NetworkError, Response, Session
from .state import StateStore, TaskState
from .stages import classify
from .trace import Trace


class TaskFailure(RuntimeError):
    def __init__(self, message, *, stage="unknown", page_url="", summary=None,
                 recoverable=False):
        super().__init__(message)
        self.stage = stage
        self.page_url = page_url
        self.summary = summary
        self.recoverable = recoverable


@dataclass
class PreviewFacts:
    qid: str
    org_label: str
    month_label: str
    type_label: str
    columns: list[str]
    row_count: int
    total: str
    rows: list[list[str]]
    download_form: Form
    url: str


def build_fields(form: Form, overrides: dict[str, str],
                 selected_multi: dict[str, list[str]] | None = None,
                 include_submit: bool = True) -> list[tuple[str, str]]:
    """Serialize a form exactly like a browser would.

    disabled -> omitted; unchecked checkbox/radio -> omitted; same-name
    checkboxes keep every checked value; the chosen submit button contributes
    its name/value.  Values are taken from *this* page, never from config.
    """
    selected_multi = selected_multi or {}
    pairs: list[tuple[str, str]] = []
    submit = None
    if include_submit:
        submits = form.submits()
        if len(submits) != 1:
            raise TaskFailure(
                f"form {form.name!r}: expected exactly one enabled submit button, "
                f"found {len(submits)}")
        submit = submits[0]
    for c in form.controls:
        if c.disabled:
            continue
        if c.kind == "submit":
            if c is submit and c.name:
                pairs.append((c.name, c.value or ""))
            continue
        if not c.name:
            continue
        if c.kind == "select":
            vals = selected_multi.get(c.name)
            if vals is not None:
                for v in vals:
                    pairs.append((c.name, v))
            else:
                chosen = _choose_option(c, overrides.get(c.name))
                if chosen is not None:
                    pairs.append((c.name, chosen))
            continue
        if c.kind in ("checkbox", "radio"):
            if c.name in selected_multi:
                if c.value in selected_multi[c.name]:
                    pairs.append((c.name, c.value or "on"))
            elif c.name in overrides and c.value == overrides[c.name]:
                pairs.append((c.name, c.value or "on"))
            continue
        if c.kind in ("text", "password", "hidden"):
            if c.name in overrides:
                pairs.append((c.name, overrides[c.name]))
            elif c.kind in ("text", "password"):
                pairs.append((c.name, c.value or ""))
            elif c.value is not None:
                pairs.append((c.name, c.value))
            continue
        if c.kind == "other":
            pairs.append((c.name, overrides.get(c.name, c.value or "")))
    return pairs


def _choose_option(c: Control, wanted: str | None) -> str | None:
    selectable = [o for o in c.options if not o.disabled]
    if wanted is not None:
        for o in selectable:
            if o.value == wanted:
                return o.value
        raise TaskFailure(
            f"control {c.name!r}: requested option value is not selectable")
    for o in selectable:
        if o.selected:
            return o.value
    return selectable[0].value if selectable else None


def option_by_label(control: Control, label: str, where: str) -> Option:
    selectable = [o for o in control.options if not o.disabled]
    hits = [o for o in selectable if o.label == label]
    if not hits:
        hits = [o for o in selectable if o.label.strip() == label.strip()]
    if not hits:
        raise TaskFailure(
            f"{where}: no selectable option labelled {label!r}; "
            f"available labels are {[o.label for o in selectable]}")
    if len(hits) > 1:
        raise TaskFailure(
            f"{where}: label {label!r} matches {len(hits)} selectable options")
    return hits[0]


def form_named(doc: FormParser, name: str, where: str) -> Form:
    matches = [f for f in doc.forms if f.name == name]
    if not matches:
        raise TaskFailure(f"{where}: no form named {name!r} on page")
    if len(matches) > 1:
        raise TaskFailure(
            f"{where}: form name {name!r} is ambiguous ({len(matches)} matches)")
    return matches[0]


def control_named(form: Form, name: str, where: str, kinds=("select",)) -> Control:
    matches = [c for c in form.controls if c.name == name and c.kind in kinds]
    if not matches:
        raise TaskFailure(
            f"{where}: no control named {name!r} in form {form.name!r}")
    if len(matches) > 1:
        raise TaskFailure(
            f"{where}: control {name!r} is ambiguous ({len(matches)} matches)")
    return matches[0]


class TaskRunner:
    def __init__(self, cfg: SiteConfig, task: ReportTask, store: StateStore,
                 trace: Trace, log: Callable[[str], None],
                 sleep: Callable[[float], None] = time.sleep):
        self.cfg = cfg
        self.task = task
        self.store = store
        self.trace = trace
        self.log = log
        self.sleep = sleep
        self.state = store.load(task.name) or TaskState(
            task=task.name, org_label=task.org_label,
            month_label=task.month_label, type_label=task.type_label)
        self.session: Session | None = None
        self.recoveries = 0
        self.event_cursor = 0
        self.org_doc: tuple[FormParser, Response] | None = None
        self.params_doc: tuple[FormParser, Response] | None = None
        self.preview: PreviewFacts | None = None

    def _fail_summary(self, doc: FormParser, resp: Response) -> dict:
        summary = summarize_page(doc)
        summary.update({"url": resp.url, "status": resp.status,
                        "classified": classify(doc, self.cfg.forms)})
        return summary

    # ------------------------------------------------------------- tracing
    def _t(self, stage: str, event: str, **fields):
        self.trace.event(self.task.name, stage, event, **fields)

    def _flush_http(self, stage: str):
        if self.session is None:
            return
        events, cursor = self.session.stage_events(self.event_cursor)
        for ev in events:
            self._t(stage, "http", **ev)
            if ev.get("redirect_to"):
                self._t(stage, "redirect", status=ev.get("status"),
                        from_url=ev.get("url"), to_url=ev.get("redirect_to"))
        self.event_cursor = cursor

    def _page(self, stage: str, resp: Response, doc: FormParser) -> str:
        classified = classify(doc, self.cfg.forms)
        summary = summarize_page(doc)
        summary.update({"url": resp.url, "status": resp.status,
                        "classified": classified})
        rel = self.trace.save_page_summary(self.task.name, stage, summary)
        self._t(stage, "page_structure", url=resp.url, http_status=resp.status,
                classified=classified, summary_file=rel)
        return classified

    # ----------------------------------------------------------- orchestrate
    def run(self) -> TaskState:
        while True:
            self.state.stage = "pending"
            self.store.save(self.state)
            recovered = False
            try:
                self._enter_session()
                self._submit_org()
                self._submit_params()
                self._download()
                self.state.stage = "done"
                self.state.save_state = "completed"
                self.state.last_error = None
            except TaskFailure as exc:
                recovered = self._handle_failure(exc)
            except NetworkError as exc:
                recovered = self._handle_failure(TaskFailure(
                    f"网络/传输错误: {exc}", stage=self.state.stage,
                    recoverable=True))
            finally:
                self._flush_http(self.state.stage)
                if self.session is not None:
                    self.session.close()
                self.store.save(self.state)
            if not recovered:
                return self.state

    def _handle_failure(self, exc: TaskFailure) -> bool:
        """Returns True if the task will be retried in a rebuilt session."""
        self._flush_http(exc.stage)
        self.state.last_error = str(exc)
        self.state.diagnostics.append(f"{_ts()} [{exc.stage}] {exc}")
        # A failure page that is *semantically* a login page is recorded
        # explicitly: HTTP 200 alone would look like success.
        if exc.summary is not None:
            page_class = exc.summary.get("classified")
            if page_class:
                self._t(exc.stage, "failure_page", page_url=exc.page_url,
                        classified=page_class, title=exc.summary.get("title"),
                        headings=exc.summary.get("headings"))
            if page_class == "login" or "登录" in str(exc) or "会话" in str(exc):
                self._t(exc.stage, "session_expired_detected",
                        page_url=exc.page_url, detected_from="page-structure",
                        http_status=exc.summary.get("status"),
                        classified=page_class or "login",
                        note="200 HTML login page is treated as session loss")
        self._t(exc.stage, "failure", reason=str(exc),
                recoverable=exc.recoverable, page_url=exc.page_url)
        if exc.recoverable and self.state.recoveries < self.cfg.retry_limit:
            self.state.recoveries += 1
            self._t(exc.stage, "recover",
                    reason=str(exc), attempt=self.state.recoveries,
                    limit=self.cfg.retry_limit, page_url=exc.page_url,
                    restart_from="task task list entry (org/month/type)")
            self.log(f"  [恢复] 任务 {self.task.name}: {exc} "
                     f"({self.state.recoveries}/{self.cfg.retry_limit})")
            self._reset_context()
            self.store.save(self.state)
            time.sleep(self.cfg.request_delay)
            return True
        self.state.stage = "failed"
        self.state.save_state = "failed"
        self.state.final_file = None
        self.store.clear_partial(self.task.name)
        self.log(f"  [失败] 任务 {self.task.name}: {exc} (阶段 {exc.stage})")
        return False

    def _reset_context(self):
        """Drop everything from the failed session: cookies and page state."""
        if self.session is not None:
            self.session.reset()
        self._t("session", "reset",
                note="丢弃 Cookie、旧 CSRF 字段、预览与下载链接；"
                     "将从任务清单重新进入")
        self.org_doc = None
        self.params_doc = None
        self.preview = None
        self.state.qid = None
        self.state.stage = "pending"

    # --------------------------------------------------------------- login
    def _enter_session(self):
        self.state.stage = "login"
        self.session = Session(timeout=self.cfg.timeout)
        login_url = _url(self.cfg, self.cfg.login_url)
        resp = self.session.get(login_url)
        self._flush_http("login")
        doc = parse_html(resp.text, resp.url)
        stage = self._page("login", resp, doc)
        if stage != "login":
            raise TaskFailure(f"未定位到登录页（页面判定为 {stage}）",
                              stage="login", page_url=resp.url,
                              summary=self._fail_summary(doc, resp), recoverable=True)
        form = form_named(doc, self.cfg.forms.login, "login")
        self._require_method(form, "POST", "login")
        user_c = control_named(form, self.cfg.forms.username_control, "login",
                               kinds=("text", "password", "text", "email"))
        pass_c = control_named(form, self.cfg.forms.password_control, "login",
                               kinds=("password",))
        overrides = {user_c.name: self.cfg.username, pass_c.name: self.cfg.password}
        fields = build_fields(form, overrides)
        self._t("login", "build_form", form=form.name, method=form.method,
                action=form.action_url(),
                fields=_redact(fields, pass_c.name))
        resp = self.session.post_form(form.action_url(), fields)
        self._flush_http("login>org")
        doc = parse_html(resp.text, resp.url)
        stage = self._page("org", resp, doc)
        if stage == "login":
            raise TaskFailure("提交登录后仍是登录页（会话未建立）",
                              stage="login", page_url=resp.url,
                              summary=self._fail_summary(doc, resp), recoverable=True)
        if stage != "org":
            raise TaskFailure(f"登录后未进入选择机构页（判定为 {stage}）",
                              stage="login", page_url=resp.url,
                              summary=self._fail_summary(doc, resp), recoverable=True)
        self.org_doc = (doc, resp)
        self._t("login", "authenticated",
                cookie_names=self.session.jar.names_for(resp.url))

    # ----------------------------------------------------------------- org
    def _submit_org(self):
        self.state.stage = "org"
        doc, resp = self.org_doc
        form = form_named(doc, self.cfg.forms.org, "org")
        self._require_method(form, "POST", "org")
        control = control_named(form, self.cfg.forms.org_control, "org",
                                kinds=("select", "radio", "checkbox"))
        opt = option_by_label(control, self.task.org_label, "org")
        fields = build_fields(form, {control.name: opt.value})
        self._t("org", "interpret_option", control=control.name,
                label=opt.label, machine_value=opt.value)
        self._t("org", "build_form", form=form.name, method=form.method,
                action=form.action_url(), fields=_redact(fields, ""))
        resp = self.session.post_form(form.action_url(), fields)
        self._flush_http("org>params")
        doc = parse_html(resp.text, resp.url)
        stage = self._page("params", resp, doc)
        if stage == "login":
            raise TaskFailure("选择机构后返回登录页（会话在此时失效）",
                              stage="org", page_url=resp.url,
                              summary=self._fail_summary(doc, resp), recoverable=True)
        if stage == "error":
            raise TaskFailure(f"选择机构被拒绝：{_page_message(doc)}",
                              stage="org", page_url=resp.url,
                              summary=self._fail_summary(doc, resp))
        if stage != "params":
            raise TaskFailure(f"选择机构后未进入参数页（判定为 {stage}）",
                              stage="org", page_url=resp.url,
                              summary=self._fail_summary(doc, resp),
                              recoverable=stage in ("unknown",))
        if not doc.dl_pairs:
            # The params page echoes the org; verify it matches the request.
            pass
        self.params_doc = (doc, resp)

    # -------------------------------------------------------------- params
    def _submit_params(self):
        self.state.stage = "params"
        if self.params_doc is None:
            raise TaskFailure("缺少参数页上下文", stage="params", recoverable=True)
        # Re-read the params page so the CSRF field is the newest one.
        doc0, resp0 = self.params_doc
        page_url = resp0.url
        resp = self.session.get(page_url)
        self._flush_http("params")
        doc = parse_html(resp.text, resp.url)
        stage = self._page("params", resp, doc)
        if stage == "login":
            raise TaskFailure("读取参数页时返回登录页（会话此时失效）",
                              stage="params", page_url=resp.url,
                              summary=self._fail_summary(doc, resp), recoverable=True)
        if stage != "params":
            raise TaskFailure(f"读取参数页失败（判定为 {stage}）",
                              stage="params", page_url=resp.url,
                              summary=self._fail_summary(doc, resp),
                              recoverable=stage in ("unknown", "error"))
        form = form_named(doc, self.cfg.forms.params, "params")
        self._require_method(form, "POST", "params")
        month_c = control_named(form, self.cfg.forms.month_control, "params",
                                kinds=("select", "radio"))
        type_c = control_named(form, self.cfg.forms.type_control, "params",
                               kinds=("select", "radio"))
        m = option_by_label(month_c, self.task.month_label, "params.month")
        t = option_by_label(type_c, self.task.type_label, "params.type")
        overrides = {month_c.name: m.value, type_c.name: t.value}
        multi = self._section_values(form)
        disabled = [c.name for c in form.controls if c.disabled]
        self._t("params", "interpret_options",
                month={"label": m.label, "machine_value": m.value},
                month_machine_value=m.value, type_label=t.label,
                type_machine_value=t.value,
                sections=multi or None, disabled_controls=disabled)
        fields = build_fields(form, overrides, multi)
        self._t("params", "build_form", form=form.name, method=form.method,
                action=form.action_url(), fields=_redact(fields, ""))
        resp = self.session.post_form(form.action_url(), fields)
        self._flush_http("params>preview")
        doc = parse_html(resp.text, resp.url)
        stage = self._page("preview", resp, doc)
        if stage == "login":
            raise TaskFailure("提交预览后返回登录页（会话此时失效）",
                              stage="params", page_url=resp.url,
                              summary=self._fail_summary(doc, resp), recoverable=True)
        if stage == "error":
            raise TaskFailure(f"预览请求被拒绝：{_page_message(doc)}",
                              stage="params", page_url=resp.url,
                              summary=self._fail_summary(doc, resp))
        if stage != "preview":
            raise TaskFailure(f"提交预览后未得到预览页（判定为 {stage}）",
                              stage="params", page_url=resp.url,
                              summary=self._fail_summary(doc, resp),
                              recoverable=stage in ("unknown",))
        self.preview = self._parse_preview(doc, resp.url)
        self.state.stage = "preview"
        self.state.qid = self.preview.qid
        self.state.preview_columns = self.preview.columns
        self.state.preview_row_count = self.preview.row_count
        self.state.preview_total = self.preview.total
        self._verify_preview(self.preview)
        self._t("preview", "verified", qid=self.preview.qid,
                org=self.preview.org_label, month=self.preview.month_label,
                type=self.preview.type_label, columns=self.preview.columns,
                row_count=self.preview.row_count,
                total=self.preview.total)
        self.store.save(self.state)

    def _section_values(self, form: Form) -> dict[str, list[str]]:
        cfg = self.cfg.forms
        if not cfg.section_control or not cfg.section_labels:
            return {}
        boxes = [c for c in form.controls if c.name == cfg.section_control
                 and c.kind == "checkbox"]
        if not boxes:
            raise TaskFailure(
                f"params: no checkboxes named {cfg.section_control!r} found")
        chosen = []
        for label in cfg.section_labels:
            hits = [c for c in boxes if c.label.strip() == label.strip()
                    and not c.disabled]
            if not hits:
                raise TaskFailure(
                    f"params: checkbox option labelled {label!r} "
                    "not found or is disabled")
            if len(hits) > 1:
                raise TaskFailure(
                    f"params: checkbox label {label!r} is ambiguous")
            chosen.append(hits[0].value or "on")
        return {cfg.section_control: chosen}

    def _parse_preview(self, doc: FormParser, url: str) -> PreviewFacts:
        form = form_named(doc, self.cfg.forms.download, "preview")
        self._require_method(form, "POST", "preview")
        terms = {term: value for term, value in doc.dl_pairs}

        def pick(*needles):
            for needle in needles:
                for term, value in terms.items():
                    if needle in term:
                        return value
            raise TaskFailure(
                f"preview: page lacks identity field {needles[0]!r}")

        qid = pick("查询标识", "查询编号", "Query")
        org = pick("机构", "组织")
        month = pick("月份", "账期")
        rtype = pick("报表类型", "类型")
        columns_raw = pick("列名", "列")
        count_raw = pick("行数", "记录数")
        total = pick("金额合计", "合计")
        columns = [c.strip() for c in re.split("[,，]", columns_raw) if c.strip()]
        try:
            row_count = int(count_raw.strip())
        except ValueError:
            raise TaskFailure(
                f"preview: declared row count is not an integer: {count_raw!r}")
        rows = _preview_rows(doc, columns)
        return PreviewFacts(qid=qid, org_label=org, month_label=month,
                            type_label=rtype, columns=columns,
                            row_count=row_count, total=total, rows=rows,
                            download_form=form, url=url)

    def _verify_preview(self, p: PreviewFacts):
        errors = []
        if not _text_eq(p.org_label, self.task.org_label):
            errors.append(f"机构 {p.org_label!r} != 请求 {self.task.org_label!r}")
        if not _month_eq(p.month_label, self.task.month_label):
            errors.append(f"月份 {p.month_label!r} != 请求 {self.task.month_label!r}")
        if not _text_eq(p.type_label, self.task.type_label):
            errors.append(f"类型 {p.type_label!r} != 请求 {self.task.type_label!r}")
        if p.row_count != len(p.rows):
            errors.append(f"声明行数 {p.row_count} 与预览表 {len(p.rows)} 不符")
        if not p.columns:
            errors.append("预览没有列名")
        data_cols = [len(r) for r in p.rows]
        if data_cols and any(n != len(p.columns) for n in data_cols):
            errors.append("预览数据行的列数与列名数量不符")
        if errors:
            raise TaskFailure("预览身份核对失败: " + "; ".join(errors),
                              stage="preview", page_url=p.url,
                              summary=None)

    # ------------------------------------------------------------ download
    def _download(self):
        self.state.stage = "download"
        p = self.preview
        form = p.download_form
        fields = build_fields(form, {})
        self._t("download", "build_form", form=form.name, method=form.method,
                action=form.action_url(), qid=p.qid,
                fields=_redact(fields, ""))
        partial = self.store.dir_for(self.task.name) / ".partial" / "download.bin"
        self.store.clear_partial(self.task.name)
        resp = self.session.post_form(form.action_url(), fields, stream_to=partial)
        self._flush_http("download")
        # "downloaded_file" (bytes arrived) is recorded before validation;
        # only successful content matching promotes the file to "completed".
        self.state.save_state = "downloaded_file"
        self.state.downloaded_file = str(partial.relative_to(
            self.store.dir_for(self.task.name)))
        self.store.save(self.state)
        self._t("download", "bytes_arrived", bytes=resp.received_length,
                declared_bytes=resp.declared_length,
                content_type=resp.content_type, qid=p.qid,
                truncated=resp.truncated)
        self._validate_download_response(resp, partial)
        facts = self._validate_csv(partial, resp.body_path)
        final, digest = self.store.promote(self.task.name, partial)
        self.state.downloaded_file = str(final.relative_to(
            self.store.dir_for(self.task.name)))
        self.state.final_file = self.state.downloaded_file
        self.state.sha256 = digest
        self.state.csv_columns = facts["columns"]
        self.state.csv_row_count = facts["row_count"]
        self.state.csv_total = facts["total"]
        self.state.save_state = "completed"
        self.state.stage = "done"
        self.state.last_error = None
        self._t("download", "content_verified", final_file=self.state.final_file,
                sha256=digest, columns=facts["columns"], rows=facts["row_count"],
                total=facts["total"], qid=p.qid)
        self.store.save(self.state)

    def _validate_download_response(self, resp: Response, partial: Path):
        if resp.status >= 400:
            raise TaskFailure(f"下载请求返回 HTTP {resp.status}",
                              stage="download", page_url=resp.url,
                              recoverable=resp.status >= 500)
        if resp.truncated:
            raise TaskFailure(
                f"下载响应不完整（收到 {resp.received_length} 字节，"
                f"声明 {resp.declared_length}）",
                stage="download", page_url=resp.url, recoverable=True)
        # A body that is HTML is classified by page structure first, so a
        # 200-status login page is reported as session loss (not merely as a
        # content-type error).  A CSV payload mislabelled text/html does not
        # look like HTML and falls through to the content-type check below.
        if _looks_html(resp):
            doc = parse_html(resp.text, resp.url)
            stage = classify(doc, self.cfg.forms)
            self._t("download", "html_response", classified=stage,
                    content_type=resp.content_type)
            if stage == "login":
                raise TaskFailure("下载阶段收到登录页（会话在此失效）",
                                  stage="download", page_url=resp.url,
                                  summary=self._fail_summary(doc, resp),
                                  recoverable=True)
            raise TaskFailure(
                f"下载响应是 HTML 错误页而非 CSV：{_page_message(doc)}",
                stage="download", page_url=resp.url,
                summary=self._fail_summary(doc, resp))
        if resp.content_type not in ("text/csv", "application/csv",
                                     "application/octet-stream"):
            raise TaskFailure(
                f"下载响应的 Content-Type 不是 CSV：{resp.content_type!r}",
                stage="download", page_url=resp.url, recoverable=True)
        header_qid = resp.header("X-Query-Id")
        if header_qid and header_qid != self.preview.qid:
            raise TaskFailure(
                f"下载响应的查询标识 {header_qid!r} 与预览 {self.preview.qid!r} 不符",
                stage="download", page_url=resp.url)

    def _validate_csv(self, body_path: Path, resp: Response) -> dict:
        raw = body_path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise TaskFailure("下载内容不是 UTF-8 文本", stage="download")
        try:
            rows = list(csv.reader(io.StringIO(text)))
        except csv.Error as exc:
            raise TaskFailure(f"下载内容不是合法 CSV: {exc}", stage="download")
        if not rows:
            raise TaskFailure("下载 CSV 为空", stage="download")
        header = [c.strip() for c in rows[0]]
        data = [r for r in rows[1:] if r and any(cell.strip() for cell in r)]
        if header != self.preview.columns:
            raise TaskFailure(
                f"CSV 列 {header!r} 与预览列 {self.preview.columns!r} 不符",
                stage="download")
        if len(data) != self.preview.row_count:
            raise TaskFailure(
                f"CSV 行数 {len(data)} 与预览 {self.preview.row_count} 不符",
                stage="download")
        idx = len(header) - 1
        total = 0.0
        for i, row in enumerate(data, start=2):
            if len(row) != len(header):
                raise TaskFailure(
                    f"CSV 第 {i} 行有 {len(row)} 列，预期 {len(header)}",
                    stage="download")
            try:
                total += float(row[idx].replace(",", ""))
            except ValueError:
                raise TaskFailure(
                    f"CSV 第 {i} 行金额无法解析：{row[idx]!r}", stage="download")
        total_str = f"{total:.2f}"
        try:
            pv = f"{float(self.preview.total.replace(',', '')):.2f}"
        except ValueError:
            pv = self.preview.total
        if total_str != pv:
            raise TaskFailure(
                f"CSV 金额合计 {total_str} 与预览 {pv} 不符", stage="download")
        return {"columns": header, "row_count": len(data), "total": total_str,
                "sha256": digest}

    # ------------------------------------------------------------- helpers
    def _require_method(self, form: Form, expected: str, where: str):
        if form.method != expected:
            raise TaskFailure(
                f"{where}: form {form.name!r} uses method {form.method}, "
                f"expected {expected}", stage=where)


def _preview_rows(doc: FormParser, columns: list[str]) -> list[list[str]]:
    for t in doc.tables:
        rows = t["rows"]
        if not rows:
            continue
        header = [c["text"].strip() for c in rows[0]]
        if header == columns:
            return [[c["text"] for c in r] for r in rows[1:]]
    raise TaskFailure("preview: could not find the preview table matching columns")


def _redact(fields, password_name: str):
    return [{"name": k, "value": "***" if k == password_name else v}
            for k, v in fields]


def _page_message(doc: FormParser) -> str:
    for p in doc.paragraphs:
        if p.get("id") == "error-message" and p.get("text"):
            return p["text"]
    for p in doc.paragraphs:
        if "warn" in p.get("class", "") and p.get("text"):
            return p["text"]
    for p in doc.paragraphs:
        if p.get("text"):
            return p["text"]
    return "（页面未提供说明）"


def _looks_html(resp: Response) -> bool:
    """True only when the *body* is markup.

    A CSV payload served with a text/html Content-Type is not HTML; the
    Content-Type check catches it separately.
    """
    head = resp.text.lstrip()[:256].lower()
    return head.startswith("<!doctype html") or head.startswith("<html") or \
        head.startswith("<?xml")


def _text_eq(a: str, b: str) -> bool:
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


def _month_eq(actual: str, wanted: str) -> bool:
    if _text_eq(actual, wanted):
        return True
    a = re.search(r"(\d{4})\D+(\d{1,2})\D", actual + " ")
    b = re.search(r"(\d{4})\D+(\d{1,2})\D", wanted + " ")
    return bool(a and b and (int(a.group(1)), int(a.group(2))
                             ) == (int(b.group(1)), int(b.group(2))))


def _url(cfg: SiteConfig, rel: str) -> str:
    return urllib.parse.urljoin(cfg.base_url + "/", rel)


def _ts() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%H:%M:%SZ")
