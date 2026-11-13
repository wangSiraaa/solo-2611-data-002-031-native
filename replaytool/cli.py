"""Command line entry points: record, serve the offline replayer, manage sessions.

Examples
--------
    python -m replaytool record \
        --workflow fixtures/workflow.json \
        --base-url http://127.0.0.1:8109 \
        --out .runtime/recorded

    python -m replaytool serve --material-dir .runtime/recorded --port 8110

    curl -s -XPOST localhost:8110/__replay/sessions
    curl -s localhost:8110/__replay/sessions/<id>
    curl -s -XDELETE localhost:8110/__replay/sessions/<id>

    python -m replaytool session delete --material-dir .runtime/recorded --id <id>
    python -m replaytool session list   --material-dir .runtime/recorded
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
from typing import Any, Mapping
from urllib.parse import urlsplit

from . import core


# ---------------------------------------------------------------------------
# recording transport
# ---------------------------------------------------------------------------


class HTTPTransport:
    """Minimal real-HTTP transport used only while recording."""

    def __init__(self, base_url: str, timeout: float = 10.0):
        parts = urlsplit(base_url)
        if parts.scheme not in ("http", "https"):
            raise core.RecordingError("base URL must be http(s)")
        self.scheme = parts.scheme
        self.host = parts.hostname or "127.0.0.1"
        self.port = parts.port or (443 if self.scheme == "https" else 80)
        self.timeout = timeout
        self.prefix = parts.path.rstrip("/")

    def __call__(self, method: str, path: str, headers: Mapping[str, str], body: bytes):
        connection_cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
        connection = connection_cls(self.host, self.port, timeout=self.timeout)
        try:
            connection.request(method, self.prefix + path, body=body or None, headers=dict(headers))
            response = connection.getresponse()
            payload = response.read()
            return response.status, list(response.getheaders()), payload
        finally:
            connection.close()


# ---------------------------------------------------------------------------
# post-write audit
# ---------------------------------------------------------------------------


def audit_directory(directory: str, forbidden: Mapping[str, str]) -> dict[str, Any]:
    """Scan every file under ``directory`` for the observed concrete values.

    Reports labels only -- never the values themselves.
    """
    findings: list[dict[str, Any]] = []
    scanned = 0
    for root, _dirs, files in os.walk(directory):
        for name in sorted(files):
            path = os.path.join(root, name)
            scanned += 1
            with open(path, "rb") as handle:
                payload = handle.read()
            hits = core.audit_bytes(payload, forbidden)
            if hits:
                findings.append(
                    {
                        "file": os.path.relpath(path, directory),
                        "labels": sorted({forbidden[value] for value in hits}),
                    }
                )
    return {"scanned_files": scanned, "clean": not findings, "findings": findings}


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_record(args: argparse.Namespace) -> int:
    workflow = core.load_workflow(args.workflow)
    transport = HTTPTransport(args.base_url)
    recorder = core.Recorder(workflow, transport)
    try:
        result = recorder.record()
    except core.RecordingError as exc:
        print(f"record: {exc}", file=sys.stderr)
        return 2

    store = core.MaterialStore(args.out)
    try:
        store.save(result["material"], result["forbidden"])
    except core.RecordingError as exc:
        print(f"record: {exc}", file=sys.stderr)
        return 3

    # Keep the observed concrete values in memory only; scan the freshly
    # written tree before this process exits.
    audit = audit_directory(args.out, result["forbidden"])
    print(json.dumps(
        {
            "material": store.material_path,
            "material_id": result["material"]["material_id"],
            "steps": [{"name": s["name"], "produces": s["produces"]} for s in result["material"]["steps"]],
            "audit": audit,
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0 if audit["clean"] else 4


def cmd_serve(args: argparse.Namespace) -> int:
    store = core.MaterialStore(args.material_dir)
    try:
        engine = core.ReplayEngine(store)
    except core.MaterialError as exc:
        print(f"serve: {exc}", file=sys.stderr)
        return 2
    server = core.ReplayHTTPServer((args.host, args.port), engine)
    print(json.dumps({
        "listening": f"http://{args.host}:{server.server_port}",
        "material_id": engine.material["material_id"],
    }, ensure_ascii=False), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def cmd_session_list(args: argparse.Namespace) -> int:
    store = core.MaterialStore(args.material_dir)
    print(json.dumps({"sessions": store.list_sessions()}, ensure_ascii=False, indent=2))
    return 0


def cmd_session_delete(args: argparse.Namespace) -> int:
    store = core.MaterialStore(args.material_dir)
    deleted = store.delete_session(args.id)
    print(json.dumps({"session_id": args.id, "deleted": deleted}, ensure_ascii=False))
    return 0 if deleted else 1


def cmd_session_status(args: argparse.Namespace) -> int:
    store = core.MaterialStore(args.material_dir)
    engine = core.ReplayEngine(store)
    try:
        print(json.dumps(engine.session_status(args.id), ensure_ascii=False, indent=2))
    except core.ReplayError as exc:
        print(f"session status: {exc}", file=sys.stderr)
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="replaytool", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    record = sub.add_parser("record", help="execute the workflow once and store a redacted material")
    record.add_argument("--workflow", required=True)
    record.add_argument("--base-url", required=True)
    record.add_argument("--out", required=True, help="recording directory (material.json is written here)")
    record.set_defaults(func=cmd_record)

    serve = sub.add_parser("serve", help="serve offline replay for a recorded material")
    serve.add_argument("--material-dir", required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8110)
    serve.set_defaults(func=cmd_serve)

    session = sub.add_parser("session", help="inspect or clean single replay sessions")
    session_sub = session.add_subparsers(dest="session_command", required=True)
    list_cmd = session_sub.add_parser("list")
    list_cmd.add_argument("--material-dir", required=True)
    list_cmd.set_defaults(func=cmd_session_list)
    delete_cmd = session_sub.add_parser("delete")
    delete_cmd.add_argument("--material-dir", required=True)
    delete_cmd.add_argument("--id", required=True)
    delete_cmd.set_defaults(func=cmd_session_delete)
    status_cmd = session_sub.add_parser("status")
    status_cmd.add_argument("--material-dir", required=True)
    status_cmd.add_argument("--id", required=True)
    status_cmd.set_defaults(func=cmd_session_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
