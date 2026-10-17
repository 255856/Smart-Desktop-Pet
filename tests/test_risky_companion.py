"""RiskyToolConfirm 接入 companion_state 单元测试。"""
import asyncio
from app.brain.hallucination_guard import RiskyToolConfirm, _RISKY_TOOLS
from app.brain.companion_policy import Decision
from app.engine.state import PetState
from app.engine.tools._risk import IRREVERSIBLE, risk_for_tool


def test_set_companion_state_none_then_state():
    """默认无状态, set_companion_state(PetState) 注入, set_companion_state(None) 清除。"""
    ctrl = RiskyToolConfirm()
    assert ctrl._companion_state is None, "默认应 None"
    ps = PetState()
    ctrl.set_companion_state(ps)
    assert ctrl._companion_state is ps, "应注入 ps"
    ctrl.set_companion_state(None)
    assert ctrl._companion_state is None, "应清除回 None"
    print("[OK] RiskyToolConfirm.set_companion_state: 注入 + 清除")


def test_get_companion_decision_no_state_returns_none():
    """无 state 时返 None = 走旧 fallback (向后兼容)。"""
    ctrl = RiskyToolConfirm()
    assert ctrl._get_companion_decision("lock_screen") is None
    assert ctrl._get_companion_decision("get_current_time") is None
    assert ctrl._get_companion_decision("") is None  # 空 tool_name 返 None
    print("[OK] RiskyToolConfirm: 无 state → None 兼容 fallback")


def test_get_companion_decision_with_state_default_satiety():
    """默认 PetState (feeling=80 sat=100 level=1) + lock_screen:
    HIGH sat+mid mood + LOW level + IRREVERSIBLE → CONFIRM (走 PDF #11 升级拦截)
    """
    ctrl = RiskyToolConfirm()
    ctrl.set_companion_state(PetState())  # default
    d = ctrl._get_companion_decision("lock_screen")
    assert d == Decision.CONFIRM, (
        f"default state + lock_screen 应 CONFIRM. 实际: {d}"
    )
    print("[OK] RiskyToolConfirm: default state + lock_screen → CONFIRM")


def test_get_companion_decision_allows_under_high_mood_high_level():
    """心情好 + 等级高 + 高风险 = PDF #7 ALLOW (主人特权)。"""
    ctrl = RiskyToolConfirm()
    ps = PetState()
    ps.feeling = 90    # HIGH
    ps.level = 18      # HIGH
    ctrl.set_companion_state(ps)
    d = ctrl._get_companion_decision("run_script")  # HIGH
    assert d == Decision.ALLOW, (
        f"心情 HIGH + 等级 HIGH + HIGH risk 应 ALLOW (PDF #7). 实际: {d}"
    )
    print("[OK] RiskyToolConfirm: 心情+等级 HIGH → 主人特权 ALLOW")


def test_get_companion_decision_rejects_under_double_low():
    """sat+mood 都 LOW + 中风险 = paper #10 variant → REFUSE。

    注意: 心情低 + 饿应该是桌宠情绪表达, 不是 technical reject.
    Decision.REFUSE 跟 REJECT 在中间件层都走 sentinel 拦截, 但语义
    不同 (REFUSE 含 "自然语言解释" 的含义)。
    """
    ctrl = RiskyToolConfirm()
    ps = PetState()
    ps.feeling = 25        # LOW
    ps.strength_food = 25  # LOW
    ctrl.set_companion_state(ps)
    d = ctrl._get_companion_decision("edit_file")  # MEDIUM
    assert d == Decision.REFUSE, (
        f"sat+mood LOW + MEDIUM 应 REFUSE (paper #10). 实际: {d}"
    )
    print("[OK] RiskyToolConfirm: sat+mood LOW + MEDIUM → REFUSE")


def test_get_companion_decision_allows_safe_tools_under_double_low():
    """sat+mood 都 LOW 但纯读 (SAFE) 仍 ALLOW (paper #10 variant override)。

    桌宠情绪再差, 纯读不做事, 不挡。这跟"先 git checkout"原理一样。
    """
    ctrl = RiskyToolConfirm()
    ps = PetState()
    ps.feeling = 25
    ps.strength_food = 25
    ctrl.set_companion_state(ps)
    d = ctrl._get_companion_decision("get_current_time")  # SAFE
    assert d == Decision.ALLOW, (
        f"sat+mood LOW 但纯读 SAFE 应 ALLOW. 实际: {d}"
    )
    print("[OK] RiskyToolConfirm: sat+mood LOW 但纯读 → ALLOW")


