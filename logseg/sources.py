"""源文件的字节级结构切分：物理行、空行块、JSONL 对象。

所有偏移都来自原始字节，绝不从解码后的字符位置推算。
"""

from __future__ import annotations

from .model import Block, Line


class SourceDecodeError(ValueError):
    """源字节不是合法 UTF-8，无法按声明编码解释。"""


def split_lines(raw: bytes) -> list[Line]:
    """把原始字节切成物理行，保留每行的半开字节区间。

    文件以换行结尾时不产生幻影空行；末尾无换行时最后一行
    term 为 b""。
    """
    lines: list[Line] = []
    start = 0
    number = 1
    size = len(raw)
    while start < size:
        nl = raw.find(b"\n", start)
        if nl == -1:
            lines.append(Line(number, start, size, raw[start:], b""))
            break
        lines.append(Line(number, start, nl + 1, raw[start:nl], b"\n"))
        start = nl + 1
        number += 1
    return lines


def split_blocks(lines: list[Line]) -> list[Block]:
    """空行划定独立块；空行本身不是事件，也不进入任何块。"""
    blocks: list[Block] = []
    current: list[Line] = []
    for line in lines:
        if line.is_blank:
            if current:
                blocks.append(Block(len(blocks), tuple(current)))
                current = []
        else:
            current.append(line)
    if current:
        blocks.append(Block(len(blocks), tuple(current)))
    return blocks


def decode_line(line: Line) -> str:
    try:
        return line.content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise SourceDecodeError(
            f"line {line.number}: invalid UTF-8: {exc}"
        ) from exc


def decode_lines(lines: list[Line]) -> list[str]:
    return [decode_line(line) for line in lines]
