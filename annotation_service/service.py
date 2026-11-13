"""Business rules: authentication, blind visibility, submissions, arbitration.

Visibility contract (applied identically by every read endpoint):

* An annotator never receives another annotator's labels for an item until
  *both* assigned annotators have submitted immutable label sets for it.
* Before an annotator's own submission, the item view contains only the text
  and the annotator's own (empty) state.  Peer status and peer labels are
  omitted entirely.
* After the annotator's own submission, whether the peer has submitted may be
  shown, but peer labels only become visible once both submissions exist.
* Final results exist only when both submissions exist (agreement) or an
  arbiter has resolved the dispute, so final results are safe to show to an
  annotator only when they exist and the annotator has submitted.
* An arbiter can see submissions only for items where both annotators have
  already submitted.  The arbitration queue and detail endpoints return no
  submission data for incomplete items.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .fixtures import TaskFixtures, canonical_json
from .store import Store, token_sha256

VISIBILITY_RULE = (
    "Peer labels are revealed to an annotator only after both assigned "
    "annotators have submitted, and only with an immutable submission record. "
    "Arbitration is available only for items with both submissions and "
    "differing label sets. Only agreed or arbitrated items are exported."
)

MAX_LABELS_PER_SUBMISSION = 100


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def utcnow() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def parse_token(header_value: str | None) -> str:
    if not header_value:
        raise ApiError(401, "unauthenticated", "missing bearer token")
    parts = header_value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise ApiError(401, "unauthenticated", "expected 'Authorization: Bearer <token>'")
    return parts[1].strip()


def parse_labels(
    fixtures: TaskFixtures, value: Any, *, field: str = "labels"
) -> list[str]:
    if not isinstance(value, list):
        raise ApiError(400, "invalid_labels", f"{field} must be a list of label ids")
    if len(value) > MAX_LABELS_PER_SUBMISSION:
        raise ApiError(400, "invalid_labels", f"{field} contains too many entries")
    if any(not isinstance(label, str) for label in value):
        raise ApiError(400, "invalid_labels", f"{field} must contain only strings")
    unknown = sorted(set(value) - fixtures.label_ids)
    if unknown:
        raise ApiError(
            400,
            "unknown_label",
            "unknown label id(s): " + ", ".join(unknown),
        )
    canonical = sorted(set(value))
    if not canonical and not fixtures.empty_set_allowed:
        raise ApiError(400, "empty_set_not_allowed", "empty label sets are not allowed")
    if len(canonical) > 1 and not fixtures.multiple_labels_allowed:
        raise ApiError(
            400,
            "multiple_labels_not_allowed",
            "at most one label may be submitted",
        )
    return canonical


class AnnotationService:
    def __init__(self, fixtures: TaskFixtures, store: Store) -> None:
        self.fixtures = fixtures
        self.store = store

    # -- authentication ----------------------------------------------------

    def authenticate(self, authorization: str | None) -> dict[str, str]:
        token = parse_token(authorization)
        digest = token_sha256(token)
        with self.store.read_txn() as conn:
            row = conn.execute(
                "SELECT user_id, role FROM users WHERE token_sha256 = ?", (digest,)
            ).fetchone()
        if row is None:
            raise ApiError(401, "unauthenticated", "invalid credentials")
        return {"id": row["user_id"], "role": row["role"]}

    def _require_role(self, user: dict[str, str], *roles: str) -> None:
        if user["role"] not in roles:
            raise ApiError(403, "forbidden", "this identity cannot perform that action")

    def _require_assigned(self, conn: sqlite3.Connection, item_id: str, user_id: str) -> None:
        row = conn.execute(
            "SELECT 1 FROM assignments WHERE item_id = ? AND user_id = ?",
            (item_id, user_id),
        ).fetchone()
        if row is None:
            raise ApiError(403, "not_assigned", "this item is not assigned to you")

    def _fetch_item(
        self, conn: sqlite3.Connection, item_id: str
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT seq, item_id, text FROM items WHERE item_id = ?", (item_id,)
        ).fetchone()
        if row is None:
            raise ApiError(404, "not_found", "item not found")
        return row

    # -- shared state ------------------------------------------------------

    def _load_state(
        self, conn: sqlite3.Connection, item_id: str
    ) -> dict[str, Any]:
        submissions = {
            row["author_id"]: row
            for row in conn.execute(
                "SELECT * FROM submissions WHERE item_id = ? ORDER BY seq", (item_id,)
            )
        }
        match = conn.execute(
            "SELECT * FROM matches WHERE item_id = ?", (item_id,)
        ).fetchone()
        arbitration = conn.execute(
            "SELECT * FROM arbitrations WHERE item_id = ?", (item_id,)
        ).fetchone()
        return {
            "submissions": submissions,
            "match": match,
            "arbitration": arbitration,
        }

    def _annotators_for_item(
        self, conn: sqlite3.Connection, item_id: str
    ) -> list[str]:
        return [
            row["user_id"]
            for row in conn.execute(
                "SELECT user_id FROM assignments WHERE item_id = ? ORDER BY user_id",
                (item_id,),
            )
        ]

    def _status(self, state: dict[str, Any]) -> str:
        if state["arbitration"] is not None:
            return "arbitrated"
        if state["match"] is not None:
            return "agreed"
        if len(state["submissions"]) >= 2:
            return "disputed"
        if len(state["submissions"]) == 1:
            return "waiting_for_peer"
        return "not_submitted"

    # -- serialization (strictly visibility-filtered) ----------------------

    def _submission_view(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "submission_id": row["seq"],
            "author_id": row["author_id"],
            "labels": json.loads(row["labels_json"]),
            "created_at": row["created_at"],
        }

    def _final_view(self, state: dict[str, Any]) -> dict[str, Any] | None:
        match, arb = state["match"], state["arbitration"]
        if arb is not None:
            return {
                "final_id": f"arbitration:{arb['seq']}",
                "status": "arbitrated",
                "source": "arbitration",
                "labels": json.loads(arb["labels_json"]),
                "created_at": arb["created_at"],
                "arbiter_id": arb["arbiter_id"],
                "binding": {
                    "item_id": arb["item_id"],
                    "submissions": [
                        {
                            "submission_id": arb["submission_a_seq"],
                            "author_id": arb["author_a_id"],
                        },
                        {
                            "submission_id": arb["submission_b_seq"],
                            "author_id": arb["author_b_id"],
                        },
                    ],
                },
            }
        if match is not None:
            return {
                "final_id": f"agreement:{match['seq']}",
                "status": "agreed",
                "source": "agreement",
                "labels": json.loads(match["labels_json"]),
                "created_at": match["created_at"],
                "arbiter_id": None,
                "binding": {
                    "item_id": match["item_id"],
                    "submissions": [
                        {
                            "submission_id": match["submission_a_seq"],
                            "author_id": match["author_a_id"],
                        },
                        {
                            "submission_id": match["submission_b_seq"],
                            "author_id": match["author_b_id"],
                        },
                    ],
                },
            }
        return None

    def _annotator_item_view(
        self,
        item: sqlite3.Row,
        state: dict[str, Any],
        viewer_id: str,
        annotators: list[str],
    ) -> dict[str, Any]:
        own = state["submissions"].get(viewer_id)
        peers = [a for a in annotators if a != viewer_id]
        peer = state["submissions"].get(peers[0]) if peers else None
        both = own is not None and peer is not None

        view: dict[str, Any] = {
            "item": {"id": item["item_id"], "text": item["text"]},
            "viewer": {"id": viewer_id, "role": "annotator"},
            "own_submission": self._submission_view(own) if own is not None else None,
        }
        if own is None:
            # Blind phase: only the text and the viewer's own (empty) state.
            # The viewer's own status is "not_submitted" regardless of whether
            # the peer has already submitted, so even peer progress is hidden.
            view["status"] = "not_submitted"
            view["visibility"] = "blind_until_own_submission"
            return view

        view["status"] = self._status(state)
        view["peer_submitted"] = peer is not None

        view["peer_submitted"] = peer is not None
        if both:
            view["visibility"] = "peer_revealed_after_both_submitted"
            view["peer_submission"] = self._submission_view(peer)
        else:
            view["visibility"] = "own_submitted_waiting_for_peer"
        view["final"] = self._final_view(state) if both else None
        return view

    # -- read endpoints ----------------------------------------------------

    def meta(self) -> dict[str, Any]:
        return {
            "task": "blind_double_annotation_with_arbitration",
            "visibility_rule": VISIBILITY_RULE,
            "item_count": len(self.fixtures.item_order),
            "label_count": len(self.fixtures.label_ids),
            "annotator_count": len(self.fixtures.annotator_ids),
            "empty_set_allowed": self.fixtures.empty_set_allowed,
            "multiple_labels_allowed": self.fixtures.multiple_labels_allowed,
        }

    def labels_catalog(self) -> dict[str, Any]:
        return {
            "labels": [
                {"id": lid, "name": self.fixtures.label_names[lid]}
                for lid in sorted(self.fixtures.label_ids)
            ],
            "empty_set_allowed": self.fixtures.empty_set_allowed,
            "multiple_labels_allowed": self.fixtures.multiple_labels_allowed,
        }

    def list_items(self, user: dict[str, str]) -> dict[str, Any]:
        with self.store.read_txn() as conn:
            if user["role"] == "annotator":
                rows = conn.execute(
                    """
                    SELECT i.seq, i.item_id, i.text
                      FROM items i
                      JOIN assignments a ON a.item_id = i.item_id
                     WHERE a.user_id = ?
                     ORDER BY i.seq
                    """,
                    (user["id"],),
                ).fetchall()
                items = []
                for item in rows:
                    annotators = self._annotators_for_item(conn, item["item_id"])
                    state = self._load_state(conn, item["item_id"])
                    items.append(
                        self._annotator_item_view(item, state, user["id"], annotators)
                    )
                return {
                    "viewer": user,
                    "visibility_rule": VISIBILITY_RULE,
                    "items": items,
                }

            self._require_role(user, "arbiter")
            rows = conn.execute(
                """
                SELECT i.seq, i.item_id, i.text
                  FROM items i
                 WHERE (SELECT COUNT(*) FROM submissions s WHERE s.item_id = i.item_id) = 2
                 ORDER BY i.seq
                """
            ).fetchall()
            items = []
            for item in rows:
                state = self._load_state(conn, item["item_id"])
                items.append(self._arbiter_item_view(item, state))
            return {
                "viewer": user,
                "visibility_rule": VISIBILITY_RULE,
                "items": items,
            }

    def get_item(self, user: dict[str, str], item_id: str) -> dict[str, Any]:
        with self.store.read_txn() as conn:
            item = self._fetch_item(conn, item_id)
            annotators = self._annotators_for_item(conn, item_id)
            state = self._load_state(conn, item_id)

            if user["role"] == "annotator":
                self._require_assigned(conn, item_id, user["id"])
                return self._annotator_item_view(item, state, user["id"], annotators)

            self._require_role(user, "arbiter")
            if len(state["submissions"]) < len(annotators):
                raise ApiError(
                    409,
                    "not_ready",
                    "this item is not ready for arbitration: both annotators must "
                    "submit first",
                )
            return self._arbiter_item_view(item, state)

    def _arbiter_item_view(
        self, item: sqlite3.Row, state: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "item": {"id": item["item_id"], "text": item["text"]},
            "viewer": {"role": "arbiter"},
            "status": self._status(state),
            "submissions": [
                self._submission_view(row)
                for row in sorted(
                    state["submissions"].values(), key=lambda r: r["seq"]
                )
            ],
            "final": self._final_view(state),
            "visibility": "both_submissions_completed",
        }

    def arbitration_queue(self, user: dict[str, str]) -> dict[str, Any]:
        self._require_role(user, "arbiter")
        with self.store.read_txn() as conn:
            rows = conn.execute(
                """
                SELECT i.seq, i.item_id, i.text
                  FROM items i
                 WHERE (SELECT COUNT(*) FROM submissions s WHERE s.item_id = i.item_id) = 2
                   AND NOT EXISTS (SELECT 1 FROM matches m WHERE m.item_id = i.item_id)
                   AND NOT EXISTS (
                         SELECT 1 FROM arbitrations r WHERE r.item_id = i.item_id)
                 ORDER BY i.seq
                """
            ).fetchall()
            items = []
            for item in rows:
                state = self._load_state(conn, item["item_id"])
                items.append(self._arbiter_item_view(item, state))
            return {
                "viewer": user,
                "visibility_rule": VISIBILITY_RULE,
                "queue": "items with two submissions and differing label sets",
                "items": items,
            }

    def export(self, user: dict[str, str]) -> dict[str, Any]:
        """Final-results export: agreed or arbitrated items only.

        Annotators only receive rows for items where their own immutable
        submission exists.  A final result cannot exist without both
        submissions, so this never reveals a label set before the viewer has
        committed their own blind submission.
        """
        with self.store.read_txn() as conn:
            finals_sql = """
                SELECT i.seq AS item_seq, i.item_id, i.text,
                       m.seq AS match_seq, a.seq AS arb_seq,
                       COALESCE(m.seq, a.seq) AS final_seq
                  FROM items i
                  LEFT JOIN matches m ON m.item_id = i.item_id
                  LEFT JOIN arbitrations a ON a.item_id = i.item_id
                 WHERE m.seq IS NOT NULL OR a.seq IS NOT NULL
                 ORDER BY i.seq
            """
            rows = conn.execute(finals_sql).fetchall()

            if user["role"] == "annotator":
                assigned = {
                    row["item_id"]
                    for row in conn.execute(
                        "SELECT item_id FROM assignments WHERE user_id = ?",
                        (user["id"],),
                    )
                }
                own_submitted = {
                    row["item_id"]
                    for row in conn.execute(
                        "SELECT item_id FROM submissions WHERE author_id = ?",
                        (user["id"],),
                    )
                }
                allowed = assigned & own_submitted
                rows = [row for row in rows if row["item_id"] in allowed]
            else:
                self._require_role(user, "arbiter")

            items = []
            for row in rows:
                state = self._load_state(conn, row["item_id"])
                final = self._final_view(state)
                if final is None:  # defensive; SQL already restricts to finals
                    continue
                items.append(
                    {
                        "item_id": row["item_id"],
                        "text": row["text"],
                        "final": final,
                        "submissions": [
                            self._submission_view(s)
                            for s in sorted(
                                state["submissions"].values(), key=lambda r: r["seq"]
                            )
                        ],
                    }
                )
            return {
                "viewer": user,
                "exported_at": utcnow(),
                "visibility_rule": VISIBILITY_RULE,
                "only_final": True,
                "item_count": len(items),
                "items": items,
            }

    # -- writes ------------------------------------------------------------

    def submit(
        self, user: dict[str, str], item_id: str, body: Any
    ) -> tuple[int, dict[str, Any]]:
        self._require_role(user, "annotator")
        if not isinstance(body, dict):
            raise ApiError(400, "invalid_body", "request body must be a JSON object")
        if "author_id" in body or "user_id" in body or "annotator_id" in body:
            raise ApiError(
                400,
                "identity_in_body",
                "the submitting identity is taken from the bearer token, not the body",
            )
        extra = set(body) - {"labels"}
        if extra:
            raise ApiError(
                400,
                "unknown_field",
                "unsupported field(s): " + ", ".join(sorted(extra)),
            )
        if "labels" not in body:
            raise ApiError(400, "invalid_body", "missing 'labels' field")
        labels = parse_labels(self.fixtures, body["labels"])
        labels_json = canonical_json(labels)
        now = utcnow()

        with self.store.write_txn() as conn:
            self._fetch_item(conn, item_id)
            self._require_assigned(conn, item_id, user["id"])
            annotators = self._annotators_for_item(conn, item_id)
            state = self._load_state(conn, item_id)

            existing = state["submissions"].get(user["id"])
            if existing is not None:
                if existing["labels_json"] == labels_json:
                    view = self._annotator_item_view(
                        self._fetch_item(conn, item_id), state, user["id"], annotators
                    )
                    view["replayed"] = True
                    return 200, view
                raise ApiError(
                    409,
                    "submission_immutable",
                    "you already submitted different labels for this item; "
                    "submissions cannot be modified",
                )

            if state["match"] is not None or state["arbitration"] is not None:
                # Unreachable while every assigned author has exactly one
                # submission, but kept as a hard guard.
                raise ApiError(409, "already_final", "this item already has a final result")

            cur = conn.execute(
                """
                INSERT INTO submissions (item_id, author_id, labels_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (item_id, user["id"], labels_json, now),
            )
            submission_seq = int(cur.lastrowid)
            state = self._load_state(conn, item_id)

            if len(state["submissions"]) == len(annotators):
                a, b = sorted(
                    state["submissions"].values(), key=lambda r: r["seq"]
                )
                if a["labels_json"] == b["labels_json"]:
                    conn.execute(
                        """
                        INSERT INTO matches (
                            item_id, submission_a_seq, submission_b_seq,
                            author_a_id, author_b_id, labels_json, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            item_id,
                            a["seq"],
                            b["seq"],
                            a["author_id"],
                            b["author_id"],
                            a["labels_json"],
                            now,
                        ),
                    )
                # Differing labels stay unfinalised and become arbitrable.

            state = self._load_state(conn, item_id)
            view = self._annotator_item_view(
                self._fetch_item(conn, item_id), state, user["id"], annotators
            )
            view["submitted"] = True
            view["submission_id"] = submission_seq
            return 201, view

    def arbitrate(
        self, user: dict[str, str], item_id: str, body: Any
    ) -> tuple[int, dict[str, Any]]:
        self._require_role(user, "arbiter")
        if not isinstance(body, dict):
            raise ApiError(400, "invalid_body", "request body must be a JSON object")
        if {"author_id", "user_id", "arbiter_id", "annotator_id"} & set(body):
            raise ApiError(
                400,
                "identity_in_body",
                "the acting identity is taken from the bearer token, not the body",
            )
        extra = set(body) - {"labels", "expected_submissions"}
        if extra:
            raise ApiError(
                400,
                "unknown_field",
                "unsupported field(s): " + ", ".join(sorted(extra)),
            )
        if "labels" not in body:
            raise ApiError(400, "invalid_body", "missing 'labels' field")
        labels = parse_labels(self.fixtures, body["labels"])
        labels_json = canonical_json(labels)
        expected = body.get("expected_submissions")
        if expected is not None:
            if not isinstance(expected, dict) or any(
                not isinstance(k, str)
                or isinstance(v, bool)
                or not isinstance(v, int)
                for k, v in expected.items()
            ):
                raise ApiError(
                    400,
                    "invalid_binding",
                    "expected_submissions must map annotator ids to submission ids",
                )
        now = utcnow()

        with self.store.write_txn() as conn:
            self._fetch_item(conn, item_id)
            # Arbiters are not item-assigned annotators; the role check above
            # is their authorization.  This mirrors the read-side rule exactly.
            annotators = self._annotators_for_item(conn, item_id)
            state = self._load_state(conn, item_id)
            by_author = state["submissions"]
            missing = [a for a in annotators if a not in by_author]
            if missing:
                raise ApiError(
                    409,
                    "not_ready",
                    "arbitration requires an immutable submission from every "
                    "assigned annotator; this item is not ready",
                )
            a, b = sorted(by_author.values(), key=lambda r: r["seq"])

            if state["arbitration"] is not None:
                if state["arbitration"]["labels_json"] == labels_json:
                    view = self._arbiter_item_view(
                        self._fetch_item(conn, item_id), state
                    )
                    view["replayed"] = True
                    return 200, view
                raise ApiError(
                    409,
                    "arbitration_immutable",
                    "this item already has a different arbitration result",
                )
            if state["match"] is not None:
                raise ApiError(
                    409,
                    "already_agreed",
                    "the two submissions agree; this item does not require arbitration",
                )
            if a["labels_json"] == b["labels_json"]:
                raise ApiError(
                    409,
                    "already_agreed",
                    "the two submissions agree; this item does not require arbitration",
                )

            if expected is not None:
                actual = {
                    row["author_id"]: row["seq"] for row in (a, b)
                }
                if expected != actual:
                    raise ApiError(
                        409,
                        "stale_binding",
                        "expected_submissions does not match the current immutable "
                        "submissions for this item",
                    )

            cur = conn.execute(
                """
                INSERT INTO arbitrations (
                    item_id, submission_a_seq, submission_b_seq,
                    author_a_id, author_b_id, labels_json, arbiter_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_id,
                    a["seq"],
                    b["seq"],
                    a["author_id"],
                    b["author_id"],
                    labels_json,
                    user["id"],
                    now,
                ),
            )
            state = self._load_state(conn, item_id)
            view = self._arbiter_item_view(self._fetch_item(conn, item_id), state)
            view["arbitration_id"] = int(cur.lastrowid)
            return 201, view
