"""Focused regression tests for the start-up chain audit.

The audit is what makes a restart trustworthy: it re-proves that every stored
final is bound to two real, immutable, disagreeing/agreeing submissions and
that no item carries two finals.  These tests tamper with a committed database
directly and require start-up to fail.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from annotation_service.__main__ import build_service
from annotation_service.store import StoreError

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"


class TestChainAudit(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "audit.db"
        build_service(FIXTURES, self.db)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def seed_pair(self, conn) -> None:
        conn.execute(
            "INSERT INTO submissions(item_id,author_id,labels_json,created_at) "
            "VALUES('T01','ann_a','[\"product\"]','2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO submissions(item_id,author_id,labels_json,created_at) "
            "VALUES('T01','ann_b','[\"product\"]','2026-01-01T00:00:01Z')"
        )
        conn.execute(
            "INSERT INTO matches(item_id,submission_a_seq,submission_b_seq,"
            "author_a_id,author_b_id,labels_json,created_at) "
            "VALUES('T01',1,2,'ann_a','ann_b','[\"product\"]','2026-01-01T00:00:02Z')"
        )

    def assert_refused(self) -> str:
        with self.assertRaises(StoreError) as ctx:
            build_service(FIXTURES, self.db)
        return str(ctx.exception)

    def test_duplicate_final_records_refused(self) -> None:
        with sqlite3.connect(self.db) as conn:
            self.seed_pair(conn)
            conn.execute(
                "INSERT INTO arbitrations(item_id,submission_a_seq,submission_b_seq,"
                "author_a_id,author_b_id,labels_json,arbiter_id,created_at) "
                "VALUES('T01',1,2,'ann_a','ann_b','[\"service\"]','arbiter',"
                "'2026-01-01T00:00:03Z')"
            )
        self.assertIn("multiple final records", self.assert_refused())

    def test_agreement_final_with_wrong_labels_refused(self) -> None:
        with sqlite3.connect(self.db) as conn:
            self.seed_pair(conn)
            conn.execute("UPDATE matches SET labels_json='[\"service\"]'")
        self.assertIn("not the agreed submission labels", self.assert_refused())

    def test_arbitration_for_agreeing_submissions_refused(self) -> None:
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "INSERT INTO submissions(item_id,author_id,labels_json,created_at) "
                "VALUES('T01','ann_a','[\"product\"]','2026-01-01T00:00:00Z')"
            )
            conn.execute(
                "INSERT INTO submissions(item_id,author_id,labels_json,created_at) "
                "VALUES('T01','ann_b','[\"product\"]','2026-01-01T00:00:01Z')"
            )
            conn.execute(
                "INSERT INTO arbitrations(item_id,submission_a_seq,submission_b_seq,"
                "author_a_id,author_b_id,labels_json,arbiter_id,created_at) "
                "VALUES('T01',1,2,'ann_a','ann_b','[\"product\"]','arbiter',"
                "'2026-01-01T00:00:03Z')"
            )
        self.assertIn("agreed submission pair", self.assert_refused())

    def test_unknown_label_in_submission_refused(self) -> None:
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "INSERT INTO submissions(item_id,author_id,labels_json,created_at) "
                "VALUES('T01','ann_a','[\"bogus\"]','2026-01-01T00:00:00Z')"
            )
        self.assertIn("unknown labels", self.assert_refused())

    def test_final_binding_to_missing_submission_refused(self) -> None:
        with sqlite3.connect(self.db) as conn:
            self.seed_pair(conn)
            conn.execute("UPDATE matches SET submission_b_seq=99")
        self.assertIn("missing submission", self.assert_refused())

    def test_untampered_database_starts(self) -> None:
        # Sanity: seeding a complete, valid pair and final keeps start-up happy.
        with sqlite3.connect(self.db) as conn:
            self.seed_pair(conn)
        build_service(FIXTURES, self.db)  # must not raise


if __name__ == "__main__":
    unittest.main(verbosity=2)
