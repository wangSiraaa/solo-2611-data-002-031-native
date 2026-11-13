"""High-level import pipeline used by the CLI and the verify suite."""

from __future__ import annotations

import os

from . import DEFAULT_MAX_DEPTH, DEFAULT_MAX_RECORD_BYTES
from .publish import publish, resolve
from .staging import Staging
from .streamparse import Limits, parse_file

STAGING_NAME = "staging.sqlite3"


def import_file(xml_path, data_dir, chunk_size=65536,
                max_depth=DEFAULT_MAX_DEPTH,
                max_record_bytes=DEFAULT_MAX_RECORD_BYTES,
                keep_staging=False) -> dict:
    """Parse ``xml_path`` and publish a new version under ``data_dir``.

    Returns the published manifest.  Raises :class:`DocumentError` for
    document-level failures - in that case nothing is published and the
    previous version (if any) is untouched.
    """
    os.makedirs(data_dir, exist_ok=True)
    limits = Limits(max_depth=max_depth, max_record_bytes=max_record_bytes)
    staging_path = os.path.join(data_dir, STAGING_NAME)
    stg = Staging(staging_path)
    try:
        stats, sha256, size = parse_file(xml_path, stg, limits, chunk_size)
        resolve(stg)
        return publish(
            stg, data_dir,
            source_path=xml_path, source_sha256=sha256, source_bytes=size,
            chunk_size=chunk_size, limits=limits, stats=stats,
        )
    finally:
        stg.close()
        if not keep_staging and os.path.exists(staging_path):
            os.remove(staging_path)
