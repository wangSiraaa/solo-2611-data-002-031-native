"""存储层单元测试：事务断点、去重、冲突、换流、缺口、人工恢复。"""

import json
import tempfile
import unittest

from collector.storage import Storage


def event(stream="streamA", seq=1, value="正常", event_type="reading"):
    data = json.dumps(
        {"stream_id": stream, "sequence": seq, "value": value},
        ensure_ascii=False,
    )
    return f"{stream}:{seq}", event_type, data


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.db = Storage(self.tmp.name)

    def tearDown(self):
        self.db.close()

    def _save(self, stream="streamA", seq=1, value="正常"):
        eid, etype, data = event(stream, seq, value)
        return self.db.save_event(eid, etype, data, None)

    def test_insert_advances_checkpoint_in_same_commit(self):
        r = self._save(seq=1)
        self.assertEqual(r.outcome, "inserted")
        s = self.db.get_stream("streamA")
        self.assertEqual(s["state"], "running")
        self.assertEqual(s["last_event_id"], "streamA:1")
        self.assertEqual(s["last_sequence"], 1)
        self.assertEqual(self.db.checkpoint_for(), "streamA:1")

    def test_duplicate_same_content_deduped_no_second_row(self):
        self._save(seq=1)
        r = self._save(seq=1)
        self.assertEqual(r.outcome, "duplicate")
        s = self.db.get_stream("streamA")
        self.assertEqual(s["duplicate_count"], 1)
        # 断点不回退也不重复推进；记录仍只有一行。
        self.assertEqual(s["last_event_id"], "streamA:1")
        n = self.db.conn.execute("SELECT COUNT(*) c FROM readings").fetchone()["c"]
        self.assertEqual(n, 1)

    def test_duplicate_with_different_whitespace_still_deduped(self):
        eid, etype, data = event(seq=1)
        self.db.save_event(eid, etype, data, None)
        # 语义相同但缩进不同（模拟上游重发时序列化差异）。
        compact = json.dumps(
            {"stream_id": "streamA", "sequence": 1, "value": "正常"},
            ensure_ascii=False, separators=(",", ":"),
        )
        r = self.db.save_event("streamA:1", "reading", compact, None)
        self.assertEqual(r.outcome, "duplicate")

    def test_conflict_keeps_original_and_stops_stream(self):
        self._save(seq=1, value="正常")
        r = self._save(seq=1, value="改写")
        self.assertEqual(r.outcome, "conflict")
        s = self.db.get_stream("streamA")
        self.assertEqual(s["state"], "stopped_conflict")
        # 原始记录未被覆盖。
        row = self.db.conn.execute(
            "SELECT raw_payload FROM readings WHERE stream_id='streamA' AND sequence=1"
        ).fetchone()
        self.assertIn("正常", row["raw_payload"])
        # 阻塞后拒绝继续写入。
        r2 = self._save(seq=2)
        self.assertEqual(r2.outcome, "blocked")

    def test_conflict_resolution_then_resend_counted_as_duplicate(self):
        self._save(seq=1, value="正常")
        r = self._save(seq=1, value="改写")
        self.db.resolve_conflict(r.incident_id, "人工确认保留原值")
        s = self.db.get_stream("streamA")
        self.assertEqual(s["state"], "running")
        # 上游继续重发冲突内容：不再阻塞，计重复；随后的新事件可入账。
        again = self._save(seq=1, value="改写")
        self.assertEqual(again.outcome, "duplicate")
        nxt = self._save(seq=2)
        self.assertEqual(nxt.outcome, "inserted")

    def test_sequence_gap_is_detected_and_stops(self):
        self._save(seq=1)
        r = self._save(seq=3)  # 跳过 2
        self.assertEqual(r.outcome, "gap")
        self.assertEqual(self.db.get_stream("streamA")["state"], "stopped_gap")

    def test_stream_rotation_does_not_reuse_sequence_space(self):
        self._save(stream="streamA", seq=1)
        r = self._save(stream="streamB", seq=1)
        self.assertEqual(r.outcome, "rotation")
        self.assertEqual(self.db.get_stream("streamA")["state"], "stopped_rotation")
        # adopt 后新流独立计数，从 1 开始。
        new_stream = self.db.adopt_stream(r.incident_id, "确认换流")
        self.assertEqual(new_stream, "streamB")
        r2 = self._save(stream="streamB", seq=1)
        self.assertEqual(r2.outcome, "inserted")
        self.assertEqual(self.db.get_stream("streamB")["last_sequence"], 1)

    def test_410_retention_gap_stops_and_manual_recovery(self):
        self._save(seq=1)
        self._save(seq=2)
        inc_id = self.db.record_retention_gap("streamA:2", '{"error":"retention_gap"}')
        s = self.db.get_stream("streamA")
        self.assertEqual(s["state"], "stopped_gap")
        # 不能偷偷续接：状态必须明确。
        self.assertIn("410", s["reason"])
        with self.assertRaises(ValueError):
            self.db.recover_gap(inc_id, accept_restart=False, abandon=False, note="")
        self.db.recover_gap(inc_id, accept_restart=True, abandon=False, note="接受缺口")
        s = self.db.get_stream("streamA")
        self.assertEqual(s["state"], "running")
        self.assertIsNone(s["last_event_id"])  # 断点清空，重连不带 Last-Event-ID
        # 重拉到的旧序号内容一致 -> 去重；新序号继续。
        self.assertEqual(self._save(seq=1).outcome, "duplicate")
        self.assertEqual(self._save(seq=2).outcome, "duplicate")
        self.assertEqual(self._save(seq=3).outcome, "inserted")

    def test_abandon_keeps_stream_stopped(self):
        self._save(seq=1)
        inc_id = self.db.record_retention_gap("streamA:1", "{}")
        self.db.recover_gap(inc_id, accept_restart=False, abandon=True, note="放弃")
        self.assertEqual(self.db.get_stream("streamA")["state"], "stopped_gap")

    def test_malformed_json_is_incident_not_a_reading(self):
        r = self.db.save_event("streamA:1", "reading", "{not json", None)
        self.assertEqual(r.outcome, "malformed")
        n = self.db.conn.execute("SELECT COUNT(*) c FROM readings").fetchone()["c"]
        self.assertEqual(n, 0)

    def test_event_id_mismatch_with_payload_rejected(self):
        data = json.dumps({"stream_id": "streamA", "sequence": 2, "value": "x"})
        r = self.db.save_event("streamA:9", "reading", data, None)
        self.assertEqual(r.outcome, "malformed")
        self.assertIn("不一致", r.detail)

    def test_checkpoint_only_returns_committed_running_stream(self):
        self.assertIsNone(self.db.checkpoint_for())
        self._save(seq=1)
        self.assertEqual(self.db.checkpoint_for(), "streamA:1")
        inc = self.db.record_retention_gap("streamA:1", "{}")
        self.assertIsNone(self.db.checkpoint_for())
        self.db.recover_gap(inc, accept_restart=True, abandon=False, note="ok")
        self.assertIsNone(self.db.checkpoint_for())  # 接受重启后断点为空

    def test_status_contains_required_fields(self):
        self._save(seq=1)
        self._save(seq=1)  # 重复
        st = self.db.status()
        (s,) = st["streams"]
        for key in ("stream_id", "state", "last_event_id", "last_sequence",
                    "duplicate_count", "reason"):
            self.assertIn(key, s)
        self.assertEqual(s["duplicate_count"], 1)

    def test_request_log_records_headers_including_last_event_id(self):
        rid = self.db.log_request_start(
            "http://x/events", {"Last-Event-ID": "streamA:3", "Accept": "text/event-stream"}
        )
        self.db.log_request_end(rid, 200, "stream_ended", 3, 1)
        row = self.db.conn.execute("SELECT * FROM request_log WHERE id=?", (rid,)).fetchone()
        headers = json.loads(row["request_headers"])
        self.assertEqual(headers["Last-Event-ID"], "streamA:3")
        self.assertEqual(row["events_saved"], 3)

    def test_rollback_on_error_leaves_no_partial_state(self):
        self._save(seq=1)
        before = self.db.status()["counts"]
        # 制造一个会在事务内失败的调用（incident id 不存在）。
        with self.assertRaises(ValueError):
            self.db.resolve_conflict(999, "x")
        after = self.db.status()["counts"]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
