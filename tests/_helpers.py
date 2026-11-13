"""测试公共夹具：独立测试库、重置、参考快照。"""

from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402

from depgraph import db, repo  # noqa: E402

ADMIN_DSN = os.environ.get(
    "DEPGRAPH_ADMIN_DSN",
    "host=/tmp/pg-task port=55432 dbname=postgres user=node",
)
TEST_DSN = os.environ.get(
    "DEPGRAPH_TEST_DSN",
    "host=/tmp/pg-task port=55432 dbname=depgraph_test user=node",
)
TEST_DB = TEST_DSN.split("dbname=")[1].split()[0]


def connect_with_recovery_retry(
    dsn: str, attempts: int = 60, delay: float = 0.5
) -> psycopg.Connection:
    """连接侧容忍集群短暂恢复/重启（崩溃演练或外部 OOM 都可能触发）。"""
    transient = (
        "recovery mode",
        "the database system is",
        "server closed the connection",
        "connection refused",
        "could not connect",
        "starting up",
    )
    last_exc: Exception | None = None
    for _ in range(attempts):
        try:
            return db.connect(dsn)
        except psycopg.OperationalError as exc:
            last_exc = exc
            if not any(flag in str(exc) for flag in transient):
                raise
            time.sleep(delay)
    raise RuntimeError(f"数据库长时间不可用: {last_exc}")


def ensure_test_database() -> str:
    """幂等创建测试库（不存在则建），返回测试 DSN。"""
    last_exc: Exception | None = None
    for _ in range(60):
        try:
            with psycopg.connect(ADMIN_DSN, autocommit=True) as admin:
                exists = admin.execute(
                    "SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB,)
                ).fetchone()
                if not exists:
                    admin.execute(f'CREATE DATABASE "{TEST_DB}"')
            return TEST_DSN
        except psycopg.OperationalError as exc:
            last_exc = exc
            time.sleep(0.5)
    raise RuntimeError(f"管理库不可用: {last_exc}")


class DepgraphTestCase(unittest.TestCase):
    """每个用例独立重置业务表，互不污染。"""

    dsn: str = TEST_DSN

    @classmethod
    def setUpClass(cls) -> None:
        ensure_test_database()
        with db.connect(cls.dsn) as conn:
            db.run_migrations(conn)

    def reset(self) -> psycopg.Connection:
        conn = connect_with_recovery_retry(self.dsn)
        conn.execute(
            "TRUNCATE operations, closure, edges, nodes RESTART IDENTITY"
        )
        conn.commit()
        return conn

    def init_graph(
        self,
        nodes: list[int],
        edges: list[tuple[int, int]],
        attempts: int = 30,
    ) -> psycopg.Connection:
        """重置并建图；若期间实例恢复/重启则整体重试。"""
        last_exc: Exception | None = None
        for _ in range(attempts):
            try:
                conn = self.reset()
                repo.initialize_graph(conn, nodes, edges)
                return conn
            except psycopg.OperationalError as exc:
                last_exc = exc
                time.sleep(1)
        raise RuntimeError(f"建图重试耗尽: {last_exc}")

    def assert_closure_matches_reference(
        self, conn: psycopg.Connection, nodes, edges
    ) -> None:
        report = repo.verify(conn)
        self.assertTrue(
            report["ok"],
            msg=f"闭包与参考遍历不一致: {report}",
        )
        self.assertEqual(report["closure_rows"], report["reference_pairs"])

    def closure_snapshot(
        self, conn: psycopg.Connection
    ) -> dict[tuple[int, int], str]:
        return repo.load_closure(conn)
