"""字节级 SSE (Server-Sent Events) 解析器。

实现要点（对照 HTML WHATWG 的事件流解析约定，并按题目要求收紧）：

- 输入是任意大小的 ``bytes`` 分块；使用增量 UTF-8 解码器，多字节字符可以
  被拆分在两次读取之间，不会产生替换字符或错位。
- 行结束同时支持 LF、CR、CRLF；CRLF 被拆到两个分块时不会多产出一个空行。
- 以 ``:`` 开头的行是注释，直接忽略；仅含注释的块不产生事件。
- 一个事件可含多个 ``data:`` 行，派发时用 LF 拼接，并去掉末尾多余的 LF。
- ``event`` / ``id`` / ``retry`` 字段按规范处理；``id`` 行含 NUL 时忽略该行，
  ``retry`` 不是整数时忽略该行；未知字段忽略。
- 只有收到空行（块结束标记）且 data 非空时才派发完整事件。
- 与浏览器不同（更严格）：流结束时仍滞留在缓冲区里的"半条消息"——没有块结束
  空行、没有行结束符的残行——一律不派发。上游断在半个 JSON 或半个码位上时，
  调用方拿不到事件，自然也不会推进可信断点。

解析器只负责语法，不做任何数据库或网络操作，因此可以独立单元测试。
"""

from __future__ import annotations

from codecs import getincrementaldecoder
from dataclasses import dataclass

__all__ = ["SSEEvent", "SSEParser"]


@dataclass(frozen=True)
class SSEEvent:
    """一个由空行结束的完整 SSE 事件。"""

    event: str | None
    event_id: str | None
    data: str
    # 该事件被派发时解析器见过的最新 retry 值（毫秒），可能为 None。
    retry_ms: int | None


class _Builder:
    """单个事件块（两次空行之间）的累积状态。"""

    __slots__ = ("event_type", "data")

    def __init__(self) -> None:
        self.event_type: str | None = None
        self.data: list[str] = []

    def reset(self) -> None:
        self.event_type = None
        self.data.clear()


class SSEParser:
    def __init__(self) -> None:
        # errors="strict" 由 IncrementalDecoder 在 final=False 时天然支持：
        # 不完整的尾部字节会被缓存，等下一个分块补齐。
        self._decoder = getincrementaldecoder("utf-8")(errors="strict")
        self._line: list[str] = []
        # 上一个字符是 CR，当前字符可能是与之配对的 LF。
        self._cr_pending = False
        self._builder = _Builder()
        # 规范中的 "last event ID buffer"：只被合法 id 行更新，不随空行重置。
        # 注意它只是解析器视图，不是可信断点；可信断点只来自数据库提交。
        self.last_event_id: str | None = None
        # 最近一次合法 retry 行的值（毫秒），解析到即更新。
        self.retry_ms: int | None = None
        self._events: list[SSEEvent] = []

    # ------------------------------------------------------------------ #
    # 公共 API
    # ------------------------------------------------------------------ #
    def feed(self, chunk: bytes) -> list[SSEEvent]:
        """喂入一个读取分块，返回本次新解析出的完整事件（可能为空列表）。"""
        events: list[SSEEvent] = []
        if chunk:
            text = self._decoder.decode(chunk, final=False)
            for ch in text:
                self._consume_char(ch)
                if self._events:
                    # 立刻摘走，避免内部列表在下次 feed 时被 clear 影响调用方。
                    events.extend(self._events)
                    self._events.clear()
        return events

    def end_stream(self) -> list[SSEEvent]:
        """通知流已结束。

        未收到块结束空行的残留内容按不完整事件丢弃，绝不派发。这包括上游断在
        半个多字节码位上的情形：增量解码器里残留的不完整字节直接丢弃即可——
        它不可能属于任何已派发事件。真正的非法 UTF-8（非法起始字节等）在
        ``feed`` 阶段就已经抛 UnicodeDecodeError，不会留到这里。
        """
        events: list[SSEEvent] = []
        try:
            tail = self._decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            tail = ""
        for ch in tail:
            self._consume_char(ch)
            if self._events:
                events.extend(self._events)
                self._events.clear()
        # 刻意不处理 _cr_pending / _line / _builder 中的残留：
        # 没有行结束符/空行的内容不是完整事件。
        return events

    # ------------------------------------------------------------------ #
    # 内部状态机
    # ------------------------------------------------------------------ #
    def _consume_char(self, ch: str) -> None:
        if self._cr_pending:
            # 前一个 CR 已经结束了一行；当前 LF 只是 CRLF 的另一半，吞掉即可。
            self._cr_pending = False
            if ch == "\n":
                return
            # 不是配对的 LF：当前字符按普通字符重新走一遍完整判断。
        if ch == "\r":
            self._cr_pending = True
            self._finish_line()
        elif ch == "\n":
            self._finish_line()
        else:
            self._line.append(ch)

    def _finish_line(self) -> None:
        line = "".join(self._line)
        self._line.clear()

        if line == "":
            # 空行 = 事件块结束。data 为空（注释块、仅 retry 块）时不派发，
            # 但仍要重置块状态。
            if self._builder.data:
                self._events.append(
                    SSEEvent(
                        event=self._builder.event_type,
                        event_id=self.last_event_id,
                        data="\n".join(self._builder.data),
                        retry_ms=self.retry_ms,
                    )
                )
            self._builder.reset()
            return

        if line.startswith(":"):
            # 注释行（heartbeat 等），忽略。
            return

        if ":" in line:
            field, value = line.split(":", 1)
            if value.startswith(" "):
                value = value[1:]
        else:
            field, value = line, ""

        self._apply_field(field, value)

    def _apply_field(self, field: str, value: str) -> None:
        if field == "event":
            self._builder.event_type = value
        elif field == "data":
            self._builder.data.append(value)
        elif field == "id":
            # 含 NUL 的 id 行按规范忽略。
            if "\x00" not in value:
                self.last_event_id = value
        elif field == "retry":
            if value.isdigit():
                self.retry_ms = int(value)
        # 其余字段（包括未知字段）忽略。
