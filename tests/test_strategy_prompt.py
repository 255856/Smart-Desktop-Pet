"""v22-3 策略层 prompt 注入单元测试。

覆盖:
  - _companion_registry 增删查
  - _strategy_guidance: HIGH/HIGH/HIGH / LOW/LOW / 无 state / 字段缺失
  - grounding_dynamic_prompt 按 request.config.thread_id 拿 state
  - LangChainAgent.set_companion_state 注册 / 反注册
"""
from app.engine.state import PetState
from app.brain._companion_registry import (
    set_state_for_thread, get_state_for_thread, clear_state_for_thread, clear_all,
)
from app.brain.hallucination_guard import (
    _strategy_guidance, grounding_dynamic_prompt,
)


# ---- registry 测试 ----

def test_registry_roundtrip():
    ps = PetState()
    set_state_for_thread("t1", ps)
    got = get_state_for_thread("t1")
    assert got is ps, f"应返回同一对象. 实际: {got is ps}"
    clear_state_for_thread("t1")
    assert get_state_for_thread("t1") is None
    print("[OK] _companion_registry: set / get / clear")


def test_registry_clear_all():
    set_state_for_thread("a", PetState())
    set_state_for_thread("b", PetState())
    clear_all()
    assert get_state_for_thread("a") is None
    assert get_state_for_thread("b") is None
    print("[OK] _companion_registry: clear_all")


# ---- _strategy_guidance 测试 ----

def test_strategy_no_state_returns_empty():
    """state=None 走 fallback 保守, 不注入策略块。"""
    assert _strategy_guidance(None) == ""
    print("[OK] _strategy_guidance(None) = ''")


def test_strategy_high_high_state():
    """心情好 + 饱食好 + 等级高 → 探索 + 自主小决定。"""
    ps = PetState()
    ps.feeling = 90
    ps.strength_food = 90
    ps.level = 18
    out = _strategy_guidance(ps)
    assert "状态好" in out, f"应有「状态好」探索段. 实际: {out}"
    assert "探索" in out or "主动建议" in out, (
        f"应提到主动建议. 实际: {out}"
    )
    assert "等级已高" in out, f"应有等级段. 实际: {out}"
    print("[OK] _strategy_guidance(HIGH/HIGH/HIGH): 探索 + 自主")


def test_strategy_low_state():
    """心情 + 饱食都 LOW → 简短 + 优先核心。"""
    ps = PetState()
    ps.feeling = 20
    ps.strength_food = 20
    out = _strategy_guidance(ps)
    assert "情绪低落" in out or "简短" in out, (
        f"应有「简短回复」. 实际: {out}"
    )
    assert "饱食度低" in out or "避免多步" in out, (
        f"应有「避免多步」. 实际: {out}"
    )
    print("[OK] _strategy_guidance(LOW/LOW): 保守 + 核心优先")


def test_strategy_satiety_low_mood_mid():
    """饱食低 + 心情中 → 仅触发饱食规则. 不应触发 mood-high 探索。"""
    ps = PetState()
    ps.strength_food = 20  # LOW
    ps.feeling = 50        # MID
    out = _strategy_guidance(ps)
    assert "饱食度低" in out
    assert "状态好" not in out, (
        f"心情中不应触发探索段. 实际: {out}"
    )
    assert "情绪低落" not in out, (
        f"心情中不应触发保守段. 实际: {out}"
    )
    print(f"[OK] _strategy_guidance(LOW/MID): 仅饱食段触发")


def test_strategy_missing_fields_silent():
    """state 缺字段不抛, 尽量产出合理策略。"""
    class FakeState:
        feeling = 30  # LOW
        # 没 strength_food 字段

    out = _strategy_guidance(FakeState())
    # 可能返空 / 可能产出部分. 关键是 not throw
    assert isinstance(out, str), f"返 string. 实际: {type(out)}"
    print(f"[OK] _strategy_guidance(FakeState) 不抛: '{out[:40]}'")


# ---- grounding_dynamic_prompt 测试 ----

def test_prompt_injects_strategy_when_state_in_registry():
    """registry 有 state + request 带 thread_id → prompt 含【状态策略】"""
    set_state_for_thread("t-strategy", PetState())  # 默认 HIGH sat+mid mood
    # 构造一个 mock request (LangChain dynamic_prompt 接口)
    class Req:
        system_prompt = "基线"
        config = {"configurable": {"thread_id": "t-strategy"}}
    prompt = grounding_dynamic_prompt(Req())
    assert "【桌宠状态策略 (v22-3 策略层)】" in prompt, (
        f"prompt 应注入策略块. 实际:\n{prompt}"
    )
    # 默认 state 是 feeling=80/sat=100/level=1 → HIGH sat + HIGH mood
    assert "状态好" in prompt, (
        f"默认 state 应触发探索. 实际:\n{prompt}"
    )
    clear_state_for_thread("t-strategy")
    print("[OK] grounding_dynamic_prompt: registry 有 state → prompt 注入策略块")


def test_prompt_no_state_no_strategy_block():
    """registry 没 state 或 thread_id 不匹配 → 不注入策略块 (但 prompt 正常 base)。"""
    set_state_for_thread("t-other", PetState())

    class Req:
        system_prompt = "基线"
        config = {"configurable": {"thread_id": "t-no-state"}}
    prompt = grounding_dynamic_prompt(Req())
    assert "【桌宠状态策略 (v22-3 策略层)】" not in prompt, (
        f"未匹配 thread 不应注入. 实际:\n{prompt}"
    )
    # 防幻觉基线仍应在
    assert "防幻觉基线" in prompt, "prompt 应仍含防幻觉基线"
    clear_state_for_thread("t-other")
    print("[OK] grounding_dynamic_prompt: 不匹配 thread_id → 不注入策略")


def test_prompt_empty_config_no_crash():
    """request 没 config 属性 / config 是空 dict 时不抛。"""
    class ReqNoCfg:
        system_prompt = "基线"
    prompt = grounding_dynamic_prompt(ReqNoCfg())
    assert "防幻觉基线" in prompt
    print("[OK] grounding_dynamic_prompt: 无 config 属性 → 不抛")


# ---- LangChainAgent.set_companion_state 测试 ----

def test_langchain_set_companion_state_register():
    """LangChainAgent.set_companion_state(PetState) → registry 按 thread_id 注册。"""
    from app.brain.langchain_agent import LangChainAgent, LangChainAgentConfig
    from app.engine.tools import ToolRegistry
    from app.core.config import LLMConfig
    # 测试 isolation: 用 unique thread_id
    test_id = "t-langchain-test"
    agent = LangChainAgent(
        llm_cfg=LLMConfig(),
        registry=ToolRegistry(),
        persona="test",
        cfg=LangChainAgentConfig(enable_checkpointer=False),
        thread_id=test_id,
    )
    try:
        ps = PetState()
        agent.set_companion_state(ps)
        got = get_state_for_thread(test_id)
        assert got is ps, f"registry 应按 thread_id={test_id} 持 ps"
        # 反注册
        agent.set_companion_state(None)
        assert get_state_for_thread(test_id) is None
    finally:
        clear_state_for_thread(test_id)
    print("[OK] LangChainAgent.set_companion_state ↔ registry sync")
