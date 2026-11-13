"""端到端测试：在后台线程启动 fixtures/sse_upstream.py，跑真实 HTTP/SSE。

覆盖：正常结束后续接、半条消息断流重连、重复去重、冲突停止、410 缺口、
换流停止，以及两个崩溃时点（提交前/提交后 os._exit）的恢复。
"""

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "fixtures" / "sse_upstream.py"


def load_fixture_handler():
    """以模块方式加载 fixture（它在 import 时解析 argparse，需要注入 argv）。"""
    sys.argv = ["sse_upstream.py", "--port", "0"]
    spec = importlib.util.spec_from_file_location("sse_upstream_fixture", FIXTURE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Handler


class FixtureServer:
    def __init__(self):
        handler = load_fixture_handler()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def url(self, case: str, client: str) -> str:
        return f"http://127.0.0.1:{self.port}/events?case={case}&client={client}"


def make_collector(url, db_path, **kw):
    from collector.client import Collector, CollectorConfig
    cfg = CollectorConfig(
        url=url,
        db_path=db_path,
        initial_backoff=0.05,
        max_backoff=0.2,
        max_attempts=kw.pop("max_attempts", 6),
        read_size=kw.pop("read_size", 3),  # 极小读取，强制跨越所有分块边界
        **kw,
    )
    return Collector(cfg)


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "coll.db")

    def tearDown(self):
        self.tmpdir.cleanup()

    def _readings(self):
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT stream_id, sequence, event_id, raw_payload FROM readings "
            "ORDER BY stream_id, sequence"
        ).fetchall()
        conn.close()
        return rows

    def test_normal_full_then_reconnect_with_checkpoint(self):
        with FixtureServer() as srv:
            c = make_collector(srv.url("normal", "c-normal"), self.db_path)
            summary = c.run()
            c.storage.close()
        self.assertEqual(summary.stop_reason, "max_attempts")
        rows = self._readings()
        self.assertEqual(len(rows), 6)
        self.assertEqual([r[1] for r in rows], [1, 2, 3, 4, 5, 6])
        # 后续请求应携带 streamA:6，服务端无数据可发，没有重复入账。
        conn = sqlite3.connect(self.db_path)
        last_req = conn.execute(
            "SELECT request_headers FROM request_log ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        conn.close()
        self.assertIn("streamA:6", last_req)

    def test_partial_half_message_then_reconnect_dedup(self):
        with FixtureServer() as srv:
            # 第一次：断在事件 4 内部（半个多字节字符、无空行）。
            c1 = make_collector(srv.url("partial", "c-part"), self.db_path,
                                max_attempts=1)
            s1 = c1.run()
            c1.storage.close()
            self.assertEqual(s1.stop_reason, "max_attempts")
            rows = self._readings()
            self.assertEqual([r[1] for r in rows], [1, 2, 3])  # 半条事件 4 未入账

            # 重连：fixture 从 4 开始重发完整事件（4/5/6），后续空连接重放 4/5/6。
            c2 = make_collector(srv.url("partial", "c-part"), self.db_path,
                                max_attempts=4)
            s2 = c2.run()
            c2.storage.close()
        rows = self._readings()
        self.assertEqual([r[1] for r in rows], [1, 2, 3, 4, 5, 6])
        # 每个序号恰好一行：半条事件未提交 + 重发去重，没有重复入账。
        self.assertEqual(len(rows), 6)
        conn = sqlite3.connect(self.db_path)
        hdr = conn.execute(
            "SELECT request_headers FROM request_log ORDER BY id LIMIT 1 OFFSET 1"
        ).fetchone()[0]
        dupes = conn.execute("SELECT duplicate_count FROM streams").fetchone()[0]
        conn.close()
        # 第二次连接必须带已提交可信断点 streamA:3。
        self.assertIn("Last-Event-ID", hdr)
        self.assertIn("streamA:3", hdr)
        # 后续空连接重放 4..6，内容一致 -> 计重复但不重复入账（去重生效的证据）。
        self.assertGreaterEqual(dupes, 1)

    def test_complete_event_resend_is_deduplicated_same_content(self):
        # 用 normal case：第一次只允许 1 次连接（拿到 1..6），第二次连接服务端
        # 依据 Last-Event-ID 不重发；为了制造"完整事件重发"，直接复用 partial 的
        # 重连机制——事件 4 的完整版本被重发，内容一致 -> 去重路径已在上面覆盖。
        # 这里再用存储层直接验证"完整消息重发"语义。
        from collector.storage import Storage
        db = Storage(self.db_path)
        payload = json.dumps(
            {"stream_id": "streamA", "sequence": 1, "value": "正常"}, ensure_ascii=False
        )
        db.save_event("streamA:1", "reading", payload, None)
        r = db.save_event("streamA:1", "reading", payload, None)
        self.assertEqual(r.outcome, "duplicate")
        db.close()

    def test_conflict_stops_stream_and_preserves_original(self):
        with FixtureServer() as srv:
            c1 = make_collector(srv.url("conflict", "c-conf"), self.db_path,
                                max_attempts=1)
            c1.run()
            c1.storage.close()
            c2 = make_collector(srv.url("conflict", "c-conf"), self.db_path,
                                max_attempts=3)
            s2 = c2.run()
            c2.storage.close()
        self.assertEqual(s2.stop_reason, "conflict")
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT raw_payload FROM readings WHERE sequence=1"
        ).fetchone()
        self.assertIn("正常", row[0])  # 原始记录未被"改写"覆盖
        state = conn.execute("SELECT state, reason FROM streams").fetchone()
        conn.close()
        self.assertEqual(state[0], "stopped_conflict")
        self.assertIn("冲突", state[1])

    def test_expired_410_sets_gap_and_manual_recovery_resumes(self):
        with FixtureServer() as srv:
            c1 = make_collector(srv.url("expired", "c-exp"), self.db_path,
                                max_attempts=1)
            c1.run()  # 拿到 1..3，断在事件 4
            c1.storage.close()
            c2 = make_collector(srv.url("expired", "c-exp"), self.db_path,
                                max_attempts=2)
            s2 = c2.run()  # 带 Last-Event-ID 重连 -> 410
            c2.storage.close()
        self.assertEqual(s2.stop_reason, "retention_gap")

        from collector.storage import Storage
        db = Storage(self.db_path)
        st = db.status()
        self.assertEqual(st["streams"][0]["state"], "stopped_gap")
        inc = st["open_incidents"][0]
        self.assertEqual(inc["kind"], "retention_gap")
        # 人工选择：接受缺口、从上游当前可用位置重拉（不带 Last-Event-ID）。
        db.recover_gap(inc["id"], accept_restart=True, abandon=False, note="演练接受")
        db.close()

        with FixtureServer() as srv:
            c3 = make_collector(srv.url("expired", "c-exp-2"), self.db_path,
                                max_attempts=2)
            s3 = c3.run()  # 新 client 名：无 410；无 Last-Event-ID -> 从 1 重发
            c3.storage.close()
        self.assertNotIn(s3.stop_reason, ("retention_gap", "conflict"))
        rows = self._readings()
        self.assertEqual([r[1] for r in rows], [1, 2, 3, 4, 5, 6])

    def test_rotation_stops_and_manual_adopt_starts_new_sequence_space(self):
        # 三个阶段必须共用同一个 fixture 实例：换流由服务端的 attempt 计数决定，
        # 新建服务会把计数归零、又回到 streamA。
        with FixtureServer() as srv:
            c1 = make_collector(srv.url("rotate", "c-rot"), self.db_path,
                                max_attempts=1)
            c1.run()  # streamA 1..3，断在 4
            c1.storage.close()
            c2 = make_collector(srv.url("rotate", "c-rot"), self.db_path,
                                max_attempts=2)
            s2 = c2.run()  # 重连 -> streamB:1，触发换流停止
            c2.storage.close()
            self.assertEqual(s2.stop_reason, "rotation")
            from collector.storage import Storage
            db = Storage(self.db_path)
            inc = db.status()["open_incidents"][0]
            self.assertEqual(inc["kind"], "rotation")
            new_stream = db.adopt_stream(inc["id"], "确认切换到 streamB")
            self.assertEqual(new_stream, "streamB")
            db.close()

            c3 = make_collector(srv.url("rotate", "c-rot"), self.db_path,
                                max_attempts=3)
            s3 = c3.run()  # 仍由 streamB 从 1 提供
            c3.storage.close()
        conn = sqlite3.connect(self.db_path)
        streams = dict(
            (r[0], r[1]) for r in conn.execute("SELECT stream_id, state FROM streams")
        )
        b_seqs = [r[0] for r in conn.execute(
            "SELECT sequence FROM readings WHERE stream_id='streamB' ORDER BY sequence"
        )]
        conn.close()
        self.assertEqual(streams["streamA"], "stopped_rotation")
        self.assertEqual(streams["streamB"], "running")
        self.assertEqual(b_seqs, [1, 2, 3, 4, 5, 6])  # 新流序号从 1 开始，不沿用旧流

    def test_retry_and_comment_blocks_create_no_readings(self):
        with FixtureServer() as srv:
            c = make_collector(srv.url("normal", "c-hb"), self.db_path,
                                max_attempts=1)
            c.run()
            c.storage.close()
        conn = sqlite3.connect(self.db_path)
        n = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        conn.close()
        self.assertEqual(n, 6)  # 心跳注释和 retry 块没有产生业务记录


