"""Reasoning effort 的测试 —— 验证 reasoning_effort 配置项的端到端传递。

LLMConfig.reasoning_effort:None / low / medium / high
LLMClient._build_payload:reasoning_effort 非空时塞进 payload
LangChainAgent._build_model:reasoning_effort 非空时传给 ChatOpenAI
"""
from __future__ import annotations

import pytest

from app.core.config import LLMConfig


class TestLLMConfig:
    def test_default_is_none(self):
        cfg = LLMConfig()
        assert cfg.reasoning_effort is None

    def test_set_low(self):
        cfg = LLMConfig(reasoning_effort="low")
        assert cfg.reasoning_effort == "low"

    def test_set_high(self):
        cfg = LLMConfig(reasoning_effort="high")
        assert cfg.reasoning_effort == "high"


class TestLLMClientPayload:
    def test_payload_excludes_when_none(self):
        from app.brain.llm_client import LLMClient, ChatMessage
        client = LLMClient(LLMConfig(reasoning_effort=None), system_prompt="sys")
        payload = client._build_payload([ChatMessage(role="user", content="hi")])
        assert "reasoning_effort" not in payload

    def test_payload_includes_low(self):
        from app.brain.llm_client import LLMClient, ChatMessage
        client = LLMClient(LLMConfig(reasoning_effort="low"), system_prompt="sys")
        payload = client._build_payload([ChatMessage(role="user", content="hi")])
        assert payload.get("reasoning_effort") == "low"

    def test_payload_includes_high(self):
        from app.brain.llm_client import LLMClient, ChatMessage
        client = LLMClient(LLMConfig(reasoning_effort="high"), system_prompt="sys")
        payload = client._build_payload([ChatMessage(role="user", content="hi")])
        assert payload.get("reasoning_effort") == "high"

    def test_payload_includes_empty_string_treated_as_none(self):
        """空字符串也算 falsy,不传(避免给不支持的后端传空值)。"""
        from app.brain.llm_client import LLMClient, ChatMessage
        client = LLMClient(LLMConfig(reasoning_effort=""), system_prompt="sys")
        payload = client._build_payload([ChatMessage(role="user", content="hi")])
        assert "reasoning_effort" not in payload


class TestChatOpenAIKwarg:
    """LangChainAgent._build_model 把 reasoning_effort 传给 ChatOpenAI。"""
    def test_build_model_passes_effort(self):
        from app.brain.langchain_agent import LangChainAgent, LangChainAgentConfig
        from app.engine.tools import ToolRegistry

        agent = LangChainAgent(
            llm_cfg=LLMConfig(reasoning_effort="medium"),
            registry=ToolRegistry(),
            cfg=LangChainAgentConfig(),
            persona="x",
            thread_id="t-r",
        )
        # _build_model 直接构造 ChatOpenAI,验证 kwargs 被传
        model = agent._build_model()
        # ChatOpenAI 把 reasoning_effort 存成内部属性,验证它存在
        assert getattr(model, "reasoning_effort", None) == "medium"

    def test_build_model_no_effort_when_none(self):
        from app.brain.langchain_agent import LangChainAgent, LangChainAgentConfig
        from app.engine.tools import ToolRegistry

        agent = LangChainAgent(
            llm_cfg=LLMConfig(),
            registry=ToolRegistry(),
            cfg=LangChainAgentConfig(),
            persona="x",
            thread_id="t-r2",
        )
        model = agent._build_model()
        # 没设时不传 —— model 上要么没这属性,要么是 None
        assert getattr(model, "reasoning_effort", None) is None