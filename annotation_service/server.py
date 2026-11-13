"""Minimal stdlib-only HTTP server exposing the annotation service."""

from __future__ import annotations

import json
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import unquote, urlsplit

from .service import ApiError, AnnotationService

log = logging.getLogger("annotation_service")

MAX_BODY_BYTES = 1_048_576


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "BlindAnnotation/1.0"

    # Injected by make_server.
    service: AnnotationService

    # -- plumbing ----------------------------------------------------------

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_error(self, status: int, code: str, message: str) -> None:
        # Error payloads deliberately carry no item or label state, so an
        # error can never act as a side channel for the other annotator's work.
        self._send_json(status, {"error": {"code": code, "message": message}})

    def _route(self) -> tuple[str, list[str]]:
        path = urlsplit(self.path).path
        parts = [unquote(p) for p in path.split("/") if p]
        return path, parts

    def _read_json_body(self) -> Any:
        length_header = self.headers.get("Content-Length")
        if length_header is None:
            raise ApiError(411, "length_required", "Content-Length is required")
        try:
            length = int(length_header)
        except ValueError:
            raise ApiError(400, "invalid_body", "invalid Content-Length")
        if length < 0 or length > MAX_BODY_BYTES:
            # We will not consume the oversized body, so the connection cannot
            # be safely reused for another request.
            self.close_connection = True
            raise ApiError(413, "body_too_large", "request body is too large")
        raw = self.rfile.read(length)
        if len(raw) != length:
            self.close_connection = True
            raise ApiError(400, "invalid_body", "request body is truncated")
        if not raw:
            raise ApiError(400, "invalid_body", "request body is empty")
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(400, "invalid_json", f"request body is not JSON: {exc}")
        return body

    def _authenticate(self) -> dict[str, str]:
        return self.service.authenticate(self.headers.get("Authorization"))

    def _dispatch(self) -> None:
        try:
            path, parts = self._route()
            method = self.command

            if path == "/healthz" and method in ("GET", "HEAD"):
                self._send_json(200, {"ok": True})
                return
            if path == "/api/meta" and method in ("GET", "HEAD"):
                self._send_json(200, self.service.meta())
                return
            if path == "/api/labels" and method in ("GET", "HEAD"):
                self._send_json(200, self.service.labels_catalog())
                return

            if len(parts) >= 2 and parts[0] == "api":
                user = self._authenticate()

                if parts == ["api", "items"] and method in ("GET", "HEAD"):
                    self._send_json(200, self.service.list_items(user))
                    return
                if len(parts) == 3 and parts[1] == "items" and method in ("GET", "HEAD"):
                    self._send_json(200, self.service.get_item(user, parts[2]))
                    return
                if (
                    len(parts) == 4
                    and parts[1] == "items"
                    and parts[3] == "submission"
                    and method == "POST"
                ):
                    body = self._read_json_body()
                    status, payload = self.service.submit(user, parts[2], body)
                    self._send_json(status, payload)
                    return
                if (
                    len(parts) == 4
                    and parts[1] == "items"
                    and parts[3] == "arbitration"
                    and method == "POST"
                ):
                    body = self._read_json_body()
                    status, payload = self.service.arbitrate(user, parts[2], body)
                    self._send_json(status, payload)
                    return
                if parts == ["api", "arbitration", "queue"] and method in ("GET", "HEAD"):
                    self._send_json(200, self.service.arbitration_queue(user))
                    return
                if parts == ["api", "export"] and method in ("GET", "HEAD"):
                    self._send_json(200, self.service.export(user))
                    return

            self._send_error(404, "not_found", "no such endpoint")

        except ApiError as exc:
            self._send_error(exc.status, exc.code, exc.message)
        except (BrokenPipeError, ConnectionResetError):
            raise
        except Exception:  # pragma: no cover - defensive
            log.exception("unhandled request error")
            self._send_error(500, "internal_error", "internal server error")

    do_GET = _dispatch
    do_HEAD = _dispatch
    do_POST = _dispatch
    do_PUT = _dispatch
    do_DELETE = _dispatch
    do_PATCH = _dispatch

    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("%s - %s", self.address_string(), fmt % args)


class AnnotationHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(host: str, port: int, service: AnnotationService) -> AnnotationHTTPServer:
    handler = type("BoundHandler", (Handler,), {"service": service})
    return AnnotationHTTPServer((host, port), handler)
