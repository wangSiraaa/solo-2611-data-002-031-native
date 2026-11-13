"""End-to-end tests over real HTTP against the stdlib server.

Run with:  python3.11 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from annotation_service.__main__ import build_service
from annotation_service.server import make_server

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"

TOKENS = {"a": "fixture-a", "b": "fixture-b", "arb": "fixture-arbiter"}


class ServerHarness:
    def __init__(self, db_path: Path) -> None:
        self.service = build_service(FIXTURES, db_path)
        self.httpd = make_server("127.0.0.1", 0, self.service)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def call(
        self, who: str | None, method: str, path: str, body: object | None = None
    ) -> tuple[int, object]:
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if who is not None:
            headers["Authorization"] = f"Bearer {TOKENS[who]}"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def submit(self, who: str, item: str, labels: list[str]):
        return self.call(who, "POST", f"/api/items/{item}/submission", {"labels": labels})

    def arbitrate(self, item: str, labels: list[str], expected=None):
        body: dict = {"labels": labels}
        if expected is not None:
            body["expected_submissions"] = expected
        return self.call("arb", "POST", f"/api/items/{item}/arbitration", body)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "annotation.db"
        self.srv = ServerHarness(self.db)

    def tearDown(self) -> None:
        self.srv.stop()
        self.tmp.cleanup()

    # -- helpers -----------------------------------------------------------

    def item_view(self, who: str, item: str) -> dict:
        status, payload = self.srv.call(who, "GET", f"/api/items/{item}")
        self.assertEqual(status, 200, payload)
        return payload

    def export(self, who: str) -> dict:
        status, payload = self.srv.call(who, "GET", "/api/export")
        self.assertEqual(status, 200, payload)
        return payload

    def assert_blind(self, payload: object) -> None:
        text = json.dumps(payload, ensure_ascii=False)
        for leaked in ("product", "service", "delivery"):
            self.assertNotIn(leaked, text)

    def submit_pair(self, item: str, a_labels: list[str], b_labels: list[str]):
        self.assertEqual(self.srv.submit("a", item, a_labels)[0], 201)
        status, body = self.srv.submit("b", item, b_labels)
        self.assertEqual(status, 201, body)
        return body


class TestBlindVisibility(Base):
    def test_before_submission_nothing_leaks_anywhere(self) -> None:
        # Another annotator submits first; A has not submitted.
        self.assertEqual(self.srv.submit("b", "T01", ["product", "service"])[0], 201)

        # A's list, detail, and export must not reveal B's labels or status.
        status, listing = self.srv.call("a", "GET", "/api/items")
        self.assertEqual(status, 200)
        for entry in listing["items"]:
            self.assertIsNone(entry["own_submission"])
            self.assertEqual(entry["status"], "not_submitted")
            self.assertNotIn("peer_submission", entry)
            self.assertNotIn("peer_submitted", entry)
        self.assert_blind(listing)

        detail = self.item_view("a", "T01")
        self.assertIsNone(detail["own_submission"])
        self.assertNotIn("peer_submission", detail)
        self.assertNotIn("peer_submitted", detail)
        self.assertEqual(detail["status"], "not_submitted")
        self.assert_blind(detail)

        self.assertEqual(self.export("a")["item_count"], 0)

        # Error payloads carry no label/status state either.
        status, err = self.srv.submit("a", "NOPE", ["product"])
        self.assertIn(status, (403, 404))
        self.assertNotIn("product", json.dumps(err))

    def test_partial_submission_does_not_reveal_peer_to_arbiter(self) -> None:
        self.assertEqual(self.srv.submit("a", "T02", ["delivery"])[0], 201)

        status, listing = self.srv.call("arb", "GET", "/api/items")
        self.assertEqual(status, 200)
        self.assertEqual(listing["items"], [])

        status, queue = self.srv.call("arb", "GET", "/api/arbitration/queue")
        self.assertEqual(status, 200)
        self.assertEqual(queue["items"], [])

        status, err = self.srv.call("arb", "GET", "/api/items/T02")
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "not_ready")
        self.assertNotIn("delivery", json.dumps(err))

        # Arbitrating an incomplete item is refused, not used as a peek.
        status, err = self.srv.arbitrate("T02", ["service"])
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "not_ready")

        # B still cannot see A's labels before submitting.
        self.assert_blind(self.item_view("b", "T02"))

    def test_peer_revealed_only_after_both_submissions(self) -> None:
        self.assertEqual(self.srv.submit("a", "T03", ["service"])[0], 201)
        await_view = self.item_view("b", "T03")
        self.assert_blind(await_view)
        self.assertNotIn("peer_submission", await_view)

        self.assertEqual(self.srv.submit("b", "T03", ["service"])[0], 201)
        body = self.item_view("a", "T03")
        self.assertEqual(body["status"], "agreed")
        self.assertIn("peer_submission", body)
        self.assertEqual(body["peer_submission"]["labels"], ["service"])

        after = self.item_view("a", "T03")
        self.assertEqual(after["peer_submission"]["labels"], ["service"])
        self.assertEqual(after["final"]["source"], "agreement")

    def test_export_never_contains_unfinished_items(self) -> None:
        self.assertEqual(self.srv.submit("a", "T01", ["product"])[0], 201)
        status, body = self.srv.call("a", "GET", "/api/export")
        self.assertEqual(status, 200)
        self.assertEqual(body["items"], [])

        self.submit_pair("T02", ["delivery"], ["service"])  # disputed
        status, body = self.srv.call("a", "GET", "/api/export")
        self.assertEqual(status, 200)
        self.assertEqual(body["items"], [])


class TestSubmissions(Base):
    def test_order_insensitive_equality_and_empty_set(self) -> None:
        body = self.submit_pair(
            "T01", ["product", "delivery"], ["delivery", "product"]
        )
        self.assertEqual(body["status"], "agreed")
        self.assertEqual(body["final"]["labels"], ["delivery", "product"])

        body = self.submit_pair("T02", [], [])
        self.assertEqual(body["status"], "agreed")
        self.assertEqual(body["final"]["labels"], [])

    def test_duplicate_and_conflicting_resubmission(self) -> None:
        self.assertEqual(self.srv.submit("a", "T01", ["product"])[0], 201)
        status, replay = self.srv.submit("a", "T01", ["product"])
        self.assertEqual(status, 200)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["own_submission"]["submission_id"], 1)

        status, err = self.srv.submit("a", "T01", ["service"])
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "submission_immutable")
        view = self.item_view("a", "T01")
        self.assertEqual(view["own_submission"]["labels"], ["product"])

    def test_validation_rejections(self) -> None:
        cases = [
            ("a", "T01", {"labels": ["bogus"]}, 400, "unknown_label"),
            ("a", "T01", {"labels": ["product"], "author_id": "ann_b"}, 400, "identity_in_body"),
            ("a", "T01", {"labels": "product"}, 400, "invalid_labels"),
            ("a", "T01", {"labels": [1]}, 400, "invalid_labels"),
            ("arb", "T01", {"labels": ["product"]}, 403, "forbidden"),
        ]
        for who, item, body, code, error_code in cases:
            status, payload = self.srv.call(
                who, "POST", f"/api/items/{item}/submission", body
            )
            self.assertEqual(status, code, payload)
            self.assertEqual(payload["error"]["code"], error_code)
        self.assertIsNone(self.item_view("a", "T01")["own_submission"])

    def test_bad_tokens_are_rejected(self) -> None:
        for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": "fixture-a"}):
            req = urllib.request.Request(self.srv.base + "/api/items", headers=headers)
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=10)
            self.assertEqual(ctx.exception.code, 401)
            ctx.exception.close()

    def test_annotator_cannot_use_arbiter_endpoints(self) -> None:
        self.submit_pair("T01", ["product"], ["service"])
        for method, path in (
            ("GET", "/api/arbitration/queue"),
            ("POST", "/api/items/T01/arbitration"),
        ):
            body = {"labels": ["product"]} if method == "POST" else None
            status, err = self.srv.call("a", method, path, body)
            self.assertEqual(status, 403)

    def test_unassigned_item_is_rejected_server_side(self) -> None:
        status, err = self.srv.call("a", "GET", "/api/items/T99")
        self.assertEqual(status, 404)
        status, err = self.srv.submit("a", "T99", ["product"])
        self.assertEqual(status, 404)

    def test_removed_assignment_is_enforced_by_server(self) -> None:
        # The fixtures assign every item to both annotators, so simulate a
        # stale/misconfigured client by removing one assignment underneath the
        # running service.  Reads and writes must both fail server-side.
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "DELETE FROM assignments WHERE item_id='T01' AND user_id='ann_a'"
            )
        status, err = self.srv.call("a", "GET", "/api/items/T01")
        self.assertEqual(status, 403)
        self.assertEqual(err["error"]["code"], "not_assigned")
        status, err = self.srv.submit("a", "T01", ["product"])
        self.assertEqual(status, 403)
        self.assertEqual(err["error"]["code"], "not_assigned")
        with sqlite3.connect(self.db) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM submissions WHERE item_id='T01'"
            ).fetchone()[0]
        self.assertEqual(count, 0)


class TestArbitration(Base):
    def test_dispute_blocks_export_until_arbitrated(self) -> None:
        self.submit_pair("T01", ["product"], ["delivery"])
        view = self.item_view("b", "T01")
        self.assertEqual(view["status"], "disputed")
        self.assertIsNone(view["final"])

        status, queue = self.srv.call("arb", "GET", "/api/arbitration/queue")
        self.assertEqual(status, 200)
        self.assertEqual([i["item"]["id"] for i in queue["items"]], ["T01"])
        self.assertEqual(self.export("arb")["item_count"], 0)

        status, result = self.srv.arbitrate("T01", ["product", "delivery"])
        self.assertEqual(status, 201, result)
        self.assertEqual(result["final"]["source"], "arbitration")
        self.assertEqual(
            result["final"]["binding"],
            {
                "item_id": "T01",
                "submissions": [
                    {"submission_id": 1, "author_id": "ann_a"},
                    {"submission_id": 2, "author_id": "ann_b"},
                ],
            },
        )

        exported = self.export("arb")
        self.assertEqual(exported["item_count"], 1)
        self.assertEqual(exported["items"][0]["final"]["labels"], ["delivery", "product"])
        self.assertEqual(exported["items"][0]["final"]["binding"]["item_id"], "T01")

    def test_arbitration_replay_and_conflict(self) -> None:
        self.submit_pair("T01", ["product"], ["service"])
        status, first = self.srv.arbitrate("T01", ["product"])
        self.assertEqual(status, 201)
        self.assertEqual(first["final"]["final_id"], "arbitration:1")

        status, replay = self.srv.arbitrate("T01", ["product"])
        self.assertEqual(status, 200)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["final"]["final_id"], "arbitration:1")

        status, err = self.srv.arbitrate("T01", ["service"])
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "arbitration_immutable")

        # Still exactly one final row for the item.
        with sqlite3.connect(self.db) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM arbitrations WHERE item_id='T01'"
            ).fetchone()[0]
            final = conn.execute(
                "SELECT labels_json FROM arbitrations WHERE item_id='T01'"
            ).fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(json.loads(final), ["product"])

    def test_arbitration_refused_for_agreed_item(self) -> None:
        self.submit_pair("T01", ["product"], ["product"])
        status, err = self.srv.arbitrate("T01", ["service"])
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "already_agreed")
        self.assertEqual(self.export("arb")["items"][0]["final"]["source"], "agreement")

    def test_binding_must_match_current_submissions(self) -> None:
        self.submit_pair("T01", ["product"], ["service"])
        status, err = self.srv.arbitrate(
            "T01", ["product"], expected={"ann_a": 999, "ann_b": 2}
        )
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "stale_binding")

        ok = {"ann_a": 1, "ann_b": 2}
        status, result = self.srv.arbitrate("T01", ["product"], expected=ok)
        self.assertEqual(status, 201)

    def test_arbiter_identity_not_taken_from_body(self) -> None:
        self.submit_pair("T01", ["product"], ["service"])
        status, err = self.srv.call(
            "arb",
            "POST",
            "/api/items/T01/arbitration",
            {"labels": ["product"], "arbiter_id": "ann_a"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(err["error"]["code"], "identity_in_body")

    def test_late_annotator_cannot_change_after_arbitration(self) -> None:
        # Both already submitted (arbitration requires it); a conflicting edit
        # must still be refused.
        self.submit_pair("T01", ["product"], ["service"])
        self.assertEqual(self.srv.arbitrate("T01", ["product"])[0], 201)
        status, err = self.srv.submit("a", "T01", ["service"])
        self.assertEqual(status, 409)
        self.assertEqual(err["error"]["code"], "submission_immutable")


class TestConcurrency(Base):
    def test_concurrent_submissions_produce_single_final(self) -> None:
        barrier = threading.Barrier(2)

        def do(who: str, labels: list[str]):
            barrier.wait()
            return self.srv.submit(who, "T04", labels)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda args: do(*args), [("a", ["product"]), ("b", ["product"])]))
        self.assertEqual(sorted(r[0] for r in results), [201, 201])

        with sqlite3.connect(self.db) as conn:
            matches = conn.execute(
                "SELECT COUNT(*) FROM matches WHERE item_id='T04'"
            ).fetchone()[0]
            subs = conn.execute(
                "SELECT COUNT(*) FROM submissions WHERE item_id='T04'"
            ).fetchone()[0]
        self.assertEqual((subs, matches), (2, 1))

    def test_concurrent_identical_duplicate_submissions(self) -> None:
        barrier = threading.Barrier(6)

        def do(_):
            barrier.wait()
            return self.srv.submit("a", "T05", ["delivery"])

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(do, range(6)))
        self.assertEqual(sorted(r[0] for r in results), [200] * 5 + [201])

        with sqlite3.connect(self.db) as conn:
            subs = conn.execute(
                "SELECT COUNT(*) FROM submissions WHERE item_id='T05' AND author_id='ann_a'"
            ).fetchone()[0]
        self.assertEqual(subs, 1)

    def test_concurrent_arbitration_yields_one_final(self) -> None:
        self.submit_pair("T01", ["product"], ["service"])
        barrier = threading.Barrier(6)

        def do(i: int):
            barrier.wait()
            labels = ["product"] if i % 2 == 0 else ["service"]
            return self.srv.arbitrate("T01", labels)

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(do, range(6)))

        created = [r for r in results if r[0] == 201]
        replayed = [r for r in results if r[0] == 200]
        conflicts = [r for r in results if r[0] == 409]
        self.assertEqual(len(created), 1, results)
        self.assertEqual(len(created) + len(replayed) + len(conflicts), 6)

        with sqlite3.connect(self.db) as conn:
            rows = conn.execute(
                "SELECT labels_json FROM arbitrations WHERE item_id='T01'"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        winner = json.loads(rows[0][0])
        for status, body in results:
            if status in (200, 201):
                self.assertEqual(body["final"]["labels"], winner)
            else:
                self.assertEqual(body["error"]["code"], "arbitration_immutable")

    def test_concurrent_conflicting_submission_same_author(self) -> None:
        barrier = threading.Barrier(2)

        def do(labels: list[str]):
            barrier.wait()
            return self.srv.submit("a", "T06", labels)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(do, [["product"], ["service"]]))
        self.assertEqual(sorted(r[0] for r in results), [201, 409])

        with sqlite3.connect(self.db) as conn:
            rows = conn.execute(
                "SELECT labels_json FROM submissions WHERE item_id='T06' AND author_id='ann_a'"
            ).fetchall()
        self.assertEqual(len(rows), 1)


class TestPersistence(Base):
    def test_state_survives_restart(self) -> None:
        self.submit_pair("T01", ["product"], ["product"])  # agreed
        self.submit_pair("T02", ["product"], ["service"])  # disputed
        self.assertEqual(self.srv.arbitrate("T02", ["service"])[0], 201)
        self.assertEqual(self.srv.submit("a", "T03", ["delivery"])[0], 201)  # partial

        before_export = self.export("arb")
        self.srv.stop()

        self.srv = ServerHarness(self.db)  # restart; initialize() re-verifies chain
        after_export = self.export("arb")
        self.assertEqual(before_export["items"], after_export["items"])

        status, replay = self.srv.submit("a", "T01", ["product"])
        self.assertEqual(status, 200)
        self.assertTrue(replay["replayed"])

        status, err = self.srv.submit("a", "T01", ["service"])
        self.assertEqual(status, 409)

        status, view = self.srv.call("arb", "GET", "/api/items/T02")
        self.assertEqual(status, 200)
        self.assertEqual(view["final"]["binding"]["submissions"][0]["submission_id"], 3)
        self.assertEqual(view["final"]["binding"]["submissions"][1]["submission_id"], 4)
        self.assertEqual(
            [s["author_id"] for s in view["final"]["binding"]["submissions"]],
            ["ann_a", "ann_b"],
        )

        status, err = self.srv.arbitrate("T02", ["product"])
        self.assertEqual(status, 409)

        # Partially submitted T03 stays invisible to the arbiter after restart.
        status, listing = self.srv.call("arb", "GET", "/api/items")
        self.assertEqual(status, 200)
        self.assertEqual([i["item"]["id"] for i in listing["items"]], ["T01", "T02"])

    def test_changed_fixtures_refused_on_existing_db(self) -> None:
        self.srv.stop()
        alt = Path(self.tmp.name) / "altfix"
        alt.mkdir()
        (alt / "items.jsonl").write_text(
            (FIXTURES / "items.jsonl").read_text(encoding="utf-8"), encoding="utf-8"
        )
        (alt / "labels.json").write_text(
            (FIXTURES / "labels.json").read_text(encoding="utf-8") + "\n",
            encoding="utf-8",
        )
        (alt / "identities.json").write_text(
            (FIXTURES / "identities.json").read_text(encoding="utf-8"), encoding="utf-8"
        )
        with self.assertRaises(Exception) as ctx:
            build_service(alt, self.db)
        self.assertIn("changed", str(ctx.exception))
        self.srv = ServerHarness(self.db)


if __name__ == "__main__":
    unittest.main(verbosity=2)