def test_legacy_fallback_still_works_for_unknown_tool():
    """未知工具 (risk 默认 MEDIUM, 但不在 _RISKY_TOOLS 通常) 走 default fallback。

    验证: companion_state 不影响 lookup_decision 的 fallback (返回 ALLOW for non-risky)。
    """
    ctrl = RiskyToolConfirm()
    ctrl.set_companion_state(PetState())
    # 一个我们没登记的工具: risk_for_tool 默认 MEDIUM, decision 默认 ALLOW
    d = ctrl._get_companion_decision("a_b_random_unknown_tool_xyz")
    assert d == Decision.ALLOW, (
        f"未知工具应 fallback ALLOW. 实际: {d}"
    )
    print("[OK] RiskyToolConfirm: 未知工具 fallback ALLOW (跟旧一致)")


def test_v215_decision_uses_state_after_apply_outcome():
    """owner 反馈循环: apply_outcome('rejected') 后 mood 跌落, 决策应从 ALLOW 变 CONFIRM。

    验证闭环:
      - 初始 mood HIGH + level HIGH + run_script → ALLOW
      - apply_outcome('rejected') 后 mood LOW
      - 同一 tool 现在查决策 → CONFIRM (心情 LOW + HIGH risk → paper #2)
    """
    ctrl = RiskyToolConfirm()
    ps = PetState()
    ps.feeling = 90    # HIGH
    ps.level = 18      # HIGH
    ctrl.set_companion_state(ps)

    before = ctrl._get_companion_decision("run_script")  # HIGH
    assert before == Decision.ALLOW, (
        f"初始应 ALLOW. 实际: {before}"
    )

    # 主人拒绝执行 → mood 骤降 1.5 → 90-1.5=88.5 仍 HIGH
    # 再 reject 几次让 mood 跌到 LOW
    for _ in range(50):
        ps.apply_outcome("rejected")
    # mood 应 < 33 (LOW)
    assert ps.feeling < 40, (
        f"多次 rejected 应让 mood 跌落. 实际 feeling={ps.feeling}"
    )
    after = ctrl._get_companion_decision("run_script")  # HIGH
    assert after == Decision.CONFIRM, (
        f"心情 LOW 后 run_script 应 CONFIRM (paper #2). 实际: {after}"
    )
    assert before != after, (
        "v21-5 反馈闭环应让决策从 ALLOW 变 CONFIRM, 但实际前后一致"
    )
    print(f"[OK] v21-5 反馈闭环: ALLOW → CONFIRM (after rejected ×50)")


def test_v224_fire_companion_reject_outcome_no_state_silent():
    """没注入 state 时, _fire_companion_reject_outcome 静默不抛。"""
    from app.brain.hallucination_guard import RiskyToolConfirm
    ctrl = RiskyToolConfirm()
    try:
        ctrl._fire_companion_reject_outcome("lock_screen")
    except Exception as e:  # noqa: BLE001
        raise AssertionError(f"无 state 不应抛: {e}")
    print("[OK] v22-4: _fire_companion_reject_outcome 无 state 静默")


def test_v224_companion_reject_drops_feeling():
    """companion REJECT/REFUSE 拦阻 → PetState.apply_outcome('rejected') → 心情骤降。

    验证中间件在拦截路径上调了 _fire_companion_reject_outcome,
    这个 helper 调 state.apply_outcome('rejected', tool_name, risk_level)。

    这里只断言"心情确实降 + favor 降", 具体数值在 test_state_outcome_yaml.py 测.
    """
    from app.brain.hallucination_guard import RiskyToolConfirm
    from app.engine.state import PetState
    from app.engine.tools._risk import HIGH
    ctrl = RiskyToolConfirm()
    ps = PetState()
    ps.likability = 50.0    # 给 favor 一个非 0 值, 避免被钳到 0 时测不出变化
    f0, fav0 = ps.feeling, ps.likability
    ctrl.set_companion_state(ps)
    # 调用 helper: 模拟一次 companion REJECT 拦阻 (心情 LOW + HIGH tool)
    ctrl._fire_companion_reject_outcome("run_script")
    assert ps.feeling < f0, (
        f"rejected outcome 应让 feeling < f0. f0={f0} → {ps.feeling}"
    )
    assert ps.likability < fav0, (
        f"rejected outcome 应让 favor < fav0. fav0={fav0} → {ps.likability}"
    )
    print(f"[OK] v22-4: companion reject → apply_outcome('rejected') -{f0 - ps.feeling:.1f} feeling, -{fav0 - ps.likability:.1f} favor")


def test_v224_companion_reject_state_missing_apply_silent():
    """注入 state 但没 apply_outcome 方法时, 静默 (不抛, 不调假函数)。"""
    from app.brain.hallucination_guard import RiskyToolConfirm
    ctrl = RiskyToolConfirm()

    class FakeNoApply:
        pass

    ctrl.set_companion_state(FakeNoApply())
    try:
        ctrl._fire_companion_reject_outcome("run_script")
    except Exception as e:  # noqa: BLE001
        raise AssertionError(f"缺 apply_outcome 不应抛: {e}")
    print("[OK] v22-4: 缺 apply_outcome 静默")
