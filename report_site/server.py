"""Routing and request handling for the synthetic legacy report site.

The helper only ever sees what a browser over HTTP sees.  No internal endpoint
exposes the SQL database; ``/__truth.csv`` is an out-of-band ground-truth
export that the helper never calls during report collection.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import store
from .catalog import lookup, month_options, org_options, resolved_actions, \
    type_options, variant_info
from .domain import amount_columns, generate_rows, summarize, valid_month
from .pages import error_page, login_page, org_page, params_page, preview_page

COOKIE_NAME = "REPORT_SID"
MAX_BODY = 1_000_000
USER_NAME = "ops"
PASSWORD = "demo-pass"

FAULT_NAMES = (
    "csrf_rotate",          # rotate CSRF on every business render/submit
    "expire_org",           # kill session right after org selection
    "expire_params",        # kill session when the params page is read
    "expire_preview",       # kill session right after preview submit
    "expire_download",      # kill session before download submit is served
    "mismatch_download",    # CSV identity differs from the preview
    "bad_content_type",     # download served as text/html
    "truncate_download",    # response is cut mid-stream
    "slow_download",        # delayed download start
)


class Faults:
    def __init__(self, raw: dict | None, default_mode: str):
        raw = raw or {}
        self.mode = raw.get("mode", default_mode)
        # bool True = unlimited, int N = trigger N times, False = off.
        self.flags: dict[str, int | bool] = {}
        for name in FAULT_NAMES:
            value = raw.get(name, False)
            if isinstance(value, bool):
                self.flags[name] = value
            elif isinstance(value, int):
                self.flags[name] = value if value > 0 else False
            else:
                self.flags[name] = bool(value)
        self.lock = threading.Lock()

    @property
    def scoped(self) -> bool:
        return self.mode == "scoped"

    def on(self, name: str) -> bool:
        """Check a knob and consume one use if it is count-limited."""
        with self.lock:
            value = self.flags.get(name, False)
            if isinstance(value, bool):
                return value
            if value > 0:
                self.flags[name] = value - 1
                return True
            return False

    def has(self, name: str) -> bool:
        """Peek without consuming."""
        with self.lock:
            value = self.flags.get(name, False)
            return value if isinstance(value, bool) else value > 0

    def set(self, name: str, value) -> bool:
        with self.lock:
            if name not in self.flags:
                return False
            if isinstance(value, bool):
                self.flags[name] = value
            elif isinstance(value, int):
                self.flags[name] = value if value > 0 else False
            else:
                self.flags[name] = bool(value)
            return True

    def snapshot(self) -> dict:
        with self.lock:
            return {"mode": self.mode, **self.flags}


class App:
    def __init__(self, variant: str, db_path: str | Path, faults: dict | None,
                 default_mode: str = "unscoped", cookie_path: str | None = None):
        self.variant = variant
        self.info = variant_info(variant)
        self.actions = resolved_actions(variant)
        self.db_path = str(db_path)
        self.faults = Faults(faults, default_mode)
        self.cookie_path = cookie_path
        self.db_lock = threading.RLock()
        self.conn = store.connect(self.db_path)
        store.init_db(self.db_path)
        self.current_path = "/"
        self.pending_truncate = 0

    # ------------------------------------------------------------ dispatch
    def handle(self, method: str, path: str, query: str, headers, body: bytes, send):
        self.current_path = path
        pages = set(self.actions.values()) | {"/login"}
        if path == "/__control":
            return self._control(method, body, send)
        if path in ("/__health", "/healthz"):
            return _send_json(send, 200, b'{"ok": true}')
        if path == "/__truth.csv":
            return self._truth(send)
        if path == "/":
            return send(HTTPStatus.FOUND, {"Location": self.info["login_page"]}, b"")
        if path == self.info["login_page"]:
            return self._login(method, headers, body, send)
        if path == self.actions["login"]:
            if method == "GET":
                return send(HTTPStatus.FOUND,
                            {"Location": self.info["login_page"]}, b"")
            return self._login(method, headers, body, send)
        if method == "POST":
            if path == self.actions["org"]:
                return self._org_post(headers, body, send)
            if path == self.actions["params"]:
                return self._params_post(headers, body, send)
            m = re.fullmatch(re.escape(self.actions["download"])
                             + r"/([A-Za-z0-9_-]{6,64})", path)
            if m:
                return self._download_post(path, m.group(1), headers, body, send)
            return self._unknown(method, path, send)
        # GET pages
        if path == self.actions["org"]:
            return self._org_render(headers, send)
        if path == self.actions["params"]:
            return self._params_render(headers, send)
        m = re.fullmatch(re.escape(self.actions["download"])
                         + r"/([A-Za-z0-9_-]{6,64})", path)
        if m:
            return self._download_get(path, m.group(1), headers, send)
        return self._unknown(method, path, send)

    def _unknown(self, method, path, send):
        if self.faults.scoped:
            return send(HTTPStatus.NOT_FOUND,
                        {"Content-Type": "text/plain; charset=utf-8"}, b"not found")
        # Unscoped/native-fault mode mimics a legacy site that 200-redirects
        # unknown paths back to its login page.
        return self._send_login(send, None, notice="页面不可用，请重新登录。")

    def _control(self, method, body, send):
        if method != "POST":
            return _send_json(send, 405, b'{"error":"POST only"}')
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except ValueError:
            return _send_json(send, 400, b'{"error":"bad json"}')
        with self.faults.lock:
            if payload.get("mode") in ("scoped", "unscoped"):
                self.faults.mode = payload["mode"]
        changed = []
        for k, v in payload.items():
            if k == "mode":
                continue
            if self.faults.set(k, v):
                changed.append(k)
        return _send_json(send, 200,
                          json.dumps({"ok": True, "changed": changed,
                                      "faults": self.faults.snapshot()}).encode())

    def _truth(self, send):
        out = io.StringIO()
        rows = self.conn.execute(
            "SELECT * FROM truth_export ORDER BY org_key, month_key, type_key"
        ).fetchall()
        writer = __import__("csv").writer(out)
        writer.writerow(["org_key", "org_label", "month_key", "month_label",
                         "type_key", "type_label", "columns", "row_count", "total"])
        for r in rows:
            writer.writerow([r["org_key"], r["org_label"], r["month_key"],
                             r["month_label"], r["type_key"], r["type_label"],
                             r["columns"], r["row_count"], r["total"]])
        return send(HTTPStatus.OK, {
            "Content-Type": "text/csv; charset=utf-8",
            "Content-Disposition": 'attachment; filename="truth.csv"',
        }, out.getvalue().encode("utf-8"))

    # ------------------------------------------------------------- session
    def _sid(self, headers) -> str | None:
        cookie = headers.get("Cookie") if headers is not None else None
        if not cookie:
            return None
        parsed = SimpleCookie()
        try:
            parsed.load(cookie)
        except Exception:
            return None
        morsel = parsed.get(COOKIE_NAME)
        return morsel.value if morsel else None

    def _session(self, headers):
        return store.fetch_session(self.conn, self._sid(headers))

    def _session_header(self, sid: str) -> str:
        parts = [f"{COOKIE_NAME}={sid}", f"Path={self.cookie_path or '/'}",
                 "HttpOnly", "SameSite=Lax"]
        return "; ".join(parts)

    def _kill(self, session) -> None:
        if session is not None:
            store.delete_session(self.conn, session["sid"])

    def _log(self, phase, *, method, path, status, session_present=False,
             note="", qid=None, redirect=None, content_type=None, size=None):
        store.log_request(
            self.conn, method=method, path=path, phase=phase, status=status,
            session_present=session_present, note=note, qid=qid,
            redirect=redirect, content_type=content_type, size=size,
        )

    # --------------------------------------------------------------- login
    def _send_login(self, send, headers, notice="", not_found_hint=False):
        token = store.create_pending(self.conn)
        html = login_page(self.variant, token, notice=notice,
                          not_found_hint=not_found_hint)
        return send(HTTPStatus.OK, {"Content-Type": "text/html; charset=utf-8",
                                    "Cache-Control": "no-store"}, html.encode("utf-8"))

    def _login(self, method, headers, body, send):
        path = self.current_path
        if method == "GET":
            self._log("login-load", method="GET", path=path,
                      status=200, note="login page",
                      content_type="text/html")
            return self._send_login(send, headers)
        form = _parse_form(body)
        csrf = _one(form.get(self.info["csrf_field"]))
        if csrf is None or not store.consume_pending(self.conn, csrf):
            self._log("login-post", method="POST", path=self.actions["login"],
                      status=200, note="stale login csrf")
            return self._send_login(send, headers, notice="登录令牌已失效，请重试。")
        user = _one(form.get("username"), "")
        password = _one(form.get("password"), "")
        if user != USER_NAME or password != PASSWORD:
            self._log("login-post", method="POST", path=self.actions["login"],
                      status=200, note="bad credentials")
            return self._send_login(send, headers, notice="账号或口令不正确。")
        submit = _one(form.get(self.info["submit_name"]))
        if submit != self.info["submit_value"]["login"]:
            self._log("login-post", method="POST", path=self.actions["login"],
                      status=200, note="wrong submit button value")
            return self._send_login(send, headers, notice="提交按钮值无效。")
        sid, _ = store.create_session(self.conn, ttl=900)
        self._log("login-post", method="POST", path=self.actions["login"],
                  status=302, note="login ok -> org", redirect=self.actions["org"])
        return send(HTTPStatus.FOUND, {
            "Location": self.actions["org"],
            "Set-Cookie": self._session_header(sid),
        }, b"")

    # ----------------------------------------------------------------- org
    def _org_render(self, headers, send):
        session = self._session(headers)
        if session is None:
            self._log("org-load", method="GET", path=self.actions["org"], status=200,
                      note="no session; login page served")
            return self._stale_login(send, headers, "选择机构页需要有效会话。")
        csrf = store.rotate_csrf(self.conn, session["sid"])
        self._log("org-load", method="GET", path=self.actions["org"], status=200,
                  session_present=True, note="org form", content_type="text/html")
        html = org_page(self.variant, csrf)
        return send(HTTPStatus.OK, {"Content-Type": "text/html; charset=utf-8",
                                    "Cache-Control": "no-store"}, html.encode("utf-8"))

    def _org_post(self, headers, body, send):
        session = self._session(headers)
        if session is None:
            self._log("org-post", method="POST", path=self.actions["org"],
                      status=200, note="no session on org submit")
            return self._stale_login(send, headers,
                                     "会话缺失或已失效，请重新登录。")
        form = _parse_form(body)
        csrf = _one(form.get(self.info["csrf_field"]))
        if csrf is None or csrf != session["csrf"]:
            self._log("org-post", method="POST", path=self.actions["org"],
                      status=200, session_present=True, note="stale csrf on org")
            return self._send_login(send, headers, notice="表单令牌无效，请重新进入。")
        machine = _one(form.get("org"))
        if machine is None:
            self._log("org-post", method="POST", path=self.actions["org"],
                      status=200, session_present=True, note="missing org field")
            return self._error(send, "缺少机构选项", "org-post")
        found = lookup(org_options(self.variant), machine)
        if found is None:
            self._log("org-post", method="POST", path=self.actions["org"],
                      status=200, session_present=True, note="unknown org option")
            return self._error(send, "未知的机构选项值", "org-post")
        org_key, org_label = found[2], found[1]
        store.set_session_org(self.conn, session["sid"], org_key, org_label)
        store.rotate_csrf(self.conn, session["sid"])
        if self.faults.on("expire_org"):
            self._kill(session)
            self._log("org-post", method="POST", path=self.actions["org"],
                      status=302, session_present=True,
                      note="org accepted; session expired (fault)",
                      redirect=self.actions["params"])
            return send(HTTPStatus.FOUND, {"Location": self.actions["params"]}, b"")
        self._log("org-post", method="POST", path=self.actions["org"], status=302,
                  session_present=True, note="org accepted -> params",
                  redirect=self.actions["params"])
        return send(HTTPStatus.FOUND, {"Location": self.actions["params"]}, b"")

    # -------------------------------------------------------------- params
    def _params_render(self, headers, send):
        session = self._session(headers)
        if session is None:
            self._log("params-load", method="GET", path=self.actions["params"],
                      status=200, note="no session; login page served")
            return self._stale_login(send, headers, "参数页需要有效会话。")
        if self.faults.on("expire_params"):
            self._kill(session)
            self._log("params-load", method="GET", path=self.actions["params"],
                      status=200, session_present=True,
                      note="params read; session expired (fault)")
            return self._stale_login(send, headers,
                                     "会话在读取参数页时失效，请重新登录。")
        months = month_options(self.variant)
        types = type_options(self.variant)
        csrf = store.rotate_csrf(self.conn, session["sid"])
        org_label = session["org_label"] or ""
        self._log("params-load", method="GET", path=self.actions["params"],
                  status=200, session_present=True, note="params form",
                  content_type="text/html")
        html = params_page(self.variant, csrf, org_label=org_label,
                           selected_month=months[0][0], selected_type=types[0][0])
        return send(HTTPStatus.OK, {"Content-Type": "text/html; charset=utf-8",
                                    "Cache-Control": "no-store"}, html.encode("utf-8"))

    def _params_post(self, headers, body, send):
        session = self._session(headers)
        if session is None:
            self._log("params-post", method="POST", path=self.actions["params"],
                      status=200, note="no session on preview submit")
            return self._stale_login(send, headers,
                                     "会话已失效，请重新登录后重新选择。")
        form = _parse_form(body)
        csrf = _one(form.get(self.info["csrf_field"]))
        if csrf is None or csrf != session["csrf"]:
            self._log("params-post", method="POST", path=self.actions["params"],
                      status=200, session_present=True, note="stale csrf on preview")
            return self._send_login(send, headers, notice="预览令牌已失效，请重新进入。")
        month_machine = _one(form.get("month"))
        type_machine = _one(form.get("rtype"))
        if month_machine is None or type_machine is None:
            self._log("params-post", method="POST", path=self.actions["params"],
                      status=200, session_present=True, note="missing required field")
            return self._error(send, "缺少月份或报表类型字段", "params-post")
        month = lookup(month_options(self.variant), month_machine)
        rtype = lookup(type_options(self.variant), type_machine)
        if month is None or rtype is None:
            self._log("params-post", method="POST", path=self.actions["params"],
                      status=200, session_present=True, note="unknown option value")
            return self._error(send, "未知的月份或报表类型选项值", "params-post")
        month_key, month_label = month[2], month[1]
        type_key, type_label = rtype[2], rtype[1]
        if not valid_month(month_key):
            return self._error(send, "月份超出支持范围", "params-post")
        org_key = session["org_key"]
        org_label = session["org_label"]
        if not org_key:
            return self._send_login(send, headers,
                                    notice="会话中没有机构，请重新选择机构。")
        store.rotate_csrf(self.conn, session["sid"])
        rows = generate_rows(org_key, month_key, type_key)
        columns = amount_columns(type_key)
        spec = summarize(rows)
        preview = store.create_preview(
            self.conn, session["sid"], org_key, org_label, month_key, month_label,
            type_key, type_label, rows, columns, spec["total"])
        expire_now = self.faults.on("expire_preview")
        # A successful business submit rotates CSRF; the preview page carries
        # the new value and the download form must use exactly that one.
        if expire_now:
            self._kill(session)
            csrf_new = store.create_pending(self.conn)
        else:
            csrf_new = store.rotate_csrf(self.conn, session["sid"])
        dl_action = self.actions["download"] + "/" + preview["handle"]
        html = preview_page(
            self.variant, csrf_new, org_label=org_label, month_label=month_label,
            type_label=type_label, columns=columns,
            rows=[{"code": r.code, "name": r.name, "amount": r.amount} for r in rows],
            total=spec["total"], qid=preview["qid"], dl_action=dl_action,
            submit_name=self.info["submit_name"],
            submit_value=self.info["submit_value"]["download"],
            extra_hidden=([("intent", "dl-" + preview["handle"][:6])]
                          if self.variant == "b" else None),
        )
        self._log("params-post", method="POST", path=self.actions["params"],
                  status=200, session_present=True, qid=preview["qid"],
                  note=("preview; session expired (fault)"
                        if expire_now else "preview"),
                  content_type="text/html")
        return send(HTTPStatus.OK, {"Content-Type": "text/html; charset=utf-8",
                                    "Cache-Control": "no-store"}, html.encode("utf-8"))

    # ------------------------------------------------------------ download
    def _download_post(self, path, handle, headers, body, send):
        session = self._session(headers)
        if session is None:
            self._log("download-post", method="POST", path=path, status=200,
                      note="no session on download submit")
            return self._stale_login(send, headers, "会话已失效，请重新登录。")
        if self.faults.on("expire_download"):
            self._kill(session)
            self._log("download-post", method="POST", path=path, status=200,
                      session_present=True,
                      note="download submit; session expired (fault)")
            return self._stale_login(send, headers,
                                     "下载前会话失效，请重新登录后重新预览。")
        form = _parse_form(body)
        csrf = _one(form.get(self.info["csrf_field"]))
        if csrf is None or csrf != session["csrf"]:
            self._log("download-post", method="POST", path=path, status=200,
                      session_present=True, note="stale csrf on download")
            return self._send_login(send, headers, notice="下载令牌已失效，请重新预览。")
        submit = _one(form.get(self.info["submit_name"]))
        if submit != self.info["submit_value"]["download"]:
            return self._error(send, "下载提交按钮值无效", "download-post")
        submitted_qid = _one(form.get("report_id"))
        preview = store.get_preview(self.conn, handle)
        if preview is None:
            return self._error(send, "下载句柄不存在", "download-post")
        if submitted_qid != preview["qid"]:
            self._log("download-post", method="POST", path=path, status=200,
                      session_present=True, note="report_id does not match handle")
            return self._error(send, "查询标识与下载句柄不匹配", "download-post")
        if self.faults.on("mismatch_download"):
            other = store.get_preview_different_from(self.conn, preview)
            if other is not None:
                store.mark_preview_downloaded(self.conn, preview["handle"])
                return self._emit_csv(send, other, page_qid=preview["qid"],
                                      note="fault: CSV identity differs from preview")
        return self._emit_csv(send, preview, page_qid=preview["qid"])

    def _download_get(self, path, handle, headers, send):
        session = self._session(headers)
        if session is None:
            return self._stale_login(send, headers, "下载链接需要有效会话。")
        preview = store.get_preview(self.conn, handle)
        if preview is None:
            return self._error(send, "下载链接不再有效", "download-get")
        store.mark_preview_downloaded(self.conn, preview["handle"])
        return self._emit_csv(send, preview, page_qid=preview["qid"], method="GET")

    def _emit_csv(self, send, preview, *, page_qid, note="csv served", method="POST"):
        columns = json.loads(preview["columns_json"])
        rows = json.loads(preview["rows_json"])
        buf = io.StringIO()
        writer = __import__("csv").writer(buf)
        writer.writerow(columns)
        for r in rows:
            writer.writerow([r["code"], r["name"], r["amount"]])
        payload = buf.getvalue().encode("utf-8")
        content_type = "text/csv; charset=utf-8"
        if self.faults.on("bad_content_type"):
            content_type = "text/html; charset=utf-8"
            note += "; bad content-type"
        truncate = self.faults.on("truncate_download")
        if truncate:
            note += "; truncated body"
        if self.faults.on("slow_download"):
            time.sleep(0.04)
        full_len = len(payload)
        if truncate:
            # Truncation is attached to *this* send call, not global state,
            # so unrelated responses (e.g. the truth export) are intact.
            send.truncate_bytes = max(32, (full_len * 2) // 3)
        headers = {
            "Content-Type": content_type,
            "Content-Disposition": f'attachment; filename="report-{page_qid}.csv"',
            "Content-Length": str(full_len),
            "X-Query-Id": preview["qid"],
            "Cache-Control": "no-store",
        }
        self._log("download-body", method=method, path=self.current_path,
                  status=200, session_present=True, qid=preview["qid"],
                  note=note, content_type=content_type, size=full_len)
        send(HTTPStatus.OK, headers, payload)

    # -------------------------------------------------------------- errors
    def _error(self, send, message, phase):
        self._log(phase, method="POST", path=self.current_path, status=200,
                  session_present=True, note=message)
        html = error_page(self.variant, message)
        return send(HTTPStatus.OK, {"Content-Type": "text/html; charset=utf-8"},
                    html.encode("utf-8"))

    def _stale_login(self, send, headers, message):
        self._send_login(send, headers, notice=message)


def _one(values, default=None):
    if values is None:
        return default
    if isinstance(values, str):
        return values
    return values[0] if values else default


def _parse_form(body: bytes) -> dict[str, list[str]]:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        text = body.decode("latin-1")
    return urllib.parse.parse_qs(text, keep_blank_values=True)


def _send_json(send, status, payload):
    send(HTTPStatus(status), {"Content-Type": "application/json"}, payload)


class Handler(BaseHTTPRequestHandler):
    app: App
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[site] " + (fmt % args) + "\n")

    def _body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        length = max(0, min(length, MAX_BODY))
        if length == 0:
            return b""
        try:
            return self.rfile.read(length)
        except OSError:
            return b""

    def _run(self, method):
        raw, _, _query = self.path.partition("?")
        body = self._body() if method == "POST" else b""

        def send(status, headers, payload=b""):
            truncate_bytes = getattr(send, "truncate_bytes", 0)
            setattr(send, "truncate_bytes", 0)
            self.send_response(int(status))
            has_length = any(k.lower() == "content-length" for k in headers)
            for k, v in headers.items():
                self.send_header(k, v)
            if not has_length:
                self.send_header("Content-Length", str(len(payload)))
            if truncate_bytes:
                self.send_header("Connection", "close")
            self.end_headers()
            if method != "HEAD" and payload:
                try:
                    if truncate_bytes:
                        self.wfile.write(payload[:truncate_bytes])
                        self.wfile.flush()
                        self.close_connection = True
                    else:
                        self.wfile.write(payload)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        try:
            self.app.handle(method, raw, _query, self.headers, body, send)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        self._run("GET")

    def do_HEAD(self):
        self._run("HEAD")

    def do_POST(self):
        self._run("POST")


def make_server(variant: str, db_path: str | Path, host: str = "127.0.0.1",
                port: int = 0, faults: dict | None = None,
                default_mode: str = "unscoped",
                cookie_path: str | None = None) -> ThreadingHTTPServer:
    app = App(variant, db_path, faults, default_mode, cookie_path)
    handler = type("BoundHandler", (Handler,), {"app": app})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    httpd.app = app
    return httpd


def run(variant, db_path, host, port, faults, mode, cookie_path):
    httpd = make_server(variant, db_path, host, port, faults, mode, cookie_path)
    actual = httpd.server_address[1]
    print(f"synthetic site variant={variant} listening on http://{host}:{actual}")
    print(f"  pages:  http://{host}:{actual}/  (variant-specific login is linked)")
    print(f"  truth:  http://{host}:{actual}/__truth.csv  (out-of-band export)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Run the synthetic legacy report site")
    p.add_argument("--variant", choices=["a", "b"], default="a")
    p.add_argument("--db", default=".runtime/site.sqlite")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--mode", choices=["unscoped", "scoped"], default="unscoped")
    p.add_argument("--cookie-path", default=None)
    p.add_argument("--enable", action="append", default=[], choices=FAULT_NAMES)
    p.add_argument("--all-faults", action="store_true")
    p.add_argument("--export-truth", default=None)
    p.add_argument("--ready-file", default=None,
                   help="write 'host:port' here once listening (for harnesses)")
    args = p.parse_args(argv)

    if args.export_truth:
        store.init_db(args.db)
        conn = store.connect(args.db)
        n = store.export_truth_csv(conn, args.export_truth)
        conn.close()
        print(f"exported {n} truth rows to {args.export_truth}")
        return 0

    faults = {name: bool(args.all_faults or name in args.enable)
              for name in FAULT_NAMES}
    faults["mode"] = args.mode
    if args.ready_file:
        httpd = make_server(args.variant, args.db, args.host, args.port, faults,
                            args.mode, args.cookie_path)
        ready = Path(args.ready_file)
        ready.parent.mkdir(parents=True, exist_ok=True)
        ready.write_text(f"{args.host}:{httpd.server_address[1]}", encoding="utf-8")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.server_close()
        return 0
    run(args.variant, args.db, args.host, args.port, faults, args.mode,
        args.cookie_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
