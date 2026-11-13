"""Guardrails: the assistant must not read SQLite or import the application."""
from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "terminal_assistant"


def test_assistant_never_imports_sqlite_or_app():
    offenders = []
    for path in PACKAGE.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in ("sqlite3", "legacy_app"):
                        offenders.append((path.name, alias.name))
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] in ("sqlite3", "legacy_app"):
                    offenders.append((path.name, node.module))
    assert offenders == [], f"assistant must not access DB/app: {offenders}"


def test_assistant_never_opens_db_file():
    # No SQLite connection strings or direct sqlite usage in the package.
    for path in PACKAGE.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "sqlite3.connect" not in text
        assert "mode=ro" not in text
