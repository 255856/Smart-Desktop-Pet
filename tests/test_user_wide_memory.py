"""User-wide memory 注入到 LangChainAgent system_prompt 的测试。

memory_store 是跨 session 共享的长期记忆。本测试验证:
- LangChainAgent 接受 memory_store 参数
- memory_store.system_block() 内容被拼到 system_prompt
- 没 memory_store 不抛
- memory_store 没实现 system_block 也不抛
- system_block 抛异常被吞掉,不污染
"""
from __future__ import annotations

import asyncio

import pytest

from app.brain.langchain_agent import LangChainAgent, LangChainAgentConfig
from app.brain.llm_client import LLMConfig
from app.engine.tools import ToolRegistry


class _FakeMemory:
    """可控的 memory_store mock。"""
    def __init__(self, block: str = "", raise_exc: bool = False):
        self.block = block
        self.raise_exc = raise_exc
        self.call_count = 0

    def system_block(self) -> str:
        self.call_count += 1
        if self.raise_exc:
            raise RuntimeError("simulated memory failure")
        return self.block


def _make_agent(memory_store=None, **kwargs):
    defaults = dict(
        llm_cfg=LLMConfig(),
        registry=ToolRegistry(),
        cfg=LangChainAgentConfig(),
        persona="我是桌宠",
        thread_id="t-mem",
    )
    defaults.update(kwargs)
    return LangChainAgent(memory_store=memory_store, **defaults)


# -------------------- 参数接受 --------------------

class TestMemoryStoreParam:
    def test_accepts_memory_store(self):
        mem = _FakeMemory()
        agent = _make_agent(memory_store=mem)
        assert agent.memory_store is mem

    def test_default_no_memory_store(self):
        agent = _make_agent()
        assert agent.memory_store is None


# -------------------- 注入到 system_prompt --------------------

class TestMemoryInjection:
    def test_memory_block_injected_into_system_prompt(self):
        mem = _FakeMemory(block="主人喜欢喝冰美式。")
        agent = _make_agent(memory_store=mem)
        asyncio.run(agent._ensure_agent())
        assert mem.call_count == 1, "system_block 必须被调用"

    def test_no_memory_store_means_no_call(self):
        agent = _make_agent(memory_store=None)
        # 不抛
        asyncio.run(agent._ensure_agent())

    def test_memory_without_system_block_method_doesnt_crash(self):
        class _PartialMemory:
            pass   # 没 system_block

        agent = _make_agent(memory_store=_PartialMemory())
        # 不抛 —— hasattr 检测保护
        asyncio.run(agent._ensure_agent())

    def test_memory_system_block_exception_swallowed(self):
        """memory 系统坏了不能拖垮 agent。"""
        mem = _FakeMemory(raise_exc=True)
        agent = _make_agent(memory_store=mem)
        # 必须不抛
        asyncio.run(agent._ensure_agent())

    def test_empty_memory_block_works(self):
        mem = _FakeMemory(block="")
        agent = _make_agent(memory_store=mem)
        asyncio.run(agent._ensure_agent())
        # 空 block 时也不会拼(因为 if mem_block 短路)


# -------------------- system_prompt 实际内容 --------------------

class TestSystemPromptComposition:
    """验证最终 system_prompt 同时包含 persona + memory block。"""
    def test_persona_and_memory_combine(self):
        mem = _FakeMemory(block="主人名字叫小雪。")
        agent = _make_agent(memory_store=mem, persona="我是桌宠")
        asyncio.run(agent._ensure_agent())
        # 用 mock 抓取 create_agent 的 system_prompt 参数
        from unittest.mock import patch
        captured = {}

        original = agent._agent  # 现在是 CompiledStateGraph
        # 通过重新 build 来捕获:clear + ensure_agent,然后看 self 的内部状态
        # 实际上我们只能检查 _cached 部分。memory 不缓存到 self 上的字段。
        # 验证:有 memory_store 且 block 非空,系统提示应该含 memory 内容。
        # 直接验:把 agent 里的 _agent graph 跑 mock 后抓取 system_prompt
        # 简化:用 unit-test 方式 —— 检查 _cached_project_docs / _cached_skills / 间接验
        # 因为 system_prompt 在 create_agent 内部构造,这里只能间接断言。
        # 真正的端到端:需要 mock create_agent 来抓 system_prompt 参数
        from langchain.agents import create_agent as real_create_agent

        with patch("app.brain.langchain_agent.create_agent",
                   side_effect=lambda **kwa: captured.update(kwa) or original) as _:
            agent._agent = None
            asyncio.run(agent._ensure_agent())
            # 模拟完整 agent:再调一次
            agent._agent = None
            asyncio.run(agent._ensure_agent())

        # 第二次跑 create_agent 时,system_prompt 应该有 memory 内容
        sp = captured.get("system_prompt", "")
        assert "我是桌宠" in sp, "persona 必须还在"
        assert "主人名字叫小雪" in sp, "memory block 必须拼到 system_prompt"

    def test_memory_added_after_project_docs(self):
        """memory 应该在 persona + project_docs + skills 之后。"""
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            # 写一个 AGENTS.md
            (td / "AGENTS.md").write_text("AGENTS_HERE", encoding="utf-8")
            mem = _FakeMemory(block="MEM_HERE")

            agent = _make_agent(
                memory_store=mem,
                persona="PERSONA_HERE",
                cwd=str(td),
            )

            from unittest.mock import patch
            captured = {}
            original = agent._agent
            with patch("app.brain.langchain_agent.create_agent",
                       side_effect=lambda **kwa: captured.update(kwa) or original):
                agent._agent = None
                asyncio.run(agent._ensure_agent())

            sp = captured.get("system_prompt", "")
            # 顺序:PERSONA 在最前,AGENTS 在中间,MEM 在最后
            p_pos = sp.find("PERSONA_HERE")
            a_pos = sp.find("AGENTS_HERE")
            m_pos = sp.find("MEM_HERE")
            assert p_pos < a_pos < m_pos, (
                f"顺序错: persona={p_pos}, agents={a_pos}, mem={m_pos}"
            )