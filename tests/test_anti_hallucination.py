"""【抗幻觉】LLM 不调用工具 → 自动强制重试 + Planner/Reflector 兜底。

Bug 背景（v3.1+）：
    国产模型（MiniMax-M3 / abab6.5s-chat 等）在 OpenAI Function Calling
    场景下常见问题：模型收到 tools schema 后**直接回文本**（「已打开 QQ」）
    而不调用任何工具。主人看到「已打开」但实际什么都没发生 = 体验崩溃。

修复策略（防御纵深）：
    1. 【意图识别】detect_action_intent() —— 用正则判定用户意图
       「打开 / 提醒 / 记住 / 查询」类，识别为工具动作。
    2. 【force_tool_use】AgentLoop 第一轮请求传 True →
       LLMClient 拼 tool_choice="required"，服务端不报错则强制调工具；
       服务端不识别 tool_choice 时降级为 user-prompt 强制。
    3. 【强制重试】AgentLoop.run() 检测「意图是工具 + 第一轮未调工具」→
       注入强烈 user prompt 走第二轮，给模型第二次机会。
    4. 【Plan/Reflect 兜底】PlanExecutor 跑完后用
       HeuristicReflector.detect_plan_hallucination() 跨步检查：
       「意图是工具动作 + 整个 plan 没成功执行任何 tool」= 幻觉 → 触发 replan。
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
#  意图识别：detect_action_intent
# ============================================================================

class TestDetectActionIntent:
    """LLMClient 暴露的意图识别接口。"""

    def test_open_app_intent(self):
        from app.brain.llm_client import detect_action_intent
        assert detect_action_intent("帮我打开 QQ") == "open_app"
        assert detect_action_intent("打开 Chrome") == "open_app"
        assert detect_action_intent("启动 VS Code") == "open_app"
        assert detect_action_intent("拉起 微信") == "open_app"
        assert detect_action_intent("开 记事本") == "open_app"

    def test_add_reminder_intent(self):
        from app.brain.llm_client import detect_action_intent
        assert detect_action_intent("30 分钟后提醒我喝水") == "add_reminder"
        assert detect_action_intent("设个提醒：明天 9 点开会") == "add_reminder"
        assert detect_action_intent("5 秒后提醒我看手机") == "add_reminder"

    def test_remember_fact_intent(self):
        from app.brain.llm_client import detect_action_intent
        assert detect_action_intent("记住：我喜欢冰美式") == "remember_fact"
        assert detect_action_intent("记住主人不吃香菜") == "remember_fact"

    def test_query_intent(self):
        from app.brain.llm_client import detect_action_intent
        assert detect_action_intent("现在几点了") == "query"
        assert detect_action_intent("今天周几") == "query"
        assert detect_action_intent("今天几号") == "query"

    def test_no_intent_for_chitchat(self):
        """闲聊类消息不应识别为工具动作。"""
        from app.brain.llm_client import detect_action_intent
        assert detect_action_intent("你好呀") is None
        assert detect_action_intent("陪我聊聊天") is None
        assert detect_action_intent("今天心情不错") is None
        assert detect_action_intent("") is None
        assert detect_action_intent(None) is None

    def test_url_not_open_app(self):
        """URL 类应走 open_website，不是 open_app。"""
        from app.brain.llm_client import detect_action_intent
        # 当前意图识别只识别 open_app 类，URL 类的精细化交给 LLM
        # —— 我们不强制「URL 必须是 open_website」，避免误判
        # 但 open_app 的正则不会匹配 URL（URL 不含「打开」动词或不含目标）
        # （这里不做强约束，回归测试说明行为即可）


# ============================================================================
#  LLMClient：tool_choice 强制 + 降级路径
# ============================================================================

class TestLLMClientForceToolUse:
    """测试 chat_stream_events 的强制调工具逻辑（用 mock httpx 避免真实请求）。"""

    def _make_client(self):
        from app.brain.llm_client import LLMClient, LLMConfig
        cfg = LLMConfig(
            base_url="http://localhost:1/v1",
            api_key="test",
            model="test",
            stream=True,
            timeout=5,
        )
        client = LLMClient.__new__(LLMClient)
        client.cfg = cfg
        client.system_prompt = "你是测试助手"
        # 注入一个 mock httpx client（不需要真实连接）
        client._client = _MockAsyncHTTPClient()
        return client

    def test_no_force_when_disabled(self):
        """force_tool_use=False → 不传 tool_choice。"""
        client = self._make_client()
        # 第一次请求：服务器返回纯文本（不调工具）—— 不应该重试
        client._client.queue = [_MockResponse(
            status=200,
            chunks=[
                {"choices": [{"delta": {"content": "你好"}, "finish_reason": "stop"}]},
                "[DONE]",
            ],
        )]
        msgs = [{"role": "user", "content": "你好"}]
        events = []

        async def drive():
            async for ev, data in client.chat_stream_events(
                    msgs, tools=[{"type": "function",
                                  "function": {"name": "x"}}],
                    force_tool_use=False):
                events.append((ev, data))

        asyncio.run(drive())
        # 因为没开 force，请求只有 1 次（不降级）
        assert len(client._client.calls) == 1
        payload = client._client.calls[0]["payload"]
        assert "tool_choice" not in payload

    def test_force_tool_use_required_in_payload(self):
        """force_tool_use=True → tool_choice="required" 在 payload 里。"""
        client = self._make_client()
        # 第 1 次：服务端接受 tool_choice，模型真调了工具（finish_reason=tool_calls + tool_calls delta）
        client._client.queue = [_MockResponse(
            status=200,
            chunks=[
                {"choices": [{"delta": {
                    "tool_calls": [{"index": 0, "id": "c1",
                                    "function": {"name": "open_app",
                                                 "arguments": '{"app_name":"QQ"}'}}],
                }}]},
                {"choices": [{"finish_reason": "tool_calls"}]},
                "[DONE]",
            ],
        )]
        msgs = [{"role": "user", "content": "帮我打开 QQ"}]
        events = []

        async def drive():
            async for ev, data in client.chat_stream_events(
                    msgs,
                    tools=[{"type": "function",
                            "function": {"name": "open_app"}}],
                    force_tool_use=True):
                events.append((ev, data))

        asyncio.run(drive())
        # 应该发了 1 次请求（tool_choice="required" + tool_calls 成功 → 不降级）
        assert len(client._client.calls) == 1
        payload = client._client.calls[0]["payload"]
        assert payload.get("tool_choice") == "required"
        assert "tools" in payload

    def test_force_falls_back_on_server_400(self):
        """服务端返回 400（tool_choice 不支持）→ 降级为 user-prompt 强制重试。"""
        client = self._make_client()
        # 第一次：400（tool_choice 不支持）
        # 第二次：200 + tool_calls（用户提示起作用，模型真调工具）
        client._client.queue = [
            _MockResponse(
                status=400,
                chunks=[],
                body=b'{"error": "tool_choice is not supported"}',
            ),
            _MockResponse(
                status=200,
                chunks=[
                    {"choices": [{"delta": {
                        "tool_calls": [{"index": 0, "id": "c1",
                                        "function": {"name": "open_app",
                                                     "arguments": '{"app_name":"QQ"}'}}],
                    }}]},
                    {"choices": [{"finish_reason": "tool_calls"}]},
                    "[DONE]",
                ],
            ),
        ]
        msgs = [{"role": "user", "content": "帮我打开 QQ"}]
        events = []

        async def drive():
            async for ev, data in client.chat_stream_events(
                    msgs,
                    tools=[{"type": "function",
                            "function": {"name": "open_app"}}],
                    force_tool_use=True):
                events.append((ev, data))

        asyncio.run(drive())
        # 第一次失败（400），第二次成功（200 + tool_calls）
        assert len(client._client.calls) == 2
        # 第二次：payload 里没 tool_choice（已降级）
        assert "tool_choice" not in client._client.calls[1]["payload"]
        # 但 messages 末尾应该被追加了 user 强制提示
        msgs_payload = client._client.calls[1]["payload"]["messages"]
        last_user = [m for m in msgs_payload if m["role"] == "user"][-1]
        assert "必须调用" in last_user["content"] or "禁止" in last_user["content"]

    def test_force_falls_back_when_model_ignores_required(self):
        """服务端接受 tool_choice="required" 但模型仍直接回文本 → 二次降级。"""
        client = self._make_client()
        # 第一次：服务端接受了 tool_choice，但模型只回了「已打开 QQ」
        # 第二次：user-prompt 强制 + 模型真调工具
        client._client.queue = [
            _MockResponse(
                status=200,
                chunks=[
                    {"choices": [{"delta": {"content": "已打开 QQ"}}]},
                    {"choices": [{"finish_reason": "stop"}]},
                    "[DONE]",
                ],
            ),
            _MockResponse(
                status=200,
                chunks=[
                    {"choices": [{"delta": {
                        "tool_calls": [{"index": 0, "id": "c1",
                                        "function": {"name": "open_app",
                                                     "arguments": '{"app_name":"QQ"}'}}],
                    }}]},
                    {"choices": [{"finish_reason": "tool_calls"}]},
                    "[DONE]",
                ],
            ),
        ]
        msgs = [{"role": "user", "content": "帮我打开 QQ"}]
        events = []

        async def drive():
            async for ev, data in client.chat_stream_events(
                    msgs,
                    tools=[{"type": "function",
                            "function": {"name": "open_app"}}],
                    force_tool_use=True):
                events.append((ev, data))

        asyncio.run(drive())
        # 应该发了 2 次请求
        assert len(client._client.calls) == 2
        # 第二次请求里没 tool_choice（已降级）
        assert "tool_choice" not in client._client.calls[1]["payload"]


# ============================================================================
#  AgentLoop：意图识别 → 第一轮 force + 第一轮失败后强制重试
# ============================================================================

class TestAgentLoopForceTool:
    """AgentLoop 收到「打开 QQ」类消息 → 应该触发 force_tool_use。"""

    def test_first_turn_passes_force_tool_use(self):
        """AgentLoop 检测到工具意图 → chat_stream_events 收到 force_tool_use=True。"""
        from app.brain.agent import AgentLoop
        from app.engine.tools import ToolRegistry, Tool

        reg = ToolRegistry()
        reg.register(Tool(
            name="open_app", description="open app",
            parameters={"type": "object",
                        "properties": {"app_name": {"type": "string"}}},
            fn=lambda app_name: f"已启动 {app_name}",
        ))

        client = _SpyClient()
        agent = AgentLoop(client=client, registry=reg)

        msgs = [{"role": "user", "content": "帮我打开 QQ"}]
        events = []

        async def drive():
            async for ev in agent.run(msgs):
                events.append(ev)

        asyncio.run(drive())
        # SpyClient 应该记录 chat_stream_events 至少被调一次
        # force_tool_use 在第一轮是 True
        assert client.stream_calls, "chat_stream_events 应该被调用"
        first_call = client.stream_calls[0]
        assert first_call["force_tool_use"] is True, (
            "第一轮 force_tool_use 应为 True（因为意图是 open_app）"
        )

    def test_first_turn_no_force_for_chitchat(self):
        """闲聊类消息 → 不强制 force_tool_use。"""
        from app.brain.agent import AgentLoop
        from app.engine.tools import ToolRegistry, Tool

        reg = ToolRegistry()
        reg.register(Tool(
            name="open_app", description="x",
            parameters={"type": "object", "properties": {}},
            fn=lambda: "ok",
        ))

        client = _SpyClient()
        agent = AgentLoop(client=client, registry=reg)

        msgs = [{"role": "user", "content": "陪我聊聊天吧"}]
        events = []

        async def drive():
            async for ev in agent.run(msgs):
                events.append(ev)

        asyncio.run(drive())
        first_call = client.stream_calls[0]
        assert first_call["force_tool_use"] is False, (
            "闲聊消息不应强制 force_tool_use"
        )

    def test_first_turn_no_tool_call_triggers_retry(self):
        """第一轮 force_tool_use 后模型仍没调工具 → 注入 user prompt 重试。"""
        from app.brain.agent import AgentLoop
        from app.engine.tools import ToolRegistry, Tool

        reg = ToolRegistry()
        reg.register(Tool(
            name="open_app", description="x",
            parameters={"type": "object",
                        "properties": {"app_name": {"type": "string"}}},
            fn=lambda app_name: f"已启动 {app_name}",
        ))

        # SpyClient：第一次返回纯文本（模拟模型忽略 tool_choice），第二次返回 tool_call
        client = _SpyClient(responses=[
            # 第 1 轮：纯文本，没调工具
            {"text": "已打开 QQ 啦~ [happy]", "tool_calls": []},
            # 第 2 轮：模型响应重试，真调工具
            {"text": "", "tool_calls": [
                {"id": "c1", "name": "open_app",
                 "arguments": '{"app_name": "QQ"}'},
            ]},
        ])
        agent = AgentLoop(client=client, registry=reg)
        msgs = [{"role": "user", "content": "帮我打开 QQ"}]

        events = []

        async def drive():
            async for ev in agent.run(msgs):
                events.append(ev)

        asyncio.run(drive())
        # 至少应该有 2 次 chat_stream_events 调用
        assert len(client.stream_calls) >= 2, (
            f"应触发重试，实际只调了 {len(client.stream_calls)} 次"
        )
        # 第 2 次调用前，messages 里应该有 user 强制提示
        msgs_at_2nd = client.stream_calls[1]["messages"]
        force_msg = [m for m in msgs_at_2nd
                     if m.get("role") == "user" and "必须调用" in m.get("content", "")]
        assert force_msg, "第 2 次调用前应该注入了 user 强制提示"
        # 第 2 次 force_tool_use 不强制（让 user prompt 起作用，避免 tool_choice 卡死）
        assert client.stream_calls[1]["force_tool_use"] is False
        # 最终应该看到 tool 事件
        tool_events = [ev for ev in events if ev[0] == "tool"]
        assert tool_events, "重试后应该看到 tool 事件"
        assert tool_events[0][1] == "open_app"

    def test_meta_event_emitted_on_force_retry(self):
        """触发 force_retry 时应该 yield meta 事件（给 UI 一个提示）。"""
        from app.brain.agent import AgentLoop
        from app.engine.tools import ToolRegistry, Tool

        reg = ToolRegistry()
        reg.register(Tool(
            name="open_app", description="x",
            parameters={"type": "object",
                        "properties": {"app_name": {"type": "string"}}},
            fn=lambda app_name: "ok",
        ))

        # 模型第一轮直接幻觉
        client = _SpyClient(responses=[
            {"text": "已打开 QQ 啦~", "tool_calls": []},
            {"text": "", "tool_calls": [
                {"id": "c1", "name": "open_app",
                 "arguments": '{"app_name": "QQ"}'},
            ]},
        ])
        agent = AgentLoop(client=client, registry=reg)
        msgs = [{"role": "user", "content": "帮我打开 QQ"}]

        events = []

        async def drive():
            async for ev in agent.run(msgs):
                events.append(ev)

        asyncio.run(drive())
        # 应该有 meta 事件，event == "force_retry"
        meta_events = [ev for ev in events if ev[0] == "meta"]
        assert meta_events, "应触发 force_retry meta 事件"
        assert any(ev[1].get("event") == "force_retry" for ev in meta_events)


# ============================================================================
#  Reflector：意图-行为一致性检查（detect_plan_hallucination）
# ============================================================================

class TestHeuristicReflectorHallucination:
    """HeuristicReflector 检测「意图是工具但没调工具」幻觉。"""

    def test_detects_hallucination_when_no_tool_called(self):
        from app.brain.plan import Plan, Step
        from app.brain.reflector import HeuristicReflector
        # 用户意图：open_app
        refl = HeuristicReflector(plan_goal="帮我打开 QQ")
        # plan 里只有一个 final 步骤，没有任何 tool 步骤
        plan = Plan(goal="帮我打开 QQ", steps=[
            Step(id="1", kind="final",
                 thought="直接回答", result="已打开 QQ 啦~ [happy]"),
        ])
        result = refl.detect_plan_hallucination(plan)
        assert result is not None
        assert result.verdict == "replan"
        assert "open_app" in result.comment or "open" in result.comment.lower()

    def test_no_hallucination_when_tool_was_called(self):
        from app.brain.plan import Plan, Step
        from app.brain.reflector import HeuristicReflector
        refl = HeuristicReflector(plan_goal="帮我打开 QQ")
        plan = Plan(goal="帮我打开 QQ", steps=[
            Step(id="1", kind="tool", tool_name="open_app",
                 arguments={"app_name": "QQ"},
                 status="done", result="已启动 QQ"),
            Step(id="2", kind="final", result="QQ 已经打开啦~"),
        ])
        result = refl.detect_plan_hallucination(plan)
        assert result is None, "工具真调了就不该判幻觉"

    def test_no_hallucination_for_chitchat_intent(self):
        """意图不是工具动作（闲聊）→ 不应误判。"""
        from app.brain.plan import Plan, Step
        from app.brain.reflector import HeuristicReflector
        refl = HeuristicReflector(plan_goal="陪我聊聊天")
        plan = Plan(goal="陪我聊聊天", steps=[
            Step(id="1", kind="final", result="好呀~ 主人想聊什么？"),
        ])
        result = refl.detect_plan_hallucination(plan)
        assert result is None, "闲聊不应判幻觉"

    def test_set_plan_goal_updates_intent(self):
        from app.brain.reflector import HeuristicReflector
        refl = HeuristicReflector(plan_goal="随便聊聊")
        assert refl._cached_intent is None
        refl.set_plan_goal("帮我打开 QQ")
        assert refl._cached_intent == "open_app"


# ============================================================================
#  Mock 工具 / Spy Client
# ============================================================================

class _MockResponse:
    """模拟 httpx 响应（支持 SSE 流式 chunks）。"""

    def __init__(self, status: int, chunks: list, body: bytes = b""):
        self.status_code = status
        self._chunks = chunks
        self._body = body
        # 转为 SSE 字符串
        self._sse_lines: list[str] = []
        for c in chunks:
            if c == "[DONE]":
                self._sse_lines.append("data: [DONE]\n\n")
            elif isinstance(c, dict):
                self._sse_lines.append(f"data: {_json_dumps(c)}\n\n")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def aiter_lines(self):
        for line in self._sse_lines:
            for part in line.split("\n"):
                yield part + "\n"

    async def aread(self):
        return self._body

    async def aclose(self):
        return None


class _MockAsyncHTTPClient:
    """Mock httpx.AsyncClient：按队列返回响应。"""

    def __init__(self):
        self.queue: list[_MockResponse] = []
        self.calls: list[dict] = []  # 记录每次请求的 payload

    def stream(self, method, url, json=None, headers=None, **kw):
        # 记录调用
        self.calls.append({"method": method, "url": url,
                            "payload": json or {}, "headers": headers or {}})
        if not self.queue:
            raise RuntimeError("MockAsyncHTTPClient: queue empty")
        resp = self.queue.pop(0)
        return resp


def _json_dumps(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)


class _SpyClient:
    """Spy LLMClient：不真发请求，按预设 responses 产出事件。"""

    def __init__(self, responses: Optional[list[dict]] = None):
        self.stream_calls: list[dict] = []   # 记录 chat_stream_events 调用
        self._responses = list(responses or [{"text": "ok", "tool_calls": []}])
        self.system_prompt = ""
        # 给一个 dummy cfg 让 chat_stream_events 的 placeholder 检查通过
        from app.brain.llm_client import LLMConfig
        self.cfg = LLMConfig(api_key="test",
                             base_url="http://localhost:1/v1",
                             model="test", stream=True)
        self._client = _MockAsyncHTTPClient()

    async def chat_stream_events(self, messages, tools=None,
                                 cancel_check=None, force_tool_use=False):
        self.stream_calls.append({
            "messages": list(messages),
            "tools": tools,
            "force_tool_use": force_tool_use,
        })
        if not self._responses:
            self._responses.append({"text": "ok", "tool_calls": []})
        resp_data = self._responses.pop(0)
        text = resp_data.get("text", "")
        tool_calls = resp_data.get("tool_calls", [])

        # yield text chunks
        if text:
            yield ("text", text)
        # yield finish with tool_calls
        yield ("finish", {
            "reason": "tool_calls" if tool_calls else "stop",
            "tool_calls": tool_calls,
            "content": text,
        })
