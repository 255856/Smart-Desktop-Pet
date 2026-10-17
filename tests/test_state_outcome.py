"""PetState 状态量化 + 反馈闭环单元测试。"""
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
    """quantize() 一次性返回 mood/satiety/level/favor 四档 dict。"""
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
    """outcome=success: feeling+8, exp+20, favor+1 (yaml 默认)。"""
    ps = PetState()
    f0, e0 = ps.feeling, ps.exp
    ps.apply_outcome("success", "run_script", risk_level=3)
    assert ps.feeling > f0, (
        f"success 后 feeling 应 +. f0={f0} → {ps.feeling}"
    )
    assert ps.exp > e0, (
        f"success 后 exp 应 +20 (yaml). e0={e0} → {ps.exp}"
    )
    # 单步 feeling +8 (yaml 默认), 不再 <5 上限
    assert ps.feeling - f0 == pytest.approx(8.0), (
        f"单步 success 应 +8 (yaml). 实际 +{ps.feeling - f0}"
    )
    print("[OK] apply_outcome success: feeling+8/exp+20 (yaml)")


def test_apply_outcome_failure_drops_feeling_and_food():
    """outcome=failure: feeling -12, favor -3 (yaml 默认)。"""
    ps = PetState()
    f0, food0 = ps.feeling, ps.strength_food
    ps.apply_outcome("failure", "open_website")
    assert ps.feeling < f0, "failure 后 feeling 应 -"
    assert ps.feeling - f0 == pytest.approx(-12.0), (
        f"failure 单步 -12 (yaml). 实际 {ps.feeling - f0}"
    )
    print("[OK] apply_outcome failure: feeling-12/favor-3 (yaml)")


def test_apply_outcome_rejected_drops_more_than_failure():
    """rejected (主人拒绝执行) 默认 -10 (yaml)。这里只验证 rejected 后心情确实降。"""
    a = PetState()
    a.feeling = 50.0
    a.apply_outcome("rejected")
    a_drop = 50.0 - a.feeling
    assert a_drop == pytest.approx(10.0), (
        f"rejected 应 -10 (yaml). 实际 -={a_drop:.2f}"
    )
    print(f"[OK] apply_outcome rejected ({a_drop:.2f})")


def test_apply_outcome_accepted_bumps_feeling_and_exp():
    """outcome=accepted (主人采纳建议): feeling+6, favor+3 (yaml 默认)。"""
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
