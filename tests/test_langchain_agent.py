"""LangChain 标准后端测试。

不发起真实 LLM 请求（避免依赖外部服务 / 消耗 token），只验证：
1. Tool → StructuredTool 转换正确
2. agent 构造流程无异常
3. event 协议兼容（yield tuple 类型匹配）
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.brain.langchain_agent import (
    LangChainAgent,
    LangChainAgentConfig,
    convert_tools,
)
from app.brain.llm_client import LLMConfig
from app.engine.tools import Tool, ToolRegistry


def _make_registry() -> ToolRegistry:
    reg = ToolRegistry()

    def echo(x: str) -> str:
        """echo back the input."""
        return f"echoed:{x}"

    def add(a: int, b: int = 1) -> str:
        """add two numbers."""
        return str(a + b)

    def now() -> str:
        """return current time."""
        return "2026-01-01 00:00:00"

    reg.register(Tool(
        name="echo",
        description="echo back the input",
        parameters={"type": "object", "properties": {"x": {"type": "string"}}},
        fn=echo,
    ))
    reg.register(Tool(
        name="add",
        description="add two numbers",
        parameters={"type": "object", "properties": {
            "a": {"type": "integer"},
            "b": {"type": "integer"},
        }},
        fn=add,
    ))
    reg.register(Tool(
        name="now",
        description="return current time",
        parameters={"type": "object", "properties": {}},
        fn=now,
    ))
    return reg


# ---------- 工具转换 ----------
def test_convert_tools_count():
    reg = _make_registry()
    lc_tools = convert_tools(reg)
    assert len(lc_tools) == 3


def test_convert_tools_preserves_name():
    reg = _make_registry()
    lc_tools = convert_tools(reg)
    names = {t.name for t in lc_tools}
    assert names == {"echo", "add", "now"}


def test_convert_tools_schema_has_args():
    reg = _make_registry()
    lc_tools = convert_tools(reg)
    echo = next(t for t in lc_tools if t.name == "echo")
    schema = echo.args_schema.model_json_schema()
    assert "x" in schema["properties"]


def test_convert_tools_callable():
    reg = _make_registry()
    lc_tools = convert_tools(reg)
    add = next(t for t in lc_tools if t.name == "add")
    assert add.invoke({"a": 2, "b": 3}) == "5"
    # 默认值
    assert add.invoke({"a": 5}) == "6"


def test_convert_tools_zero_tool():
    """空注册表转换应当返回空列表。"""
    empty = ToolRegistry()
    assert convert_tools(empty) == []


# ---------- LangChainAgent 构造 ----------
def test_agent_construction_with_registry():
    reg = _make_registry()
    llm = LLMConfig(api_key="sk-test", model="gpt-test", stream=False)
    agent = LangChainAgent(
        llm_cfg=llm,
        registry=reg,
        persona="你是助手",
    )
    assert agent.registry is reg
    assert agent.llm_cfg is llm
    assert agent.persona == "你是助手"


def test_agent_config_defaults():
    cfg = LangChainAgentConfig()
    assert cfg.enable_checkpointer is True
    assert cfg.max_iterations == 10
    assert cfg.return_intermediate_steps is False


# ---------- event 协议兼容 ----------
def test_history_to_lc_messages_skips_system():
    """system role 应当被跳过（已通过 system_prompt 注入）。"""
    msgs = [
        {"role": "system", "content": "you are a helper"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "how are you?"},
    ]
    lc_msgs = LangChainAgent._to_lc_history(msgs)
    # 4 → 2 (跳过 system) + history 中保留 user/assistant
    assert len(lc_msgs) == 3


def test_history_to_lc_messages_empty():
    lc = LangChainAgent._to_lc_history([])
    assert lc == []


# ---------- 用 mock astream 验证 event 协议 ----------
def test_run_yields_event_protocol():
    """用 mock 替换 astream，验证 event tuple 类型与字段。"""
    from langchain_core.messages import AIMessageChunk, ToolMessage

    reg = _make_registry()
    llm = LLMConfig(api_key="sk-test", model="gpt-test", stream=False)

    agent = LangChainAgent(
        llm_cfg=llm, registry=reg, persona="你是助手",
        cfg=LangChainAgentConfig(enable_checkpointer=False),
    )

    # Mock compiled graph：astream 返回受控 chunk 序列
    fake_graph = MagicMock()

    async def fake_astream(payload, config=None, stream_mode=None):
        yield AIMessageChunk(content="你好"), {}
        yield ToolMessage(content="echoed:hi", name="echo", tool_call_id="x"), {}
        yield AIMessageChunk(content=" [happy]"), {}

    fake_graph.astream = fake_astream

    # 跳过真实 create_agent，直接注入 mock graph
    agent._agent = fake_graph
    msgs = [{"role": "user", "content": "hi"}]

    async def collect():
        out = []
        async for ev in agent.run(msgs):
            out.append(ev)
        return out

    result = asyncio.run(collect())
    # 第一条是 AIMessageChunk → ("text", "你好")
    assert result[0] == ("text", "你好")
    # 第二条是 ToolMessage → ("tool", "echo", "", "echoed:hi")
    assert result[1] == ("tool", "echo", "", "echoed:hi")
    # 第三条是 AIMessageChunk → ("text", " [happy]")
    assert result[2][0] == "text"
    # 最后一条是 ("done", 全文本)
    assert result[-1][0] == "done"


def test_run_propagates_error():
    """异常应当 yield ("error", ...)。"""
    reg = _make_registry()
    llm = LLMConfig(api_key="sk-test", model="gpt-test", stream=False)
    agent = LangChainAgent(llm_cfg=llm, registry=reg, persona="x",
                            cfg=LangChainAgentConfig(enable_checkpointer=False))

    fake_graph = MagicMock()

    async def fake_astream(payload, config=None, stream_mode=None):
        raise RuntimeError("boom")
        yield  # noqa: never reached

    fake_graph.astream = fake_astream
    agent._agent = fake_graph

    async def collect():
        out = []
        async for ev in agent.run([{"role": "user", "content": "hi"}]):
            out.append(ev)
        return out

    result = asyncio.run(collect())
    assert any(ev[0] == "error" for ev in result)
    assert any("boom" in ev[1] for ev in result if ev[0] == "error")


def test_run_respects_cancel_check():
    """cancel_check 触发时停止并 yield ("error", "cancelled")。"""
    from langchain_core.messages import AIMessageChunk

    reg = _make_registry()
    llm = LLMConfig(api_key="sk-test", model="gpt-test", stream=False)
    agent = LangChainAgent(llm_cfg=llm, registry=reg, persona="x",
                            cfg=LangChainAgentConfig(enable_checkpointer=False))

    fake_graph = MagicMock()

    call_count = {"n": 0}

    async def fake_astream(payload, config=None, stream_mode=None):
        for _ in range(10):
            call_count["n"] += 1
            yield AIMessageChunk(content="chunk"), {}

    fake_graph.astream = fake_astream
    agent._agent = fake_graph

    cancelled = {"v": False}

    def cancel_check() -> bool:
        return cancelled["v"]

    async def collect():
        out = []
        async for ev in agent.run([{"role": "user", "content": "x"}],
                                  cancel_check=cancel_check):
            if call_count["n"] >= 3:
                cancelled["v"] = True
            out.append(ev)
        return out

    result = asyncio.run(collect())
    assert any(ev[0] == "error" and "cancel" in str(ev[1]).lower() for ev in result)
    # 不应跑完 10 次
    assert call_count["n"] < 10


# ---------- 关闭语义 ----------
def test_close_is_idempotent_without_checkpointer():
    """关闭一个没启用 checkpointer 的 agent 不会出错。"""
    reg = _make_registry()
    llm = LLMConfig(api_key="sk-test", model="gpt-test", stream=False)
    agent = LangChainAgent(llm_cfg=llm, registry=reg, persona="x",
                            cfg=LangChainAgentConfig(enable_checkpointer=False))
    # 没创建过 checkpointer
    agent.close()  # 不应抛异常
    agent.close()  # 多次调用也安全