# -*- coding: utf-8 -*-
"""Content-Length 分帧层（宿主与连接器共用同一套规则）。

线路格式（每条消息）::

    Content-Length: <字节数>\r\n
    \r\n
    <恰好 N 字节的 UTF-8 JSON>

- 长度按 **字节** 而不是字符计算；
- 读取端可能在一次 read() 中收到多条消息、半条头、半条 JSON，
  甚至半个 UTF-8 多字节字符（中文一个字符 3 字节）；
- :class:`FrameAssembler` 是纯字节缓冲，不依赖 read 边界，逐喂逐产出完整消息。
"""

from __future__ import annotations

import json
from typing import List, Optional, Tuple

HEADER_NAME = b"Content-Length:"
# 头区最大字节数，防止畸形流（永远等不到空行）导致内存无限增长。
MAX_HEADER_BYTES = 4096


class ProtocolError(Exception):
    """可定位的协议错误。

    ``offset`` 是从该连接器 stdout 字节流起点算起的偏移；
    ``connector`` 由上层（supervisor）补上。
    """

    def __init__(self, reason: str, offset: int, connector: Optional[str] = None):
        self.reason = reason
        self.offset = offset
        self.connector = connector
        super().__init__(self._render())

    def _render(self) -> str:
        where = self.connector if self.connector else "<unknown-connector>"
        return (f"protocol error: connector={where} offset={self.offset} "
                f"reason={self.reason}")

    def describe(self) -> str:
        """connector 可能在构造后才补全，用当前字段重新渲染消息。"""
        return self._render()

    def __str__(self) -> str:
        return self._render()


class FrameAssembler:
    """增量字节 -> 完整 JSON 帧。

    使用方式::

        asm = FrameAssembler(max_message_bytes=1 << 20)
        for chunk in stream:
            for msg in asm.feed(chunk):
                ...          # msg 是已解析的 dict / list

    所有偏移均相对本对象生命周期内累计喂入的字节数，
    因此出错时可以明确报出“连接器 X 在字节偏移 N 处协议违规”。
    """

    def __init__(self, max_message_bytes: int):
        self.max_message_bytes = max_message_bytes
        self._buf = bytearray()
        self._fed = 0          # 累计已接收字节数（流偏移基准）
        self._header_done = False
        self._body_len: Optional[int] = None

    @property
    def stream_offset(self) -> int:
        """下一个待处理字节在 stdout 流中的绝对偏移。"""
        return self._fed - len(self._buf)

    def feed(self, chunk: bytes) -> List[dict]:
        self._buf.extend(chunk)
        self._fed += len(chunk)
        messages: List[dict] = []
        while True:
            if not self._header_done:
                if not self._parse_header():
                    break
            if len(self._buf) < self._body_len:
                break  # 主体未到齐（可能正卡在半个中文字符上）
            raw = bytes(self._buf[: self._body_len])
            frame_start = self.stream_offset
            del self._buf[: self._body_len]
            self._header_done = False
            declared = self._body_len
            self._body_len = None
            messages.append(self._decode_body(raw, declared, frame_start))
        return messages

    def _parse_header(self) -> bool:
        """解析头区，返回 True 表示头已完整（self._body_len 已就绪）。"""
        sep = self._buf.find(b"\r\n\r\n")
        if sep == -1:
            # 宽松接受只有 LF 的头（规范是 CRLF）。
            sep_lf = self._buf.find(b"\n\n")
            if sep_lf == -1:
                if len(self._buf) > MAX_HEADER_BYTES:
                    raise ProtocolError(
                        "header exceeds %d bytes without blank line" % MAX_HEADER_BYTES,
                        self.stream_offset,
                    )
                return False
            sep = sep_lf
            head_end = sep + 2
        else:
            head_end = sep + 4

        header_block = bytes(self._buf[:sep])
        length: Optional[int] = None
        for line in header_block.replace(b"\r\n", b"\n").split(b"\n"):
            stripped = line.strip()
            if not stripped:
                continue
            if not stripped.lower().startswith(HEADER_NAME.lower()):
                raise ProtocolError(
                    "unexpected header line %r" % stripped[:80], self.stream_offset
                )
            value = stripped[len(HEADER_NAME):].strip()
            try:
                length = int(value)
            except ValueError:
                raise ProtocolError(
                    "invalid Content-Length %r" % value[:80], self.stream_offset
                ) from None
            if length < 0:
                raise ProtocolError("negative Content-Length", self.stream_offset)
        if length is None:
            raise ProtocolError("missing Content-Length header", self.stream_offset)
        if length > self.max_message_bytes:
            # 声明长度超限：在真正读取主体之前就拒绝，避免缓冲巨型数据。
            raise ProtocolError(
                "declared message length %d exceeds limit %d"
                % (length, self.max_message_bytes),
                self.stream_offset,
            )
        del self._buf[:head_end]
        self._body_len = length
        self._header_done = True
        return True

    def _decode_body(self, raw: bytes, declared_len: int, frame_start: int) -> dict:
        if len(raw) != declared_len:
            raise ProtocolError(
                "body length mismatch: declared=%d actual=%d" % (declared_len, len(raw)),
                frame_start,
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError(
                "invalid UTF-8 in message body: %s" % exc, frame_start
            ) from None
        try:
            message = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProtocolError("invalid JSON: %s" % exc, frame_start) from None
        if not isinstance(message, dict):
            raise ProtocolError("JSON-RPC message must be an object", frame_start)
        return message


def encode_frame(payload: dict) -> bytes:
    """把一个 JSON-RPC 对象编码成带 Content-Length 头的字节帧。"""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return b"Content-Length: %d\r\n\r\n%s" % (len(body), body)
