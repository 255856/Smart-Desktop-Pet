"""Stage 1.5 —— 桌宠状态策略收窄：一份文本 / 只挡高风险工具 / 走易变通道。

为什么这些断言长这样
--------------------
1. MiniMax 按 8192-token 块做前缀缓存，system message 落在**第一块**里。
   块里每轮变一次的内容（时间戳、桌宠状态）都会让整块作废 ——
   实测缓存命中率 98.9% → 1.5%（Stage 0/P0-11 实测）。
   ⇒ 状态策略（随心情/饱食/好感变化）属于**易变通道**：
   每轮动态上下文（user 消息），**不是** system prompt。
2. Stage 1.5 之前 hallucination_guard.py 里有**三份**同样的
   「状态差 → 直接拒绝」指令（_strategy_guidance 的【重要】块 +
   grounding_dynamic_prompt 的 top_directive + final_override），
   都写着「直接说不, 不调用工具」+「禁止 preemptive 问」+
   「主人追问也继续拒绝」。模型把「直接说不」套用到普通请求上 →
   心情一低就什么都拒。
   ⇒ 现在只有 build_state_strategy() 一处产出状态文本，且收窄成
   「只挡高风险工具，普通请求照常回答」。
"""
from __future__ import annotations

import inspect

from app.brain._companion_registry import (
    clear_all,
    clear_state_for_thread,
    set_state_for_thread,
)
from app.brain.hallucination_guard import (
    _ANTI_HALLUCINATION_SUFFIX,
    _RISKY_TOOLS,
    _strategy_guidance,
    build_state_strategy,
    build_state_strategy_for_thread,
    grounding_dynamic_prompt,
    resolve_state,
)
from app.engine.state import PetState


# --------------------------------------------------------------------------
# fixture 状态的构造
# --------------------------------------------------------------------------

def _low_state() -> PetState:
    """心情 / 饱食 / 好感全 LOW —— 触发「状态差」分支。"""
    ps = PetState()
    ps.feeling = 0
    ps.strength_food = 0
    ps.likability = 0
    ps.normalize()
    return ps


def _healthy_state() -> PetState:
    """状态全好 —— 不应触发任何「拒绝」措辞。"""
    ps = PetState()
    ps.feeling = 90
    ps.strength_food = 90
    ps.likability = 90
    ps.level = 1
    ps.normalize()
    return ps


class _Req:
    """grounding_dynamic_prompt 只需要 system_prompt + config。"""

    def __init__(self, thread_id: str, system_prompt: str = "PERSONA"):
        self.system_prompt = system_prompt
        self.config = {"configurable": {"thread_id": thread_id}}


# --------------------------------------------------------------------------
# A1 —— 状态相关文本只有一个出处
# --------------------------------------------------------------------------

