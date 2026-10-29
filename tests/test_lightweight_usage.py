"""两条后端路径的 usage 事件测试（全部离线，不需要 API Key、不发网络包）。

- 轻量路径: 用 httpx.MockTransport 把 HTTP 层打桩, 驱动 LLMClient.chat_stream,
  断言它真的抛出了 usage 事件（以前只写不抛 → 无工具纯聊天 token 永远是 0）。
- 标准路径: 用假的 astream 驱动 LangChainAgent._astream_with_retry,
  断言 usage 事件形状跟轻量路径完全一致。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from app.brain.llm_client import (
    ChatMessage,
    LLMClient,
    UsageEvent,
)
from app.core.config import LLMConfig


_SSE = (
    'data: {"choices":[{"delta":{"content":"你好"}}]}\n'
    "\n"
    'data: {"choices":[{"delta":{"content":"呀"}}]}\n'
    "\n"
    'data: {"choices":[],"usage":{"prompt_tokens":120,"completion_tokens":7,'
    '"total_tokens":127,"prompt_tokens_details":{"cached_tokens":100}}}\n'
    "\n"
    "data: [DONE]\n"
    "\n"
)

#: 两条路径都必须有的载荷键
USAGE_KEYS = {
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cache_hit_tokens",
    "cache_miss_tokens",
    "cache_write_tokens",
    "got_real",
    "cache_hit_rate",
}


def _handler(request: httpx.Request) -> httpx.Response:
    """打桩的 HTTP 层：一个包都不发出去。"""
    return httpx.Response(200, content=_SSE.encode("utf-8"))


def _rows(path):
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _use_tmp_ledger(monkeypatch, tmp_path):
    from app.brain import token_ledger
    path = tmp_path / "token-usage.jsonl"
    monkeypatch.setattr(token_ledger, "_SINGLETON",
                        token_ledger.TokenLedger(path))
    return path


def test_lightweight_chat_stream_yields_usage_event(tmp_path, monkeypatch):
    """B1: 轻量无工具路径真的抛出 usage 事件（token 数非 0）。"""
    ledger_path = _use_tmp_ledger(monkeypatch, tmp_path)

    cfg = LLMConfig(base_url="http://127.0.0.1:9/v1", api_key="sk-test-key",
                    model="test-model")
    client = LLMClient(cfg, "测试人设")

    async def _run():
        try:
            # 把 HTTP 层换成 MockTransport —— 一个包都不发出去
            await client._client.aclose()
            client._client = httpx.AsyncClient(
                transport=httpx.MockTransport(_handler))
            items = []
            async for item in client.chat_stream(
                    [ChatMessage(role="user", content="你好")]):
                items.append(item)
            return items
        finally:
            await client.close()

    items = asyncio.run(_run())

    # 用户看到的文本一个字都没变
    texts = [x for x in items if not isinstance(x, UsageEvent)]
    assert "".join(texts) == "你好呀"

    # usage 事件真的抛出来了
    events = [x for x in items if isinstance(x, UsageEvent)]
    assert len(events) == 1
    payload = events[0].payload
    assert payload["event"] == "usage"
    assert payload["prompt_tokens"] == 120
    assert payload["completion_tokens"] == 7
    assert payload["total_tokens"] == 127
    assert payload["cache_hit_tokens"] == 100
    assert payload["cache_miss_tokens"] == 20
    assert payload["cache_write_tokens"] == 0
    assert payload["got_real"] is True
    assert payload["cache_hit_rate"] == round(100 / 120, 4)
    assert set(payload.keys()) == USAGE_KEYS | {"event"}

    # 形状跟 LangChain 路径一致: ("meta", payload)
    assert events[0][0] == "meta"
    assert tuple(events[0]) == ("meta", payload)

    # 老消费方（chat_window/worker._StreamWorker 只认 str）看到的是空文本
    assert str(events[0]) == ""
    assert not events[0]
    assert "".join(items) == "你好呀"

    # 台账收到了一条（backend=lightweight）
    rows = _rows(ledger_path)
    assert len(rows) == 1
    assert rows[0]["backend"] == "lightweight"
    assert rows[0]["prompt_tokens"] == 120
    assert rows[0]["cache_hit_tokens"] == 100
    assert rows[0]["cache_miss_tokens"] == 20


def test_standard_path_yields_the_same_usage_shape(tmp_path, monkeypatch):
    """B2: 标准（LangChain）路径的 usage 事件形状跟轻量路径一致。"""
    from langchain_core.messages import AIMessageChunk

    from app.brain.langchain_agent import LangChainAgent

    ledger_path = _use_tmp_ledger(monkeypatch, tmp_path)

    class _FakeAgent:
        async def astream(self, input_payload, config=None, stream_mode=None):
            yield (AIMessageChunk(content="你好", usage_metadata={
                "input_tokens": 120,
                "output_tokens": 7,
                "total_tokens": 127,
                "input_token_details": {"cache_read": 100, "cache_creation": 0},
            }), {})

    class _Stub:
        _agent = _FakeAgent()
        llm_cfg = LLMConfig(model="test-model")

    async def _run():
        events = []
        async for ev in LangChainAgent._astream_with_retry(_Stub(), {}, {}, None):
            events.append(ev)
        return events

    events = asyncio.run(_run())
    usage_events = [ev for ev in events if isinstance(ev, UsageEvent)]
    assert len(usage_events) == 1
    payload = usage_events[0].payload
    assert payload["event"] == "usage"
    assert payload["prompt_tokens"] == 120
    assert payload["completion_tokens"] == 7
    assert payload["total_tokens"] == 127
    assert payload["cache_hit_tokens"] == 100
    assert payload["cache_miss_tokens"] == 20
    assert set(payload.keys()) == USAGE_KEYS | {"event"}
    assert usage_events[0][0] == "meta"

    # 台账也收到了（backend=standard）
    rows = _rows(ledger_path)
    assert len(rows) == 1
    assert rows[0]["backend"] == "standard"
    assert rows[0]["cache_hit_tokens"] == 100


def test_lightweight_and_standard_paths_use_the_same_session_id(tmp_path,
                                                                 monkeypatch):
    """C2: 轻量路径的台账行 session_id == 标准路径用的那个。

    标准路径用 LangChainAgent._session_id（聊天窗传进去的 self._session_id），
    轻量路径用台账的当前会话（聊天窗在构造 LLMClient 之前写进去）——
    两条路径因此落进同一个 session 桶。
    """
    from langchain_core.messages import AIMessageChunk

    from app.brain import token_ledger
    from app.brain.langchain_agent import LangChainAgent

    ledger_path = _use_tmp_ledger(monkeypatch, tmp_path)
    session_id = "char:台账测试娘:main"
    old_session = token_ledger.current_session()

    class _FakeAgent:
        async def astream(self, input_payload, config=None, stream_mode=None):
            yield (AIMessageChunk(content="你好", usage_metadata={
                "input_tokens": 120,
                "output_tokens": 7,
                "total_tokens": 127,
                "input_token_details": {"cache_read": 100},
            }), {})

    class _Stub:
        _agent = _FakeAgent()
        llm_cfg = LLMConfig(model="test-model")
        _session_id = session_id

    async def _run_standard():
        async for _ev in LangChainAgent._astream_with_retry(_Stub(), {}, {}, None):
            pass

    try:
        asyncio.run(_run_standard())

        # 轻量路径: 聊天窗把 session_id 写进台账的当前会话
        token_ledger.set_session(session_id)
        cfg = LLMConfig(base_url="http://127.0.0.1:9/v1", api_key="sk-test-key",
                        model="test-model")
        client = LLMClient(cfg, "人设")

        async def _run_light():
            await client._client.aclose()
            client._client = httpx.AsyncClient(
                transport=httpx.MockTransport(_handler))
            try:
                async for _item in client.chat_stream(
                        [ChatMessage(role="user", content="你好")]):
                    pass
            finally:
                await client.close()

        asyncio.run(_run_light())
    finally:
        token_ledger.set_session(old_session)

    rows = _rows(ledger_path)
    standard = [r for r in rows if r["backend"] == "standard"]
    lightweight = [r for r in rows if r["backend"] == "lightweight"]
    assert standard and lightweight
    assert standard[0]["session_id"] == session_id
    assert lightweight[0]["session_id"] == session_id
    assert standard[0]["session_id"] == lightweight[0]["session_id"]


def test_default_ledger_file_is_created_and_populated():
    """台账文件会被创建并写进一行。

    测试会话里台账路径被 DESKTOP_PET_TOKEN_LEDGER 覆盖到临时文件 ——
    仓库的 data/token-usage.jsonl 是 0.5 的基线测量文件，测试不许碰它。
    """
    from app.brain import token_ledger

    usage = {
        "prompt_tokens": 120,
        "completion_tokens": 7,
        "total_tokens": 127,
        "cache_hit_tokens": 100,
        "cache_miss_tokens": 20,
        "cache_write_tokens": 0,
        "got_real": True,
        "cache_hit_rate": round(100 / 120, 4),
    }
    ok = token_ledger.record_usage(
        usage, backend="lightweight", model="test-model",
        session_id="test:default-ledger", request_index=1)
    assert ok is True

    path = token_ledger.default_ledger_path()
    assert path.is_file()
    # 不是仓库里的基线测量文件
    repo_ledger = (Path(__file__).resolve().parent.parent
                   / "data" / "token-usage.jsonl")
    assert path != repo_ledger, "测试不许写仓库里的基线台账文件"
    # 台账里这个 key 折叠后只有一条（重复到达 → 覆盖，不累加）
    summary = token_ledger.get_ledger().summary("test:default-ledger")
    assert summary["requests"] == 1
    assert summary["prompt_tokens"] == 120
    assert summary["cache_hit_tokens"] == 100
    assert summary["cache_miss_tokens"] == 20
    assert summary["by_backend"]["lightweight"]["prompt_tokens"] == 120
