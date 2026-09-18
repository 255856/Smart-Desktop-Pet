"""Sub-agent 多智能体框架测试。"""
import asyncio
import json

import pytest

from app.agents.base import (
    AgentContext, AgentResult, BaseAgent, CodeAgent, LifeAgent,
    Orchestrator, OrchestratorConfig, ResearchAgent,
    make_dispatch_tool,
)
from app.brain.llm_client import LLMClient
from app.engine.tools import Tool, ToolRegistry


class _FakeClient:
    """不真正调 LLM 的客户端：返回预设字符串。"""

    def __init__(self, responses: list[str] | None = None):
        self.responses = list(responses or ["mock reply"])
        self.calls: list[tuple[str, str, list | None]] = []
        self.system_prompt = ""
        self.cfg = type("C", (), {"api_key": "fake"})()

    async def chat_once(self, messages):
        self.calls.append(("once", self.system_prompt, messages))
        if self.responses:
            return self.responses.pop(0)
        return "default"

    async def chat_stream_events(self, messages, tools=None, cancel_check=None):
        self.calls.append(("stream", self.system_prompt, messages))
        # 默认：纯文本回复，不调工具
        if self.responses:
            text = self.responses.pop(0)
        else:
            text = "hello from fake"
        yield ("text", text)
        yield ("finish", {"reason": "stop", "tool_calls": [], "content": text})


class TestAgentBase:
    def test_life_agent_default_tools(self):
        assert "remember_fact" in LifeAgent.allowed_tools
        assert "get_pet_status" in LifeAgent.allowed_tools

    def test_research_agent_default_tools(self):
        from app.engine.tools import ToolRegistry
        agent = ResearchAgent(_FakeClient(), ToolRegistry())
        assert "calculate" in agent.allowed_tools

    def test_code_agent_default_tools(self):
        from app.engine.tools import ToolRegistry
        agent = CodeAgent(_FakeClient(), ToolRegistry())
        assert "read_text_file" in agent.allowed_tools

    def test_filter_tools_by_allowed(self):
        reg = ToolRegistry()
        for name in ["add_reminder", "calculate", "read_text_file"]:
            reg.register(Tool(name=name, description=name,
                              parameters={"type": "object", "properties": {}},
                              fn=lambda: "ok"))
        client = _FakeClient()
        agent = LifeAgent(client, reg)
        tools = agent._tools_for_agent()
        names = [t["function"]["name"] for t in tools]
        assert "add_reminder" in names
        assert "calculate" not in names
        assert "read_text_file" not in names

    def test_agent_run_uses_correct_system_prompt(self):
        reg = ToolRegistry()
        client = _FakeClient(responses=["hi"])
        agent = LifeAgent(client, reg)
        ctx = AgentContext(user_message="hi")
        result = asyncio.run(agent.run(ctx))
        assert result.text == "hi"
        assert result.agent_name == "life"
        # system prompt 改回来了
        assert client.system_prompt == ""

    def test_agent_run_with_tool_call(self):
        reg = ToolRegistry()
        reg.register(Tool(name="t1", description="t1",
                          parameters={"type": "object", "properties": {}},
                          fn=lambda: "tool-result"))

        # 第一次流：触发工具调用
        class StreamFake(_FakeClient):
            n = 0

            async def chat_stream_events(self, messages, tools=None, cancel_check=None):
                self.n += 1
                if self.n == 1:
                    yield ("text", "调用工具中 ")
                    yield ("finish", {
                        "reason": "tool_calls",
                        "tool_calls": [{"id": "c1", "name": "t1",
                                         "arguments": "{}"}],
                        "content": "调用工具中 ",
                    })
                else:
                    yield ("text", "完成")
                    yield ("finish", {"reason": "stop", "tool_calls": [],
                                       "content": "完成"})

        client = StreamFake()
        agent = LifeAgent(client, reg, allowed_tools=["t1"])
        ctx = AgentContext(user_message="go")
        result = asyncio.run(agent.run(ctx))
        assert "完成" in result.text
        assert result.tool_calls == 1


