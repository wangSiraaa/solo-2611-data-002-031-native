"""Parse the *current* emulated screen into structured state.

All recognition is based on the 80x24 screen rebuilt by pyte, never on raw ANSI
history concatenation.  The legacy application has a single layout, which is
what this parser is written for.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .screen import ScreenModel

_TITLE_RE = re.compile(r"^资料终端\s*\|\s*(.+)$")
_FIELD_RE = re.compile(r"^(编码|名称|单位|价格|修订号): (.*)$")
_UNIT_OPT_RE = re.compile(r"([0-9]+)=([^\s；;，,、]+)")
_DIRTY = " [尚未保存]"


@dataclass
class ScreenState:
    lines: tuple[str, ...]
    title: str | None = None
    record: dict[str, str] = field(default_factory=dict)
    dirty: dict[str, bool] = field(default_factory=dict)
    row18: str = ""
    row19: str = ""
    row22: str = ""
    row23: str = ""

    # ---------------------------------------------------------------- helpers
    @property
    def code(self) -> str | None:
        return self.record.get("编码")

    @property
    def revision(self) -> int | None:
        value = self.record.get("修订号")
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            return None

    @property
    def values(self) -> dict[str, str] | None:
        if all(k in self.record for k in ("名称", "单位", "价格")):
            return {
                "name": self.record["名称"],
                "unit": self.record["单位"],
                "price": self.record["价格"],
            }
        return None

    @property
    def is_main_menu(self) -> bool:
        return self.title == "主菜单" and "Q=查询" in self.row22

    @property
    def await_code(self) -> bool:
        return self.title == "主菜单" and "输入资料编码" in self.row22

    @property
    def is_query_result(self) -> bool:
        return self.title == "查询结果" and self.values is not None

    @property
    def is_edit(self) -> bool:
        return self.title == "编辑"

    @property
    def query_unavailable(self) -> bool:
        return self.title == "查询暂不可用"

    @property
    def not_found(self) -> bool:
        return self.title == "没有此编码"

    @property
    def unknown_popup(self) -> bool:
        return "异常弹窗" in self.row18

    @property
    def save_success(self) -> bool:
        return "保存成功" in self.row19

    @property
    def save_conflict(self) -> bool:
        return "保存冲突" in self.row19

    @property
    def save_rejected(self) -> bool:
        return "保存被业务规则拒绝" in self.row19

    @property
    def field_reject(self) -> bool:
        return "字段拒绝" in self.row19

    @property
    def edit_name_prompt(self) -> bool:
        return self.is_edit and "请输入名称" in self.row22

    @property
    def edit_unit_prompt(self) -> bool:
        return self.is_edit and "选择单位" in self.row22

    @property
    def edit_price_prompt(self) -> bool:
        return self.is_edit and "请输入价格" in self.row22

    @property
    def edit_action_prompt(self) -> bool:
        return self.is_edit and "S=保存" in self.row22

    @property
    def ack_query(self) -> bool:
        return "回车返回查询" in self.row22

    @property
    def ack_main(self) -> bool:
        return "回车返回主菜单" in self.row22

    @property
    def unit_options(self) -> dict[str, str]:
        """Visible unit choices, e.g. {'支': '1', '盒': '2', '袋': '3'}."""
        match = _UNIT_OPT_RE.findall(self.row22)
        return {value: key for key, value in match}

    @property
    def feedback(self) -> str:
        if self.unknown_popup:
            return self.row18.strip()
        if self.save_success:
            return self.row19.strip()
        if self.save_conflict:
            return self.row19.strip()
        if self.save_rejected:
            return self.row19.strip()
        if self.field_reject:
            return self.row19.strip()
        if self.query_unavailable:
            return self.title or ""
        if self.not_found:
            return self.title or ""
        return self.row22.strip()

    def summary(self) -> dict:
        return {
            "title": self.title,
            "record": dict(self.record),
            "dirty": dict(self.dirty),
            "feedback": self.feedback,
            "row18": self.row18.strip(),
            "row19": self.row19.strip(),
            "row22": self.row22.strip(),
        }


def parse_screen(model: ScreenModel) -> ScreenState:
    lines = model.lines
    state = ScreenState(lines=lines)

    first = _row(model, 1)
    match = _TITLE_RE.match(first.strip())
    if match:
        state.title = match.group(1).strip()

    for number in range(4, 9):
        line = _row(model, number).strip()
        match = _FIELD_RE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2)
        dirty = value.endswith(_DIRTY)
        if dirty:
            value = value[: -len(_DIRTY)]
        state.record[key] = value
        if key in ("名称", "单位", "价格"):
            state.dirty[key] = dirty

    state.row18 = _row(model, 18).strip()
    state.row19 = _row(model, 19).strip()
    state.row22 = _row(model, 22).strip()
    state.row23 = _row(model, 23).strip()
    return state


def _row(model: ScreenModel, number: int) -> str:
    return model.row(number)


def full_screen(model: ScreenModel) -> str:
    return "\n".join(f"{i:02d}|{line.rstrip()}" for i, line in enumerate(model.lines, 1))
