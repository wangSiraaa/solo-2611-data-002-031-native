"""Screen parser tests using pyte on recorded ANSI, not live PTY."""
from __future__ import annotations

import pytest

from terminal_assistant.protocol import parse_screen
from terminal_assistant.screen import ScreenModel


def render(*chunks: bytes) -> ScreenModel:
    model = ScreenModel(80, 24)
    for chunk in chunks:
        model.feed(chunk)
    return model


def title_line(title: str) -> bytes:
    return f"\x1b[1;1H\x1b[2K资料终端 | {title}".encode()


def row(n: int, text: str) -> bytes:
    return f"\x1b[{n};1H\x1b[2K{text}".encode()


def test_main_menu_and_await_code():
    model = render(title_line("主菜单"), row(22, "Q=查询 X=退出"))
    state = parse_screen(model)
    assert state.is_main_menu and not state.await_code
    model.feed(row(22, "输入资料编码"))
    state = parse_screen(model)
    assert state.await_code


def test_query_result_fields():
    model = render(
        title_line("查询结果"),
        row(4, "编码: A001"),
        row(5, "名称: 铅笔"),
        row(6, "单位: 支"),
        row(7, "价格: 1.20"),
        row(8, "修订号: 1"),
        row(22, "E=编辑 B=返回"),
    )
    state = parse_screen(model)
    assert state.is_query_result
    assert state.code == "A001"
    assert state.values == {"name": "铅笔", "unit": "支", "price": "1.20"}
    assert state.revision == 1
    assert not any(state.dirty.values())


def test_edit_dirty_markers_and_prompts():
    model = render(
        title_line("编辑"),
        row(4, "编码: A001"),
        row(5, "名称: 中华铅笔 [尚未保存]"),
        row(6, "单位: 支 [尚未保存]"),
        row(7, "价格: 1.50 [尚未保存]"),
        row(8, "修订号: 1"),
        row(22, "S=保存 B=放弃"),
    )
    state = parse_screen(model)
    assert state.is_edit
    assert state.values == {"name": "中华铅笔", "unit": "支", "price": "1.50"}
    assert all(state.dirty.values())
    assert state.edit_action_prompt


def test_unit_options_parsed_from_visible_prompt():
    model = render(
        title_line("编辑"),
        row(22, "选择单位: 1=支 2=盒 3=袋；空行保留"),
    )
    state = parse_screen(model)
    assert state.unit_options == {"支": "1", "盒": "2", "袋": "3"}


@pytest.mark.parametrize(
    "r19,r18,attr",
    [
        ("保存成功；请重新查询确认", "", "save_success"),
        ("保存冲突: 修订号已变化，请重新查询", "", "save_conflict"),
        ("保存被业务规则拒绝", "", "save_rejected"),
        ("字段拒绝: 价格无效", "", "field_reject"),
        ("", "异常弹窗: E97 操作条件需要人工确认", "unknown_popup"),
    ],
)
def test_feedback_detection(r19, r18, attr):
    model = render(title_line("编辑"), row(18, r18), row(19, r19))
    state = parse_screen(model)
    assert getattr(state, attr)


def test_ack_prompts():
    state = parse_screen(render(title_line("编辑"), row(22, "回车返回查询")))
    assert state.ack_query
    state = parse_screen(render(title_line("查询暂不可用"), row(22, "回车返回主菜单")))
    assert state.ack_main and state.query_unavailable
    state = parse_screen(render(title_line("没有此编码"), row(22, "回车返回主菜单")))
    assert state.not_found


def test_clear_screen_erases_stale_lines():
    model = render(title_line("编辑"), row(19, "保存冲突: 修订号已变化，请重新查询"), row(22, "回车返回查询"))
    assert parse_screen(model).save_conflict
    # A full clear + new main menu must not retain the old feedback line.
    model.feed(b"\x1b[2J\x1b[H")
    model.feed(title_line("主菜单"))
    model.feed(row(22, "Q=查询 X=退出"))
    state = parse_screen(model)
    assert state.is_main_menu
    assert not state.save_conflict and not state.ack_query
