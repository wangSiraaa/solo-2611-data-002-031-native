"""Real PTY session whose output feeds the pyte screen model.

`pexpect` provides the PTY.  Observation and debounce live in the client, which
reads raw bytes and feeds them here; this module owns only the process, the
screen model, the audit byte log and line-oriented key sending.  No fixed
sleeps are used for protocol timing.
"""
from __future__ import annotations

import pexpect

from .screen import ScreenModel


class TerminalError(RuntimeError):
    pass


class TerminalDisconnected(TerminalError):
    pass


class TerminalSession:
    def __init__(
        self,
        argv: list[str],
        cwd: str | None = None,
        cols: int = 80,
        rows: int = 24,
        settle_seconds: float = 0.35,
        read_timeout: float = 10.0,
    ) -> None:
        self.argv = argv
        self.cwd = cwd
        self.settle_seconds = settle_seconds
        self.read_timeout = read_timeout
        self.model = ScreenModel(cols, rows)
        self.raw_log = bytearray()  # audit only; never parsed for decisions
        self.eof = False
        try:
            self.child = pexpect.spawn(
                argv[0],
                argv[1:],
                cwd=cwd,
                dimensions=(rows, cols),
                encoding=None,  # bytes
                echo=False,     # do not let the pty echo typed input onto screen
                timeout=None,
            )
        except pexpect.ExceptionPexpect as exc:  # pragma: no cover - startup guard
            raise TerminalError(f"无法启动终端程序: {exc}") from exc
        # Canonical mode + no echo; the legacy app reads full lines.
        try:
            self.child.setecho(False)
        except Exception:
            pass
        self.pid = self.child.pid

    @property
    def lines(self) -> tuple[str, ...]:
        return self.model.lines

    def snapshot(self) -> str:
        return self.model.text()

    # --------------------------------------------------------------- writes
    def send_line(self, text: str) -> None:
        """Send one visible line through the PTY (line discipline adds ENTER)."""
        if "\n" in text or "\r" in text:
            raise TerminalError("拒绝发送含换行的内容")
        try:
            self.child.send(text.encode("utf-8") + b"\n")
        except (pexpect.EOF, OSError) as exc:
            raise TerminalDisconnected(f"发送按键时 PTY 已断开: {exc}") from exc

    def send_enter(self) -> None:
        self.send_line("")

    # --------------------------------------------------------------- lifecycle
    def is_alive(self) -> bool:
        try:
            return bool(self.child.isalive())
        except Exception:
            return False

    def terminate(self) -> None:
        """Terminate the child with SIGTERM.  Never sends an application key."""
        try:
            if self.child.isalive():
                self.child.terminate(force=False)
        except Exception:
            pass
        try:
            self.child.close()
        except Exception:
            pass

    def close(self) -> None:
        try:
            self.child.close()
        except Exception:
            pass