class TestOrchestrator:
    def test_dispatch_by_rule_life(self):
        reg = ToolRegistry()
        client = _FakeClient()
        orch = Orchestrator(client)
        orch.register(LifeAgent(client, reg))
        orch.register(ResearchAgent(client, reg))
        orch.register(CodeAgent(client, reg))
        assert orch.dispatch_by_rule("帮我设个提醒") == "life"
        assert orch.dispatch_by_rule("我有点寂寞") == "life"

    def test_dispatch_by_rule_research(self):
        reg = ToolRegistry()
        client = _FakeClient()
        orch = Orchestrator(client)
        orch.register(LifeAgent(client, reg))
        orch.register(ResearchAgent(client, reg))
        orch.register(CodeAgent(client, reg))
        assert orch.dispatch_by_rule("100 摄氏度等于多少华氏度") == "research"
        assert orch.dispatch_by_rule("今天是星期几") == "research"

    def test_dispatch_by_rule_code(self):
        reg = ToolRegistry()
        client = _FakeClient()
        orch = Orchestrator(client)
        orch.register(LifeAgent(client, reg))
        orch.register(ResearchAgent(client, reg))
        orch.register(CodeAgent(client, reg))
        assert orch.dispatch_by_rule("帮我打开桌面上的文件") == "code"
        assert orch.dispatch_by_rule("查一下 cpu 占用") == "code"

    def test_dispatch_by_rule_unknown_falls_back_to_life(self):
        reg = ToolRegistry()
        client = _FakeClient()
        orch = Orchestrator(client)
        orch.register(LifeAgent(client, reg))
        orch.register(ResearchAgent(client, reg))
        orch.register(CodeAgent(client, reg))
        assert orch.dispatch_by_rule("随便聊聊") == "life"

    def test_dispatch_returns_agent_result(self):
        reg = ToolRegistry()
        client = _FakeClient(responses=["research answer"])
        orch = Orchestrator(client)
        orch.register(LifeAgent(client, reg))
        orch.register(ResearchAgent(client, reg))
        result = asyncio.run(orch.dispatch("100 摄氏度是多少华氏度",
                                           prefer="rule"))
        assert result.agent_name == "research"
        assert "research answer" in result.text

    def test_dispatch_prefer_specific(self):
        reg = ToolRegistry()
        client = _FakeClient(responses=["x"])
        orch = Orchestrator(client)
        orch.register(LifeAgent(client, reg))
        orch.register(CodeAgent(client, reg))
        result = asyncio.run(orch.dispatch("啥都行",
                                           prefer="code"))
        assert result.agent_name == "code"

    def test_dispatch_handles_empty_agents(self):
        client = _FakeClient()
        orch = Orchestrator(client)
        result = asyncio.run(orch.dispatch("hi", prefer="rule"))
        assert not result.success


class TestDispatchTool:
    def test_make_dispatch_tool_schema(self):
        reg = ToolRegistry()
        client = _FakeClient()
        orch = Orchestrator(client)
        orch.register(LifeAgent(client, reg))
        orch.register(CodeAgent(client, reg))
        tool = make_dispatch_tool(orch)
        schema = tool.parameters
        assert "agent_name" in schema["properties"]
        assert set(schema["properties"]["agent_name"]["enum"]) == {"life", "code"}

    def test_dispatch_tool_delegates(self):
        reg = ToolRegistry()
        client = _FakeClient(responses=["done from life"])
        orch = Orchestrator(client)
        orch.register(LifeAgent(client, reg))
        orch.register(CodeAgent(client, reg))
        tool = make_dispatch_tool(orch)
        result = tool.fn(agent_name="life", task="聊天")
        assert "done from life" in result

    def test_dispatch_tool_unknown_agent(self):
        reg = ToolRegistry()
        client = _FakeClient()
        orch = Orchestrator(client)
        orch.register(LifeAgent(client, reg))
        tool = make_dispatch_tool(orch)
        result = tool.fn(agent_name="nonexistent", task="x")
        assert "未知" in result