class CrashRecoveryTests(unittest.TestCase):
    """用真实子进程验证 os._exit 两个时点后的恢复（模拟 SIGKILL）。"""

    def _run_subprocess(self, url, db_path, env_extra):
        env = os.environ.copy()
        env.update(env_extra)
        # 使用 CollectorConfig.from_env，使 COLLECTOR_CRASH_* 钩子在子进程生效。
        code = (
            "from collector.client import Collector, CollectorConfig;"
            "c=Collector(CollectorConfig.from_env(url=%r, db_path=%r, "
            "initial_backoff=0.05, max_backoff=0.2, max_attempts=1, read_size=3));"
            "c.run()" % (url, db_path)
        )
        return subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=60,
        )

    def test_crash_after_full_event_before_commit(self):
        with FixtureServer() as srv:
            with tempfile.TemporaryDirectory() as td:
                db = os.path.join(td, "crash1.db")
                url = srv.url("normal", "c-crash-pre")
                # 事件 2 完整接收后、提交前被杀。
                p1 = self._run_subprocess(url, db, {"COLLECTOR_CRASH_AFTER_EVENT": "2"})
                self.assertEqual(p1.returncode, 137, p1.stderr)
                conn = sqlite3.connect(db)
                seqs = [r[0] for r in conn.execute(
                    "SELECT sequence FROM readings ORDER BY sequence")]
                cp = conn.execute(
                    "SELECT last_event_id FROM streams").fetchone()[0]
                conn.close()
                self.assertEqual(seqs, [1])       # 事件 2 未提交
                self.assertEqual(cp, "streamA:1")  # 断点停在 1

                # 重启：不带崩溃钩子，断点续接，服务端从 2 重发 -> 不缺不重。
                p2 = self._run_subprocess(url, db, {})
                self.assertEqual(p2.returncode, 0, p2.stderr)
                conn = sqlite3.connect(db)
                seqs = [r[0] for r in conn.execute(
                    "SELECT sequence FROM readings ORDER BY sequence")]
                conn.close()
                self.assertEqual(seqs, [1, 2, 3, 4, 5, 6])

    def test_crash_after_commit_then_resend_dedup(self):
        with FixtureServer() as srv:
            with tempfile.TemporaryDirectory() as td:
                db = os.path.join(td, "crash2.db")
                url = srv.url("partial", "c-crash-post")
                # partial 首次连接在事件 4 断流；让进程在事件 3 提交后被杀，
                # 重启后 fixture 重发 4/5/6，同时验证已提交断点的恢复。
                p1 = self._run_subprocess(url, db, {"COLLECTOR_CRASH_AFTER_COMMIT": "3"})
                self.assertEqual(p1.returncode, 137, p1.stderr)
                conn = sqlite3.connect(db)
                seqs = [r[0] for r in conn.execute(
                    "SELECT sequence FROM readings ORDER BY sequence")]
                cp = conn.execute(
                    "SELECT last_event_id FROM streams").fetchone()[0]
                conn.close()
                self.assertEqual(seqs, [1, 2, 3])
                self.assertEqual(cp, "streamA:3")

                p2 = self._run_subprocess(url, db, {})
                self.assertEqual(p2.returncode, 0, p2.stderr)
                conn = sqlite3.connect(db)
                seqs = [r[0] for r in conn.execute(
                    "SELECT sequence FROM readings ORDER BY sequence")]
                dup = conn.execute(
                    "SELECT duplicate_count FROM streams").fetchone()[0]
                hdr = conn.execute(
                    "SELECT request_headers FROM request_log "
                    "WHERE status_code IS NOT NULL "
                    "ORDER BY id DESC LIMIT 1"
                ).fetchone()[0]
                conn.close()
                self.assertEqual(seqs, [1, 2, 3, 4, 5, 6])
                self.assertIn("streamA:3", hdr)  # 用已提交断点恢复
                # 事件 3 提交后才被杀：3 已落盘；重连后上游至少重放一次事件 4，
                # 内容一致 -> 计 1 次重复但 readings 仍只有 6 行（不重复入账）。
                self.assertGreaterEqual(dup, 1)


if __name__ == "__main__":
    unittest.main()
