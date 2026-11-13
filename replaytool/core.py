"""Deterministic recording and offline replay for a fixed sequential flow.

Public surface
--------------
``load_workflow``
    Parse and validate ``workflow.json``.
``Recorder``
    Drive the real service through the declared steps and build a material.
``MaterialStore`` / ``SessionState``
    Persist material and per-replay-session symbol bindings.
``ReplayEngine`` / ``ReplayHTTPServer``
    Offline replay, strict step ordering and per-session bindings.

Material model (see ``docs/MATERIAL_FORMAT.md``)
------------------------------------------------
* Generated symbols are emitted as ``<symbol:NAME>`` at the response position
  that *produces* them, and consumed as ``<ref:NAME:req>`` / ``<ref:NAME:resp>``.
  This keeps the producer/consumer relation visible without storing the value.
* Declared secrets (password / Authorization / Cookie) become
  ``<redacted:LABEL>``: a placeholder that records *which* kind of secret was
  present and nothing derived from its bytes, so no digest or dictionary attack
  can recover the original.  Dynamic values that are not symbol-bound are
  rejected at recording time rather than silently imprinted.
* Replay of a redacted *request* placeholder is an opaque wildcard (any
  non-empty string); its original value is unrecoverable, so it cannot be
  compared offline.  Symbol placeholders are always compared against the
  current session's freshly generated value.

The module never writes request/response bodies to diagnostics.  Every file
write goes through ``write_files``, which audits the final bytes against all
observed dynamic/secret values before touching the filesystem.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

FORMAT = "sequential-http-replay"
VERSION = 1
MATERIAL_FILE = "material.json"
MAX_BODY = 1 << 20

REDACT_HEADERS_DEFAULT = frozenset({"authorization", "cookie"})

# A header whose *name* matches one of these is treated as secret even when the
# workflow did not list it explicitly.
_SECRET_HEADER_HINTS = ("token", "secret", "apikey", "api-key", "password", "auth", "cookie", "session")

_SECRET_FIELD_NAMES = frozenset(
    {
        "password", "passwd", "pwd", "secret", "token", "access_token",
        "refresh_token", "api_key", "apikey", "authorization", "cookie",
    }
)

# Transport headers that are recomputed by HTTP clients/servers and therefore
# never part of the business contract.  Business-relevant and secret headers
# (Authorization, Cookie, custom auth headers) are NOT in this set and remain
# subject to strict matching.
_TRANSPORT_HEADERS = frozenset(
    {"host", "connection", "content-length", "content-type", "accept",
     "accept-encoding", "accept-language", "accept-charset", "user-agent",
     "cache-control", "pragma", "referer", "origin", "dnt", "transfer-encoding",
     "x-replay-session", "te", "upgrade"}
)

class ToolError(Exception):
    """Base class for expected tool failures."""


class RecordingError(ToolError):
    pass


class ReplayError(ToolError):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


class MaterialError(ToolError):
    pass


# ---------------------------------------------------------------------------
# generic helpers
# ---------------------------------------------------------------------------


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    raise MaterialError(f"unsupported JSON type {type(value).__name__}")


def header_value(headers: Mapping[str, str] | list[tuple[str, str]], name: str) -> str | None:
    wanted = name.lower()
    found = None
    items = headers.items() if isinstance(headers, Mapping) else headers
    for key, value in items:
        if key.lower() == wanted:
            found = value
    return found


def _json_escaped_forms(value: str) -> set[bytes]:
    """Encodings a leaked value can take inside a JSON/text file."""
    forms = {value.encode("utf-8")}
    escaped = json.dumps(value)[1:-1]
    forms.add(escaped.encode("utf-8"))
    return forms


def audit_bytes(payload: bytes, forbidden: Mapping[str, str]) -> list[str]:
    """Return the forbidden concrete values found in ``payload``."""
    hits: list[str] = []
    for value in forbidden:
        if not value:
            continue
        if any(form in payload for form in _json_escaped_forms(value)):
            hits.append(value)
    return hits


def write_files(files: Mapping[str, bytes], forbidden: Mapping[str, str]) -> dict[str, Any]:
    """Audit then atomically write files.  Raises before writing on a hit."""
    for path, payload in files.items():
        found = audit_bytes(payload, forbidden)
        if found:
            labels = sorted({forbidden[value] for value in found})
            raise RecordingError(
                f"pre-write audit rejected {os.path.basename(path)!r}: "
                f"unredacted value(s) for {labels} are still present"
            )
    for path, payload in files.items():
        atomic_write(path, payload)
    return {
        "files": {os.path.basename(p): sha256_text(payload.decode("utf-8", "replace")) for p, payload in files.items()},
        "values_checked": len(forbidden),
    }


def atomic_write(path: str, payload: bytes) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# placeholders
# ---------------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(
    r"<(?:(?P<kind>symbol|ref):(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::(?P<side>req|resp))?"
    r"|redacted:(?P<label>[A-Za-z0-9_.-]+))>"
)


@dataclass(frozen=True)
class Placeholder:
    kind: str            # symbol | ref | redacted
    name: str            # symbol name or redacted label
    side: str            # req | resp | "" (symbol / redacted)
    start: int
    end: int


def placeholders(text: str) -> list[Placeholder]:
    if not isinstance(text, str):
        return []
    found: list[Placeholder] = []
    for match in _PLACEHOLDER_RE.finditer(text):
        if match.group("kind"):
            found.append(Placeholder(match.group("kind"), match.group("name"), match.group("side") or "", match.start(), match.end()))
        else:
            found.append(Placeholder("redacted", match.group("label"), "", match.start(), match.end()))
    return found


def single_placeholder(text: Any) -> Placeholder | None:
    if not isinstance(text, str):
        return None
    match = _PLACEHOLDER_RE.fullmatch(text)
    if not match:
        return None
    if match.group("kind"):
        return Placeholder(match.group("kind"), match.group("name"), match.group("side") or "", 0, len(text))
    return Placeholder("redacted", match.group("label"), "", 0, len(text))


def symbol_marker(name: str) -> str:
    return f"<symbol:{name}>"


def ref_marker(name: str, side: str) -> str:
    return f"<ref:{name}:{side}>"


def redacted_marker(label: str) -> str:
    """Static-secret placeholder with *no* value-derived bytes.

    An earlier draft used a truncated keyed digest.  Even a keyed digest of a
    low-entropy secret (a password) can be confirmed by an offline dictionary
    attack, so the digest was dropped: the placeholder records only *what kind
    of secret* was here, never anything derived from its bytes.  Replay treats
    the slot as an opaque wildcard (see ``Binder``).
    """
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", label) or "VALUE"
    return f"<redacted:{safe}>"


def _replace_known_values(text: Any, mapping: Mapping[str, str]) -> Any:
    """Replace longest-first concrete values with their marker."""
    if not isinstance(text, str) or not mapping:
        return text
    for value in sorted(mapping, key=len, reverse=True):
        if value and value in text:
            text = text.replace(value, mapping[value])
    return text


# ---------------------------------------------------------------------------
# workflow
# ---------------------------------------------------------------------------


def load_workflow(path: str) -> dict[str, Any]:
    with open(path, "rb") as handle:
        raw = handle.read()
    workflow = json.loads(raw)
    validate_workflow(workflow)
    return workflow


def validate_workflow(workflow: Any) -> None:
    if not isinstance(workflow, dict) or not isinstance(workflow.get("steps"), list) or not workflow["steps"]:
        raise MaterialError("workflow must be an object with a non-empty 'steps' array")
    names: set[str] = set()
    bound: set[str] = set()
    for index, step in enumerate(workflow["steps"]):
        where = f"steps[{index}]"
        for required in ("name", "method", "path"):
            if not isinstance(step.get(required), str) or not step[required]:
                raise MaterialError(f"{where}: missing non-empty string {required!r}")
        if step["name"] in names:
            raise MaterialError(f"{where}: duplicate step name {step['name']!r}")
        names.add(step["name"])
        if not isinstance(step.get("headers", {}), dict):
            raise MaterialError(f"{where}: 'headers' must be an object")
        for symbol, pointer in (step.get("response_bindings") or {}).items():
            if not isinstance(pointer, str) or not pointer.startswith("/"):
                raise MaterialError(f"{where}: binding {symbol!r} must be a JSON pointer")
            bound.add(symbol)
    for symbol in workflow.get("generated_symbols") or bound:
        if symbol not in bound:
            raise MaterialError(f"generated symbol {symbol!r} has no response binding")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", symbol):
            raise MaterialError(f"invalid symbol name {symbol!r}")


_SYMBOL_IN_TEMPLATE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def substitute_workflow_symbols(text: str, bindings: Mapping[str, str], where: str) -> str:
    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in bindings:
            raise RecordingError(
                f"{where}: ${{{name}}} is used before the response producing it"
            )
        return bindings[name]

    return _SYMBOL_IN_TEMPLATE.sub(repl, text)


def substitute_workflow_value(value: Any, bindings: Mapping[str, str], where: str) -> Any:
    if isinstance(value, str):
        return substitute_workflow_symbols(value, bindings, where)
    if isinstance(value, dict):
        return {k: substitute_workflow_value(v, bindings, f"{where}/{k}") for k, v in value.items()}
    if isinstance(value, list):
        return [substitute_workflow_value(v, bindings, f"{where}/{i}") for i, v in enumerate(value)]
    return value


# ---------------------------------------------------------------------------
# JSON pointer (RFC 6901)
# ---------------------------------------------------------------------------

_MISSING = object()


def resolve_pointer(document: Any, pointer: str) -> Any:
    """Return the value at ``pointer`` or ``_MISSING`` (distinct from null)."""
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise MaterialError(f"invalid JSON pointer {pointer!r}")
    current = document
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                return _MISSING
            current = current[token]
        elif isinstance(current, list):
            if not token.isdigit() or int(token) >= len(current):
                return _MISSING
            current = current[int(token)]
        else:
            return _MISSING
    return current


# ---------------------------------------------------------------------------
# recorder
# ---------------------------------------------------------------------------

Transport = Callable[[str, str, Mapping[str, str], bytes], tuple[int, list[tuple[str, str]], bytes]]


@dataclass
class _Forbidden:
    values: dict[str, str] = field(default_factory=dict)  # value -> label

    def add(self, value: Any, label: str) -> None:
        if isinstance(value, str) and value:
            self.values.setdefault(value, label)


def _secret_header(name: str) -> bool:
    lower = name.lower()
    return lower in REDACT_HEADERS_DEFAULT or any(hint in lower for hint in _SECRET_HEADER_HINTS)


def _auth_parts(value: str) -> tuple[str, str] | None:
    match = re.fullmatch(r"\s*([A-Za-z][A-Za-z0-9+.-]*)\s+(.+?)\s*", value, re.S)
    if match:
        return match.group(1), match.group(2)
    return None


class Recorder:
    """Execute the workflow once and return a redacted, replayable material."""

    def __init__(self, workflow: dict[str, Any], transport: Transport):
        self.workflow = workflow
        self.transport = transport
        self.steps = workflow["steps"]
        self.declared_json_secrets = set(workflow.get("redact_request_json_pointers") or [])
        self.declared_headers = {h.lower() for h in (workflow.get("redact_request_headers") or [])}

    def record(self) -> dict[str, Any]:
        bindings: dict[str, str] = {}                 # symbol -> concrete value (memory only)
        value_to_symbol: dict[str, str] = {}
        forbidden = _Forbidden()
        material_steps: list[dict[str, Any]] = []
        step_log: list[dict[str, Any]] = []

        for index, step in enumerate(self.steps):
            name = step["name"]
            where = f"step {index + 1} ({name})"

            path = substitute_workflow_symbols(step["path"], bindings, f"{where}.path")
            headers = {
                header: substitute_workflow_symbols(str(value), bindings, f"{where}.headers.{header}")
                for header, value in (step.get("headers") or {}).items()
            }
            live_body: Any = None
            body_bytes = b""
            if step.get("body") is not None:
                live_body = substitute_workflow_value(step["body"], bindings, f"{where}.body")
                body_bytes = canonical_json(live_body).encode("utf-8")
                headers.setdefault("Content-Type", "application/json")
            if body_bytes:
                headers["Content-Length"] = str(len(body_bytes))

            status, response_headers, response_bytes = self.transport(step["method"].upper(), path, headers, body_bytes)
            if status >= 400:
                raise RecordingError(f"{where} returned HTTP {status}; recording aborted")
            try:
                response_json = json.loads(response_bytes.decode("utf-8"))
                response_is_json = True
            except (UnicodeDecodeError, ValueError):
                response_json = None
                response_is_json = False

            produced: list[str] = []
            for symbol, pointer in (step.get("response_bindings") or {}).items():
                found = resolve_pointer(response_json, pointer) if response_is_json else _MISSING
                if found is _MISSING:
                    raise RecordingError(f"{where} did not expose binding {symbol!r} at {pointer!r}")
                if not isinstance(found, str) or not found:
                    raise RecordingError(f"{where} binding {symbol!r} is not a non-empty string")
                if symbol in bindings:
                    raise RecordingError(f"{where} re-binds symbol {symbol!r}")
                if found in value_to_symbol and value_to_symbol[found] != symbol:
                    raise RecordingError(
                        f"{where} produced a value already bound to symbol "
                        f"{value_to_symbol[found]!r}; ambiguous bindings are rejected"
                    )
                bindings[symbol] = found
                value_to_symbol.setdefault(found, symbol)
                forbidden.add(found, f"GENERATED.{symbol}")
                produced.append(symbol)

            # Request templates may only reference symbols produced by *earlier*
            # steps.  Passing the pre-step map prevents a coincidental collision
            # with a value produced in this same response from turning a secret
            # into a symbol reference.
            prior_symbols = {v: s for v, s in value_to_symbol.items() if s not in produced}
            request_template = self._render_request(
                step["method"].upper(), path, headers, live_body, prior_symbols, forbidden,
            )
            response_template = self._render_response(
                status, response_headers, response_bytes, response_json, response_is_json,
                produced, value_to_symbol, forbidden,
            )

            material_steps.append(
                {
                    "index": index,
                    "name": name,
                    "method": step["method"].upper(),
                    "produces": produced,
                    "request": request_template,
                    "response": response_template,
                }
            )
            step_log.append({"event": "step", "index": index, "name": name, "status": status, "produces": produced})

        material = self._build_material(material_steps)
        return {"material": material, "forbidden": forbidden.values, "step_log": step_log}

    # -- request rendering ---------------------------------------------------

    def _render_request(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        live_body: Any,
        value_to_symbol: Mapping[str, str],
        forbidden: _Forbidden,
    ) -> dict[str, Any]:
        refs = {value: ref_marker(symbol, "req") for value, symbol in value_to_symbol.items()}

        header_templates: list[dict[str, Any]] = []
        for header, value in headers.items():
            lower = header.lower()
            if lower == "content-length":
                continue
            is_secret_header = lower in self.declared_headers or _secret_header(header)
            consumed_symbol = any(known in value for known in value_to_symbol)
            if not is_secret_header and not consumed_symbol:
                # Transport-only headers (Content-Type, User-Agent, Host, ...) are
                # neither business data nor secret; they are recomputed by the
                # replay client and are not part of the match contract.
                continue
            if consumed_symbol:
                # A symbol value is consumed here: keep the association even when
                # the header itself is secret (e.g. Authorization: Bearer ${session}).
                rendered = _replace_known_values(value, refs)
                for known in value_to_symbol:
                    if known in value:
                        forbidden.add(known, f"GENERATED.{value_to_symbol[known]}")
            elif is_secret_header:
                parts = _auth_parts(value) if lower == "authorization" else None
                if parts:
                    scheme, token = parts
                    rendered = f"{scheme} {redacted_marker(header.upper())}"
                    forbidden.add(token, header.upper())
                    forbidden.add(value, header.upper())
                else:
                    rendered = redacted_marker(header.upper())
                    forbidden.add(value, header.upper())
            header_templates.append({"name": header, "value": rendered, "secret": is_secret_header})

        if live_body is not None:
            body_template = {
                "kind": "json",
                "value": self._render_json_request(
                    live_body, "", None, self.declared_json_secrets, refs, forbidden
                ),
            }
        else:
            body_template = {"kind": "empty"}

        return {"method": method, "path": _replace_known_values(path, refs), "headers": header_templates, "body": body_template}

    def _render_json_request(
        self,
        value: Any,
        pointer: str,
        name: str | None,
        declared_secrets: set[str],
        refs: Mapping[str, str],
        forbidden: _Forbidden,
    ) -> Any:
        if isinstance(value, dict):
            return {
                key: self._render_json_request(child, f"{pointer}/{key}", key, declared_secrets, refs, forbidden)
                for key, child in value.items()
            }
        if isinstance(value, list):
            return [
                self._render_json_request(child, f"{pointer}/{index}", name, declared_secrets, refs, forbidden)
                for index, child in enumerate(value)
            ]
        is_secret = pointer in declared_secrets or (name is not None and name.lower() in _SECRET_FIELD_NAMES)
        if is_secret and isinstance(value, str) and value:
            label = (name or "SECRET").upper()
            forbidden.add(value, label)
            return redacted_marker(label)
        if isinstance(value, str):
            for known in refs:
                if known in value:
                    forbidden.add(known, "GENERATED")
            return _replace_known_values(value, refs)
        return value

    # -- response rendering --------------------------------------------------

    def _render_response(
        self,
        status: int,
        response_headers: list[tuple[str, str]],
        response_bytes: bytes,
        response_json: Any,
        response_is_json: bool,
        produced: list[str],
        value_to_symbol: Mapping[str, str],
        forbidden: _Forbidden,
    ) -> dict[str, Any]:
        current = {value: symbol for value, symbol in value_to_symbol.items() if symbol in produced}
        earlier = {value: symbol for value, symbol in value_to_symbol.items() if symbol not in produced}
        current_markers = {value: symbol_marker(symbol) for value, symbol in current.items()}
        earlier_markers = {value: ref_marker(symbol, "resp") for value, symbol in earlier.items()}

        if response_is_json:
            body = {
                "kind": "json",
                "value": self._render_json_response(
                    response_json, "", None, current_markers, earlier_markers, current, earlier, forbidden
                ),
            }
        else:
            # A non-JSON body cannot be redacted structurally.  Rather than risk
            # leaking an unknown token, store only its length; replay fails
            # explicitly instead of fabricating a substitute.
            body = {"kind": "opaque", "length": len(response_bytes)}

        content_type = header_value(response_headers, "Content-Type")
        response_template = {
            "status": status,
            "headers": ([{"name": "Content-Type", "value": content_type}] if content_type else []),
            "body": body,
        }
        return response_template

    def _render_json_response(
        self,
        value: Any,
        pointer: str,
        name: str | None,
        current_markers: Mapping[str, str],
        earlier_markers: Mapping[str, str],
        current: Mapping[str, str],
        earlier: Mapping[str, str],
        forbidden: _Forbidden,
    ) -> Any:
        if isinstance(value, dict):
            return {
                key: self._render_json_response(
                    child, f"{pointer}/{key}", key, current_markers, earlier_markers, current, earlier, forbidden
                )
                for key, child in value.items()
            }
        if isinstance(value, list):
            return [
                self._render_json_response(
                    child, f"{pointer}/{index}", name, current_markers, earlier_markers, current, earlier, forbidden
                )
                for index, child in enumerate(value)
            ]
        if isinstance(value, str):
            if name is not None and name.lower() in _SECRET_FIELD_NAMES and value and value not in current and value not in earlier:
                raise RecordingError(
                    f"response field {name!r} looks like a secret but is not bound to a symbol; "
                    "bind it in response_bindings so replay can generate a fresh value"
                )
            for known in sorted(earlier, key=len, reverse=True):
                if known in value:
                    value = value.replace(known, earlier_markers[known])
                    forbidden.add(known, f"GENERATED.{earlier[known]}")
            for known in sorted(current, key=len, reverse=True):
                if known in value:
                    value = value.replace(known, current_markers[known])
                    forbidden.add(known, f"GENERATED.{current[known]}")
            return value
        return value

    # -- material ------------------------------------------------------------

    def _build_material(self, steps: list[dict[str, Any]]) -> dict[str, Any]:
        symbols = []
        for step in self.steps:
            for symbol, pointer in (step.get("response_bindings") or {}).items():
                symbols.append(
                    {
                        "name": symbol,
                        "produced_by_step": step["name"],
                        "json_pointer": pointer,
                        "response_marker": symbol_marker(symbol),
                        "request_reference": ref_marker(symbol, "req"),
                    }
                )
        generated = list(self.workflow.get("generated_symbols") or [s["name"] for s in symbols])
        # Value-free identity.  The shape hash makes the id meaningful; the
        # random suffix makes a *new recording* distinct so sessions recorded
        # against an older material cannot silently be replayed against a newer
        # one (the session file stores this id and it must match).
        shape = sha256_text(canonical_json([s["name"] for s in steps]))
        material_id = f"mat_{shape[:8]}_{uuid.uuid4().hex[:8]}"
        return {
            "format": FORMAT,
            "version": VERSION,
            "material_id": material_id,
            "created_at": utc_now(),
            "generated_symbols": generated,
            "symbols": symbols,
            "redaction_policy": {
                "request_json_pointers": sorted(self.declared_json_secrets),
                "request_headers": sorted(self.declared_headers | REDACT_HEADERS_DEFAULT),
                "static_secret_placeholder": "<redacted:LABEL> (no value-derived bytes)",
                "generated_value_placeholder": "<symbol:NAME> / <ref:NAME:req|resp>",
                "replay_semantics": {
                    "symbol": "compared against the current session's generated value",
                    "redacted": "opaque wildcard; original secret is unrecoverable",
                },
            },
            "steps": steps,
        }


# ---------------------------------------------------------------------------
# material store + replay session state
# ---------------------------------------------------------------------------


class MaterialStore:
    def __init__(self, directory: str):
        self.directory = directory
        self.material_path = os.path.join(directory, MATERIAL_FILE)
        self._material: dict[str, Any] | None = None

    def exists(self) -> bool:
        return os.path.exists(self.material_path)

    def load(self) -> dict[str, Any]:
        if self._material is None:
            if not self.exists():
                raise MaterialError(f"no recorded material at {self.material_path}")
            with open(self.material_path, encoding="utf-8") as handle:
                material = json.load(handle)
            validate_material(material)
            self._material = material
        return self._material

    def save(self, material: dict[str, Any], forbidden: Mapping[str, str]) -> dict[str, Any]:
        validate_material(material)
        payload = (json.dumps(material, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        return write_files({self.material_path: payload}, forbidden)

    @property
    def material_id(self) -> str:
        return self.load()["material_id"]

    def _session_path(self, session_id: str) -> str:
        return os.path.join(self.directory, f"session-{session_id}.json")

    def list_sessions(self) -> list[str]:
        if not os.path.isdir(self.directory):
            return []
        names = []
        for name in sorted(os.listdir(self.directory)):
            if name.startswith("session-") and name.endswith(".json"):
                names.append(name[len("session-"):-len(".json")])
        return names

    def save_session(self, state: "SessionState") -> None:
        state.updated_at = utc_now()
        payload = (json.dumps(state.to_json(), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        atomic_write(self._session_path(state.session_id), payload)

    def load_session(self, session_id: str) -> "SessionState":
        if not re.fullmatch(r"[a-f0-9]{8,64}", session_id):
            raise ReplayError("malformed replay session id", 400)
        path = self._session_path(session_id)
        if not os.path.exists(path):
            raise ReplayError(f"unknown replay session {session_id!r}", 404)
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return SessionState.from_json(self.load(), data, self.directory)

    def delete_session(self, session_id: str) -> bool:
        path = self._session_path(session_id)
        if os.path.exists(path):
            os.unlink(path)
            return True
        return False


def validate_material(material: Any) -> None:
    if not isinstance(material, dict) or material.get("format") != FORMAT or material.get("version") != VERSION:
        raise MaterialError("unsupported material format or version")
    steps = material.get("steps")
    if not isinstance(steps, list) or not steps:
        raise MaterialError("material has no steps")
    for index, step in enumerate(steps):
        for required in ("name", "method", "request", "response"):
            if required not in step:
                raise MaterialError(f"material step {index} lacks {required!r}")
    # Every generated symbol must be *produced* by a step response.  Checking the
    # whole document would be fooled by the symbols[] description block, so the
    # search is restricted to the steps themselves.
    for symbol in material.get("generated_symbols") or []:
        producers = [s for s in steps if symbol in (s.get("produces") or [])]
        if not producers:
            raise MaterialError(f"generated symbol {symbol!r} is declared but no step produces it")
        if symbol_marker(symbol) not in json.dumps(producers[0]["response"], ensure_ascii=False):
            raise MaterialError(
                f"generated symbol {symbol!r} does not appear as {symbol_marker(symbol)} "
                "in the response that produces it; a random identifier must not be "
                "stored as a business literal"
            )


@dataclass
class SessionState:
    material: dict[str, Any]
    session_id: str
    directory: str
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    next_step: int = 0
    completed: bool = False
    symbols: dict[str, str] = field(default_factory=dict)
    dependencies: dict[str, str] = field(default_factory=dict)

    @property
    def steps(self) -> list[dict[str, Any]]:
        return self.material["steps"]

    def current_step(self) -> dict[str, Any] | None:
        if self.completed or self.next_step >= len(self.steps):
            return None
        return self.steps[self.next_step]

    def to_json(self) -> dict[str, Any]:
        return {
            "format": "sequential-http-replay/session",
            "version": VERSION,
            "session_id": self.session_id,
            "material_id": self.material["material_id"],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "next_step": self.next_step,
            "completed": self.completed,
            "symbols": dict(self.symbols),
            "dependencies": dict(self.dependencies),
        }

    @classmethod
    def from_json(cls, material: dict[str, Any], data: dict[str, Any], directory: str) -> "SessionState":
        if data.get("material_id") != material["material_id"]:
            raise ReplayError("session belongs to a different recorded material", 409)
        return cls(
            material=material,
            session_id=data["session_id"],
            directory=directory,
            created_at=data.get("created_at", utc_now()),
            updated_at=data.get("updated_at", utc_now()),
            next_step=int(data.get("next_step", 0)),
            completed=bool(data.get("completed", False)),
            symbols=dict(data.get("symbols") or {}),
            dependencies=dict(data.get("dependencies") or {}),
        )


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------


class Binder:
    """Template-vs-actual comparison for one step, with a fresh binding scope."""

    def __init__(self, state: SessionState):
        self.state = state

    def resolve(self, placeholder: Placeholder) -> str | None:
        if placeholder.kind in ("symbol", "ref"):
            if placeholder.name not in self.state.symbols:
                raise ReplayError(
                    f"missing dependency: symbol {placeholder.name!r} is not bound for this session "
                    f"(next step is {self.state.current_step()['name']!r})"
                )
            if placeholder.kind == "ref" and placeholder.side == "resp":
                raise ReplayError(
                    f"{ref_marker(placeholder.name, 'resp')} refers to a response value that is not "
                    "materialisable at this point"
                )
            return self.state.symbols[placeholder.name]
        if placeholder.kind == "redacted":
            return None  # opaque wildcard
        raise MaterialError(f"unknown placeholder kind {placeholder.kind!r}")

    def compare_string(self, template: str, actual: str, where: str) -> None:
        found = placeholders(template)
        if not found:
            if template != actual:
                raise ReplayError(f"{where} does not match the recorded step")
            return
        pattern: list[str] = []
        cursor = 0
        symbols_in_template = []
        for placeholder in found:
            pattern.append(re.escape(template[cursor:placeholder.start]))
            value = self.resolve(placeholder)
            if value is None:
                pattern.append(".+?")
            else:
                pattern.append(re.escape(value))
                symbols_in_template.append(placeholder.name)
            cursor = placeholder.end
        pattern.append(re.escape(template[cursor:]))
        if re.fullmatch("".join(pattern), actual) is None:
            detail = ""
            if symbols_in_template:
                detail = (
                    f" (the value must equal this session's binding for "
                    f"{', '.join(symbols_in_template)}; values from another replay "
                    "session are rejected)"
                )
            raise ReplayError(f"{where} does not match the recorded step for the current session{detail}")

    def compare_value(self, template: Any, actual: Any, where: str) -> None:
        marker = single_placeholder(template)
        if marker is not None:
            if marker.kind == "redacted":
                if not isinstance(actual, str) or not actual:
                    raise ReplayError(f"{where} must be a non-empty string secret")
                return
            expected = self.resolve(marker)
            if not isinstance(actual, str):
                raise ReplayError(f"{where} expected a string bound from {marker.name!r}, got {json_type(actual)}")
            if actual != expected:
                raise ReplayError(f"{where} is not the value bound to {marker.name!r} for this session")
            return
        if placeholders(template):
            if not isinstance(actual, str):
                raise ReplayError(f"{where} expected a string, got {json_type(actual)}")
            self.compare_string(template, actual, where)
            return
        if isinstance(template, dict):
            if not isinstance(actual, dict):
                raise ReplayError(f"{where} expected object, got {json_type(actual)}")
            missing = sorted(set(template) - set(actual))
            if missing:
                raise ReplayError(f"{where} is missing required field(s) {missing}")
            unexpected = sorted(set(actual) - set(template))
            if unexpected:
                raise ReplayError(f"{where} has unexpected business field(s) {unexpected}")
            for key, child in template.items():
                self.compare_value(child, actual[key], f"{where}/{key}")
            return
        if isinstance(template, list):
            if not isinstance(actual, list):
                raise ReplayError(f"{where} expected array, got {json_type(actual)}")
            if len(template) != len(actual):
                raise ReplayError(f"{where} array length differs from the recorded step")
            for index, child in enumerate(template):
                self.compare_value(child, actual[index], f"{where}/{index}")
            return
        if json_type(template) != json_type(actual) or template != actual:
            raise ReplayError(
                f"{where} literal differs (recorded {json_type(template)}, actual {json_type(actual)})"
            )

    def compare_headers(self, templates: list[dict[str, Any]], actual: Mapping[str, str], where: str) -> None:
        expected = {entry["name"].lower() for entry in templates}
        for entry in templates:
            value = header_value(actual, entry["name"])
            if value is None:
                raise ReplayError(f"{where}: required header {entry['name']!r} is missing")
            self.compare_string(entry["value"], value, f"{where} header {entry['name']}")
        extras = sorted({name.lower() for name in actual} - expected - _TRANSPORT_HEADERS)
        if extras:
            raise ReplayError(f"{where}: unexpected header(s) {extras}")


# ---------------------------------------------------------------------------
# replay engine
# ---------------------------------------------------------------------------

def generate_symbol_value(symbol: str, existing: Mapping[str, str]) -> str:
    """Fresh per-session value; format hints at the symbol, never the recording."""
    prefix = re.sub(r"[^a-z0-9]", "", symbol.lower())[:1] or "v"
    while True:
        value = f"{prefix}_{uuid.uuid4().hex}"
        if value not in existing.values():
            return value


class ReplayEngine:
    def __init__(self, store: MaterialStore, log_path: str | None = None):
        self.store = store
        self.material = store.load()
        self.log_path = log_path or os.path.join(store.directory, "replay.log")

    # -- sessions ------------------------------------------------------------

    def new_session(self) -> SessionState:
        for _ in range(8):
            session_id = uuid.uuid4().hex[:16]
            if session_id not in self.store.list_sessions():
                break
        state = SessionState(material=self.material, session_id=session_id, directory=self.store.directory)
        self.store.save_session(state)
        self.log(session_id, "session_created", step=self._step_name(state))
        return state

    def reset_session(self, session_id: str) -> bool:
        deleted = self.store.delete_session(session_id)
        self.log(session_id, "session_cleaned" if deleted else "session_clean_noop")
        return deleted

    def session_status(self, session_id: str) -> dict[str, Any]:
        state = self.store.load_session(session_id)
        step = state.current_step()
        return {
            "session_id": state.session_id,
            "material_id": self.material["material_id"],
            "next_step": state.next_step,
            "next_step_name": step["name"] if step else None,
            "completed": state.completed,
            "symbols_bound": sorted(state.symbols),
            "dependencies": dict(state.dependencies),
        }

    def log(self, session_id: str, event: str, step: str | None = None, **extra: Any) -> None:
        record = {"ts": utc_now(), "session": session_id, "event": event}
        if step:
            record["step"] = step
        record.update(extra)
        line = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        os.makedirs(os.path.dirname(self.log_path) or ".", exist_ok=True)
        with open(self.log_path, "ab") as handle:
            handle.write(line)

    @staticmethod
    def _step_name(state: SessionState) -> str | None:
        step = state.current_step()
        return step["name"] if step else None

    # -- replay --------------------------------------------------------------

    def handle(self, state: SessionState, method: str, path: str, headers: Mapping[str, str], body: bytes) -> tuple[int, dict[str, str], bytes]:
        step = state.current_step()
        if step is None:
            raise ReplayError("the workflow for this session is already complete", 409)
        request = step["request"]

        if method.upper() != request["method"].upper():
            raise ReplayError(
                f"out of order: expected {step['name']!r} ({request['method']} {request['path']}), "
                f"got {method.upper()}",
                409,
            )

        binder = Binder(state)
        actual_path = self._normalise_path(path)

        # Diagnose order mistakes by recognisable route *shape* first, so the
        # caller is told which step is actually expected rather than only that
        # the current step did not match.
        attempted = self._route_shape_match(state, method, actual_path)
        expected_name = step["name"]
        if attempted is not None and attempted != expected_name:
            raise ReplayError(
                f"out of order: the request looks like step {attempted!r}, but the next "
                f"step for this session is {expected_name!r} "
                f"({request['method']} {request['path']})"
            )

        binder.compare_string(request["path"], actual_path, "path")
        binder.compare_headers(request["headers"], headers, "request")

        if request["body"]["kind"] == "json":
            try:
                actual_body = json.loads(body.decode("utf-8")) if body else _MISSING
            except (UnicodeDecodeError, ValueError):
                raise ReplayError("request body is not valid JSON", 400) from None
            if actual_body is _MISSING:
                raise ReplayError("request body is required for this step", 400)
            binder.compare_value(request["body"]["value"], actual_body, "body")
        elif request["body"]["kind"] == "empty":
            if body:
                raise ReplayError("unexpected request body", 409)

        status, response_headers, payload = self._materialise_response(state, step)
        self._check_response_shape(step, payload)
        state.next_step += 1
        state.completed = state.next_step >= len(state.steps)
        self.store.save_session(state)
        self.log(
            state.session_id,
            "step_completed",
            step=step["name"],
            produces=step.get("produces") or [],
            next_step=state.next_step,
            completed=state.completed,
        )
        return status, response_headers, payload

    @staticmethod
    def _normalise_path(path: str) -> str:
        return urlsplit(path).path

    def _route_shape_match(self, state: SessionState, method: str, actual_path: str) -> str | None:
        """Return the name of a step whose path shape matches this request.

        Shape matching only replaces placeholder values with a wildcard.  It is
        used *solely* for better ordering diagnostics; it never selects the
        response to return.
        """
        for candidate in state.steps:
            request = candidate["request"]
            if request["method"].upper() != method.upper():
                continue
            pattern = self._shape_pattern(request["path"])
            if re.fullmatch(pattern, actual_path):
                return candidate["name"]
        return None

    @staticmethod
    def _shape_pattern(template: str) -> str:
        pattern: list[str] = []
        cursor = 0
        for placeholder in placeholders(template):
            pattern.append(re.escape(template[cursor:placeholder.start]))
            pattern.append("[^/]+")
            cursor = placeholder.end
        pattern.append(re.escape(template[cursor:]))
        return "".join(pattern)

    def _materialise_response(self, state: SessionState, step: dict[str, Any]) -> tuple[int, dict[str, str], bytes]:
        # Expand against a scratch copy of the bindings, committing only after
        # the materialised payload has passed its shape check.  A failure must
        # not leave half-produced symbols behind for a retry.
        scratch_symbols = dict(state.symbols)
        for symbol in step.get("produces") or []:
            if symbol in scratch_symbols:
                raise ReplayError(f"symbol {symbol!r} was already produced for this session")
            scratch_symbols[symbol] = generate_symbol_value(symbol, scratch_symbols)

        response = step["response"]
        if response["body"]["kind"] != "json":
            raise ReplayError(
                f"step {step['name']!r} recorded a non-JSON response body that cannot be "
                "replayed offline without leaking or inventing content"
            )
        scratch_state = SessionState(
            material=state.material,
            session_id=state.session_id,
            directory=state.directory,
            symbols=scratch_symbols,
        )
        try:
            body = self._expand_json(response["body"]["value"], scratch_state)
        except ReplayError as exc:
            raise ReplayError(f"step {step['name']!r} response cannot be materialised: {exc}") from None
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self._check_response_shape(step, payload)

        for symbol in step.get("produces") or []:
            state.symbols[symbol] = scratch_symbols[symbol]
            state.dependencies[symbol] = step["name"]
        headers = {entry["name"]: entry["value"] for entry in response.get("headers") or []}
        return int(response["status"]), headers, payload

    def _check_response_shape(self, step: dict[str, Any], payload: bytes) -> None:
        """Assert the freshly materialised response still has the recorded shape.

        This guards against an expansion bug (for example a symbol producing a
        non-string) being served as if it were valid.
        """
        recorded = step["response"]["body"]
        if recorded["kind"] != "json":
            return
        try:
            actual = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ReplayError(f"materialised response for step {step['name']!r} is not JSON") from None

        def shape(template: Any, value: Any, where: str) -> None:
            if isinstance(template, dict):
                if not isinstance(value, dict) or set(template) != set(value):
                    raise ReplayError(f"materialised response {where} changed object keys")
                for key in template:
                    shape(template[key], value[key], f"{where}/{key}")
                return
            if isinstance(template, list):
                if not isinstance(value, list) or len(template) != len(value):
                    raise ReplayError(f"materialised response {where} changed array length")
                for index, child in enumerate(template):
                    shape(child, value[index], f"{where}/{index}")
                return
            if isinstance(template, str) and placeholders(template):
                if not isinstance(value, str):
                    raise ReplayError(f"materialised response {where} is not a string symbol")
                return
            if json_type(template) != json_type(value):
                raise ReplayError(f"materialised response {where} changed JSON type")

        shape(recorded["value"], actual, "body")

    def _expand_json(self, value: Any, state: SessionState) -> Any:
        if isinstance(value, dict):
            return {key: self._expand_json(child, state) for key, child in value.items()}
        if isinstance(value, list):
            return [self._expand_json(child, state) for child in value]
        if isinstance(value, str):
            return self._expand_text(value, state)
        return value

    def _expand_text(self, template: str, state: SessionState) -> str:
        output: list[str] = []
        cursor = 0
        for placeholder in placeholders(template):
            output.append(template[cursor:placeholder.start])
            if placeholder.kind in ("symbol", "ref"):
                if placeholder.name not in state.symbols:
                    raise ReplayError(f"{placeholder.name!r} is not bound for this session")
                output.append(state.symbols[placeholder.name])
            else:
                raise ReplayError(
                    f"response material contains {redacted_marker(placeholder.name)}; "
                    "a redacted secret cannot be reconstructed"
                )
            cursor = placeholder.end
        output.append(template[cursor:])
        return "".join(output)


# ---------------------------------------------------------------------------
# replay HTTP server
# ---------------------------------------------------------------------------


class ReplayHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], engine: ReplayEngine):
        super().__init__(address, _ReplayHandler)
        self.engine = engine


class _ReplayHandler(BaseHTTPRequestHandler):
    server: ReplayHTTPServer
    protocol_version = "HTTP/1.1"

    # -- plumbing ------------------------------------------------------------

    def _read_body(self) -> bytes:
        raw = self.headers.get("Content-Length")
        if raw is None:
            return b""
        try:
            length = int(raw)
        except ValueError:
            raise ReplayError("invalid Content-Length", 400) from None
        if length < 0 or length > MAX_BODY:
            raise ReplayError("request body too large", 413)
        return self.rfile.read(length)

    def _send_json(self, status: int, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_payload(self, status: int, headers: Mapping[str, str], payload: bytes) -> None:
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args: Any) -> None:
        pass

    # -- dispatch ------------------------------------------------------------

    def _dispatch(self, method: str) -> None:
        engine = self.server.engine
        try:
            body = self._read_body()
        except ReplayError as exc:
            self._send_json(exc.status, {"error": str(exc)})
            return

        path = self.path.split("?", 1)[0]
        if path == "/__replay/health":
            self._send_json(200, {"status": "ok", "material_id": engine.material["material_id"]})
            return
        if path == "/__replay/sessions":
            if method != "POST":
                self._send_json(405, {"error": "use POST to create a replay session"})
                return
            state = engine.new_session()
            self._send_json(201, engine.session_status(state.session_id))
            return
        if path.startswith("/__replay/sessions/"):
            tail = path[len("/__replay/sessions/"):]
            session_id, _, action = tail.partition("/")
            if not re.fullmatch(r"[a-f0-9]{8,64}", session_id):
                self._send_json(400, {"error": "malformed replay session id"})
                return
            if action:
                self._send_json(404, {"error": "unknown admin action"})
                return
            if method == "GET":
                try:
                    self._send_json(200, engine.session_status(session_id))
                except ReplayError as exc:
                    self._send_json(exc.status, {"error": str(exc)})
                return
            if method == "DELETE":
                deleted = engine.reset_session(session_id)
                self._send_json(200, {"deleted": deleted, "session_id": session_id})
                return
            self._send_json(405, {"error": "use GET or DELETE"})
            return
        if path.startswith("/__replay/"):
            self._send_json(404, {"error": "unknown replay admin endpoint"})
            return

        session_id = self.headers.get("X-Replay-Session")
        try:
            if not session_id:
                raise ReplayError("missing X-Replay-Session header", 400)
            if not re.fullmatch(r"[a-f0-9]{8,64}", session_id):
                raise ReplayError("malformed X-Replay-Session header", 400)
            state = engine.store.load_session(session_id)
            actual_headers = {key: self.headers.get(key) for key in self.headers.keys()}
            status, headers, payload = engine.handle(state, method, self.path, actual_headers, body)
        except ReplayError as exc:
            engine.log(session_id or "<none>", "rejected", detail=str(exc), status=exc.status)
            self._send_json(exc.status, {"error": str(exc)})
            return
        except MaterialError:
            engine.log(session_id or "<none>", "rejected", detail="material error", status=500)
            self._send_json(500, {"error": "material error"})
            return
        except Exception as exc:  # noqa: BLE001
            engine.log(session_id or "<none>", "error", detail=type(exc).__name__, status=500)
            self._send_json(500, {"error": "internal replay error"})
            return
        self._send_payload(status, headers, payload)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")
