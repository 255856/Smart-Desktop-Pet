# -*- coding: utf-8 -*-
"""工具结果状态：界面用**工具信封的状态**渲染，不再从结果文本猜成败（Stage 3.5）。

背景（实测过的信任 bug —— 同一个 bug 的两个方向）：
    信封 status=error   文本='打不开记事本'（没有「错误：」前缀）
        → 界面 ✓ 完成（谎报成功：宠物说它做了，其实没做）
    信封 status=denied  文本='错误：工具 X 未登记到白名单，已拒绝执行。'
        → 界面 ⊘ 取消（主人根本没取消，是策略不让做）
    信封 status=ok      文本='今天天气不错'
        → 界面 ✓ 完成（本来就对）
原因：工具事件以前只把**结果文本**送到界面（信封的 status 被丢掉），
界面只好用文本启发式复算成败（app/ui/chat_window/utils.py::_tool_status）。

修复：信封的 status 随工具事件一路送到界面
      （agent.py 的 tool 事件 → worker.py 的 tool_used 信号 → _on_tool），
      界面有 status 就用它，没有才回退到老的文本判定（老路径行为不变）。
"""
from __future__ import annotations

import asyncio
import sys

import pytest

from app.brain.agent import _tool_event_status
from app.engine.tools._core import ToolResult

# QApplication 必须先于任何 QWidget 存在（跟 tests/test_chat_rendering.py 同款）
from app.core.qt_compat import QApplication
app = QApplication.instance() or QApplication(sys.argv)

from app.ui.chat_window.utils import _tool_status, _unpack_status_mark


@pytest.fixture(autouse=True)
def _no_asr_native_load(monkeypatch):
    """挡住 ASR 预热真加载 CTranslate2（Windows 上会崩进程）。

    跟 tests/test_chat_rendering.py 的同名 fixture 同款：本文件会造一个
    真 ChatWindow，ASR 预热不需要真模型。
    """
    from app.voice import asr as asr_mod
    monkeypatch.setattr(asr_mod.SpeechRecognizer, "_get_model",
                        lambda self, *a, **k: None, raising=False)


def _ui_status(env, text: str = None) -> str:
    """走真实链路：工具信封 → 工具事件里带的状态 → 界面显示的状态。"""
    body = env["data"] if text is None else text
    return _tool_status(body, _tool_event_status(env))


#: (信封 status, error_code, 结果文本, 期望的界面状态)
_ENVELOPE_CASES = [
    # A1：失败但文本没有「错误：」前缀 → 必须是失败（以前显示 ✓）
    ("error", "tool_error", "打不开记事本", "fail"),
    ("error", "", "打不开记事本", "fail"),
    # A2：策略拒绝（白名单 / 等级闸）→ 失败（以前显示 ⊘ 取消）
    ("denied", "not_allowed", "错误：工具 X 未登记到白名单，已拒绝执行。", "fail"),
    ("denied", "level_locked", "错误：X 需要升级到 Lv.2 才能使用。", "fail"),
    # A3：成功（哪怕文本里出现「错误：」）→ 成功
    ("ok", "", "今天天气不错", "ok"),
    ("ok", "", "已取消静音", "ok"),
    ("ok", "", "错误：xxx，但工具本身成功", "ok"),
    # A4：主人主动取消 → ⊘ 取消（信封：denied + user_cancelled）
    ("denied", "user_cancelled", "用户取消了此操作。", "cancel"),
    # 确认弹窗超时：不是主人取消 → 失败
    ("denied", "confirm_timeout", "确认弹窗超时未响应，未执行。", "fail"),
]


@pytest.mark.parametrize("status,error_code,text,expected", _ENVELOPE_CASES)
def test_envelope_status_is_authoritative(status, error_code, text, expected):
    """有信封状态时，界面必须按状态显示，不能再看文本。"""
    env = ToolResult(status, text, error_code=error_code)
    assert _ui_status(env) == expected
    print(f"[OK] 信封 {status}/{error_code or '-'} 文本={text!r} → {expected}", flush=True)


