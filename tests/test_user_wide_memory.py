"""User-wide memory 与 LangChainAgent 的接线测试。

memory_store 是跨 session 共享的长期记忆。本测试验证:
- LangChainAgent 接受 memory_store 参数
- **记忆块不进 system_prompt**（Stage 2.1 + 记忆通道搬家：它按本轮 query
  召回 ⇒ 每轮都变 ⇒ 放 system 会打掉 8192-token 前缀缓存，也会让 agent
  指纹每轮变 → 每轮重建 agent）。它走**易变通道**（独立 user 消息）。
- 记忆块**确实**通过易变通道到达模型
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


# -------------------- 通道：易变通道（不是 system_prompt） --------------------

class TestMemoryInjection:
    def test_memory_block_not_injected_into_system_prompt(self):
        """记忆块**不进** system prompt（它每轮都变 ⇒ 会打掉前缀缓存）。"""
        mem = _FakeMemory(block="主人喜欢喝冰美式。")
        agent = _make_agent(memory_store=mem)
        asyncio.run(agent._ensure_agent())
        sp = agent.assemble_system_prompt()
        assert "主人喜欢喝冰美式" not in sp, (
            "长期记忆块不许进 system prompt —— 它走易变通道")

    def test_memory_block_reaches_the_model_via_volatile_channel(self):
        """记忆块通过易变通道到达模型（build_turn_context → provider）。"""
        from app.brain import prompt_layer

        mem = _FakeMemory(block="主人喜欢喝冰美式。")
        old_provider = prompt_layer._memory_block_provider
        prompt_layer.register_memory_block_provider(
            lambda q="": mem.system_block())
        try:
            ctx = prompt_layer.build_turn_context(query="我喜欢喝什么")
        finally:
            prompt_layer.register_memory_block_provider(old_provider)

        assert "主人喜欢喝冰美式" in ctx, "记忆块必须出现在易变通道里"
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
    """验证最终 system_prompt 含 persona，但**不含** memory block。"""
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

        # system_prompt 里有 persona，但**没有** memory block
        # （记忆块走易变通道：每轮按 query 召回 ⇒ 不能进 system prompt）
        sp = captured.get("system_prompt", "")
        assert "我是桌宠" in sp, "persona 必须还在"
        assert "主人名字叫小雪" not in sp, (
            "memory block 不许拼到 system_prompt —— 它走易变通道")

    def test_memory_not_in_system_prompt_at_all(self):
        """记忆块不在 system 串里（连位置都没有）—— 稳定段顺序不受影响。"""
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
            # 稳定段顺序：PERSONA 在最前，AGENTS 在中间
            p_pos = sp.find("PERSONA_HERE")
            a_pos = sp.find("AGENTS_HERE")
            assert p_pos >= 0 and a_pos > p_pos, (
                f"顺序错: persona={p_pos}, agents={a_pos}")
            # 记忆块**不在** system 串里（走易变通道）
            assert "MEM_HERE" not in sp, (
                "memory block 不许进 system_prompt —— 它走易变通道")