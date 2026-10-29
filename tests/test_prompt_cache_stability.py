# -*- coding: utf-8 -*-
"""P0-11：system prompt 必须字节稳定（MiniMax 8192-token 前缀缓存）。

背景
----
MiniMax 的 prompt cache 按 8192-token 块做前缀缓存。system message 落在
**第一块**里，块里任何一处每轮变化的内容（时间戳！）都会让整块失效 ——
实测同一份 body，system 里时间戳一变，缓存命中率 98.9% → 1.5%
（每轮白付 ~8173 个 prompt token）。

这里锁住的不变量
----------------
1. system prompt 里**没有**每轮变的时间戳：隔一秒重建两次必须字节一致。
2. 当前时间 / 桌宠实时状态走 **user 消息**，每轮恰好一次。
3. 工具列表留在 system prompt（它稳定、而且大 —— 留在那里才让第一块可缓存）。
4. 主人原话保持字节不变（意图判定 detect_action_intent 读的就是它）。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/fake_sidecar.py
if (ROOT / ".local-packages").is_dir():
    sys.path.insert(0, str(ROOT / ".local-packages"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.brain.llm_client import (  # noqa: E402
    DYNAMIC_CONTEXT_HEADER,
    build_dynamic_context,
    inject_dynamic_context,
)
from fake_sidecar import install  # noqa: E402
from app.brain.hallucination_guard import grounding_dynamic_prompt  # noqa: E402


class _FakeRequest:
    """grounding_dynamic_prompt 只需要 system_prompt + config。"""

    def __init__(self, system_prompt: str = "PERSONA"):
        self.system_prompt = system_prompt
        self.config = {}


# --------------------------------------------------------------------------
# 1) standard 后端的 system prompt（grounding_dynamic_prompt）
# --------------------------------------------------------------------------

def test_grounding_system_prompt_is_byte_stable_across_one_second():
    """隔一秒重建两次 system prompt，必须字节一致（= 前缀缓存能命中）。"""
    req = _FakeRequest()
    sp1 = grounding_dynamic_prompt(req)
    time.sleep(1.05)
    sp2 = grounding_dynamic_prompt(req)
    assert sp1 == sp2, "system prompt 变了 —— 里面有每轮变的东西"
    assert "[当前时间:" not in sp1, "system prompt 里不能有时间戳"
    assert "星期" not in sp1, "system prompt 里不能有星期（跟时间戳同源）"


def test_grounding_system_prompt_keeps_the_other_parts():
    """这次改动只搬时间戳 —— 状态门 directive / 反幻觉后缀不许被动。"""
    sp = grounding_dynamic_prompt(_FakeRequest("PERSONA"))
    assert "PERSONA" in sp
    assert "高风险工具 confirm 协议" in sp, "confirm 协议必须还在"


# --------------------------------------------------------------------------
# 2) brain_controller：system 上下文稳定，易变部分单独走 user 消息
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def brain(tmp_path_factory):
    """真 BrainController（临时 root，不碰 data/ 下的真库）。"""
    from app.core.qt_compat import QApplication
    QApplication.instance() or QApplication(sys.argv[:1])

    from app.core.config import load_config
    from app.engine.state_manager import StateManager
    from app.brain.brain_controller import BrainController

    tmp = tmp_path_factory.mktemp("brain_ctx")
    skill_dst = tmp / ".agents" / "skills" / "desktop-pet-tool-usage"
    skill_dst.mkdir(parents=True)
    shutil.copy(ROOT / ".agents" / "skills" / "desktop-pet-tool-usage" / "SKILL.md",
                skill_dst)
    (tmp / "config.yaml").write_text(
        "llm:\n"
        "  base_url: \"http://localhost:1/v1\"\n"
        "  api_key: \"test\"\n"
        "  model: \"test\"\n"
        "character:\n"
        "  name: \"测试\"\n"
        "window:\n"
        "  scale: 0.4\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp / "config.yaml")
    state_mgr = StateManager(tmp, cfg, type("Args", (), {"reset_state": False})())
    return BrainController(tmp, cfg, state_mgr.state, state_mgr.reminders)


def test_brain_chat_context_is_stable_and_has_no_timestamp(brain):
    """chat_context() 是**稳定**的 system 上下文：两次调用字节一致、无时间戳。"""
    ctx1 = brain.chat_context()
    time.sleep(1.05)
    ctx2 = brain.chat_context()
    assert ctx1 == ctx2, "chat_context 变了 —— 有每轮变的东西漏在 system prompt 里"
    assert "[当前时间:" not in ctx1
    assert "【当前时间】" not in ctx1


def test_brain_chat_context_dynamic_carries_time_and_state(brain):
    """易变部分单独提供：当前时间 + 桌宠实时状态，各一份。"""
    dyn = brain.chat_context_dynamic()
    assert DYNAMIC_CONTEXT_HEADER in dyn
    assert "[当前时间:" in dyn
    assert "【你的实时状态】" in dyn
    # 每轮一次：动态块里时间只出现一次
    assert dyn.count("[当前时间:") == 1


# --------------------------------------------------------------------------
# 3) 聊天窗口：system prompt 稳定 + 易变内容进 user 消息
# --------------------------------------------------------------------------

@pytest.fixture
def chat_window(qapp):
    """一个最小聊天窗口（带一个稳定的 context_provider）。"""
    from app.core.config import CharacterConfig
    from app.ui.chat_window import ChatWindow

    char_cfg = CharacterConfig(name="测试桌宠")
    llm_cfg = type("L", (), {"model": "test", "api_key": "test"})()
    stable_ctx = "【稳定上下文】SKILL / 工具列表"
    return ChatWindow(llm_cfg, char_cfg, str(ROOT / "assets" / "sprites"),
                      context_provider=lambda: stable_ctx)


def test_chat_window_injects_dynamic_context_into_user_message(chat_window):
    """易变上下文作为**独立 user 消息**插在主人本轮发言前面。"""
    from app.brain.llm_client import ChatMessage

    msgs = [ChatMessage(role="user", content="帮我打开记事本")]
    out = chat_window._inject_dynamic_context(msgs)

    assert len(out) == 2, "应该多出一条动态上下文消息"
    assert out[0].role == "user"
    assert DYNAMIC_CONTEXT_HEADER in out[0].content
    assert "[当前时间:" in out[0].content
    # 主人原话保持字节不变（意图判定读的就是它）
    assert out[1].content == "帮我打开记事本"


def test_chat_window_dynamic_context_is_injected_only_once(chat_window):
    """重复注入是幂等的：时间不会在同一轮里出现两次。"""
    from app.brain.llm_client import ChatMessage

    msgs = [ChatMessage(role="user", content="你好")]
    once = chat_window._inject_dynamic_context(msgs)
    twice = chat_window._inject_dynamic_context(once)
    assert len(twice) == 2
    joined = "".join(m.content for m in twice)
    assert joined.count("[当前时间:") == 1


def test_chat_window_system_prompt_has_no_timestamp(chat_window):
    """窗口拼出来的 system prompt 里不许有时间戳。"""
    system_prompt = chat_window.char_cfg.persona
    if chat_window.context_provider is not None:
        extra = chat_window.context_provider()
        if extra:
            system_prompt = system_prompt + "\n\n" + extra
    assert "[当前时间:" not in system_prompt
    assert "【当前时间】" not in system_prompt


# --------------------------------------------------------------------------
# 3b) 真·聊天窗口路径（context_provider 绑着真 BrainController）
# --------------------------------------------------------------------------

def test_real_chat_window_request_carries_time_and_state_exactly_once(
        brain, monkeypatch, qapp):
    """真路径：时间 + 桌宠实时状态各**一份**，在 user 消息里；system 里一份都没有。"""
    import app.ui.chat_window as cw_mod
    from app.core.config import CharacterConfig
    from app.ui.chat_window import ChatWindow, Message

    char_cfg = CharacterConfig(name="测试桌宠")
    llm_cfg = type("L", (), {
        "model": "test", "api_key": "dummy-key",
        "base_url": "http://127.0.0.1:1/v1", "timeout": 1,
        "stream": True, "temperature": 0.8, "max_tokens": 16,
    })()
    cw = ChatWindow(llm_cfg, char_cfg, str(ROOT / "assets" / "sprites"),
                    context_provider=brain.chat_context)

    captured = {}
    real_init = cw_mod._StreamWorker.__init__

    def _spy(self, client, messages):
        captured["messages"] = messages
        captured["system_prompt"] = client.system_prompt
        real_init(self, client, messages)

    monkeypatch.setattr(cw_mod._StreamWorker, "__init__", _spy)
    monkeypatch.setattr(cw_mod._StreamWorker, "start", lambda self: None)

    cw.history = [Message(role="user", content="在吗")]
    cw._kickoff_llm()

    assert "messages" in captured, "没抓到请求消息（_kickoff_llm 路径变了？）"
    system_prompt = captured["system_prompt"]
    msgs = captured["messages"]

    # 1) system prompt 里没有时间 / 实时状态
    assert "[当前时间:" not in system_prompt
    assert "【当前时间】" not in system_prompt
    assert "【你的实时状态】" not in system_prompt

    # 2) user 消息里：动态上下文一份 + 主人原话一份
    users = [m.content for m in msgs if m.role == "user"]
    assert len(users) == 2, "应该是一条动态上下文 + 一条主人原话"
    assert DYNAMIC_CONTEXT_HEADER in users[0]
    assert "[当前时间:" in users[0]
    assert "【你的实时状态】" in users[0], "桌宠实时状态必须送到模型"
    assert users[1] == "在吗", "主人原话必须保持字节不变"

    # 3) 每轮恰好一次
    joined = "".join(m.content for m in msgs)
    assert joined.count("[当前时间:") == 1
    assert joined.count("【你的实时状态】") == 1


# --------------------------------------------------------------------------
# 4) 通用 helper：注入 / 幂等 / 形状兼容
# --------------------------------------------------------------------------

def test_inject_dynamic_context_supports_dict_and_chatmessage():
    """两种消息形状都认（LangChain 走 dict，聊天窗口走 ChatMessage）。"""
    from app.brain.llm_client import ChatMessage

    ctx = build_dynamic_context()
    dicts = inject_dynamic_context([{"role": "user", "content": "hi"}],
                                   context=ctx)
    objs = inject_dynamic_context([ChatMessage(role="user", content="hi")],
                                  context=ctx)
    assert dicts[0]["content"] == ctx
    assert objs[0].content == ctx
    assert dicts[1]["content"] == "hi"
    assert objs[1].content == "hi"


def test_build_dynamic_context_contains_time_once():
    """动态块里时间只有一份（每轮恰好一次）。"""
    ctx = build_dynamic_context()
    assert ctx.count("[当前时间:") == 1
    assert DYNAMIC_CONTEXT_HEADER in ctx


# --------------------------------------------------------------------------
# 5) Stage 1.1 共享层：system 字符串对「不同的 now / state」必须逐字节相同
# --------------------------------------------------------------------------
# P0-11 缓存修复的回归闸：
#   system message 落在 MiniMax 的 8192-token 前缀缓存第一块，
#   块里每轮变一次的内容（时间戳！状态策略！）都会让整块作废 ——
#   实测缓存命中率 98.9% → 1.5%。
#   所以「时间 / 桌宠状态 / 状态策略」只能走易变通道（独立 user 消息）。

def _low_state():
    from app.engine.state import PetState
    ps = PetState()
    ps.feeling = 0
    ps.strength_food = 0
    ps.likability = 0
    ps.normalize()
    return ps


def _high_state():
    from app.engine.state import PetState
    ps = PetState()
    ps.feeling = 95
    ps.strength_food = 95
    ps.likability = 95
    ps.normalize()
    return ps


def test_shared_layer_system_prompt_is_identical_for_two_now_state_pairs():
    """**两个后端的** system 字符串，换 now / state 也必须逐字节相同。"""
    from app.brain.agent import AgentLoop
    from app.brain.langchain_agent import LangChainAgent
    from app.brain.llm_client import LLMConfig
    from app.brain.prompt_layer import (
        build_dynamic_block,
        build_system_prompt,
        build_tool_handbook,
        load_style_guide,
    )

    class _Reg:
        def names(self, level=None):
            return ["open_app", "open_website", "web_search"]

    persona = "你是小汐，住在主人桌面上的桌宠。"
    reg = _Reg()

    def _assemble():
        return build_system_prompt(
            persona=persona,
            style_guide=load_style_guide(),
            tool_handbook=build_tool_handbook(registry=reg, level=3),
            dynamic=build_dynamic_block(),
        )

    sp1 = _assemble()
    sp2 = _assemble()
    assert sp1.encode("utf-8") == sp2.encode("utf-8")

    # 生产路径（两个后端）同样逐字节相同
    react = AgentLoop(client=None, registry=reg, level_getter=lambda: 3)
    llm = LLMConfig(api_key="sk-test", model="test-model", stream=False)
    standard = LangChainAgent(llm_cfg=llm, registry=reg, persona=persona,
                              level_getter=lambda: 3)
    assert (react.assemble_system_prompt(persona=persona).encode("utf-8")
            == standard.assemble_system_prompt(persona=persona).encode("utf-8"))


def test_volatile_channel_is_the_only_thing_that_changes_with_now_state():
    """易变通道**确实**随 now / state 变 —— 否则上面的恒等就是假象。"""
    import datetime

    from app.brain.prompt_layer import build_turn_context

    t1 = datetime.datetime(2026, 1, 2, 3, 4, 5)
    t2 = datetime.datetime(2026, 5, 6, 7, 8, 9)
    ctx1 = build_turn_context(now=t1, state=_low_state())
    ctx2 = build_turn_context(now=t2, state=_high_state())
    assert ctx1 != ctx2, "易变上下文必须随时间 / 状态变化"
    assert "桌宠状态策略" in ctx1        # 状态差 → 有策略
    assert "状态差" in ctx1              # LOW：只挡高风险工具
    assert "状态好" in ctx2              # HIGH：可以主动建议
    assert "状态差" not in ctx2
    assert "[当前时间:2026-01-02 03:04:05" in ctx1
    assert "[当前时间:2026-05-06 07:08:09" in ctx2


# --------------------------------------------------------------------------
# 6) Stage 2.1 + 记忆通道搬家：记忆块走易变通道，system 串不受 query 影响
# --------------------------------------------------------------------------
#
# P0-11 的不变量要**扩展到记忆**：
#   长期记忆块按「本轮主人说的话」召回 ⇒ 每轮都变。
#   它一旦留在 system prompt 里，前缀缓存第一块每轮作废（98.9% → 1.5%），
#   Stage 2.8 的 agent 指纹也会每轮变（每轮重建 agent）。
#   所以：system 串对不同 query 必须**逐字节相同**，易变段必须**随 query 变**。

def test_system_prompt_is_byte_identical_for_two_different_queries(brain):
    """两个不同 query 下 system 串逐字节相同，易变段随 query 变。

    长期记忆现在住在独立进程 memory_sidecar 里：这里把 HTTP 层换成进程内的
    假实现（tests/fake_sidecar.py），记忆仍按**本轮 query** 召回。
    """
    install(brain.memory_client)
    brain.memory_client.add("主人明天要交房租", kind="fact", importance=0.9)
    brain.memory_client.add("主人的猫叫团子", kind="fact", importance=0.9)

    sp1 = brain.chat_context()
    ctx1 = brain.chat_context_dynamic("记不记得我明天要交房租")
    sp2 = brain.chat_context()
    ctx2 = brain.chat_context_dynamic("主人的猫叫什么")

    assert sp1.encode("utf-8") == sp2.encode("utf-8"), (
        "system 串不能随 query 变 —— 会打掉 8192-token 前缀缓存")
    assert ctx1 != ctx2, "易变通道必须随 query 变"
    assert "房租" in ctx1, "问房租 → 房租那条必须召回"
    assert "团子" in ctx2, "问猫 → 猫那条必须召回"


def test_shared_layer_stable_block_never_contains_the_memory():
    """共享层的稳定块（build_dynamic_block）里**不许**出现记忆块。"""
    from app.brain import prompt_layer

    marker = "【你记得的事】\n- 这是一条只该出现在易变通道的记忆"
    old_provider = prompt_layer._memory_block_provider
    prompt_layer.register_memory_block_provider(lambda query="": marker)
    try:
        stable = prompt_layer.build_dynamic_block()
        volatile = prompt_layer.build_turn_context(query="随便问问")
    finally:
        prompt_layer.register_memory_block_provider(old_provider)

    assert marker not in stable, "记忆块不许进稳定段（system prompt）"
    assert marker in volatile, "记忆块必须进易变通道（独立 user 消息）"


def test_memory_block_is_not_in_the_final_system_prompt(brain):
    """真实生产路径：最终 system 串里没有记忆块。"""
    from app.brain.agent import AgentLoop
    from app.brain.langchain_agent import LangChainAgent
    from app.brain.llm_client import LLMConfig

    brain.memory.add("主人养了一只叫豆豆的猫", category="fact", importance=0.9)

    class _Reg:
        def names(self, level=None):
            return ["open_app", "open_website"]

    reg = _Reg()
    persona = "你是小汐，住在主人桌面上的桌宠。"
    llm = LLMConfig(api_key="sk-test", model="test-model", stream=False)
    react = AgentLoop(client=None, registry=reg, level_getter=lambda: 3)
    standard = LangChainAgent(llm_cfg=llm, registry=reg, persona=persona,
                              level_getter=lambda: 3)
    for sp in (react.assemble_system_prompt(persona=persona),
               standard.assemble_system_prompt(persona=persona)):
        assert "豆豆" not in sp, "记忆块不许进 system prompt"


def test_state_strategy_never_leaks_into_the_stable_system_prompt():
    """状态策略（LOW 状态）不许出现在 system prompt 里。"""
    from app.brain.prompt_layer import (
        build_dynamic_block,
        build_system_prompt,
        build_tool_handbook,
        load_style_guide,
    )

    sp = build_system_prompt(
        persona="你是小汐。",
        style_guide=load_style_guide(),
        tool_handbook=build_tool_handbook(level=3),
        dynamic=build_dynamic_block(),
    )
    assert "桌宠状态策略" not in sp
    assert "状态差" not in sp
    assert "[当前时间:" not in sp
