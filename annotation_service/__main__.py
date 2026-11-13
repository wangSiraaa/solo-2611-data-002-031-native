"""Entry point: load fixtures, open the store, serve HTTP.

Usage:
    python3.11 -m annotation_service --db .runtime/annotation.db --port 8765
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .fixtures import FixtureError, load_fixtures
from .server import make_server
from .service import AnnotationService
from .store import Store, StoreError


def build_service(fixtures_dir: str | Path, db_path: str | Path) -> AnnotationService:
    fixtures = load_fixtures(fixtures_dir)
    store = Store(db_path)
    store.initialize(fixtures)
    return AnnotationService(fixtures, store)


def main(argv: list[str] | None = None) -> int:
    here = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fixtures", default=str(here / "fixtures"), help="fixture directory"
    )
    parser.add_argument(
        "--db", default=str(here / ".runtime" / "annotation.db"), help="SQLite path"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        service = build_service(args.fixtures, args.db)
    except (FixtureError, StoreError) as exc:
        print(f"startup failed: {exc}", file=sys.stderr)
        return 2

    httpd = make_server(args.host, args.port, service)
    print(f"listening on http://{args.host}:{args.port}  db={args.db}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
