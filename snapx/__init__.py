"""snapx — consistent snapshot export tool (PostgreSQL pg_export_snapshot demo)."""

import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATIONS_DIR = os.path.join(PROJECT_ROOT, "migrations")


def exports_root() -> str:
    return os.environ.get("SNAPX_EXPORTS", os.path.join(PROJECT_ROOT, "exports"))
