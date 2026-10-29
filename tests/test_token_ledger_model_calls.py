"""P0-12：标准后端「一次模型调用 = 一条台账行」的回归测试。

缺陷：标准后端（LangChainAgent）以前按「整个 astream」分配 request_index。
一个回合里模型被调用多次（模型调工具 -> 工具结果 -> 再调模型）时，这些调用
共用同一个台账主键 (session_id, request_index, backend, retry)，台账按 key
折叠（后到覆盖先到）-> **只有最后一次调用的 token 留下来，前几次被静默丢掉**。
工具调用越多，少算得越多。

修法：request_index 按「模型调用」分配（轻量后端本来就是按 provider HTTP
请求分配的，两个后端语义因此一致）。

全部离线：mock graph 的 astream，一个包都不发出去。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

from app.brain import token_ledger
from app.brain.langchain_agent import (
    LangChainAgent,
    LangChainAgentConfig,
)
from app.brain.llm_client import LLMConfig
from app.engine.tools import Tool, ToolRegistry


def _usage_meta(prompt: int, completion: int, cache_read: int = 0) -> dict:
    """LangChain 的 usage_metadata 形状（缓存桶在 input_token_details 里）。"""
    return {
        "input_tokens": prompt,
        "output_tokens": completion,
        "total_tokens": prompt + completion,
        "input_token_details": {"cache_read": cache_read, "cache_creation": 0},
    }


def _registry() -> ToolRegistry:
    reg = ToolRegistry()

    def now() -> str:
        """return current time."""
        return "2026-01-01 00:00:00"

    reg.register(Tool(
        name="now",
        description="return current time",
        parameters={"type": "object", "properties": {}},
        fn=now,
    ))
    return reg


def _agent() -> LangChainAgent:
    llm = LLMConfig(api_key="sk-test", model="test-model", stream=False)
    return LangChainAgent(
        llm_cfg=llm, registry=_registry(), persona="你是助手",
        cfg=LangChainAgentConfig(enable_checkpointer=False),
    )


def _use_tmp_ledger(monkeypatch, tmp_path) -> Path:
    """把台账指向临时文件（绝不碰 data/token-usage.jsonl）。"""
    path = tmp_path / "token-usage.jsonl"
    monkeypatch.setattr(token_ledger, "_SINGLETON",
                        token_ledger.TokenLedger(path))
    return path


def _rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _fake_graph(chunks):
    """造一个只吐受控 (chunk, meta) 序列的 mock graph。"""
    fake_graph = MagicMock()

    async def fake_astream(payload, config=None, stream_mode=None):
        for chunk, meta in chunks:
            yield chunk, meta

    fake_graph.astream = fake_astream
    return fake_graph


def _run(agent, chunks):
    """用 mock graph 跑一轮标准后端 turn，返回事件列表。"""
    agent._agent = _fake_graph(chunks)

    async def collect():
        out = []
        async for ev in agent.run([{"role": "user", "content": "hi"}]):
            out.append(ev)
        return out

    return asyncio.run(collect())


def test_tool_turn_records_one_row_per_model_call(monkeypatch, tmp_path):
    """一个回合里模型被调用两次（调工具 -> 工具结果 -> 再调模型）-> 台账两行。"""
    from langchain_core.messages import AIMessageChunk, ToolMessage

    path = _use_tmp_ledger(monkeypatch, tmp_path)
    agent = _agent()

    _run(agent, [
        (AIMessageChunk(content="", usage_metadata=_usage_meta(100, 10, 80)),
         {"langgraph_step": 1, "langgraph_node": "model"}),
        (ToolMessage(content="2026-01-01 00:00:00", name="now", tool_call_id="t1"),
         {"langgraph_step": 2, "langgraph_node": "tools"}),
        (AIMessageChunk(content="现在 0 点", usage_metadata=_usage_meta(200, 20, 180)),
         {"langgraph_step": 3, "langgraph_node": "model"}),
    ])

    rows = _rows(path)
    # 两次模型调用 -> 两行（修之前只有一行：后一次按同一主键覆盖前一次）
    assert len(rows) == 2
    # 主键不同（request_index 按模型调用分配）
    assert rows[0]["request_index"] != rows[1]["request_index"]
    # token 一个都不少
    assert sum(r["prompt_tokens"] for r in rows) == 300
    assert sum(r["completion_tokens"] for r in rows) == 30
    assert sum(r["total_tokens"] for r in rows) == 330
    assert sum(r["cache_hit_tokens"] for r in rows) == 260
    assert sum(r["cache_miss_tokens"] for r in rows) == 40
    # 汇总也认这两次请求
    summary = token_ledger.get_ledger().summary()
    assert summary["requests"] == 2
    assert summary["prompt_tokens"] == 300
    assert summary["completion_tokens"] == 30


def test_same_model_call_usage_twice_records_one_row(monkeypatch, tmp_path):
    """同一次模型调用里 provider 重复上报 usage -> 折叠成一行（不重复计数）。"""
    from langchain_core.messages import AIMessageChunk

    path = _use_tmp_ledger(monkeypatch, tmp_path)
    agent = _agent()

    _run(agent, [
        (AIMessageChunk(content="a", usage_metadata=_usage_meta(100, 10)),
         {"langgraph_step": 1, "langgraph_node": "model"}),
        (AIMessageChunk(content="b", usage_metadata=_usage_meta(100, 12)),
         {"langgraph_step": 1, "langgraph_node": "model"}),
    ])

    rows = _rows(path)
    assert len(rows) == 1
    # 同主键折叠：后到的覆盖先到的
    assert rows[0]["prompt_tokens"] == 100
    assert rows[0]["completion_tokens"] == 12


def test_single_model_call_records_exactly_one_row(monkeypatch, tmp_path):
    """只有一次模型调用的回合 -> 仍然只有一行（行为不变）。"""
    from langchain_core.messages import AIMessageChunk

    path = _use_tmp_ledger(monkeypatch, tmp_path)
    agent = _agent()

    _run(agent, [
        (AIMessageChunk(content="你好", usage_metadata=_usage_meta(100, 10)),
         {"langgraph_step": 1, "langgraph_node": "model"}),
    ])

    rows = _rows(path)
    assert len(rows) == 1
    assert rows[0]["prompt_tokens"] == 100
    assert rows[0]["completion_tokens"] == 10
    assert rows[0]["backend"] == "standard"
    summary = token_ledger.get_ledger().summary()
    assert summary["requests"] == 1


def test_plan_mode_records_one_row_per_model_call(monkeypatch, tmp_path):
    """plan 路径用的是同一个记账口径（一次模型调用 = 一行）。"""
    from langchain_core.messages import AIMessageChunk

    path = _use_tmp_ledger(monkeypatch, tmp_path)
    agent = _agent()
    agent._agent = _fake_graph([
        (AIMessageChunk(content="计划", usage_metadata=_usage_meta(50, 5)),
         {"langgraph_step": 1, "langgraph_node": "model"}),
    ])

    async def collect():
        out = []
        async for ev in agent.plan([{"role": "user", "content": "做个计划"}]):
            out.append(ev)
        return out

    asyncio.run(collect())

    rows = _rows(path)
    assert len(rows) == 1
    assert rows[0]["prompt_tokens"] == 50
    assert rows[0]["completion_tokens"] == 5


def test_tracker_reuses_index_within_one_model_call():
    """同一次模型调用（同一个 langgraph_step）-> 同一个 request_index。"""
    from app.brain.langchain_agent import _ModelCallUsageTracker

    tracker = _ModelCallUsageTracker()
    first = tracker.request_index({"langgraph_step": 1, "langgraph_node": "model"})
    again = tracker.request_index({"langgraph_step": 1, "langgraph_node": "model"})
    later = tracker.request_index({"langgraph_step": 3, "langgraph_node": "model"})
    assert first == again
    assert later != first


def test_tracker_without_metadata_allocates_per_usage_event():
    """拿不到 LangGraph 元数据 -> 退回「一次 usage 事件 = 一次模型调用」。"""
    from app.brain.langchain_agent import _ModelCallUsageTracker

    tracker = _ModelCallUsageTracker()
    assert tracker.request_index({}) != tracker.request_index({})
    assert tracker.request_index(None) != tracker.request_index(None)


def test_ledger_folds_same_key_but_keeps_different_keys(tmp_path):
    """台账层不变量：同主键 -> 一行（后到覆盖先到）；不同 request_index -> 两行。"""
    led = token_ledger.TokenLedger(tmp_path / "ledger.jsonl")
    first = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
    second = {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}
    third = {"prompt_tokens": 200, "completion_tokens": 20, "total_tokens": 220}

    led.record("s1", 7, "standard", 0, first, model="m")
    led.record("s1", 7, "standard", 0, second, model="m")
    led.record("s1", 8, "standard", 0, third, model="m")

    rows = led.rows("s1")
    assert len(rows) == 2
    by_index = {r["request_index"]: r for r in rows}
    assert by_index[7]["prompt_tokens"] == 1
    assert by_index[7]["completion_tokens"] == 2
    assert by_index[8]["prompt_tokens"] == 200
    assert led.summary("s1")["requests"] == 2