def test_denied_policy_is_failure_not_cancel():
    """A2 单独钉死：策略拒绝 ≠ 主人取消（不能显示 ⊘ 取消）。"""
    env = ToolResult("denied", "错误：工具 X 未登记到白名单，已拒绝执行。",
                     error_code="not_allowed")
    mark = _unpack_status_mark(env["data"], _tool_event_status(env))
    assert "✕ 失败" in mark, mark
    assert "取消" not in mark, mark


def test_user_cancelled_still_shows_cancel():
    """A4：主人主动取消仍然显示 ⊘ 取消。"""
    env = ToolResult("denied", "用户取消了此操作。", error_code="user_cancelled")
    mark = _unpack_status_mark(env["data"], _tool_event_status(env))
    assert "⊘ 取消" in mark, mark


def test_ok_status_with_error_text_shows_success():
    """A3：信封说成功就是成功（文本里的「错误：」不改判）。"""
    env = ToolResult("ok", "错误：xxx，但工具本身成功")
    mark = _unpack_status_mark(env["data"], _tool_event_status(env))
    assert "✓" in mark, mark
    assert "失败" not in mark, mark


def test_text_only_fallback_keeps_old_behaviour():
    """A5：没有 status 的调用方（老路径 / 只有文本的折叠预览）行为**不变**。

    这里把老启发式原来的两条错判也钉住 —— 那是**文本兜底**的既有行为，
    有 status 的新路径不再受它们影响（见上面的用例）。
    """
    assert _tool_status("错误：打不开记事本") == "fail"
    assert _tool_status("打不开记事本") == "ok"          # 老启发式：没前缀 → ok
    # 老启发式把「拒绝执行」当成主人取消（取消标记里有「拒绝执行」）
    assert _tool_status("错误：工具 X 未登记到白名单，已拒绝执行。") == "cancel"
    assert _tool_status("今天天气不错") == "ok"
    assert _tool_status("已取消静音") == "ok"
    assert _tool_status("用户取消了此操作。") == "cancel"


def test_unknown_status_falls_back_to_text():
    """不认识的状态不能当成成功 —— 回退到文本判定。"""
    assert _tool_status("错误：打不开记事本", "什么鬼") == "fail"
    assert _tool_status("今天天气不错", "什么鬼") == "ok"


# ---------------------------------------------------------------------------
#  真实链路：agent 事件 → worker 信号 → 界面
# ---------------------------------------------------------------------------

class _SpyClient:
    """Spy LLMClient：按 responses 列表依次返回，支持多轮 ReAct。"""

    def __init__(self, responses):
        self.stream_calls: list[dict] = []
        self._responses = list(responses)
        self.system_prompt = ""
        from app.brain.llm_client import LLMConfig
        self.cfg = LLMConfig(api_key="test",
                             base_url="http://localhost:1/v1",
                             model="test", stream=True)

    async def chat_stream_events(self, messages, tools=None,
                                 cancel_check=None, force_tool_use=False):
        self.stream_calls.append({"messages": [dict(m) for m in messages]})
        if not self._responses:
            self._responses.append({"text": "done", "tool_calls": []})
        resp_data = self._responses.pop(0)
        text = resp_data.get("text", "")
        tool_calls = resp_data.get("tool_calls", [])
        if text:
            yield ("text", text)
        yield ("finish", {"reason": "tool_calls" if tool_calls else "stop",
                          "tool_calls": tool_calls,
                          "content": text})


def _boom(app_name):
    raise RuntimeError("打不开")


