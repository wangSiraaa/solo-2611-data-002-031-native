"""Authoritative terminal screen model rebuilt from ANSI output.

Decisions are made exclusively from the emulated screen (pyte), never from
concatenated raw ANSI history.  Raw bytes are only kept for audit.
"""
from __future__ import annotations

import pyte


class ScreenModel:
    """An 80x24 terminal emulator plus a light audit counter."""

    def __init__(self, cols: int = 80, rows: int = 24) -> None:
        self.cols = cols
        self.rows = rows
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.ByteStream(self.screen)
        self.bytes_seen = 0
        self.chunks_seen = 0

    def feed(self, data: bytes) -> None:
        if not data:
            return
        self.bytes_seen += len(data)
        self.chunks_seen += 1
        self.stream.feed(data)

    @property
    def lines(self) -> tuple[str, ...]:
        """Rendered lines, padded to the screen width by pyte."""
        return tuple(self.screen.display)

    def text(self) -> str:
        return "\n".join(self.lines)

    def fingerprint(self) -> tuple[str, ...]:
        """A cheap identity for the visible screen (used for change detection)."""
        return self.lines

    def row(self, number: int) -> str:
        """1-based row access; returns '' when out of range."""
        if 1 <= number <= len(self.screen.display):
            return self.screen.display[number - 1].rstrip()
        return ""
