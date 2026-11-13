"""Loading and validation of the immutable task inputs.

The item text and the label vocabulary are fixed for the lifetime of the
task.  They are loaded once at start-up and hash-pinned into the database so
that a modified fixture file cannot silently change what a submission means.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class FixtureError(Exception):
    """Raised when the fixture files are missing or malformed."""


def canonical_json(value: Any) -> str:
    """Deterministic JSON encoding used for hashing and idempotency compare."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class Item:
    id: str
    text: str


@dataclass(frozen=True)
class TaskFixtures:
    items: dict[str, Item]
    item_order: tuple[str, ...]
    label_ids: frozenset[str]
    label_names: dict[str, str]
    empty_set_allowed: bool
    multiple_labels_allowed: bool
    identities: dict[str, dict[str, str]]
    items_sha256: str
    labels_sha256: str
    identities_sha256: str

    @property
    def annotator_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                uid
                for uid, ident in self.identities.items()
                if ident["role"] == "annotator"
            )
        )

    @property
    def item_assignments(self) -> dict[str, tuple[str, ...]]:
        """All items are assigned to every annotator in the fixed fixtures."""
        annotators = self.annotator_ids
        return {item_id: annotators for item_id in self.item_order}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FixtureError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise FixtureError(f"invalid JSON in {path}: {exc}") from exc


def load_fixtures(fixtures_dir: str | Path) -> TaskFixtures:
    root = Path(fixtures_dir)
    items_path = root / "items.jsonl"
    labels_path = root / "labels.json"
    identities_path = root / "identities.json"

    try:
        items_sha = _sha256_file(items_path)
        labels_sha = _sha256_file(labels_path)
        identities_sha = _sha256_file(identities_path)
    except OSError as exc:
        raise FixtureError(f"cannot read fixtures in {root}: {exc}") from exc

    items: dict[str, Item] = {}
    order: list[str] = []
    for lineno, line in enumerate(items_path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise FixtureError(f"{items_path}:{lineno}: invalid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise FixtureError(f"{items_path}:{lineno}: expected an object")
        item_id, text = raw.get("id"), raw.get("text")
        if not isinstance(item_id, str) or not item_id:
            raise FixtureError(f"{items_path}:{lineno}: missing string id")
        if not isinstance(text, str):
            raise FixtureError(f"{items_path}:{lineno}: missing string text")
        if item_id in items:
            raise FixtureError(f"{items_path}:{lineno}: duplicate item id {item_id!r}")
        items[item_id] = Item(id=item_id, text=text)
        order.append(item_id)
    if not items:
        raise FixtureError(f"{items_path}: no items found")

    labels_raw = _read_json(labels_path)
    if not isinstance(labels_raw, dict) or not isinstance(labels_raw.get("labels"), list):
        raise FixtureError(f"{labels_path}: expected an object with a 'labels' list")
    label_names: dict[str, str] = {}
    for entry in labels_raw["labels"]:
        if not isinstance(entry, dict):
            raise FixtureError(f"{labels_path}: label entries must be objects")
        lid, name = entry.get("id"), entry.get("name")
        if not isinstance(lid, str) or not lid:
            raise FixtureError(f"{labels_path}: label entry missing string id")
        if lid in label_names:
            raise FixtureError(f"{labels_path}: duplicate label id {lid!r}")
        label_names[lid] = name if isinstance(name, str) else lid

    identities_raw = _read_json(identities_path)
    if not isinstance(identities_raw, dict) or not isinstance(identities_raw.get("users"), list):
        raise FixtureError(f"{identities_path}: expected an object with a 'users' list")
    identities: dict[str, dict[str, str]] = {}
    tokens: set[str] = set()
    for entry in identities_raw["users"]:
        if not isinstance(entry, dict):
            raise FixtureError(f"{identities_path}: user entries must be objects")
        uid, role, token = entry.get("id"), entry.get("role"), entry.get("token")
        if not isinstance(uid, str) or not uid:
            raise FixtureError(f"{identities_path}: user entry missing string id")
        if role not in {"annotator", "arbiter"}:
            raise FixtureError(f"{identities_path}: user {uid!r} has unsupported role {role!r}")
        if not isinstance(token, str) or not token:
            raise FixtureError(f"{identities_path}: user {uid!r} missing string token")
        if uid in identities:
            raise FixtureError(f"{identities_path}: duplicate user id {uid!r}")
        if token in tokens:
            raise FixtureError(f"{identities_path}: duplicate token for user {uid!r}")
        identities[uid] = {"id": uid, "role": role, "token": token}
        tokens.add(token)
    annotators = [u for u in identities.values() if u["role"] == "annotator"]
    if len(annotators) != 2:
        raise FixtureError(
            f"{identities_path}: the double-annotation task requires exactly 2 annotators, "
            f"found {len(annotators)}"
        )
    if not any(u["role"] == "arbiter" for u in identities.values()):
        raise FixtureError(f"{identities_path}: no arbiter identity found")

    return TaskFixtures(
        items=items,
        item_order=tuple(order),
        label_ids=frozenset(label_names),
        label_names=label_names,
        empty_set_allowed=bool(labels_raw.get("empty_set_allowed", True)),
        multiple_labels_allowed=bool(labels_raw.get("multiple_labels_allowed", True)),
        identities=identities,
        items_sha256=items_sha,
        labels_sha256=labels_sha,
        identities_sha256=identities_sha,
    )
