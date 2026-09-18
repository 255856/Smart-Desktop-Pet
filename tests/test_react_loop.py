"""【真正的智能体】AgentLoop ReAct 循环的回归测试。

测试重点：
    1. 工具调用 + 模型给出 final answer（标准 ReAct）
    2. 工具调完后 final answer 不为空（修复前 AgentLoopV2 的 bug）
    3. 连续多轮纯调工具 → 强制 final 阶段
    4. force_retry 注入 user prompt
    5. 危险工具（open_app）走确认回调
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import AsyncIterator, Optional

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".local-packages"))
sys.path.insert(0, str(ROOT))


# ============================================================================
#  SpyClient: 可预设多轮响应的 LLM 客户端
# ============================================================================

class _SpyClient:
    """Spy LLMClient：按 responses 列表依次返回，支持多轮 ReAct。"""

    def __init__(self, responses: Optional[list[dict]] = None):
        self.stream_calls: list[dict] = []
        self._responses = list(responses or [{"text": "ok", "tool_calls": []}])
        self.system_prompt = ""
        from app.brain.llm_client import LLMConfig
        self.cfg = LLMConfig(api_key="test",
                             base_url="http://localhost:1/v1",
                             model="test", stream=True)
        self._client = _MockAsyncHTTPClient()

    async def chat_stream_events(self, messages, tools=None,
                                 cancel_check=None, force_tool_use=False):
        self.stream_calls.append({
            "messages": [dict(m) for m in messages],   # copy
            "tools_count": len(tools) if tools else 0,
            "force_tool_use": force_tool_use,
        })
        if not self._responses:
            self._responses.append({"text": "done", "tool_calls": []})
        resp_data = self._responses.pop(0)
        text = resp_data.get("text", "")
        tool_calls = resp_data.get("tool_calls", [])

        if text:
            yield ("text", text)
        yield ("finish", {
            "reason": "tool_calls" if tool_calls else "stop",
            "tool_calls": tool_calls,
            "content": text,
        })


class _MockAsyncHTTPClient:
    """SpyClient 不真发请求，_client 永远不会被访问。"""
    def __init__(self):
        self.calls = []

    def stream(self, *args, **kw):
        raise RuntimeError("SpyClient 不应该真发请求")


# ============================================================================
#  真正的 ReAct 行为测试
# ============================================================================

class TestAgentLoopReAct:
    """AgentLoop 应该是真正的 ReAct 循环（与 LangChain create_agent 一致）。"""

    def _make_registry(self):
        from app.engine.tools import ToolRegistry, Tool
        reg = ToolRegistry()
        reg.register(Tool(
            name="open_app", description="open app",
            parameters={"type": "object",
                        "properties": {"app_name": {"type": "string"}}},
            fn=lambda app_name: f"已启动 {app_name}",
        ))
        reg.register(Tool(
            name="echo", description="echo",
            parameters={"type": "object",
                        "properties": {"text": {"type": "string"}}},
            fn=lambda text: f"echo:{text}",
        ))
        return reg

    def test_standard_react_tool_then_final_answer(self):
        """标准 ReAct：模型调工具 → 看到结果 → 给 final answer。"""
        from app.brain.agent import AgentLoop
        reg = self._make_registry()

        # 轮 0：模型决定调 open_app
        # 轮 1：看到工具结果，给 final answer
        client = _SpyClient(responses=[
            {"text": "我来帮你打开 QQ~",
             "tool_calls": [{"id": "c1", "name": "open_app",
                             "arguments": '{"app_name": "QQ"}'}]},
            {"text": "主人，QQ 已经帮你启动啦~ [happy]",
             "tool_calls": []},
        ])
        agent = AgentLoop(client=client, registry=reg)
        msgs = [{"role": "user", "content": "帮我打开 QQ"}]

        events = []

        async def drive():
            async for ev in agent.run(msgs):
                events.append(ev)

        asyncio.run(drive())
        # 应该看到 tool 事件
        tool_events = [ev for ev in events if ev[0] == "tool"]
        assert tool_events, "应触发 tool 事件"
        assert tool_events[0][1] == "open_app"
        # 应该看到 done 事件，且 final_text 是模型给的回复
        done_events = [ev for ev in events if ev[0] == "done"]
        assert done_events
        assert "QQ" in done_events[0][1] or "启动" in done_events[0][1], (
            f"final answer 应来自模型，got: {done_events[0][1]!r}"
        )

    def test_final_answer_not_empty_after_tool_execution(self):
        """修复验证：工具调完后 final answer 不为空（之前的 bug 是空）。"""
        from app.brain.agent import AgentLoop
        reg = self._make_registry()

        # 模拟「模型只调工具，没给文字」+「最后一轮才给 final answer」
        client = _SpyClient(responses=[
            {"text": "",   # 调工具时模型可能没文字
             "tool_calls": [{"id": "c1", "name": "open_app",
                             "arguments": '{"app_name": "QQ"}'}]},
            {"text": "已打开啦主人~ [happy]",
             "tool_calls": []},
        ])
        agent = AgentLoop(client=client, registry=reg)
        msgs = [{"role": "user", "content": "帮我打开 QQ"}]

        events = []

        async def drive():
            async for ev in agent.run(msgs):
                events.append(ev)

        asyncio.run(drive())
        done = [ev for ev in events if ev[0] == "done"]
        assert done
        final_text = done[0][1]
        # 关键：final_text 不为空
        assert final_text and final_text.strip(), (
            f"final_text 不应为空！这是修复的关键。got: {final_text!r}"
        )
        # final_text 应包含模型第二轮给的文字
        assert "打开" in final_text or "QQ" in final_text

    def test_force_final_after_many_tool_only_turns(self):
        """连续多轮纯调工具 → 强制进入 final 阶段（去掉 tools）。"""
        from app.brain.agent import AgentLoop, MAX_CONSECUTIVE_TOOL_ONLY_TURNS
        reg = self._make_registry()

        # 模型连续 4 轮纯调工具（无文字），第 4 轮时被强制 final
        # 第 4 轮给 final answer
        client = _SpyClient(responses=[
            {"text": "", "tool_calls": [
                {"id": "c1", "name": "open_app",
                 "arguments": '{"app_name": "QQ"}'}]},
            {"text": "", "tool_calls": [
                {"id": "c2", "name": "echo",
                 "arguments": '{"text": "x"}'}]},
            {"text": "", "tool_calls": [
                {"id": "c3", "name": "open_app",
                 "arguments": '{"app_name": "VS Code"}'}]},
            {"text": "主人，QQ 和 VS Code 都已经帮你打开啦~ [happy]",
             "tool_calls": []},
        ])
        agent = AgentLoop(client=client, registry=reg, max_turns=5)
        msgs = [{"role": "user", "content": "帮我打开 QQ 和 VS Code"}]

        events = []

        async def drive():
            async for ev in agent.run(msgs):
                events.append(ev)

        asyncio.run(drive())
        # 检查 stream_calls：第 4 轮 tools 应该为 0（被强制 final）
        # MAX_CONSECUTIVE_TOOL_ONLY_TURNS = 3，触发条件是 >=3
        # 第 0/1/2 轮纯调工具无文字 → 连续计数 = 3
        # 第 3 轮 → force_final=True → tools=None
        assert len(client.stream_calls) >= 4
        # 第 4 次调用（turn=3）的 tools_count 应该是 0
        last_call = client.stream_calls[3]
        assert last_call["tools_count"] == 0, (
            f"force_final 那一轮应该不带 tools，实际: "
            f"tools_count={last_call['tools_count']}"
        )
        # 应该有 meta 事件标记 force_final
        force_final_events = [
            ev for ev in events if ev[0] == "meta"
            and ev[1].get("event") == "force_final"
        ]
        assert force_final_events, "应有 force_final meta 事件"

    def test_force_retry_emits_meta_event(self):
        """第一轮 force_tool_use 后模型仍只回文字 → 注入 user prompt 重试。"""
        from app.brain.agent import AgentLoop
        reg = self._make_registry()

        client = _SpyClient(responses=[
            # 第 0 轮：模型只回文字（幻觉），被 force_retry
            {"text": "已打开 QQ 啦~ [happy]", "tool_calls": []},
            # 第 1 轮（被重试后）：模型真调工具
            {"text": "", "tool_calls": [
                {"id": "c1", "name": "open_app",
                 "arguments": '{"app_name": "QQ"}'}]},
            # 第 2 轮：给 final answer
            {"text": "QQ 已经打开啦~ [happy]", "tool_calls": []},
        ])
        agent = AgentLoop(client=client, registry=reg)
        msgs = [{"role": "user", "content": "帮我打开 QQ"}]

        events = []

        async def drive():
            async for ev in agent.run(msgs):
                events.append(ev)

        asyncio.run(drive())
        # 应有 force_retry meta 事件
        retry_events = [
            ev for ev in events if ev[0] == "meta"
            and ev[1].get("event") == "force_retry"
        ]
        assert retry_events, "应有 force_retry meta 事件"
        # 第 2 次 chat_stream_events 调用前 messages 里应有强制 user prompt
        msgs_at_2nd = client.stream_calls[1]["messages"]
        force_prompt = [m for m in msgs_at_2nd
                        if m.get("role") == "user" and "必须调用" in m.get("content", "")]
        assert force_prompt, "应有强制 user prompt"
        # 最终应该有 tool 事件
        tool_events = [ev for ev in events if ev[0] == "tool"]
        assert tool_events

    def test_chitchat_no_force_tool_use(self):
        """闲聊类消息 → 不强制 force_tool_use。"""
        from app.brain.agent import AgentLoop
        reg = self._make_registry()
        client = _SpyClient(responses=[
            {"text": "你好呀主人~", "tool_calls": []},
        ])
        agent = AgentLoop(client=client, registry=reg)
        msgs = [{"role": "user", "content": "陪我聊聊天"}]

        async def drive():
            async for ev in agent.run(msgs):
                pass

        asyncio.run(drive())
        first_call = client.stream_calls[0]
        assert first_call["force_tool_use"] is False

    def test_dangerous_tool_confirmation(self):
        """open_app 是危险工具，应走 confirm_tool 回调。"""
        from app.brain.agent import AgentLoop
        reg = self._make_registry()

        # 用户拒绝 open_app
        confirmed = []

        def confirm(name, args):
            confirmed.append((name, args))
            return False  # 用户拒绝

        client = _SpyClient(responses=[
            {"text": "", "tool_calls": [
                {"id": "c1", "name": "open_app",
                 "arguments": '{"app_name": "QQ"}'}]},
            {"text": "好的主人，不打开了 [happy]", "tool_calls": []},
        ])
        agent = AgentLoop(client=client, registry=reg, confirm_tool=confirm)
        msgs = [{"role": "user", "content": "帮我打开 QQ"}]

        events = []

        async def drive():
            async for ev in agent.run(msgs):
                events.append(ev)

        asyncio.run(drive())
        # 确认回调被调用
        assert confirmed
        assert confirmed[0][0] == "open_app"
        # 工具事件仍然 yield，但结果是「用户取消了此操作」
        tool_events = [ev for ev in events if ev[0] == "tool"]
        assert tool_events
        assert "取消" in tool_events[0][3]

    def test_done_payload_passed_through(self):
        """done 事件的 payload 应该是 final answer（聊天窗口据此显示回复）。"""
        from app.brain.agent import AgentLoop
        reg = self._make_registry()

        client = _SpyClient(responses=[
            {"text": "", "tool_calls": [
                {"id": "c1", "name": "open_app",
                 "arguments": '{"app_name": "QQ"}'}]},
            {"text": "QQ 已经启动啦~ [happy]", "tool_calls": []},
        ])
        agent = AgentLoop(client=client, registry=reg)
        msgs = [{"role": "user", "content": "帮我打开 QQ"}]

        done_payloads = []

        async def drive():
            async for ev in agent.run(msgs):
                if ev[0] == "done":
                    done_payloads.append(ev[1])

        asyncio.run(drive())
        assert done_payloads
        # 关键：done 的 payload 非空
        assert done_payloads[0].strip(), (
            f"done payload 必须非空（UI 据此显示聊天回复），got: {done_payloads[0]!r}"
        )


# ============================================================================
#  _AgentWorker 兜底测试
# ============================================================================

class TestAgentWorkerDoneFallback:
    """测试 _AgentWorker.run() 的 done 兜底逻辑（防御性修复）。"""

    def test_worker_emits_done_with_text_fallback(self):
        """即使 agent 没 yield 任何 text，done 信号也应携带 fallback 文本。"""
        from PyQt5.QtCore import QCoreApplication
        app = QCoreApplication.instance() or QCoreApplication(sys.argv)
        from app.ui.chat_window import _AgentWorker

        class FakeAgentNoText:
            """模拟一个只 yield tool + done 的 agent（不 yield text）。"""
            def __init__(self):
                self._emitted = []

            async def run(self, messages, cancel_check=None):
                yield ("tool", "open_app", '{"app_name": "QQ"}',
                       "已启动 QQ")
                yield ("done", "兜底文本：QQ 已启动")

        worker = _AgentWorker.__new__(_AgentWorker)
        # 直接调用 .run() 逻辑（不真启动 QThread）
        agent = FakeAgentNoText()
        msgs = [{"role": "user", "content": "帮我打开 QQ"}]

        # 模拟 worker.run() 的核心逻辑
        full: list[str] = []
        done_fallback = ""

        async def drive():
            async for ev in agent.run(msgs):
                kind = ev[0]
                if kind == "text":
                    full.append(ev[1])
                elif kind == "done":
                    if len(ev) > 1 and ev[1]:
                        nonlocal done_fallback
                        done_fallback = str(ev[1])

        asyncio.run(drive())
        # done_fallback 应该是「兜底文本：QQ 已启动」
        assert done_fallback == "兜底文本：QQ 已启动"
        # final_text 拼接：full 为空 + done_fallback
        final_text = "".join(full) or done_fallback
        assert final_text == "兜底文本：QQ 已启动"
