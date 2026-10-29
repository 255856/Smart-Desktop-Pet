"""Stage 2.1 + 记忆通道搬家：长期记忆**不再**烘死在 system prompt 里。

以前 agent 只构建一次（`if self._agent is not None: return`），记忆块在构建时
被烘进 system prompt ⇒ 会话中写入的新事实永远进不了 system prompt。
Stage 2.8 修过一次（按记忆块指纹重建 agent），但 Stage 2.1 之后这条路走不通了：

    记忆块按**本轮主人说的话**召回 ⇒ 每轮都变。
    放 system prompt 里 ⇒ ① MiniMax 的 8192-token 前缀缓存每轮作废
    （实测 98.9% → 1.5%）；② 指纹每轮变 ⇒ 每轮重建 agent
    （编译好的图 + checkpointer 状态每轮丢掉）。

所以记忆块搬到了**易变通道**（独立的 user 消息）：
    - system 串对不同 query **逐字节相同**（缓存不废）
    - 易变段随 query 变（记忆按句召回）
    - agent 只构建一次，记忆变化**不再**触发重建

全部离线：create_agent 被换成假实现，不发任何网络请求。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from app.brain.langchain_agent import LangChainAgent, LangChainAgentConfig
from app.brain.llm_client import LLMConfig
from app.brain.memory import MemoryStore
from app.engine.tools import ToolRegistry


# --------------------------------------------------------------------------
# 假实现
# --------------------------------------------------------------------------

class _FakeGraph:
    """假的编译图 —— 只记住 system_prompt，不发任何请求。"""

    def __init__(self, system_prompt: str) -> None:
        self.system_prompt = system_prompt


def _fake_create_agent(captured: list[str]):
    """替换 create_agent：把每次构建用的 system_prompt 记下来。"""
    def _side_effect(**kwargs):
        captured.append(kwargs.get("system_prompt", ""))
        return _FakeGraph(kwargs.get("system_prompt", ""))
    return _side_effect


def _make_store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path / "memory_probe.db", backend_name="tfidf")


def _make_agent(store: MemoryStore | None = None, **kwargs) -> LangChainAgent:
    defaults = dict(
        llm_cfg=LLMConfig(api_key="sk-test", model="test-model", stream=False),
        registry=ToolRegistry(),
        persona="我是桌宠",
        # 关掉 checkpointer：别在仓库 data/ 下建 checkpoint 库
        cfg=LangChainAgentConfig(enable_checkpointer=False),
        memory_store=store,
        thread_id="t-mem-refresh",
    )
    defaults.update(kwargs)
    return LangChainAgent(**defaults)


def _turn_context(query: str, store: MemoryStore | None = None) -> str:
    """易变通道（独立 user 消息）—— 记忆块就走这里。"""
    from app.brain import prompt_layer

    old_provider = prompt_layer._memory_block_provider
    if store is not None:
        prompt_layer.register_memory_block_provider(
            lambda q="": store.system_block(q))
    try:
        return prompt_layer.build_turn_context(query=query)
    finally:
        prompt_layer.register_memory_block_provider(old_provider)


# --------------------------------------------------------------------------
# 1) 记忆块**不在** system prompt 里
# --------------------------------------------------------------------------

def test_memory_block_is_not_in_the_system_prompt(tmp_path):
    """system prompt 里不许有记忆块（它每轮都变 ⇒ 会打掉前缀缓存）。"""
    store = _make_store(tmp_path)
    store.add("主人明天要交房租", category="fact", importance=0.9)
    agent = _make_agent(store)
    sp = agent.assemble_system_prompt()
    assert "主人明天要交房租" not in sp, (
        "长期记忆块不许进 system prompt —— 它走易变通道")


def test_memory_block_reaches_the_model_via_the_volatile_channel(tmp_path):
    """记忆块必须出现在易变通道（独立 user 消息）里。"""
    store = _make_store(tmp_path)
    store.add("主人明天要交房租", category="fact", importance=0.9)
    ctx = _turn_context("记不记得我明天要交房租", store)
    assert "主人明天要交房租" in ctx
    assert "【你记得的事】" in ctx, "记忆块要带【你记得的事】标题 + 免责文案"


def test_memory_written_mid_session_reaches_next_turn_context(tmp_path):
    """会话中途写入的事实，下一轮的易变通道里必须能召回。"""
    store = _make_store(tmp_path)
    assert "主人明天要交房租" not in _turn_context(
        "记不记得我明天要交房租", store)

    store.add("主人明天要交房租", category="fact", importance=0.9)

    assert "主人明天要交房租" in _turn_context(
        "记不记得我明天要交房租", store), (
        "下一轮必须能召回刚写入的事实 —— "
        "否则桌宠就是「听见了但当没听见」")


def test_memory_change_via_provider_is_picked_up(tmp_path):
    """没有 memory_store 时（走共享层 provider），记忆照样能召回。"""
    from app.brain import prompt_layer

    store = _make_store(tmp_path)
    old_provider = prompt_layer._memory_block_provider
    prompt_layer.register_memory_block_provider(
        lambda q="": store.system_block(q))
    try:
        ctx = prompt_layer.build_turn_context(query="我养了几只宠物")
        assert "主人养了一只叫豆豆的猫" not in ctx

        store.add("主人养了一只叫豆豆的猫", category="fact", importance=0.9)
        ctx = prompt_layer.build_turn_context(query="我养了几只宠物")
    finally:
        prompt_layer.register_memory_block_provider(old_provider)

    assert "主人养了一只叫豆豆的猫" in ctx


# --------------------------------------------------------------------------
# 2) agent 的构建次数：记忆变化**不再**触发重建
# --------------------------------------------------------------------------

def test_agent_not_rebuilt_when_memory_unchanged(tmp_path):
    """记忆没变 → agent 实例必须是**同一个对象**（不重建）。"""
    store = _make_store(tmp_path)
    store.add("主人喜欢喝冰美式", category="preference", importance=0.7)
    agent = _make_agent(store)
    prompts: list[str] = []

    with patch("app.brain.langchain_agent.create_agent",
               side_effect=_fake_create_agent(prompts)):
        asyncio.run(agent._ensure_agent())
        first_agent = agent._agent
        assert first_agent is not None

        asyncio.run(agent._ensure_agent())
        asyncio.run(agent._ensure_agent())

    assert len(prompts) == 1, f"记忆没变就不该重建 agent：构建了 {len(prompts)} 次"
    assert agent._agent is first_agent, "agent 实例必须复用同一个对象"


def test_memory_change_does_not_rebuild_agent(tmp_path):
    """记忆变了也**不重建** agent —— 记忆不在 system 串里，重建没有意义。

    （旧行为：按记忆块指纹重建。记忆搬到易变通道后，指纹恒定，
      每轮重建会把编译好的图 + checkpointer 状态全丢掉。）
    """
    store = _make_store(tmp_path)
    agent = _make_agent(store)
    prompts: list[str] = []

    with patch("app.brain.langchain_agent.create_agent",
               side_effect=_fake_create_agent(prompts)):
        asyncio.run(agent._ensure_agent())
        model = agent._model
        store.add("主人住在杭州", category="fact", importance=0.9)
        asyncio.run(agent._ensure_agent())

    assert len(prompts) == 1, (
        f"记忆变了不该重建 agent（记忆不在 system 串里）：构建了 {len(prompts)} 次")
    assert agent._model is model, "model 必须复用"


# --------------------------------------------------------------------------
# 3) 两个后端：system 串一致（都不含记忆）+ 易变通道拿到同一份
# --------------------------------------------------------------------------

class _FakeClient:
    """最小 client —— ReAct 后端只要 base_system_prompt / system_prompt。"""

    base_system_prompt = "我是桌宠"
    system_prompt = ""


def test_react_backend_system_prompt_has_no_memory(tmp_path):
    """ReAct 后端的 system prompt 里也不许有记忆块。"""
    from app.brain.agent import AgentLoop
    from app.brain import prompt_layer

    store = _make_store(tmp_path)
    old_provider = prompt_layer._memory_block_provider
    prompt_layer.register_memory_block_provider(
        lambda q="": store.system_block(q))
    try:
        client = _FakeClient()
        react = AgentLoop(client=client, registry=ToolRegistry())
        react._apply_system_prompt()
        assert "主人明天要交房租" not in client.system_prompt

        store.add("主人明天要交房租", category="fact", importance=0.9)
        react._apply_system_prompt()
    finally:
        prompt_layer.register_memory_block_provider(old_provider)

    assert "主人明天要交房租" not in client.system_prompt, (
        "ReAct 后端每轮重算 system prompt —— 记忆块也不许在里面")


def test_both_backends_agree_on_the_volatile_memory_block(tmp_path):
    """两个后端拿到的**易变通道**记忆块必须一致（system 串里都没有）。"""
    from app.brain.agent import AgentLoop
    from app.brain import prompt_layer

    store = _make_store(tmp_path)
    store.add("主人的生日是2005年2月4号", category="fact", importance=0.9)

    old_provider = prompt_layer._memory_block_provider
    prompt_layer.register_memory_block_provider(
        lambda q="": store.system_block(q))
    try:
        client = _FakeClient()
        react = AgentLoop(client=client, registry=ToolRegistry())
        react_sp = react.assemble_system_prompt("我是桌宠")

        agent = _make_agent(store)
        standard_sp = agent.assemble_system_prompt("我是桌宠")

        # 两个后端的易变段都走共享层的 build_turn_context ⇒ 同一份
        react_ctx = prompt_layer.build_turn_context(query="主人的生日")
        standard_ctx = prompt_layer.build_turn_context(query="主人的生日")
    finally:
        prompt_layer.register_memory_block_provider(old_provider)

    assert "主人的生日是2005年2月4号" not in react_sp
    assert "主人的生日是2005年2月4号" not in standard_sp
    assert "主人的生日是2005年2月4号" in react_ctx
    assert react_ctx == standard_ctx, "两个后端的易变段必须逐字节相同"
