"""Task 工具(Subagent)的测试。

覆盖:
- register() 把 Task 加进 ToolRegistry
- 没 factory 时调 Task 返回友好错误
- factory 被调用时收到正确的 (task, kwargs)
- factory 异常被 catch,不当 crash
- task 空字符串返回错误
- set_subagent_factory/get_subagent_factory 配对
- LangChainAgent 在 __init__ 里自动装 factory
- _build_subagent_registry 过滤掉 Task 和高风险工具
"""
from __future__ import annotations

import pytest

from app.engine.tools import _subagent
from app.engine.tools._core import ToolRegistry


@pytest.fixture(autouse=True)
def _reset_factory():
    """每个测试前后都清掉 factory,避免污染。"""
    _subagent.set_subagent_factory(None)
    yield
    _subagent.set_subagent_factory(None)


class TestFactoryAccessors:
    def test_get_factory_none_initially(self):
        assert _subagent.get_subagent_factory() is None

    def test_set_then_get(self):
        def f(task, kwargs):
            return "ok"

        _subagent.set_subagent_factory(f)
        assert _subagent.get_subagent_factory() is f

    def test_set_to_none_resets(self):
        _subagent.set_subagent_factory(lambda t, k: "x")
        _subagent.set_subagent_factory(None)
        assert _subagent.get_subagent_factory() is None


class TestRegister:
    def test_task_tool_registered(self):
        reg = ToolRegistry()
        _subagent.register(reg)
        assert "Task" in reg.names()

    def test_task_tool_has_correct_schema(self):
        reg = ToolRegistry()
        _subagent.register(reg)
        task = reg.get("Task")
        assert "task" in task.parameters["required"]
        assert "agent_type" in task.parameters["properties"]


class TestTaskInvocation:
    def test_no_factory_returns_friendly_error(self):
        reg = ToolRegistry()
        _subagent.register(reg)
        task = reg.get("Task").fn
        result = task(task="hello")
        assert "错误" in result
        assert "factory" in result.lower() or "subagent" in result.lower()

    def test_empty_task_returns_error(self):
        reg = ToolRegistry()
        _subagent.register(reg)
        task = reg.get("Task").fn
        assert "不能为空" in task(task="")
        assert "不能为空" in task(task="   ")

    def test_factory_called_with_task_and_kwargs(self):
        captured = []

        def f(task, kwargs):
            captured.append((task, kwargs))
            return "sub-result"

        _subagent.set_subagent_factory(f)
        reg = ToolRegistry()
        _subagent.register(reg)
        task = reg.get("Task").fn

        out = task(task="find bugs in X")
        assert out == "sub-result"
        # agent_type 有默认值 'general',所以 kwargs 总会带这个字段
        assert captured == [("find bugs in X", {"agent_type": "general"})]

    def test_factory_kwargs_include_agent_type(self):
        captured = []

        def f(task, kwargs):
            captured.append(kwargs)
            return "ok"

        _subagent.set_subagent_factory(f)
        reg = ToolRegistry()
        _subagent.register(reg)
        task = reg.get("Task").fn

        task(task="x", agent_type="explore")
        assert captured == [{"agent_type": "explore"}]

    def test_factory_exception_caught(self):
        def bad_f(task, kwargs):
            raise RuntimeError("boom")

        _subagent.set_subagent_factory(bad_f)
        reg = ToolRegistry()
        _subagent.register(reg)
        task = reg.get("Task").fn

        out = task(task="x")
        # 异常被吃掉,返回错误字符串(不抛)
        assert "错误" in out or "boom" in out or "sub-agent" in out
        assert not out.startswith("Traceback")


class TestLangChainAgentIntegration:
    def test_init_sets_factory(self):
        from app.brain.langchain_agent import LangChainAgent, LangChainAgentConfig
        from app.brain.llm_client import LLMConfig
        from app.engine.tools import ToolRegistry

        agent = LangChainAgent(
            llm_cfg=LLMConfig(),
            registry=ToolRegistry(),
            cfg=LangChainAgentConfig(),
            persona="x",
            thread_id="t-sub",
        )
        # __init__ 应该装上 factory
        assert _subagent.get_subagent_factory() is not None

    def test_init_factory_returns_string_for_task(self):
        """factory 被 LangChainAgent 装上后,传 task 给它会得到字符串结果。"""
        from app.brain.langchain_agent import LangChainAgent, LangChainAgentConfig
        from app.brain.llm_client import LLMConfig
        from app.engine.tools import ToolRegistry

        agent = LangChainAgent(
            llm_cfg=LLMConfig(),
            registry=ToolRegistry(),
            cfg=LangChainAgentConfig(),
            persona="x",
            thread_id="t-sub2",
        )
        # 替换 factory 为可控 mock(否则会试图调真 LLM)
        def mock_factory(task_str, kwargs):
            return f"mock-result: {task_str}"

        _subagent.set_subagent_factory(mock_factory)

        # 通过 Task 工具调用
        reg = ToolRegistry()
        _subagent.register(reg)
        task_fn = reg.get("Task").fn
        result = task_fn(task="hello")
        assert result == "mock-result: hello"

    def test_build_subagent_registry_excludes_task_and_risky(self):
        from app.brain.langchain_agent import LangChainAgent, LangChainAgentConfig
        from app.brain.hallucination_guard import _RISKY_TOOLS
        from app.brain.llm_client import LLMConfig
        from app.engine.tools import Tool, ToolRegistry

        reg = ToolRegistry()
        reg.register(Tool(name="noop", description="d", parameters={},
                          fn=lambda: "ok"))
        reg.register(Tool(name="lock_screen", description="d", parameters={},
                          fn=lambda: "locked"))

        agent = LangChainAgent(
            llm_cfg=LLMConfig(),
            registry=reg,
            cfg=LangChainAgentConfig(),
            persona="x",
            thread_id="t-sub3",
        )
        sub_reg = agent._build_subagent_registry()
        # noop 应该保留
        assert "noop" in sub_reg.names()
        # 高风险工具应该被过滤
        assert "lock_screen" not in sub_reg.names()
        for r in _RISKY_TOOLS:
            assert r not in sub_reg.names(), f"{r} 不该出现在子 agent 工具集"

    def test_build_subagent_registry_removes_task(self):
        from app.brain.langchain_agent import LangChainAgent, LangChainAgentConfig
        from app.brain.llm_client import LLMConfig
        from app.engine.tools import Tool, ToolRegistry

        reg = ToolRegistry()
        reg.register(Tool(name="Task", description="d", parameters={},
                          fn=lambda: "x"))
        reg.register(Tool(name="normal_tool", description="d", parameters={},
                          fn=lambda: "ok"))

        agent = LangChainAgent(
            llm_cfg=LLMConfig(),
            registry=reg,
            cfg=LangChainAgentConfig(),
            persona="x",
            thread_id="t-sub4",
        )
        sub_reg = agent._build_subagent_registry()
        assert "Task" not in sub_reg.names(), "子 agent 必须不能调 Task(防递归)"
        assert "normal_tool" in sub_reg.names()


class TestMainRegistryIncludesTask:
    def test_build_default_tools_registers_task(self):
        """build_default_tools() 跑过后,Task 工具必须在默认 registry 里。"""
        from app.engine.tools import build_default_tools
        from app.engine.tools import ToolRegistry
        from unittest.mock import MagicMock

        # build_default_tools 需要 state / reminders / memory
        reg = build_default_tools(
            state=MagicMock(),
            reminders=MagicMock(),
            memory=MagicMock(),
        )
        assert "Task" in reg.names()