def test_a1_state_text_has_exactly_one_source():
    """A1：三份副本合并成一份，且只剩 build_state_strategy() 一处产出。"""
    import app.brain.hallucination_guard as hg

    src = inspect.getsource(hg)

    # 1) 三份副本的特征串全部消失
    for gone in ("第一原则", "本轮最高优先级", "final_override",
                 "top_directive", "继续拒绝", "禁止 preemptive 问"):
        assert gone not in src, f"旧副本残留: {gone!r}"

    # 2) 新文本的特征串在**整个模块里只出现一次**（只有 build_state_strategy）
    marker = "只对下列高风险工具**直接拒绝**"
    assert src.count(marker) == 1, (
        f"状态差指令必须只有一处, 实际 {src.count(marker)} 处"
    )

    # 3) grounding_dynamic_prompt 不再**调用**状态策略（它不产出状态文本）
    import ast
    tree = ast.parse(inspect.getsource(grounding_dynamic_prompt))
    called = {
        n.func.id for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }
    assert "build_state_strategy" not in called, (
        "system prompt 不该再拼状态策略"
    )
    assert "_strategy_guidance" not in called

    # 4) 旧私有名只是别名，不是第二份实现
    alias_tree = ast.parse(inspect.getsource(_strategy_guidance))
    alias_calls = [
        n for n in ast.walk(alias_tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    ]
    assert len(alias_calls) == 1, "_strategy_guidance 里只能有一次调用"
    assert alias_calls[0].func.id == "build_state_strategy", (
        "_strategy_guidance 必须只是转发到 build_state_strategy"
    )


# --------------------------------------------------------------------------
# A2 —— 不许出现「什么都拒」的措辞；普通请求照常回答
# --------------------------------------------------------------------------

def test_a2_low_state_text_has_no_over_refusal_phrases():
    """A2：状态策略里没有「禁止 preemptive 问」/「继续拒绝」，且明说普通请求照答。"""
    text = build_state_strategy(_low_state())
    assert text, "LOW 状态应该产出策略文本"

    for banned in ("禁止 preemptive 问", "继续拒绝", "preemptive",
                   "直接说不", "不调工具", "不调用工具", "停止"):
        assert banned not in text, (
            f"状态策略文本不应含 {banned!r}. 实际:\n{text}"
        )

    # 正向：普通请求照常回答
    assert "普通请求一律照常回答" in text, f"实际:\n{text}"
    assert "不要因为状态差就拒绝帮忙" in text, f"实际:\n{text}"


# --------------------------------------------------------------------------
# A3 —— 安全行为保留：LOW 时高风险工具仍然被挡
# --------------------------------------------------------------------------

def test_a3_low_state_text_names_every_risky_tool_and_refuses_only_those():
    """A3：状态差时高风险工具照旧被拒（名字全部点名），普通工具不挡。"""
    text = build_state_strategy(_low_state())

    missing = [n for n in sorted(_RISKY_TOOLS) if n not in text]
    assert not missing, f"高风险工具没被点名: {missing}"

    for must in ("lock_screen", "run_script", "kill_process", "send_email"):
        assert must in text, f"必须点名 {must}"

    # 风险表漏掉的真实危险工具也不能丢 (fail-safe 并集):
    # shutdown_computer 只在 ReAct 后端注册, 没进 TOOL_RISK_LEVEL
    assert "shutdown_computer" in text, (
        "shutdown_computer 是 ReAct 后端的真实危险工具, 风险表漏了也不能丢"
    )

    assert "直接拒绝" in text, f"实际:\n{text}"
    assert "不要走 confirm 流程" in text, f"实际:\n{text}"

    # 只挡高风险工具 —— 不许出现「拒绝一切」的措辞
    assert "普通请求一律照常回答" in text
    assert "不要因为状态差就拒绝帮忙" in text


# --------------------------------------------------------------------------
# A5 —— 健康状态 / 易变通道
# --------------------------------------------------------------------------

def test_a5_healthy_state_strategy_is_empty_or_harmless():
    """A5：状态好 → 空文本或无害文本（不含任何拒绝措辞）。"""
    assert build_state_strategy(None) == "", "没有 state 时不产出任何文本"

    text = build_state_strategy(_healthy_state())
    if text:
        for banned in ("直接拒绝", "拒绝", "不要走 confirm 流程", "preemptive"):
            assert banned not in text, (
                f"健康状态不该有拒绝措辞 {banned!r}. 实际:\n{text}"
            )


def test_a5_state_strategy_is_not_in_the_stable_system_prompt():
    """A5：状态策略属于**易变**通道，不能进 system prompt（否则缓存块作废）。"""
    tid = "test-state-strategy-volatile"
    clear_all()
    try:
        set_state_for_thread(tid, _low_state())

        sp_low = grounding_dynamic_prompt(_Req(tid))

        # 1) system prompt 里没有状态策略
        assert "桌宠状态策略" not in sp_low
        assert "状态差" not in sp_low

        # 2) 稳定部分（防幻觉基线 + confirm 协议）还在
        assert "PERSONA" in sp_low
        assert "防幻觉基线" in sp_low
        assert "高风险工具 confirm 协议" in sp_low
        assert sp_low.endswith(_ANTI_HALLUCINATION_SUFFIX)

        # 3) state 变了 system prompt 不能变（逐字节稳定 → 缓存能命中）
        set_state_for_thread(tid, _healthy_state())
        sp_high = grounding_dynamic_prompt(_Req(tid))
        assert sp_low == sp_high, (
            "state 变了 system prompt 也变了 → 前缀缓存块会作废"
        )
    finally:
        clear_state_for_thread(tid)
        clear_all()


def test_a5_state_strategy_can_be_injected_into_the_dynamic_context():
    """A5：状态策略**能**进易变通道（每轮动态上下文 = user 消息）。"""
    from app.brain.llm_client import (
        DYNAMIC_CONTEXT_HEADER,
        build_dynamic_context,
        inject_dynamic_context,
    )

    state = _low_state()
    ctx = build_dynamic_context(state=state)
    assert DYNAMIC_CONTEXT_HEADER in ctx, "动态上下文有固定标记行"

    strategy = build_state_strategy(state)
    assert strategy.strip(), "LOW 状态应有策略文本"

    # 共享层（Stage 1.1）的拼法：策略文本接在动态上下文后面
    merged = ctx + "\n" + strategy.strip()
    msgs = inject_dynamic_context(
        [{"role": "user", "content": "今天天气怎么样"}], context=merged,
    )
    assert msgs[0]["content"] == merged, "策略文本应落在动态上下文消息里"
    assert msgs[-1]["content"] == "今天天气怎么样", (
        "主人的原话必须保持字节不变（意图判定读最后一条 user 消息）"
    )
    assert "普通请求一律照常回答" in msgs[0]["content"]


# --------------------------------------------------------------------------
# 给 Stage 1.1 共享层的接线契约
# --------------------------------------------------------------------------

def test_resolve_state_and_for_thread_helper():
    """接线契约：resolve_state(thread_id) / build_state_strategy_for_thread(tid)。"""
    tid = "test-state-strategy-resolve"
    clear_all()
    try:
        assert resolve_state(tid) is None, "没注册时返 None"
        assert resolve_state("") is None
        assert build_state_strategy_for_thread(tid) == ""

        ps = _low_state()
        set_state_for_thread(tid, ps)
        assert resolve_state(tid) is ps
        assert build_state_strategy_for_thread(tid) == build_state_strategy(ps)
    finally:
        clear_state_for_thread(tid)
        clear_all()


def test_legacy_alias_strategy_guidance_delegates():
    """旧私有名还在（app/ui/settings_window.py 的策略预览在用），但只做转发。"""
    ps = _low_state()
    assert _strategy_guidance(ps) == build_state_strategy(ps)
    assert _strategy_guidance(None) == ""
