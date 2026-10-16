"""PetState 状态量化 + 反馈闭环 (创新点 2) 单元测试。

v21-3:
  - PetState.quantize_mood / quantize_satiety / quantize_level / quantize
    把连续数值离散成 LOW/MID/HIGH 三档, 配 hysteresis 防边界抖动
  - PetState.apply_outcome(outcome_type, tool_name, risk_level)
    任务结果反向更新 PetState (反馈层闭环)
"""
from app.engine.state import PetState


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


def test_quantize_returns_three_keys():
    """quantize() 一次性返回 mood/satiety/level 三档 dict。"""
    ps = PetState()
    ps.feeling = 70
    ps.strength_food = 60
    ps.level = 12
    out = ps.quantize()
    assert set(out.keys()) == {"mood", "satiety", "level"}, (
        f"quantize 应返 mood/satiety/level 三键. 实际: {out}"
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
    """outcome=success: feeling+exp 上升, 风险等级越高 +exp 越多。"""
    ps = PetState()
    f0, e0 = ps.feeling, ps.exp
    ps.apply_outcome("success", "run_script", risk_level=3)
    assert ps.feeling > f0, (
        f"success 后 feeling 应 +. f0={f0} → {ps.feeling}"
    )
    assert ps.exp > e0, (
        f"success 后 exp 应 +3 (risk=3). e0={e0} → {ps.exp}"
    )
    # 单步 success 应有上限 (delta 不应让 feeling 跳 +10)
    assert ps.feeling - f0 < 5.0, (
        f"单步 success 不应 +5 以上. 实际 +{ps.feeling - f0}"
    )
    print("[OK] apply_outcome success: feeling+exp+按风险权重")


def test_apply_outcome_failure_drops_feeling_and_food():
    """outcome=failure: feeling 轻降, strength_food 轻扣。"""
    ps = PetState()
    f0, food0 = ps.feeling, ps.strength_food
    ps.apply_outcome("failure", "open_website")
    assert ps.feeling < f0, "failure 后 feeling 应 -"
    assert ps.strength_food <= food0, "failure 后 strength_food 应 0 或微 -"
    assert f0 - ps.feeling < 1.0, (
        "failure 单步 feeling - 不应 > 1 (avoid 主人觉得任务失败心情骤崩)"
    )
    print("[OK] apply_outcome failure: feeling-/food- 轻扣")


def test_apply_outcome_rejected_drops_more_than_failure():
    """rejected (主人拒绝执行) 比 failure 更有情绪影响, feeling -1.5 > -0.3。

    用同一个初始 state, 跑 rejected / failure 比较 feeling 变化幅度。
    """
    a = PetState()
    b = PetState()
    a.feeling = b.feeling = 50.0
    a.apply_outcome("rejected")
    b.apply_outcome("failure")
    a_drop = 50.0 - a.feeling
    b_drop = 50.0 - b.feeling
    assert a_drop > b_drop, (
        f"rejected 应比 failure 更伤 feeling. "
        f"rejected -={a_drop:.2f}, failure -={b_drop:.2f}"
    )
    assert a_drop >= 1.4 and a_drop <= 1.6, (
        f"rejected 应 -1.5. 实际 -={a_drop:.2f}"
    )
    print(f"[OK] apply_outcome rejected ({a_drop:.2f}) > failure ({b_drop:.2f})")


def test_apply_outcome_accepted_bumps_feeling_and_exp():
    """outcome=accepted (主人采纳建议): feeling+1.0, exp+2。"""
    ps = PetState()
    f0, e0 = ps.feeling, ps.exp
    ps.apply_outcome("accepted", "Task", risk_level=2)
    assert ps.feeling - f0 == 1.0, (
        f"accepted 应 +1.0 feeling. 实际 +{ps.feeling - f0:.2f}"
    )
    assert ps.exp - e0 == 2.0, (
        f"accepted 应 +2 exp. 实际 +{ps.exp - e0:.2f}"
    )
    print("[OK] apply_outcome accepted: feeling+1.0, exp+2")


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
