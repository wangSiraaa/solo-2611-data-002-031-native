"""Line-protocol client for the legacy terminal, based on observed screens.

Every decision below reads the rebuilt pyte screen.  Typed keys are only sent
after a recognised prompt is visibly on screen.  Unknown popups are never
dismissed.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import pexpect

from .protocol import ScreenState, parse_screen, full_screen
from .terminal import TerminalDisconnected, TerminalSession

ACK_HINTS = ("回车返回",)


@dataclass
class Observation:
    state: ScreenState
    eof: bool = False
    timed_out: bool = False
    elapsed: float = 0.0
    detail: str = ""


@dataclass
class ActionRecord:
    ts: str
    step: str
    sent: str | None
    title: str | None
    feedback: str
    detail: str = ""
    screen: str = ""

    def as_dict(self) -> dict:
        return {
            "ts": self.ts,
            "step": self.step,
            "sent": self.sent,
            "title": self.title,
            "feedback": self.feedback,
            "detail": self.detail,
            "screen": self.screen,
        }


@dataclass
class QueryOutcome:
    kind: str  # result | not_found | unavailable | disconnected | timeout | unexpected
    state: ScreenState | None = None
    detail: str = ""


@dataclass
class EditOutcome:
    kind: str  # success | conflict | rejected | field_rejected | aborted | unknown_popup | disconnected | timeout | unexpected
    state: ScreenState | None = None
    feedback: str = ""
    detail: str = ""
    steps: list[ActionRecord] = field(default_factory=list)
    at_main: bool = False


class TerminalClient:
    def __init__(self, session: TerminalSession, action_sink=None) -> None:
        self.session = session
        self.actions: list[ActionRecord] = []
        self._action_sink = action_sink

    # --------------------------------------------------------------- primitives
    @property
    def state(self) -> ScreenState:
        return parse_screen(self.session.model)

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _record(self, step: str, sent: str | None, state: ScreenState, detail: str = "") -> ActionRecord:
        record = ActionRecord(
            ts=self._now(),
            step=step,
            sent=sent,
            title=state.title,
            feedback=state.feedback,
            detail=detail,
            screen=full_screen(self.session.model),
        )
        self.actions.append(record)
        if self._action_sink is not None:
            try:
                self._action_sink(record)
            except Exception:
                pass
        return record

    def _observe(self, predicate, timeout: float, desc: str, require_change_from=None) -> Observation:
        start = time.monotonic()
        last_output = start
        saw_output = False
        while True:
            now = time.monotonic()
            if now - start > timeout:
                return Observation(self.state, timed_out=True, elapsed=now - start, detail=desc)
            try:
                chunk = self.session.child.read_nonblocking(size=65536, timeout=0.05)
            except pexpect.TIMEOUT:
                chunk = b""
            except pexpect.EOF:
                self.session.eof = True
                return Observation(self.state, eof=True, elapsed=time.monotonic() - start, detail=desc)

            if chunk:
                self.session.raw_log.extend(chunk)
                self.session.model.feed(chunk)
                saw_output = True
                last_output = time.monotonic()

            state = self.state
            changed = (
                require_change_from is None
                or self.session.model.fingerprint() != require_change_from
            )
            if predicate(state) and changed and (time.monotonic() - last_output) >= self.session.settle_seconds:
                return Observation(state, elapsed=time.monotonic() - start, detail=desc)
            if (
                require_change_from is None
                and not saw_output
                and now - start > self.session.settle_seconds
                and predicate(state)
            ):
                # Startup-style wait: the screen was already in the expected
                # state and no new output is arriving.
                return Observation(state, elapsed=time.monotonic() - start, detail=desc)

    def _send_and_observe(
        self,
        text: str | None,
        predicate,
        step: str,
        timeout: float = 8.0,
        detail: str = "",
    ) -> Observation:
        pre_fingerprint = self.session.model.fingerprint()
        try:
            if text is None:
                self.session.send_enter()
                sent = ""
            else:
                self.session.send_line(text)
                sent = text
        except TerminalDisconnected as exc:
            state = self.state
            self._record(step, text if text is not None else "", state, f"PTY 断开: {exc}")
            return Observation(state, eof=True, detail=str(exc))
        obs = self._observe(predicate, timeout, step, require_change_from=pre_fingerprint)
        note = detail
        if obs.eof:
            note = (note + " | " if note else "") + "等待反馈时 PTY 断开"
        elif obs.timed_out:
            note = (note + " | " if note else "") + f"等待超时({timeout:.1f}s)"
        self._record(step, sent, obs.state, note)
        return obs

    # --------------------------------------------------------------- navigation
    def start(self) -> Observation:
        obs = self._observe(lambda s: s.is_main_menu, timeout=10.0, desc="等待主菜单")
        self._record("startup", None, obs.state, "主菜单已识别" if obs.state.is_main_menu else "启动后未识别主菜单")
        return obs

    def ack_to_main(self, step: str) -> tuple[bool, ScreenState]:
        """Press ENTER only when a visible 回车返回 prompt asks for it."""
        state = self.state
        if state.is_main_menu:
            return True, state
        if not any(hint in state.row22 for hint in ACK_HINTS):
            self._record(step, None, state, "屏幕没有回车返回提示，拒绝发送回车")
            return False, state
        obs = self._send_and_observe("", lambda s: s.is_main_menu, step, timeout=8.0)
        return (not obs.eof and not obs.timed_out and obs.state.is_main_menu), obs.state

    def query(self, code: str, step_prefix: str = "query") -> QueryOutcome:
        """Navigate Q -> code -> result.  Leaves the client on a result screen
        or back at the main menu after a not-found/unavailable notice."""
        state = self.state
        if not state.is_main_menu:
            return QueryOutcome("unexpected", state, "查询前置状态不是主菜单")
        obs = self._send_and_observe("q", lambda s: s.await_code or s.unknown_popup, f"{step_prefix}:menu")
        self._record(f"{step_prefix}:send-code", code, obs.state, "已发送编码")
        if obs.eof or obs.timed_out or not obs.state.await_code:
            return QueryOutcome("disconnected" if obs.eof else "unexpected", obs.state, "未进入编码输入提示")
        obs = self._send_and_observe(
            code,
            lambda s: s.is_query_result or s.not_found or s.query_unavailable,
            f"{step_prefix}:lookup",
            timeout=8.0,
        )
        if obs.eof:
            return QueryOutcome("disconnected", obs.state, "查询时 PTY 断开")
        if obs.timed_out:
            return QueryOutcome("timeout", obs.state, "查询结果超时")
        if obs.state.is_query_result:
            return QueryOutcome("result", obs.state)
        if obs.state.not_found:
            ok, _ = self.ack_to_main(f"{step_prefix}:ack-not-found")
            return QueryOutcome("not_found", obs.state, "编码不存在；已确认返回" if ok else "编码不存在；回车返回未确认")
        if obs.state.query_unavailable:
            ok, _ = self.ack_to_main(f"{step_prefix}:ack-unavailable")
            return QueryOutcome("unavailable", obs.state, "查询暂不可用；已确认返回" if ok else "查询暂不可用；回车返回未确认")
        return QueryOutcome("unexpected", obs.state, "查询出现未知屏幕")

    # -------------------------------------------------------------------- editing
    def edit_and_save(self, plan, pre: dict, step_prefix: str = "edit") -> EditOutcome:
        """Drive the edit screen for one record and, for known non-success
        outcomes, acknowledge the explicit 回车返回 prompt back to the menu.

        Unknown popups and unrecognised screens are left untouched.
        """
        steps: list[ActionRecord] = []

        def mark(count: int) -> None:
            steps.extend(self.actions[count:])

        start_index = len(self.actions)

        obs = self._send_and_observe(
            "e", lambda s: s.edit_name_prompt or s.unknown_popup, f"{step_prefix}:enter", timeout=8.0
        )
        if obs.eof:
            return EditOutcome("disconnected", obs.state, detail="进入编辑时 PTY 断开", steps=steps)
        if obs.timed_out or not obs.state.edit_name_prompt:
            mark(start_index)
            return EditOutcome(
                "unknown_popup" if obs.state.unknown_popup else "unexpected",
                obs.state,
                detail="进入编辑后未看到名称提示；未发送其他按键",
                steps=steps,
            )

        # Name: blank keeps the old value, used when the plan does not change it.
        name_send = "" if plan.name == pre["name"] else plan.name
        obs = self._send_and_observe(
            name_send,
            lambda s: s.edit_unit_prompt or s.field_reject or s.ack_query or s.unknown_popup,
            f"{step_prefix}:name",
            timeout=8.0,
        )
        if obs.eof:
            mark(start_index)
            return EditOutcome("disconnected", obs.state, detail="提交名称时 PTY 断开", steps=steps)
        if obs.timed_out:
            mark(start_index)
            return EditOutcome("unexpected", obs.state, detail="名称步骤超时；未发送其他按键", steps=steps)
        if obs.state.unknown_popup:
            mark(start_index)
            return EditOutcome("unknown_popup", obs.state, feedback=obs.state.feedback,
                               detail="名称步骤后出现未知弹窗；未发送任何按键，现场保留", steps=steps)
        if obs.state.field_reject:
            return self._finish_rejected("field_rejected", obs.state, obs.state.row19, start_index, steps)
        if not obs.state.edit_unit_prompt:
            mark(start_index)
            return EditOutcome("unexpected", obs.state, detail="名称后未看到单位提示；未发送其他按键", steps=steps)
        if obs.state.record.get("名称") != plan.name:
            return self._abandon(obs.state, start_index, steps, f"名称显示 {obs.state.record.get('名称')!r} 与计划不符")

        # Unit: select from the options actually visible on this screen.
        if plan.unit == pre["unit"]:
            unit_send = ""  # 空行保留
        else:
            options = obs.state.unit_options
            if plan.unit not in options:
                return self._abandon(
                    obs.state,
                    start_index,
                    steps,
                    f"计划单位 {plan.unit!r} 不在可见选项 {sorted(options)} 中",
                )
            unit_send = options[plan.unit]
        obs = self._send_and_observe(
            unit_send,
            lambda s: s.edit_price_prompt or s.field_reject or s.ack_query or s.unknown_popup,
            f"{step_prefix}:unit",
            timeout=8.0,
        )
        if obs.eof:
            mark(start_index)
            return EditOutcome("disconnected", obs.state, detail="提交单位时 PTY 断开", steps=steps)
        if obs.timed_out:
            mark(start_index)
            return EditOutcome("unexpected", obs.state, detail="单位步骤超时；未发送其他按键", steps=steps)
        if obs.state.unknown_popup:
            mark(start_index)
            return EditOutcome("unknown_popup", obs.state, feedback=obs.state.feedback,
                               detail="单位步骤后出现未知弹窗；未发送任何按键，现场保留", steps=steps)
        if obs.state.field_reject:
            return self._finish_rejected("field_rejected", obs.state, obs.state.row19, start_index, steps)
        if not obs.state.edit_price_prompt:
            mark(start_index)
            return EditOutcome("unexpected", obs.state, detail="单位后未看到价格提示；未发送其他按键", steps=steps)
        if obs.state.record.get("单位") != plan.unit:
            return self._abandon(obs.state, start_index, steps, f"单位显示 {obs.state.record.get('单位')!r} 与计划不符")

        # Price: send only when it changes; blank keeps the old value.
        price_send = "" if plan.price == pre["price"] else plan.price_text
        obs = self._send_and_observe(
            price_send,
            lambda s: s.edit_action_prompt or s.field_reject or s.ack_query or s.unknown_popup,
            f"{step_prefix}:price",
            timeout=8.0,
        )
        if obs.eof:
            mark(start_index)
            return EditOutcome("disconnected", obs.state, detail="提交价格时 PTY 断开", steps=steps)
        if obs.timed_out:
            mark(start_index)
            return EditOutcome("unexpected", obs.state, detail="价格步骤超时；未发送其他按键", steps=steps)
        if obs.state.unknown_popup:
            mark(start_index)
            return EditOutcome("unknown_popup", obs.state, feedback=obs.state.feedback,
                               detail="价格步骤后出现未知弹窗；未发送任何按键，现场保留", steps=steps)
        if obs.state.field_reject:
            return self._finish_rejected("field_rejected", obs.state, obs.state.row19, start_index, steps)
        if not obs.state.edit_action_prompt:
            mark(start_index)
            return EditOutcome("unexpected", obs.state, detail="价格后未看到保存提示；未发送其他按键", steps=steps)
        shown_price = obs.state.record.get("价格", "")
        if not _price_matches(shown_price, plan.price):
            return self._abandon(
                obs.state, start_index, steps, f"价格显示 {shown_price!r} 与计划 {plan.price_text} 不符"
            )
        # Pre-save revision drift is visible evidence of an external change: stop.
        if obs.state.revision is not None and obs.state.revision != pre["revision"]:
            return self._abandon(
                obs.state,
                start_index,
                steps,
                f"进入编辑后修订号 {obs.state.revision} 与查询时 {pre['revision']} 不同",
            )

        # Save.  This is the only key that may commit.
        obs = self._send_and_observe(
            "s",
            lambda s: s.save_success
            or s.save_conflict
            or s.save_rejected
            or s.unknown_popup
            or s.ack_query,
            f"{step_prefix}:save",
            timeout=10.0,
            detail="已发送 S",
        )
        if obs.eof:
            mark(start_index)
            return EditOutcome("disconnected", obs.state, detail="发送保存后、保存反馈前 PTY 断开", steps=steps)
        if obs.timed_out:
            mark(start_index)
            return EditOutcome("timeout", obs.state, detail="保存反馈超时；未发送其他按键", steps=steps)
        if obs.state.unknown_popup:
            mark(start_index)
            return EditOutcome(
                "unknown_popup",
                obs.state,
                feedback=obs.state.feedback,
                detail="出现未知弹窗；未发送任何按键，现场保留",
                steps=steps,
            )
        if obs.state.save_success:
            feedback = obs.state.row19
            ok, state = self.ack_to_main(f"{step_prefix}:ack-success")
            mark(start_index)
            return EditOutcome(
                "success",
                state,
                feedback=feedback,
                detail="保存成功提示已见；已回车退出编辑" if ok else "保存成功提示后回车未确认",
                steps=steps,
                at_main=ok,
            )
        if obs.state.save_conflict:
            feedback = obs.state.row19
            ok, state = self.ack_to_main(f"{step_prefix}:ack-conflict")
            mark(start_index)
            return EditOutcome(
                "conflict",
                state,
                feedback=feedback,
                detail="保存冲突；已回车退出编辑，等待重新查询" if ok else "保存冲突；回车返回未确认",
                steps=steps,
                at_main=ok,
            )
        if obs.state.save_rejected:
            feedback = obs.state.row19
            ok, state = self.ack_to_main(f"{step_prefix}:ack-rejected")
            mark(start_index)
            return EditOutcome(
                "rejected",
                state,
                feedback=feedback,
                detail="保存被业务规则拒绝；已回车退出编辑" if ok else "保存被拒绝；回车返回未确认",
                steps=steps,
                at_main=ok,
            )
        mark(start_index)
        return EditOutcome(
            "unexpected", obs.state, detail="保存后出现未知屏幕；未发送其他按键", steps=steps
        )

    def _finish_rejected(
        self, kind: str, state: ScreenState, feedback: str, start_index: int, steps: list[ActionRecord]
    ) -> EditOutcome:
        ok, after = self.ack_to_main("edit:ack-field-reject")
        steps.extend(self.actions[start_index:])
        return EditOutcome(
            kind,
            after,
            feedback=feedback,
            detail="字段被拒绝；已回车退出编辑" if ok else "字段被拒绝；回车返回未确认",
            steps=steps,
            at_main=ok,
        )

    def _abandon(
        self, state: ScreenState, start_index: int, steps: list[ActionRecord], reason: str
    ) -> EditOutcome:
        """Leave the edit screen without saving, preserving old values."""
        if state.edit_unit_prompt:
            obs = self._send_and_observe(
                "", lambda s: s.edit_price_prompt or s.ack_query, "edit:abandon-unit", timeout=8.0
            )
            if obs.eof or obs.timed_out or not obs.state.edit_price_prompt:
                steps.extend(self.actions[start_index:])
                return EditOutcome("unexpected", obs.state, detail=f"{reason}；放弃路径中止且未发送其他按键", steps=steps)
            state = obs.state
        if state.edit_price_prompt:
            obs = self._send_and_observe(
                "", lambda s: s.edit_action_prompt or s.ack_query, "edit:abandon-price", timeout=8.0
            )
            if obs.eof or obs.timed_out or not obs.state.edit_action_prompt:
                steps.extend(self.actions[start_index:])
                return EditOutcome("unexpected", obs.state, detail=f"{reason}；放弃路径中止且未发送其他按键", steps=steps)
            state = obs.state
        if state.edit_action_prompt:
            obs = self._send_and_observe(
                "b", lambda s: s.is_main_menu, "edit:abandon-b", timeout=8.0
            )
            steps.extend(self.actions[start_index:])
            if obs.eof or obs.timed_out or not obs.state.is_main_menu:
                return EditOutcome("unexpected", obs.state, detail=f"{reason}；B 后未确认返回主菜单", steps=steps)
            return EditOutcome(
                "aborted", obs.state, detail=reason + "；已用 B 放弃，未写入", steps=steps, at_main=True
            )
        steps.extend(self.actions[start_index:])
        return EditOutcome("unexpected", state, detail=f"{reason}；不在可放弃的编辑提示上，未发送按键", steps=steps)


def _price_matches(shown: str, plan_price: Decimal) -> bool:
    try:
        value = Decimal(shown.strip())
    except (InvalidOperation, ValueError):
        return False
    return value.is_finite() and value == plan_price


def _quit_main(client: TerminalClient) -> bool:
    """Send X only on a recognised main menu and drain until the app exits."""
    state = client.state
    if not state.is_main_menu:
        client._record("quit", None, state, "不在主菜单，拒绝发送 X")
        return False
    try:
        client.session.send_line("x")
    except TerminalDisconnected:
        return True
    client._record("quit", "x", state, "主菜单发送 X")
    end = time.monotonic() + 5.0
    while time.monotonic() < end:
        try:
            chunk = client.session.child.read_nonblocking(size=65536, timeout=0.05)
        except pexpect.TIMEOUT:
            continue
        except pexpect.EOF:
            client.session.eof = True
            return True
        if chunk:
            client.session.raw_log.extend(chunk)
            client.session.model.feed(chunk)
    return not client.session.is_alive()


def record_query(outcome: QueryOutcome) -> dict:
    return {
        "kind": outcome.kind,
        "detail": outcome.detail,
        "title": outcome.state.title if outcome.state else None,
        "record": dict(outcome.state.record) if outcome.state else None,
    }
