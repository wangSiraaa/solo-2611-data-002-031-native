#!/usr/bin/env python3
"""Entry point: python3 importer.py <generate|import|inspect|verify> ..."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from orderimp.cli import main

if __name__ == "__main__":
    sys.exit(main())
