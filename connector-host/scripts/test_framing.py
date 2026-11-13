# -*- coding: utf-8 -*-
"""分帧层单元测试（纯标准库，无需 pytest：python3 scripts/test_framing.py）。

覆盖：字节长度（含中文多字节）、粘连多帧、半头、半主体、半个中文字符、
畸形头、非法 UTF-8、非法 JSON、声明长度超限。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from host.framing import FrameAssembler, ProtocolError, encode_frame  # noqa: E402


def test_chinese_byte_length_and_reassembly() -> None:
    msg = {"jsonrpc": "2.0", "id": 1, "result": {"text": "中文-αβγ"}}
    frame = encode_frame(msg)
    declared = int(frame.split(b"\r\n", 1)[0].split(b":")[1].strip())
    body = frame.split(b"\r\n\r\n", 1)[1]
    assert declared == len(body), "Content-Length 必须是字节数"
    assert declared > len(body.decode("utf-8")), "中文 UTF-8 字节数应大于字符数"
    for chunk_size in (1, 2, 3, 5, 7, 64, 4096):
        asm = FrameAssembler(max_message_bytes=1 << 20)
        got = []
        for i in range(0, len(frame), chunk_size):
            got.extend(asm.feed(frame[i : i + chunk_size]))
        assert got == [msg], f"chunk_size={chunk_size} 重组失败"


def test_glued_partial_header_and_body() -> None:
    msgs = [{"jsonrpc": "2.0", "id": i, "result": {"v": f"数据{i}"}} for i in range(5)]
    stream = b"".join(encode_frame(m) for m in msgs)
    asm = FrameAssembler(1 << 20)
    got = []
    cuts = [0, 12, 13, 40, 41, 100, 250, 251, len(stream)]
    for a, b in zip(cuts, cuts[1:]):
        got.extend(asm.feed(stream[a:b]))
    assert got == msgs


def test_oversize_declaration_rejected_before_body() -> None:
    asm = FrameAssembler(max_message_bytes=128)
    try:
        asm.feed(b"Content-Length: 999999\r\n\r\n" + b"x")
    except ProtocolError as exc:
        assert "exceeds limit" in exc.reason and exc.offset == 0
    else:
        raise AssertionError("超限声明未被拒绝")


def test_bad_header_and_invalid_utf8() -> None:
    asm = FrameAssembler(1 << 20)
    try:
        asm.feed(b"Bad-Header: x\r\n\r\n{}")
    except ProtocolError:
        pass
    else:
        raise AssertionError("畸形头未被拒绝")

    asm2 = FrameAssembler(1 << 20)
    try:
        asm2.feed(b"Content-Length: 3\r\n\r\n\xff\xfe\x00")
    except ProtocolError as exc:
        assert "invalid UTF-8" in exc.reason
    else:
        raise AssertionError("非法 UTF-8 未被拒绝")


def test_invalid_json_and_empty_chunk() -> None:
    asm = FrameAssembler(1 << 20)
    try:
        # 主体长度自洽但内容不是合法 JSON
        asm.feed(b"Content-Length: 5\r\n\r\n{bad}")
    except ProtocolError as exc:
        assert "invalid JSON" in exc.reason
    else:
        raise AssertionError("非法 JSON 未被拒绝")
    assert FrameAssembler(1 << 20).feed(b"") == []


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"\n{len(tests)} framing tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
