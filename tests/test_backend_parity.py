# -*- coding: utf-8 -*-
"""Stage 1.1 —— 两个后端的最终 system 字符串必须**逐字节相同**。

背景
----
ReAct（轻量）与 standard（LangChain）以前**各自**拼 system prompt，
同一个模型会因后端不同而表现出两套性格（中文输出约束 / 说话铁律 /
工具解锁 / 项目文档+skills+记忆块 各只在一侧）。

这里锁住的不变量
----------------
1. 两个后端的**生产组装函数**产出的 system 字符串逐字节相同
   （ReAct: agent.AgentLoop.assemble_system_prompt；
     standard: langchain_agent.LangChainAgent.assemble_system_prompt）；
2. ReAct 真正发出去的那条 system 消息（LLMClient.system_prompt，
   经 AgentLoop 起跑前重拼）与 standard 侧逐字节相同；
3. 防幻觉基线只出现**一次**（不能两边各拼一遍）；
4. characters/style-guide.md **逐字**出现在最终字符串里；
5. 硬上限「回复 1~3 句」已经不在（Stage 1.4），换成长度策略；
6. 状态策略 / 当前时间**只在易变通道**（独立的 user 消息），不进 system prompt。

测试里所有「每轮会变」的值（时间 / 桌宠状态）都是**固定常量** ——
system prompt 本来就不该含它们。
"""
from __future__ import annotations

