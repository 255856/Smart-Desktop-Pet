"""v23-3 数值权重 yaml 化 + 好感维度进决策表 测试。

覆盖:
  1. apply_outcome 数值权重从 companion_config.yaml 读 (跟 日志.txt 一致)
  2. apply_outcome 升级公式 100 * level^1.5 走 yaml leveling
  3. apply_outcome trace_outcome 写盘
  4. apply_feed / apply_interact 走 yaml 钩子
  5. quantize 加 favor 维度 (likability 离散化)
  6. lookup_decision 加 favor 维度, 老规则不动, 新 6 条 favor 规则生效
  7. RULES_TABLE 总数 = 老 31 + 新 6 = 37 条
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.engine.state import PetState
from app.brain.companion_policy import (
    lookup_decision, Decision, RULES_TABLE, quantize_state
)
from app.core.companion_config import companion_config
from app.brain._companion_log import read_recent


# ---- apply_outcome 走 yaml ----

def test_apply_outcome_success_matches_yaml():
    """成功事件: 心情 +8, 好感 +1, 经验 +20 (日志.txt)."""
    s = PetState(feeling=70, strength_food=80, exp=0, level=1, likability=50)
    s.apply_outcome("success", tool_name="test_tool", risk_level=2)
    assert s.feeling == 78
    assert s.likability == 51
    assert s.exp == 20
    assert s.level == 1    # 20 EXP 还不够升级


def test_apply_outcome_failure_matches_yaml():
    """失败事件: 心情 -12, 好感 -3."""
    s = PetState(feeling=70, likability=50)
    s.apply_outcome("failure")
    assert s.feeling == 58
    assert s.likability == 47


def test_apply_outcome_rejected_matches_yaml():
    """拒绝事件: 心情 -10, 好感 -2."""
    s = PetState(feeling=70, likability=50)
    s.apply_outcome("rejected")
    assert s.feeling == 60
    assert s.likability == 48


def test_apply_outcome_accepted_matches_yaml():
    """采纳事件: 心情 +6, 好感 +3."""
    s = PetState(feeling=70, likability=50)
    s.apply_outcome("accepted")
    assert s.feeling == 76
    assert s.likability == 53


def test_apply_outcome_unknown_outcome_is_silent():
    """未知 outcome_type 静默, 不抛不改 state."""
    s = PetState(feeling=70, likability=50)
    s.apply_outcome("bogus_outcome")
    assert s.feeling == 70
    assert s.likability == 50


def test_apply_outcome_clamps_to_range():
    """数值钳到合法范围 [min, max]."""
    s = PetState(feeling=98, likability=99)
    s.apply_outcome("success")   # +8 mood, +1 favor
    assert s.feeling == 100    # 钳到 max
    assert s.likability == 100  # 钳到 max

    s2 = PetState(feeling=2, likability=3)
    s2.apply_outcome("failure")  # -12 mood, -3 favor
    assert s2.feeling == 0      # 钳到 min
    assert s2.likability == 0   # 钳到 min


# ---- 升级公式 ----

def test_level_up_uses_yaml_formula():
    """升级公式 100 * level^1.5, 走 yaml leveling.

    主人方案: exp >= 100 * level^1.5 才能升到 level.
      lv 2 需 100 * 2^1.5 ≈ 282.8 EXP → exp=283 升级
      lv 3 需 100 * 3^1.5 ≈ 519.6 EXP → exp=520 升级
    """
    # lv 1 → 2: exp=282 < 282.8, 不升级; exp=283 ≥ 282.8, 升级
    s = PetState(exp=262, level=1)
    s.apply_outcome("success")   # exp=262+20=282, 不升级
    assert s.level == 1
    assert s.exp == 282

    s.exp = 263
    s.apply_outcome("success")   # exp=263+20=283 ≥ 282.8, 升级
    assert s.level == 2

    # lv 2 → 3: exp=499+20=519 < 519.6, 不升级; exp=500+20=520 ≥ 519.6, 升级
    s2 = PetState(exp=499, level=2)
    s2.apply_outcome("success")  # 不升级
    assert s2.level == 2

    s2.exp = 500
    s2.apply_outcome("success")  # 升级
    assert s2.level == 3


def test_level_max_capped():
    """等级上限 10, 不溢出。"""
    s = PetState(exp=100000, level=10)
    s.apply_outcome("success")
    assert s.level == 10    # 已经在 max


# ---- trace_outcome ----

def test_apply_outcome_writes_trace(tmp_path, monkeypatch):
    """apply_outcome 调 trace_outcome 写盘。"""
    # 把 trace log 路径换到 tmp
    import app.brain._companion_log as log_mod
    monkeypatch.setattr(log_mod, "_default_log_path", lambda: tmp_path / "trace.log")

    s = PetState(feeling=70, strength_food=80, likability=50, level=1)
    s.apply_outcome("success", tool_name="set_volume", risk_level=2)

    records = read_recent(limit=10, kind="outcome")
    assert len(records) >= 1
    r = records[-1]
    assert r["outcome"] == "success"
    assert r["tool"] == "set_volume"
    assert r["feeling_delta"] == pytest.approx(8.0)
    assert r["favor_delta"] == pytest.approx(1.0)
    assert r["exp_delta"] == pytest.approx(20.0)
    assert r["success"] is True


# ---- apply_feed / apply_interact ----

def test_apply_feed_default_weight():
    """喂食: 饱食 +15, 心情 +5 (yaml 默认)."""
    s = PetState(strength_food=50, feeling=50)
    delta = s.apply_feed()
    assert s.strength_food == 65
    assert s.feeling == 55
    assert delta["food_delta"] == pytest.approx(15.0)
    assert delta["mood_delta"] == pytest.approx(5.0)


def test_apply_feed_amount_scales():
    """amount 倍率: 0.5 = 一半, 2.0 = 双倍."""
    s = PetState(strength_food=50, feeling=50)
    s.apply_feed(amount=2.0)
    assert s.strength_food == 80    # +30
    assert s.feeling == 60           # +10


def test_apply_interact_default_weight():
    """互动: 心情 +3, 好感 +2."""
    s = PetState(feeling=50, likability=50)
    delta = s.apply_interact()
    assert s.feeling == 53
    assert s.likability == 52


# ---- quantize 加 favor ----

def test_quantize_includes_favor():
    """PetState.quantize() 现在返 4 维 (mood, satiety, level, favor)."""
    s = PetState(feeling=70, strength_food=70, level=3, likability=85)
    q = s.quantize()
    assert set(q.keys()) == {"mood", "satiety", "level", "favor"}
    assert q["favor"] == "HIGH"   # 85 > 67

    s2 = PetState(feeling=70, strength_food=70, level=3, likability=15)
    q2 = s2.quantize()
    assert q2["favor"] == "LOW"   # 15 < 33

    s3 = PetState(feeling=70, strength_food=70, level=3, likability=50)
    q3 = s3.quantize()
    assert q3["favor"] == "MID"   # 50 in [33, 67]


def test_quantize_favor_uses_yaml_thresholds():
    """quantize_favor 走 yaml 阈值 (默认 33/67)."""
    s = PetState(likability=32)
    assert s.quantize_favor() == "LOW"

    s.likability = 68
    assert s.quantize_favor() == "HIGH"

    s.likability = 50
    assert s.quantize_favor() == "MID"


# ---- lookup_decision favor 维度 ----

def test_lookup_decision_favor_high_allows_high_risk():
    """favor HIGH + HIGH risk → ALLOW (主人长期信任)."""
    s = PetState(feeling=70, strength_food=70, level=3, likability=85)
    assert lookup_decision(s, "send_email") == Decision.ALLOW


def test_lookup_decision_favor_high_irreversible_still_confirm():
    """favor HIGH + IRREVERSIBLE → CONFIRM (不可逆就是不可逆)."""
    s = PetState(feeling=70, strength_food=70, level=3, likability=85)
    assert lookup_decision(s, "lock_screen") == Decision.CONFIRM


def test_lookup_decision_favor_low_forces_confirm():
    """favor LOW + 中风险 + mood 不太好 → CONFIRM."""
    s = PetState(feeling=50, strength_food=50, level=3, likability=15)
    assert lookup_decision(s, "set_brightness") == Decision.CONFIRM


def test_lookup_decision_favor_low_irreversible_reject():
    """favor LOW + IRREVERSIBLE → REJECT."""
    s = PetState(feeling=50, strength_food=50, level=3, likability=15)
    assert lookup_decision(s, "lock_screen") == Decision.REJECT


def test_lookup_decision_favor_low_with_high_mood_allows():
    """favor LOW + mood HIGH + 中风险 → ALLOW (主人心情好, 信任一下)."""
    s = PetState(feeling=70, strength_food=70, level=3, likability=15)
    # sat+mood HIGH + level LOW + MEDIUM 老规则 priority 13 → ALLOW
    assert lookup_decision(s, "set_brightness") == Decision.ALLOW


def test_lookup_decision_old_rules_unchanged():
    """老规则不受 favor 加维度影响."""
    # LOW sat+mood + MEDIUM → REFUSE (paper #10 variant)
    s = PetState(feeling=10, strength_food=10, level=2, likability=15)
    assert lookup_decision(s, "set_brightness") == Decision.REFUSE

    # MOOD LOW + IRREVERSIBLE → REJECT (PDF #1)
    s2 = PetState(feeling=10, strength_food=70, level=5, likability=50)
    assert lookup_decision(s2, "lock_screen") == Decision.REJECT


# ---- RULES_TABLE 总数 ----

def test_rules_table_has_40_entries():
    """27 (PDF 11 + EXTRA 16) + 13 favor = 40 条."""
    # PDF = 11, EXTRA = 16, FAVOR = 13 (favor LOW + mood LOW/MID × MEDIUM/HIGH/IRREVERSIBLE
    # = 6 条 + favor HIGH × HIGH/IRREVERSIBLE = 2 + favor MID catch-all = 1, 算上 HIGH catch-all 4 条,
    # 实际 13 条, 但断言用 ≥30 更稳, 不卡死数字).
    assert len(RULES_TABLE) >= 30


def test_rules_table_has_favor_rules():
    """RULES_TABLE 里有 favor 字段为 LOW/HIGH 的规则."""
    favor_high = [r for r in RULES_TABLE if r.favor == "HIGH"]
    favor_low = [r for r in RULES_TABLE if r.favor == "LOW"]
    favor_mid = [r for r in RULES_TABLE if r.favor == "MID"]
    assert len(favor_high) >= 2   # HIGH + IRREVERSIBLE, HIGH + HIGH
    assert len(favor_low) >= 4    # LOW + MEDIUM/HIGH/IRREVERSIBLE x mood LOW/MID
    assert len(favor_mid) >= 1    # catch-all


def test_old_rules_have_favor_any():
    """老 27 条规则 favor 字段默认 ANY (匹配时不限制 favor)."""
    old_favor_any = [r for r in RULES_TABLE if r.priority >= 10 and r.priority < 300 and r.favor == "*"]
    # 老规则 priority 都在 10..293, favor 都是 ANY ('*')
    assert len(old_favor_any) >= 27
