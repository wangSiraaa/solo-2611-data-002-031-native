"""End-to-end demo used by the README.

Records the fixture once, shuts the fixture down, then:
  * runs two complete replay rounds and shows that ids differ per round while
    the intra-round dependencies still hold,
  * sends out-of-order and cross-session requests to show hard failures,
  * scans the recording directory and replay log for the fixture password and
    the recorded dynamic ids, and reports the check boundary.

Run from the repository root:
    python3 examples/demo.py
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.client import HTTPConnection

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from replaytool import core  # noqa: E402
from replaytool.cli import HTTPTransport, audit_directory  # noqa: E402

WORKFLOW = os.path.join(ROOT, "fixtures", "workflow.json")
SERVICE = os.path.join(ROOT, "fixtures", "service.py")
PASSWORD = "demo-secret"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(port: int, timeout: float = 8.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"port {port} did not open")


def call(port: int, method: str, path: str, headers=None, body=None):
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    payload = None if body is None else json.dumps(body).encode()
    try:
        connection.request(method, path, body=payload, headers=headers or {})
        response = connection.getresponse()
        raw = response.read()
        return response.status, (json.loads(raw.decode()) if raw else None)
    finally:
        connection.close()


def main() -> int:
    workdir = tempfile.mkdtemp(prefix="replay-demo-")
    service_port = free_port()
    replay_port = free_port()
    recorded_values: dict[str, str] = {}
    try:
        print("== record (live fixture) ==")
        service = subprocess.Popen(
            [sys.executable, SERVICE, "--port", str(service_port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        wait_for(service_port)
        workflow = core.load_workflow(WORKFLOW)
        recorder = core.Recorder(workflow, HTTPTransport(f"http://127.0.0.1:{service_port}"))
        result = recorder.record()
        store = core.MaterialStore(os.path.join(workdir, "recorded"))
        store.save(result["material"], result["forbidden"])
        # result["forbidden"] maps each concrete secret/dynamic value -> label.
        # It is used for the audit only and is never printed.
        recorded_values: dict[str, str] = dict(result["forbidden"])
        print(f"  material: {store.material_path}")
        print(f"  audit after record: {audit_directory(store.directory, recorded_values)}")
        service.terminate()
        service.wait(timeout=5)

        print("\n== start offline replay (fixture is now stopped) ==")
        engine = core.ReplayEngine(store)
        server = core.ReplayHTTPServer(("127.0.0.1", replay_port), engine)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        wait_for(replay_port)
        print(f"  listening on 127.0.0.1:{replay_port}")

        def new_session() -> str:
            status, body = call(replay_port, "POST", "/__replay/sessions")
            assert status == 201, body
            return body["session_id"]

        def run_round(title: str) -> dict:
            sid = new_session()
            status, login = call(replay_port, "POST", "/sessions", {"X-Replay-Session": sid},
                                  {"user": "demo", "password": PASSWORD})
            session = login["session"]["id"]
            status, created = call(
                replay_port, "POST", f"/sessions/{session}/reports",
                {"X-Replay-Session": sid, "Authorization": f"Bearer {session}"},
                {"period": "2026-10"},
            )
            report = created["report"]["id"]
            status, read = call(
                replay_port, "GET", f"/reports/{report}",
                {"X-Replay-Session": sid, "Authorization": f"Bearer {session}"},
            )
            print(f"  [{title}] session={session} report={report} read_total={read['total']}")
            return {"replay_session": sid, "session": session, "report": report}

        print("\n== two replay rounds ==")
        first = run_round("round 1")
        second = run_round("round 2")
        assert first["session"] != second["session"]
        assert first["report"] != second["report"]
        print("  -> ids differ between rounds, yet intra-round dependencies match")

        print("\n== failure cases ==")
        sid = new_session()
        status, body = call(replay_port, "POST", "/sessions/x/reports",
                            {"X-Replay-Session": sid, "Authorization": "Bearer x"},
                            {"period": "2026-10"})
        print(f"  out-of-order create before login -> HTTP {status}: {body['error']}")

        sid_b = new_session()
        call(replay_port, "POST", "/sessions", {"X-Replay-Session": sid_b},
             {"user": "demo", "password": PASSWORD})
        status, body = call(
            replay_port, "POST", f"/sessions/{first['session']}/reports",
            {"X-Replay-Session": sid_b, "Authorization": f"Bearer {first['session']}"},
            {"period": "2026-10"},
        )
        print(f"  cross-session foreign session id -> HTTP {status}: {body['error']}")

        server.shutdown()
        server.server_close()

        print("\n== leakage scan ==")
        # What must not be on disk: the *recorded* secrets and dynamic values
        # observed from the fixture.  Replay-generated ids are the offline
        # simulator's own output and are intentionally persisted in session
        # files so a session can be resumed; they are audited separately below.
        audit = audit_directory(store.directory, recorded_values)
        log_path = os.path.join(store.directory, "replay.log")
        print(f"  scanned {audit['scanned_files']} files in the recording dir "
              f"(material, session state and replay.log): clean={audit['clean']}")
        if not audit["clean"]:
            print(f"  findings: {audit['findings']}")

        # The password must appear nowhere, including the log.
        blob = b""
        for name in sorted(os.listdir(store.directory)):
            path = os.path.join(store.directory, name)
            if os.path.isfile(path):
                with open(path, "rb") as handle:
                    blob += handle.read()
        password_hits = blob.count(PASSWORD.encode())
        print(f"  literal password occurrences in recording dir: {password_hits}")

        # Replay-generated ids are present only as the current session's own
        # bindings, never inside material.json.
        material_bytes = open(store.material_path, "rb").read()
        replay_ids = [first["session"], first["report"], second["session"], second["report"]]
        leaked_into_material = sum(value.encode() in material_bytes for value in replay_ids)
        print(f"  replay ids found inside material.json: {leaked_into_material}")

        print("  check boundary: the scan uses the recorder's in-memory registry of")
        print("  every observed secret and dynamic value (raw and JSON-escaped) and")
        print("  covers material.json, every session-<id>.json and replay.log.  It")
        print("  does not scan the fixture's own memory (stopped before replay) or")
        print("  the OS page cache.  Static secrets are held as <redacted:LABEL>")
        print("  with no value-derived bytes; original ids are not stored at all.")
        ok = audit["clean"] and password_hits == 0 and leaked_into_material == 0
        return 0 if ok else 1
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
