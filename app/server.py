"""HTTP entry point: POST /graphql with a JSON body {"query", "variables"?}.

Each response carries extensions.sqlCount and extensions.sql so the
per-request database cost is visible. Run:  python -m app.server
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from graphql import execute, parse, validate

from .db import RequestContext, connect
from .schema import schema


async def execute_graphql(payload: dict) -> dict:
    query = payload.get("query")
    if not isinstance(query, str):
        return {"errors": [{"message": "body must be JSON with a string 'query'"}]}

    # 1. GraphQL syntax check.
    try:
        document = parse(query)
    except Exception as exc:  # GraphQLSyntaxError
        return {"errors": [getattr(exc, "formatted", {"message": str(exc)})]}

    # 2. Validation against the fixed schema (unknown fields, missing
    #    required arguments like `first`, wrong types, ...).
    validation_errors = validate(schema, document)
    if validation_errors:
        return {"errors": [err.formatted for err in validation_errors]}

    # 3. Execution with a fresh per-request context (connection, SQL log,
    #    loader cache). Nothing is shared with previous requests.
    conn = await connect()
    try:
        ctx = RequestContext(conn)
        result = execute(
            schema,
            document,
            context_value=ctx,
            variable_values=payload.get("variables"),
            operation_name=payload.get("operationName"),
        )
        if inspect.isawaitable(result):
            result = await result
        body = result.formatted
        body["extensions"] = {"sqlCount": len(ctx.sql_log), "sql": ctx.sql_log}
        return body
    finally:
        await conn.close()


class GraphQLHandler(BaseHTTPRequestHandler):
    server_version = "LimitedGraphQL/1.0"

    def _send_json(self, status: int, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(200, {"ok": True})
        else:
            self._send_json(
                200,
                {"usage": "POST /graphql with JSON body: "
                 '{"query": "...", "variables": {...}, "operationName": "..."}'},
            )

    def do_POST(self) -> None:
        if self.path.split("?")[0] != "/graphql":
            self._send_json(404, {"errors": [{"message": "unknown path"}]})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send_json(400, {"errors": [{"message": "invalid JSON body"}]})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"errors": [{"message": "JSON body must be an object"}]})
            return
        try:
            body = asyncio.run(execute_graphql(payload))
        except Exception as exc:  # e.g. database unreachable
            self._send_json(500, {"errors": [{"message": f"internal error: {exc}"}]})
            return
        status = 200 if "errors" not in body else (400 if "data" not in body else 200)
        self._send_json(status, body)


def main() -> None:
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("127.0.0.1", port), GraphQLHandler)
    print(f"GraphQL listening on http://127.0.0.1:{port}/graphql")
    server.serve_forever()


if __name__ == "__main__":
    main()
