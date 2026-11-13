"""共享数据模型与常量。

字节偏移一律为半开区间 [start, end)，从原始字节计算；
UTF-8 字符位置绝不充当字节位置。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 候选起始行必须以 "ISO UTC 时间戳 + 一个空格" 开头。
# 候选逻辑消息只去掉首个时间戳前缀（含该空格），内部时间戳行原样保留。
TIMESTAMP_PREFIX = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z ")


@dataclass(frozen=True)
class Line:
    """物理行：内容字节 + 行终止符，附带半开字节区间。"""

    number: int  # 1 起始的文件内行号
    start: int  # 行首字节偏移
    end: int  # 行终止符之后（或 EOF）的字节偏移
    content: bytes  # 不含终止符的行字节
    term: bytes  # b"\n" 或 b""（文件末尾无换行）

    @property
    def is_blank(self) -> bool:
        # 只有完全空的行才算空行（块边界）；空白字符行仍属正文。
        return self.content == b""


@dataclass(frozen=True)
class Block:
    """由空行划定的独立文本块。"""

    index: int  # 0 起始的块序号
    lines: tuple[Line, ...]

    @property
    def start(self) -> int:
        return self.lines[0].start

    @property
    def end(self) -> int:
        return self.lines[-1].end

    @property
    def line_range(self) -> tuple[int, int]:
        return (self.lines[0].number, self.lines[-1].number)
