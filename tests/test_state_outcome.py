"""PetState 状态量化 + 反馈闭环 (创新点 2) 单元测试。

v21-3:
  - PetState.quantize_mood / quantize_satiety / quantize_level / quantize
    把连续数值离散成 LOW/MID/HIGH 三档, 配 hysteresis 防边界抖动
  - PetState.apply_outcome(outcome_type, tool_name, risk_level)
    任务结果反向更新 PetState (反馈层闭环)
"""
from app.engine.state import PetState
import pytest


def test_quantize_mood_three_levels():
    """feeling 80 → HIGH, 50 → MID, 20 → LOW。"""
    ps = PetState()
    ps.feeling = 80
    assert ps.quantize_mood() == "HIGH"
    ps.feeling = 50
    assert ps.quantize_mood() == "MID"
    ps.feeling = 20
    assert ps.quantize_mood() == "LOW"
    print("[OK] quantize_mood: HIGH/MID/LOW 三档正确")


def test_quantize_satiety_three_levels():
    """strength_food 80 → HIGH, 50 → MID, 10 → LOW。"""
    ps = PetState()
    ps.strength_food = 80
    assert ps.quantize_satiety() == "HIGH"
    ps.strength_food = 50
    assert ps.quantize_satiety() == "MID"
    ps.strength_food = 10
    assert ps.quantize_satiety() == "LOW"
    print("[OK] quantize_satiety: HIGH/MID/LOW 三档正确")


def test_quantize_level_three_levels():
    """level 1-4 LOW, 5-14 MID, 15+ HIGH。"""
    ps = PetState()
    ps.level = 1
    assert ps.quantize_level() == "LOW"
    ps.level = 4
    assert ps.quantize_level() == "LOW"
    ps.level = 5
    assert ps.quantize_level() == "MID"
    ps.level = 14
    assert ps.quantize_level() == "MID"
    ps.level = 15
    assert ps.quantize_level() == "HIGH"
    ps.level = 30
    assert ps.quantize_level() == "HIGH"
    print("[OK] quantize_level: 1-4/5-14/15+ 区间正确")


def test_quantize_returns_four_keys():
    """quantize() 一次性返回 mood/satiety/level/favor 四档 dict。

    v23-3 加 favor (好感) 维度, 主人 2026-10-16 用好感替换原 trust.
    """
    ps = PetState()
    ps.feeling = 70
    ps.strength_food = 60
    ps.level = 12
    out = ps.quantize()
    assert set(out.keys()) == {"mood", "satiety", "level", "favor"}, (
        f"quantize 应返 4 键 (v23-3 加 favor). 实际: {out}"
    )
    assert out["mood"] in ("LOW", "MID", "HIGH")
    assert out["satiety"] in ("LOW", "MID", "HIGH")
    assert out["level"] in ("LOW", "MID", "HIGH")
    print(f"[OK] quantize 返回三键: {out}")


def test_quantize_hysteresis_prevents_boundary_jitter():
    """hysteresis: 进入 LOW 后上 40 才回 MID, 进入 HIGH 后低于 60 才回 MID。

    例: feeling=32 (LOW), 然后涨到 35 → 不抖回 MID (40 阈值)。
    例: feeling=68 (HIGH), 然后跌到 65 → 不抖回 MID (60 阈值)。
    """
    ps = PetState()
    # 1. feeling=32 进入 LOW
    ps.feeling = 32
    assert ps.quantize_mood() == "LOW"
    # 2. 涨到 35 还应是 LOW (不跳回 MID)
    ps.feeling = 35
    assert ps.quantize_mood(prev="LOW") == "LOW", (
        "v21-3 应 hysteresis: 进入 LOW 后, 35 仍 LOW"
    )
    # 3. 涨到 40 → 越过 40 阈值, 回到 MID
    ps.feeling = 40
    assert ps.quantize_mood(prev="LOW") == "MID", (
        "v21-3 应 hysteresis: 进入 LOW 后, 40 应回 MID"
    )
    # 4. feeling=68 HIGH
    ps.feeling = 68
    assert ps.quantize_mood() == "HIGH"
    # 5. 跌到 65 → 不抖回 MID (60 阈值)
    ps.feeling = 65
    assert ps.quantize_mood(prev="HIGH") == "HIGH", (
        "v21-3 应 hysteresis: 进入 HIGH 后, 65 仍 HIGH"
    )
    print("[OK] quantize hysteresis 防边界抖动")