import datetime
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/fake_sidecar.py
if (ROOT / ".local-packages").is_dir():
    sys.path.insert(0, str(ROOT / ".local-packages"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.brain.prompt_layer import (  # noqa: E402
    anti_hallucination_suffix,
    build_turn_context,
    load_style_guide,
)
from fake_sidecar import install  # noqa: E402

#: 固定的人设 + 会话上下文（模拟 BrainController.chat_context() 拼的那段）
PERSONA = "你是小汐，住在主人桌面上的桌宠，只跟主人一个人说话。"
MEMORY_BLOCK = "【你对主人的长期记忆】\n- 主人喜欢喝冰美式"
PERSONA_BLOB = PERSONA + "\n\n" + MEMORY_BLOCK

#: 固定等级（工具解锁提示要用）
LEVEL = 3

#: 固定工具集（两个后端拿到**同一个**注册表）
ALL_TOOLS = ["open_app", "open_website", "web_search", "add_reminder",
             "remember_fact", "lock_screen"]
VISIBLE_TOOLS = ["open_app", "open_website", "web_search", "add_reminder"]

STYLE_GUIDE_PATH = ROOT / "characters" / "style-guide.md"


class _FakeRegistry:
    """够用的工具注册表替身：names(level) 按等级过滤。"""

    def __init__(self, names=ALL_TOOLS, visible=VISIBLE_TOOLS):
        self._names = list(names)
        self._visible = set(visible)

    def names(self, level=None):
        if level is None:
            return list(self._names)
        return [n for n in self._names if n in self._visible]


def _make_react(registry, level=LEVEL):
    """ReAct 后端（轻量循环）。client 传 None —— 这里只测 prompt 组装。"""
    from app.brain.agent import AgentLoop
    return AgentLoop(client=None, registry=registry,
                     level_getter=lambda: level)


def _make_standard(registry, level=LEVEL, persona=PERSONA_BLOB):
    """standard 后端（LangChain）。"""
    from app.brain.langchain_agent import LangChainAgent
    from app.brain.llm_client import LLMConfig
    llm = LLMConfig(api_key="sk-test", model="test-model", stream=False)
    return LangChainAgent(llm_cfg=llm, registry=registry, persona=persona,
                          level_getter=lambda: level)


# --------------------------------------------------------------------------
# 1) 两个后端的生产组装函数 → 逐字节相同
# --------------------------------------------------------------------------

def test_react_and_standard_system_prompt_are_byte_identical():
    """ReAct 与 standard 的生产组装路径，产出的 system 字符串必须逐字节相同。"""
    reg = _FakeRegistry()
    react_sp = _make_react(reg).assemble_system_prompt(persona=PERSONA_BLOB)
    standard_sp = _make_standard(reg).assemble_system_prompt(persona=PERSONA_BLOB)

    # 字节级比较（不是解码后的 ==）
    assert react_sp.encode("utf-8") == standard_sp.encode("utf-8"), (
        "两个后端的 system prompt 不一致 —— "
        f"ReAct {len(react_sp.encode('utf-8'))}B / "
        f"standard {len(standard_sp.encode('utf-8'))}B"
    )


# --------------------------------------------------------------------------
# 1b) 真·生产路径：BrainController 出来的会话块 → 两个后端
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def brain(tmp_path_factory):
    """真 BrainController（临时 root，不碰 data/ 下的真库）。"""
    import shutil

    from app.core.qt_compat import QApplication
    QApplication.instance() or QApplication(sys.argv[:1])

    from app.core.config import load_config
    from app.engine.state_manager import StateManager
    from app.brain.brain_controller import BrainController

    tmp = tmp_path_factory.mktemp("brain_parity")
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


def test_production_path_system_prompts_are_byte_identical(brain):
    """真·生产路径（不是手工喂同一份输入）两个后端逐字节相同。

    生产路径 = 聊天窗口的拼法：
        system = persona + "\n\n" + BrainController.chat_context() + skill_prompt
    然后 ReAct 侧走 LLMClient + AgentLoop，standard 侧走 LangChainAgent。
    """
    from app.brain.agent import AgentLoop
    from app.brain.langchain_agent import LangChainAgent
    from app.brain.llm_client import LLMClient, LLMConfig

    # 往长期记忆里塞一条 —— 证明记忆块在**两个后端**都在。
    # 长期记忆现在住在独立进程 memory_sidecar 里：HTTP 层换成进程内的假实现
    # （tests/fake_sidecar.py），这样用例不需要真 sidecar / 网络。
    install(brain.memory_client)
    brain.memory_client.add("主人名字叫小雪", kind="fact", importance=0.9)

    persona = "你是小汐，住在主人桌面上的桌宠。"
    system_blob = persona + "\n\n" + brain.chat_context()

    registry = brain.tool_registry or _FakeRegistry()
    llm = LLMConfig(api_key="sk-test", model="test-model", stream=False)

    client = LLMClient(llm, system_blob)
    try:
        react = AgentLoop(client=client, registry=registry,
                          level_getter=lambda: LEVEL)
        react._apply_system_prompt()
        react_sp = client.system_prompt
    finally:
        import asyncio
        asyncio.run(client.close())

    standard = LangChainAgent(
        llm_cfg=llm, registry=registry, persona=system_blob,
        level_getter=lambda: LEVEL, memory_store=brain.memory,
    )
    standard_sp = standard.assemble_system_prompt()

    assert react_sp.encode("utf-8") == standard_sp.encode("utf-8"), (
        f"生产路径两个后端不一致："
        f"ReAct {len(react_sp.encode('utf-8'))}B / "
        f"standard {len(standard_sp.encode('utf-8'))}B"
    )

    # 长期记忆块**不在** system prompt 里（Stage 2.1 + 记忆通道搬家）：
    # 它按本轮 query 召回 ⇒ 每轮都变 ⇒ 放 system 会打掉 8192-token 前缀缓存，
    # 也会让 Stage 2.8 的 agent 指纹每轮变（每轮重建 agent）。
    for sp in (react_sp, standard_sp):
        assert "主人名字叫小雪" not in sp, (
            "长期记忆块不许进 system prompt —— 它走易变通道")
        assert "【可用工具列表】" in sp, "工具列表必须在两边的 system prompt 里"
        assert "HARD RULES" in sp or "硬规则" in sp, "SKILL 必须在两边的 system prompt 里"

    # 记忆块走**易变通道**（独立的 user 消息），两个后端拿到的是**同一份**
    turn_ctx = brain.chat_context_dynamic("主人名字叫什么")
    assert "主人名字叫小雪" in turn_ctx, (
        "长期记忆块必须出现在易变通道里（它按 query 召回）")
    assert "【你记得的事】" in turn_ctx, "记忆块要带【你记得的事】标题 + 免责文案"


def test_memory_block_is_not_in_system_prompt_but_is_in_volatile_channel(brain):
    """记忆块的通道不变量：system 里没有，易变通道里有，且随 query 变。

    Stage 2.1 + 记忆通道搬家：
    - 记忆块按本轮 query 召回 ⇒ 每轮都变 ⇒ 只能走易变通道；
    - system 串必须**逐字节**不受 query 影响（否则前缀缓存全废）。
    """
    install(brain.memory_client)
    brain.memory_client.add("主人养了一只叫豆豆的猫", kind="fact", importance=0.9)
    brain.memory_client.add("主人住在杭州", kind="fact", importance=0.9)

    sp = brain.chat_context()
    assert "豆豆" not in sp, "记忆块不许进稳定段（system prompt）"

    ctx_cat = brain.chat_context_dynamic("我家的猫叫什么名字")
    ctx_city = brain.chat_context_dynamic("我住在哪里")
    assert "豆豆" in ctx_cat, "问猫 → 猫那条必须召回"
    assert "杭州" in ctx_city, "问住址 → 住址那条必须召回"
    assert ctx_cat != ctx_city, "易变通道必须随 query 变"


def test_react_production_client_prompt_matches_standard():
    """ReAct **真正发出去**的那条 system 消息（LLMClient.system_prompt）
    与 standard 侧逐字节相同。

    走的是生产路径：LLMClient.__init__ → AgentLoop 起跑前重拼
    （工具注册表 + 当前等级只有 ReAct 这边拿得到）。
    """
    from app.brain.agent import AgentLoop
    from app.brain.llm_client import LLMClient, LLMConfig

    reg = _FakeRegistry()
    llm = LLMConfig(api_key="sk-test", model="test-model", stream=False)
    client = LLMClient(llm, PERSONA_BLOB)
    try:
        agent = AgentLoop(client=client, registry=reg,
                          level_getter=lambda: LEVEL)
        agent._apply_system_prompt()
        standard_sp = _make_standard(reg).assemble_system_prompt(
            persona=PERSONA_BLOB)
        assert client.system_prompt.encode("utf-8") == standard_sp.encode("utf-8")
    finally:
        import asyncio
        asyncio.run(client.close())


# --------------------------------------------------------------------------
# 2) 防幻觉基线只出现一次
# --------------------------------------------------------------------------

def test_anti_hallucination_baseline_appears_exactly_once():
    """防幻觉基线 + confirm 协议在最终 system 字符串里**只有一份**。"""
    reg = _FakeRegistry()
    sp = _make_react(reg).assemble_system_prompt(persona=PERSONA_BLOB)
    assert sp.count("【防幻觉基线】") == 1, "防幻觉基线必须只出现一次"
    assert sp.count("【高风险工具 confirm 协议】") == 1, "confirm 协议必须只出现一次"
    # 常量本身完整地落在这份 system 字符串里（不是被拆散重排）
    assert anti_hallucination_suffix() in sp


def test_grounding_middleware_does_not_duplicate_the_baseline():
    """LangChain 的 grounding middleware 现在是**幂等透传**，不再重复注入。"""
    from app.brain.hallucination_guard import grounding_dynamic_prompt

    class _Req:
        def __init__(self, system_prompt):
            self.system_prompt = system_prompt
            self.config = {}

    reg = _FakeRegistry()
    sp = _make_standard(reg).assemble_system_prompt(persona=PERSONA_BLOB)
    out = grounding_dynamic_prompt(_Req(sp))
    assert out == sp, "middleware 必须原样透传（不能再拼一遍后缀）"
    assert out.count("【防幻觉基线】") == 1


# --------------------------------------------------------------------------
# 3) style guide 逐字进 prompt
# --------------------------------------------------------------------------

def test_style_guide_is_appended_verbatim():
    """characters/style-guide.md 的内容**逐字**出现在最终 system 字符串里。"""
    if not STYLE_GUIDE_PATH.is_file():
        pytest.skip("characters/style-guide.md 不在（Stage 1.2 的文件还没落地）")
    raw = STYLE_GUIDE_PATH.read_text(encoding="utf-8")
    reg = _FakeRegistry()
    sp = _make_react(reg).assemble_system_prompt(persona=PERSONA_BLOB)
    assert raw in sp, "style guide 必须逐字出现（不许改写 / 摘要 / 重新包标题）"
    # 共享层读出来的也是同一份
    assert load_style_guide() == raw


def test_missing_style_guide_does_not_break_the_prompt():
    """style guide 读不到时**静默跳过**，绝不让聊天崩。"""
    from app.brain.prompt_layer import load_style_guide as _load

    missing = ROOT / "characters" / "__not_exist__.md"
    assert _load(missing) == ""


# --------------------------------------------------------------------------
# 4) 五个组件都在共享层（最终 system 字符串里都要看得到）
# --------------------------------------------------------------------------

def test_five_components_are_all_in_the_final_system_prompt():
    reg = _FakeRegistry()
    sp = _make_react(reg).assemble_system_prompt(persona=PERSONA_BLOB)

    # ① 中文输出约束
    assert "你是用简体中文聊天的桌宠" in sp
    # ② _STYLE_RULE 拆出来的两段（Stage 1.6）
    assert "【说话方式】" in sp
    assert "【任务执行协议】" in sp
    # ③ 工具解锁等级提示
    assert "【工具解锁】" in sp
    assert f"当前等级 Lv.{LEVEL}" in sp
    # ④ project_docs + skills
    #    （MEMORY_BLOCK 只是「塞进人设里的固定字符串」这个测试夹具 ——
    #     真实的长期记忆块现在走易变通道，不在 system prompt 里）
    assert MEMORY_BLOCK in sp
    if (ROOT / "skills").is_dir() and any((ROOT / "skills").iterdir()):
        # Stage 1.8：skills/ 目录里的行为包**默认不注入**（要显式激活）
        assert "以下是主人激活的 Skills" not in sp
    # ⑤ 状态策略：**不在** system prompt（它在易变通道）
    assert "桌宠状态策略" not in sp
    # 防幻觉基线也在（共享层的稳定段）
    assert "【防幻觉基线】" in sp


def test_hard_cap_is_replaced_by_a_length_strategy():
    """Stage 1.4：硬上限「回复 1~3 句」没了，换成长度策略。"""
    reg = _FakeRegistry()
    sp = _make_react(reg).assemble_system_prompt(persona=PERSONA_BLOB)
    assert "回复 1~3 句" not in sp
    assert "【回复长度】" in sp
    assert "不要每次都一样长" in sp


# --------------------------------------------------------------------------
# 5) 易变内容只在 user 消息里（两个后端同一条通道）
# --------------------------------------------------------------------------

def test_state_strategy_only_lives_in_the_volatile_channel():
    """状态策略进易变上下文（独立 user 消息），不进 system prompt。"""
    from app.engine.state import PetState

    ps = PetState()
    ps.feeling = 0
    ps.strength_food = 0
    ps.likability = 0
    ps.normalize()

    reg = _FakeRegistry()
    sp = _make_react(reg).assemble_system_prompt(persona=PERSONA_BLOB)
    assert "桌宠状态策略" not in sp

    ctx = build_turn_context(now=datetime.datetime(2026, 1, 2, 3, 4, 5), state=ps)
    assert "桌宠状态策略" in ctx, "状态策略必须进易变通道"
    assert "[当前时间:2026-01-02 03:04:05 星期五]" in ctx
    assert ctx.count("桌宠状态策略") == 1, "状态策略只出现一次"


def test_turn_context_keeps_owner_message_untouched():
    """易变上下文是**独立的 user 消息**，主人的原话保持字节不变。"""
    from app.brain.llm_client import inject_dynamic_context

    ctx = build_turn_context(now=datetime.datetime(2026, 1, 2, 3, 4, 5))
    msgs = inject_dynamic_context(
        [{"role": "user", "content": "今天天气怎么样"}], context=ctx)
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == ctx
    assert msgs[-1]["content"] == "今天天气怎么样"
