#!/usr/bin/env python3
"""Convenience wrapper so the CLI can be run as ./ledger_cli.py."""
import os
import sys

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, REPO_ROOT)
from ledger.ledger import main  # noqa: E402

if __name__ == "__main__":
    main()
