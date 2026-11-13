"""End-to-end tests: record against the real fixture, replay fully offline.

Run with ``python3 -m unittest discover -s tests -v`` from the repo root.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

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


def call(port: int, method: str, path: str, headers=None, body=None) -> tuple[int, dict, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    payload = None if body is None else json.dumps(body).encode()
    try:
        connection.request(method, path, body=payload, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def json_body(raw: bytes):
    return json.loads(raw.decode()) if raw else None


class FixtureCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.service_port = free_port()
        cls.service = subprocess.Popen(
            [sys.executable, SERVICE, "--port", str(cls.service_port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        wait_for(cls.service_port)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.service.terminate()
        cls.service.wait(timeout=5)

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="replaytest-")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def record(self, out: str) -> tuple[dict, dict]:
        workflow = core.load_workflow(WORKFLOW)
        result = core.Recorder(
            workflow, HTTPTransport(f"http://127.0.0.1:{self.service_port}")
        ).record()
        store = core.MaterialStore(out)
        store.save(result["material"], result["forbidden"])
        return result["material"], result["forbidden"]


class TestRecording(FixtureCase):
    def test_material_uses_symbols_not_literal_ids(self) -> None:
        out = os.path.join(self.tmp, "rec")
        material, forbidden = self.record(out)
        text = open(os.path.join(out, "material.json"), encoding="utf-8").read()

        self.assertIn("<symbol:session>", text)
        self.assertIn("<symbol:report>", text)
        self.assertIn("<ref:session:req>", text)
        self.assertIn("<ref:report:req>", text)
        self.assertIn("<redacted:PASSWORD>", text)
        # No observed random id / password may appear anywhere.
        self.assertNotIn(PASSWORD, text)
        for value in forbidden:
            if len(value) >= 8:
                self.assertNotIn(value, text)
        self.assertNotIn('"s_', text)
        self.assertNotIn('"r_', text)

    def test_password_placeholder_has_no_value_derived_bytes(self) -> None:
        out = os.path.join(self.tmp, "rec")
        self.record(out)
        with open(os.path.join(out, "material.json"), encoding="utf-8") as handle:
            material = json.load(handle)
        password_template = material["steps"][0]["request"]["body"]["value"]["password"]
        self.assertEqual(password_template, "<redacted:PASSWORD>")
        self.assertNotIn(PASSWORD, password_template)

    def test_audit_clean(self) -> None:
        out = os.path.join(self.tmp, "rec")
        _material, forbidden = self.record(out)
        self.assertTrue(audit_directory(out, forbidden)["clean"])


class TestReplay(FixtureCase):
    def setUp(self) -> None:
        super().setUp()
        self.material_dir = os.path.join(self.tmp, "rec")
        self.record(self.material_dir)
        # Offline from here on: the fixture server is not used again.
        self.store = core.MaterialStore(self.material_dir)
        self.engine = core.ReplayEngine(self.store)
        self.port = free_port()
        self.server = core.ReplayHTTPServer(("127.0.0.1", self.port), self.engine)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        wait_for(self.port)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def new_session(self) -> str:
        status, _h, body = call(self.port, "POST", "/__replay/sessions")
        self.assertEqual(status, 201)
        return json_body(body)["session_id"]

    def run_flow(self, sid: str) -> dict:
        status, _h, body = call(self.port, "POST", "/sessions", {"X-Replay-Session": sid}, {"user": "demo", "password": PASSWORD})
        self.assertEqual(status, 201, body)
        session = json_body(body)["session"]["id"]
        status, _h, body = call(
            self.port, "POST", f"/sessions/{session}/reports",
            {"X-Replay-Session": sid, "Authorization": f"Bearer {session}"}, {"period": "2026-10"},
        )
        self.assertEqual(status, 201, body)
        report = json_body(body)["report"]["id"]
        status, _h, body = call(
            self.port, "GET", f"/reports/{report}",
            {"X-Replay-Session": sid, "Authorization": f"Bearer {session}"},
        )
        self.assertEqual(status, 200, body)
        data = json_body(body)
        self.assertEqual(data["report"]["id"], report)
        return {"session": session, "report": report, "read": data}

    def test_two_rounds_have_distinct_values_but_valid_dependencies(self) -> None:
        first = self.run_flow(self.new_session())
        second = self.run_flow(self.new_session())
        self.assertNotEqual(first["session"], second["session"])
        self.assertNotEqual(first["report"], second["report"])
        self.assertEqual(first["read"]["total"], "42.50")
        self.assertEqual(second["read"]["total"], "42.50")

    def test_out_of_order_step_fails(self) -> None:
        sid = self.new_session()
        status, _h, body = call(
            self.port, "POST", "/sessions/xxx/reports",
            {"X-Replay-Session": sid, "Authorization": "Bearer xxx"}, {"period": "2026-10"},
        )
        self.assertEqual(status, 409)
        self.assertIn("out of order", json_body(body)["error"])
        # The failed attempt must not advance state.
        self.assertEqual(self.engine.session_status(sid)["next_step"], 0)

    def test_repeated_step_fails_and_does_not_advance(self) -> None:
        sid = self.new_session()
        call(self.port, "POST", "/sessions", {"X-Replay-Session": sid}, {"user": "demo", "password": PASSWORD})
        self.assertEqual(self.engine.session_status(sid)["next_step"], 1)
        status, _h, _b = call(self.port, "POST", "/sessions", {"X-Replay-Session": sid}, {"user": "demo", "password": PASSWORD})
        self.assertEqual(status, 409)
        self.assertEqual(self.engine.session_status(sid)["next_step"], 1)

    def test_cross_session_value_rejected(self) -> None:
        a = self.run_flow(self.new_session())
        sid_b = self.new_session()
        call(self.port, "POST", "/sessions", {"X-Replay-Session": sid_b}, {"user": "demo", "password": PASSWORD})
        status, _h, _b = call(
            self.port, "POST", f"/sessions/{a['session']}/reports",
            {"X-Replay-Session": sid_b, "Authorization": f"Bearer {a['session']}"}, {"period": "2026-10"},
        )
        self.assertEqual(status, 409)

    def test_cross_session_material_mismatch(self) -> None:
        state = self.engine.new_session()
        # Copy the session into a directory whose material_id differs; the
        # session file still carries the original id, so loading must refuse.
        other = os.path.join(self.tmp, "other")
        os.makedirs(other)
        material = dict(self.engine.material)
        material["material_id"] = "mat_different"
        with open(os.path.join(other, "material.json"), "w", encoding="utf-8") as handle:
            json.dump(material, handle)
        with open(self.engine.store._session_path(state.session_id), encoding="utf-8") as handle:
            payload = json.load(handle)
        with open(os.path.join(other, f"session-{state.session_id}.json"), "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        store = core.MaterialStore(other)
        with self.assertRaises(core.ReplayError):
            store.load_session(state.session_id)

    def test_missing_dependency_is_explicit(self) -> None:
        # A hand-built material whose first request consumes an unbound symbol.
        material = json.load(open(os.path.join(self.material_dir, "material.json"), encoding="utf-8"))
        material["steps"][0]["request"]["path"] = "/sessions/<ref:session:req>"
        store = core.MaterialStore(os.path.join(self.tmp, "broken"))
        os.makedirs(store.directory)
        store.save(material, {})
        engine = core.ReplayEngine(store)
        state = engine.new_session()
        with self.assertRaises(core.ReplayError) as caught:
            engine.handle(state, "POST", "/sessions/abc", {}, b"{}")
        self.assertIn("missing dependency", str(caught.exception))

    def test_unexpected_business_field_rejected(self) -> None:
        sid = self.new_session()
        status, _h, body = call(
            self.port, "POST", "/sessions", {"X-Replay-Session": sid},
            {"user": "demo", "password": PASSWORD, "extra": 1},
        )
        self.assertEqual(status, 409)
        self.assertIn("unexpected business field", json_body(body)["error"])
        # missing vs null are different failures
        status, _h, body = call(self.port, "POST", "/sessions", {"X-Replay-Session": sid}, {"user": "demo"})
        self.assertEqual(status, 409)
        self.assertIn("missing required field", json_body(body)["error"])
        status, _h, body = call(self.port, "POST", "/sessions", {"X-Replay-Session": sid}, {"user": "demo", "password": None})
        self.assertEqual(status, 409)
        self.assertIn("non-empty string secret", json_body(body)["error"])

    def test_wrong_method_and_bad_session_header(self) -> None:
        sid = self.new_session()
        status, _h, _b = call(self.port, "GET", "/sessions", {"X-Replay-Session": sid})
        self.assertEqual(status, 409)
        status, _h, _b = call(self.port, "POST", "/sessions", {"X-Replay-Session": "nope"}, {})
        self.assertEqual(status, 400)

    def test_session_cleanup_entrypoint(self) -> None:
        sid = self.new_session()
        self.assertTrue(self.engine.reset_session(sid))
        self.assertFalse(self.engine.reset_session(sid))
        self.assertNotIn(sid, self.store.list_sessions())

    def test_replay_log_has_no_secret_or_dynamic_value(self) -> None:
        a = self.run_flow(self.new_session())
        with open(os.path.join(self.material_dir, "replay.log"), encoding="utf-8") as handle:
            log = handle.read()
        self.assertNotIn(PASSWORD, log)
        for value in (a["session"], a["report"]):
            self.assertNotIn(value, log)
        logged = [json.loads(line) for line in log.splitlines()]
        self.assertTrue(logged)
        # Only metadata keys are allowed in the log.
        allowed = {"ts", "session", "event", "step", "produces", "next_step", "completed", "detail", "status"}
        for record in logged:
            self.assertLessEqual(set(record), allowed)


if __name__ == "__main__":
    unittest.main()