def test_agent_tool_event_carries_envelope_status():
    """真实 AgentLoop：tool 事件必须把信封状态带出来（第 5 元）。"""
    from app.brain.agent import AgentLoop
    from app.engine.tools import ToolRegistry, Tool

    reg = ToolRegistry()
    reg.register(Tool(
        name="open_app", description="open app",
        parameters={"type": "object",
                    "properties": {"app_name": {"type": "string"}}},
        fn=_boom,
    ))
    client = _SpyClient(responses=[
        {"text": "我来帮你打开",
         "tool_calls": [{"id": "c1", "name": "open_app",
                         "arguments": '{"app_name": "记事本"}'}]},
        {"text": "主人，好像没打开成功呢", "tool_calls": []},
    ])
    agent = AgentLoop(client=client, registry=reg)
    events = []

    async def drive():
        async for ev in agent.run([{"role": "user", "content": "帮我打开记事本"}]):
            events.append(ev)

    asyncio.run(drive())
    tool_events = [ev for ev in events if ev[0] == "tool"]
    assert tool_events, "应触发 tool 事件"
    ev = tool_events[0]
    assert len(ev) == 5, f"tool 事件应带出信封状态: {ev!r}"
    assert ev[4] == "error", f"失败工具的状态应是 error: {ev!r}"


def test_worker_signal_carries_status_to_the_ui_slot():
    """真实 worker：agent 的 tool 事件 → tool_used 信号 → 界面槽。

    信号元数变了（3 → 4），这里用真实 worker 验一遍：
      · 新事件（5 元，带状态）→ 信号收到 4 个参数（含状态）；
      · 老事件（3 元，LangChain 后端）→ 状态补空串，界面回退文本判定。
    """
    from app.ui.chat_window.worker import _AgentWorker

    class _Agent5:
        def run_sync(self, messages, cancel_check=None):
            yield ("tool", "open_app", '{"app_name":"记事本"}',
                   "打不开记事本", "error")
            yield ("done", "好的")

    class _Agent3:
        def run_sync(self, messages, cancel_check=None):
            yield ("tool", "open_app", '{"app_name":"记事本"}', "打不开记事本")
            yield ("done", "好的")

    got5 = []
    w5 = _AgentWorker(_Agent5(), [])
    w5.tool_used.connect(lambda *a: got5.append(a))
    w5.run()
    assert got5 == [("open_app", '{"app_name":"记事本"}', "打不开记事本", "error")]

    got3 = []
    w3 = _AgentWorker(_Agent3(), [])
    w3.tool_used.connect(lambda *a: got3.append(a))
    w3.run()
    assert got3 == [("open_app", '{"app_name":"记事本"}', "打不开记事本", "")]


def test_chat_window_tool_card_shows_the_envelope_status():
    """真实 ChatWindow：工具卡片按信封状态渲染（失败不是 ✓ 完成）。"""
    from app.core.config import CharacterConfig
    from app.ui.chat_window import ChatWindow
    from app.ui.chat_window.message import Message

    llm_cfg = type("L", (), {"model": "test", "api_key": "test"})()
    char_cfg = CharacterConfig(name="测试")
    cw = ChatWindow(llm_cfg, char_cfg, "assets/sprites", asr_enabled=False)

    msg = Message(role="assistant", content="", tools=[
        ("open_app", '{"app_name":"记事本"}', "打不开记事本", "error"),
        ("open_app", '{"app_name":"QQ"}', "今天天气不错", "ok"),
    ])
    cw._tools_collapsed_by_msg[id(msg)] = False
    html = cw._tool_steps_html(msg.tools, msg_id=id(msg))
    assert "[失败]" in html, html
    assert "[完成]" in html, html
    print("[OK] 展开态工具卡片:", [s for s in html.split("><")
                                 if "失败" in s or "完成" in s], flush=True)

    # 折叠态预览行也要用状态
    cw._tools_collapsed_by_msg[id(msg)] = True
    html2 = cw._tool_steps_html(msg.tools, msg_id=id(msg))
    assert "✕ 失败" in html2, html2
    print("[OK] 折叠态预览行:", [s for s in html2.split("><")
                                if "✕" in s or "✓" in s], flush=True)

    # _on_tool 把状态一起记进工具记录
    msg2 = Message(role="assistant", content="")
    cw._current_bot_msg = msg2
    cw._on_tool("open_app", "{}", "打不开记事本", "error")
    assert msg2.tools == [("open_app", "{}", "打不开记事本", "error")]
    print("[OK] _on_tool 记录:", msg2.tools, flush=True)