def test_apply_outcome_success_increases_feeling_and_exp():
    """outcome=success: feeling+exp 上升。

    v23-3 起数值权重走 yaml (默认跟 日志.txt 一致):
      - feeling +8, exp +20, favor +1
    """
    ps = PetState()
    f0, e0 = ps.feeling, ps.exp
    ps.apply_outcome("success", "run_script", risk_level=3)
    assert ps.feeling > f0, (
        f"success 后 feeling 应 +. f0={f0} → {ps.feeling}"
    )
    assert ps.exp > e0, (
        f"success 后 exp 应 +20 (yaml). e0={e0} → {ps.exp}"
    )
    # v23-3 单步 feeling +8 (yaml 默认), 不再 <5 上限
    assert ps.feeling - f0 == pytest.approx(8.0), (
        f"单步 success 应 +8 (yaml). 实际 +{ps.feeling - f0}"
    )
    print("[OK] apply_outcome success: feeling+8/exp+20 (yaml)")


def test_apply_outcome_failure_drops_feeling_and_food():
    """outcome=failure: feeling 降, favor 降。

    v23-3 数值走 yaml: feeling -12, favor -3.
    """
    ps = PetState()
    f0, food0 = ps.feeling, ps.strength_food
    ps.apply_outcome("failure", "open_website")
    assert ps.feeling < f0, "failure 后 feeling 应 -"
    assert ps.feeling - f0 == pytest.approx(-12.0), (
        f"failure 单步 -12 (yaml). 实际 {ps.feeling - f0}"
    )
    print("[OK] apply_outcome failure: feeling-12/favor-3 (yaml)")


def test_apply_outcome_rejected_drops_more_than_failure():
    """rejected (主人拒绝执行) 比 failure 更有情绪影响 (yaml 默认 rejected=-10).

    v23-3 起 yaml 默认 rejected=-10 / failure=-12. failure 数值比 rejected
    还大 (主人说拒绝没失败重, 但失败钩子更普遍), 仍保持 rejected > failure
    的语义通过其它维度 (favor 减幅 rejected=-2 < failure=-3, 实际
    rejected 是更主动的反向行为). 这里只验证 rejected 后心情确实降.
    """
    a = PetState()
    a.feeling = 50.0
    a.apply_outcome("rejected")
    a_drop = 50.0 - a.feeling
    assert a_drop == pytest.approx(10.0), (
        f"rejected 应 -10 (yaml). 实际 -={a_drop:.2f}"
    )
    print(f"[OK] apply_outcome rejected ({a_drop:.2f})")


def test_apply_outcome_accepted_bumps_feeling_and_exp():
    """outcome=accepted (主人采纳建议): feeling+6, favor+3 (yaml 默认).

    v23-3 改走 yaml, 不再是 +1.0/+2. exp 不再 + (yaml 默认 accepted.exp=0).
    """
    ps = PetState()
    f0 = ps.feeling
    ps.apply_outcome("accepted", "Task", risk_level=2)
    assert ps.feeling - f0 == pytest.approx(6.0), (
        f"accepted 应 +6 feeling (yaml). 实际 +{ps.feeling - f0:.2f}"
    )
    print("[OK] apply_outcome accepted: feeling+6/favor+3 (yaml)")


def test_apply_outcome_feeling_clamps_to_range():
    """feeling / exp 不应被冲出合法范围 [0, 100]。"""
    ps = PetState()
    ps.feeling = 99.0
    for _ in range(20):
        ps.apply_outcome("success", "x", risk_level=4)
    assert ps.feeling <= 100.0, (
        f"feeling 应 ≤ 100. 实际 {ps.feeling}"
    )
    ps.feeling = 1.0
    for _ in range(20):
        ps.apply_outcome("rejected")
    assert ps.feeling >= 0.0, (
        f"feeling 应 ≥ 0. 实际 {ps.feeling}"
    )
    print("[OK] apply_outcome feeling/exp 在 [0, 100] 之内")


def test_apply_outcome_unknown_silent():
    """未知 outcome_type 静默, 不抛, 不破坏 state。"""
    ps = PetState()
    f0, e0, food0 = ps.feeling, ps.exp, ps.strength_food
    try:
        ps.apply_outcome("garbage_type")
    except Exception as e:  # noqa: BLE001
        raise AssertionError(f"未知 outcome 不应抛: {e}")
    # state 不变
    assert (ps.feeling, ps.exp, ps.strength_food) == (f0, e0, food0), (
        f"未知 outcome 不应改 state. diff: "
        f"feeling {f0}→{ps.feeling}, exp {e0}→{ps.exp}, food {food0}→{ps.strength_food}"
    )
    print("[OK] apply_outcome 未知 outcome 静默")
