"""【抗推理污染】区分 reasoning_content 和 content，确保思考痕迹不污染 UI。

测试场景：
    1. 推理模型先输出 reasoning_content（思考痕迹），然后输出 content（最终回复）
    2. UI 应只看到 content（最终回复），不应看到 reasoning_content（思考痕迹）
    3. reasoning_content 应通过 meta 事件（reasoning_delta）转发给 Trace
    4. `` 标签包裹的思考内容也应被剥离
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".local-packages"))
sys.path.insert(0, str(ROOT))


class _MockResponse:
    """模拟 SSE 流式响应。"""

    def __init__(self, chunks: list[str]):
        self.status_code = 200
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def aiter_lines(self):
        for c in self._chunks:
            for line in c.split("\n"):
                if line:
                    yield line + "\n"

    async def aread(self):
        return b""

    async def aclose(self):
        return None


class _MockHTTPClient:
    """按队列返回 mock 响应。"""

    def __init__(self):
        self.queue: list[_MockResponse] = []
        self.calls: list[dict] = []

    def stream(self, *args, **kwargs):
        self.calls.append({"payload": kwargs.get("json")})
        return self.queue.pop(0)


def _make_chunk(delta: dict) -> str:
    """构造一行 SSE chunk。"""
    return f"data: {json.dumps({'choices': [{'delta': delta}]}, ensure_ascii=False)}\n\n"


# ============================================================================
#  LLMClient._do_stream_request 推理分离测试
# ============================================================================

class TestLLMClientReasoningSeparation:
    """LLMClient 必须区分 reasoning_content / reasoning 和 content。"""

    def _make_client(self):
        from app.brain.llm_client import LLMClient, LLMConfig
        cfg = LLMConfig(
            base_url="http://localhost:1/v1",
            api_key="test", model="test", stream=True, timeout=5,
        )
        client = LLMClient.__new__(LLMClient)
        client.cfg = cfg
        client.system_prompt = "你是助手"
        client._client = _MockHTTPClient()
        return client

    def test_reasoning_content_not_yielded_as_text(self):
        """reasoning_content 字段不应 yield 为 text 事件。"""
        client = self._make_client()
        # 推理模型：先发 reasoning_content，然后 content
        chunks = [
            _make_chunk({"reasoning_content": "嗯，用户说晚上好，我应该友好回复。"}),
            _make_chunk({"reasoning_content": "让我想一个温暖的回复。"}),
            _make_chunk({"content": "晚上好主人~"}),
            _make_chunk({"content": "今天过得怎么样呀？"}),
            "data: [DONE]\n\n",
        ]
        client._client.queue = [_MockResponse(chunks)]
        events: list[tuple] = []

        async def drive():
            # 走 chat_stream_events 而不是 chat_stream（事件协议版本）
            async for ev, data in client.chat_stream_events(
                    [{"role": "user", "content": "晚上好啊"}]):
                events.append((ev, data))

        asyncio.run(drive())
        # 收集 text 事件和 meta 事件
        text_events = [ev for ev in events if ev[0] == "text"]
        meta_events = [ev for ev in events if ev[0] == "meta"
                       and isinstance(ev[1], dict)
                       and ev[1].get("event") == "reasoning_delta"]
        finish_events = [ev for ev in events if ev[0] == "finish"]

        # 关键：text 事件只含 content，不含 reasoning_content
        text_concat = "".join(ev[1] for ev in text_events)
        assert text_concat == "晚上好主人~今天过得怎么样呀？", (
            f"text 应只含 content，实际: {text_concat!r}"
        )
        # reasoning_content 应通过 reasoning_delta meta 事件转发
        reasoning_concat = "".join(
            ev[1].get("content", "") for ev in meta_events
        )
        assert "嗯，用户说晚上好" in reasoning_concat, (
            f"reasoning_content 应通过 meta 转发，实际: {reasoning_concat!r}"
        )
        # finish 事件应含 reasoning 字段（调试用）
        assert finish_events
        finish_payload = finish_events[0][1]
        assert "reasoning" in finish_payload
        assert "嗯，用户说晚上好" in finish_payload["reasoning"]

    def test_only_content_no_reasoning(self):
        """纯 content 场景（普通模型）：text 事件正常输出。"""
        client = self._make_client()
        chunks = [
            _make_chunk({"content": "你好~"}),
            "data: [DONE]\n\n",
        ]
        client._client.queue = [_MockResponse(chunks)]
        events = []

        async def drive():
            async for ev, data in client.chat_stream_events(
                    [{"role": "user", "content": "你好"}]):
                events.append((ev, data))

        asyncio.run(drive())
        text_concat = "".join(ev[1] for ev in events if ev[0] == "text")
        assert text_concat == "你好~"

    def test_only_reasoning_no_content_no_tool_calls(self):
        """纯 reasoning_content，无 content 无 tool_calls → fallback 到非流式。"""
        client = self._make_client()
        # 模拟：流式返回的只有 reasoning，没有 content 也没有 tool_calls
        # → _do_stream_request 应该走 fallback（_fetch_non_streaming）
        chunks = [
            _make_chunk({"reasoning_content": "思考..."}),
            "data: [DONE]\n\n",
        ]
        client._client.queue = [
            _MockResponse(chunks),
            # 第二次：非流式 fallback 应该被调（但我们要 mock 它返回空字符串）
        ]
        events = []

        async def drive():
            async for ev, data in client.chat_stream_events(
                    [{"role": "user", "content": "你好"}]):
                events.append((ev, data))

        asyncio.run(drive())
        # 因为 fetch_non_streaming 没真发请求，会返回空 → events 至少应有 finish
        kinds = [ev[0] for ev in events]
        assert "finish" in kinds

    def test_extract_text_chunk_only_returns_content(self):
        """_extract_text_chunk 工具函数只返回 content，不返回 reasoning。"""
        from app.brain.llm_client import _extract_text_chunk, _extract_reasoning_chunk
        # 只有 content
        assert _extract_text_chunk({"content": "hi"}) == "hi"
        # 只有 reasoning → 应返回空字符串（这是关键差异）
        assert _extract_text_chunk({"reasoning_content": "思考"}) == ""
        assert _extract_text_chunk({"reasoning": "思考"}) == ""
        # 两者都有 → 只返回 content
        assert _extract_text_chunk(
            {"content": "hi", "reasoning_content": "思考"}) == "hi"
        # 都没有 → 空
        assert _extract_text_chunk({}) == ""
        # reasoning 提取辅助函数
        assert _extract_reasoning_chunk({"reasoning_content": "r"}) == "r"
        assert _extract_reasoning_chunk({"content": "c"}) == ""
        assert _extract_reasoning_chunk({}) == ""


# ============================================================================
#  AgentLoop 端到端测试：UI 应只看到最终回复
# ============================================================================

class TestAgentLoopReasoningEndToEnd:
    """AgentLoop.run() 产出的 text 事件不应含 reasoning_content。"""

    def _make_setup(self):
        """构造 SpyClient 模拟推理模型（先 reasoning_content 后 content）。"""
        from app.engine.tools import ToolRegistry, Tool
        from app.brain.llm_client import LLMClient, LLMConfig

        reg = ToolRegistry()
        reg.register(Tool(
            name="echo", description="echo",
            parameters={"type": "object",
                        "properties": {"text": {"type": "string"}}},
            fn=lambda text: f"echo:{text}",
        ))

        cfg = LLMConfig(
            base_url="http://localhost:1/v1",
            api_key="test", model="test", stream=True,
        )
        client = LLMClient.__new__(LLMClient)
        client.cfg = cfg
        client.system_prompt = "你是助手"
        client._client = _MockHTTPClient()
        return client, reg

    def test_chitchat_with_reasoning_content(self):
        """闲聊 + 推理模型：UI 应只看到「晚上好主人~」，不应看到思考痕迹。"""
        client, reg = self._make_setup()
        chunks = [
            _make_chunk({"reasoning_content": "用户说晚上好，友好的回复。"}),
            _make_chunk({"reasoning_content": "语气要可爱。"}),
            _make_chunk({"content": "晚上好主人~"}),
            "data: [DONE]\n\n",
        ]
        client._client.queue = [_MockResponse(chunks)]

        from app.brain.agent import AgentLoop
        agent = AgentLoop(client=client, registry=reg)
        msgs = [{"role": "user", "content": "晚上好啊"}]
        events = []

        async def drive():
            async for ev in agent.run(msgs):
                events.append(ev)

        asyncio.run(drive())
        # 收集 text 事件
        text_events = [ev for ev in events if ev[0] == "text"]
        text_concat = "".join(ev[1] for ev in text_events)
        # UI 应只看到最终回复
        assert text_concat == "晚上好主人~", (
            f"UI text 应只含最终回复，实际: {text_concat!r}"
        )
        # 不应包含任何推理痕迹
        assert "用户说晚上好" not in text_concat
        assert "友好" not in text_concat
        # 最终 done 事件的 payload 也应只是最终回复
        done_events = [ev for ev in events if ev[0] == "done"]
        assert done_events
        assert done_events[0][1] == "晚上好主人~"

    def test_tool_call_with_reasoning_content(self):
        """工具调用 + 推理模型：tool 事件正常触发，UI 不看到推理。"""
        client, reg = self._make_setup()
        # 第一轮：模型先思考，然后调工具
        chunks_1 = [
            _make_chunk({"reasoning_content": "用户要 echo x，我调工具。"}),
            _make_chunk({"content": "", "tool_calls": [
                {"index": 0, "id": "c1",
                 "function": {"name": "echo", "arguments": '{"text": "hello"}'}},
            ]}),
            "data: [DONE]\n\n",
        ]
        # 第二轮：工具结果回传，模型给最终回复
        chunks_2 = [
            _make_chunk({"reasoning_content": "工具返回 echo:hello，我整理回复。"}),
            _make_chunk({"content": "已经回显 hello 啦~"}),
            "data: [DONE]\n\n",
        ]
        client._client.queue = [
            _MockResponse(chunks_1),
            _MockResponse(chunks_2),
        ]

        from app.brain.agent import AgentLoop
        agent = AgentLoop(client=client, registry=reg)
        msgs = [{"role": "user", "content": "echo hello"}]
        events = []

        async def drive():
            async for ev in agent.run(msgs):
                events.append(ev)

        asyncio.run(drive())
        # text 事件只含最终回复
        text_concat = "".join(ev[1] for ev in events if ev[0] == "text")
        assert text_concat == "已经回显 hello 啦~"
        # 不应含推理痕迹
        assert "echo:hello" not in text_concat
        assert "整理回复" not in text_concat
        # tool 事件应触发
        tool_events = [ev for ev in events if ev[0] == "tool"]
        assert tool_events
        assert tool_events[0][1] == "echo"
        # done 应是最终回复
        done_events = [ev for ev in events if ev[0] == "done"]
        assert done_events
        assert done_events[0][1] == "已经回显 hello 啦~"


# ============================================================================
#  chat_stream (non-tool) 推理分离测试
# ============================================================================

class TestChatStreamReasoningSeparation:
    """chat_stream（无工具调用路径）也必须分离 reasoning_content。"""

    def test_chat_stream_skips_reasoning_field(self):
        from app.brain.llm_client import LLMClient, LLMConfig
        cfg = LLMConfig(
            base_url="http://localhost:1/v1",
            api_key="test", model="test", stream=True,
        )
        client = LLMClient.__new__(LLMClient)
        client.cfg = cfg
        client.system_prompt = "你是助手"
        client._client = _MockHTTPClient()

        chunks = [
            _make_chunk({"reasoning_content": "思考痕迹"}),
            _make_chunk({"content": "正文"}),
            "data: [DONE]\n\n",
        ]
        client._client.queue = [_MockResponse(chunks)]
        results = []

        async def drive():
            async for tok in client.chat_stream(
                    [type("Msg", (), {"role": "user", "content": "hi"})()]):
                results.append(tok)

        asyncio.run(drive())
        # chat_stream 直接 yield 字符串
        text_concat = "".join(results)
        assert text_concat == "正文"
        assert "思考痕迹" not in text_concat

    def test_chat_stream_strips_think_tags_in_content(self):
        """如果模型把 `` 标签直接写在 content 字段里，应被剥掉。"""
        from app.brain.llm_client import LLMClient, LLMConfig
        cfg = LLMConfig(
            base_url="http://localhost:1/v1",
            api_key="test", model="test", stream=True,
        )
        client = LLMClient.__new__(LLMClient)
        client.cfg = cfg
        client.system_prompt = "你是助手"
        client._client = _MockHTTPClient()

        # 把 `` 标签写在 content 字段里（模型未走 reasoning_content 字段的情况）
        chunks = [
            _make_chunk({"content": "<think>思考</think>"}),
            _make_chunk({"content": "正文"}),
            "data: [DONE]\n\n",
        ]
        client._client.queue = [_MockResponse(chunks)]
        results = []

        async def drive():
            async for tok in client.chat_stream(
                    [type("Msg", (), {"role": "user", "content": "hi"})()]):
                results.append(tok)

        asyncio.run(drive())
        text_concat = "".join(results)
        assert "正文" in text_concat
        assert "思考" not in text_concat
