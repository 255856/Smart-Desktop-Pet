"""v23-4 数值持久化 + v23-5 事件驱动钩子 + v23-6 时间驱动衰减 测试。

覆盖:
  - 升级公式 100 * level^1.5 走 yaml leveling
  - 存档 SaveStore 走 yaml persistence.file (data/pet_state.json)
  - write_on_update 立即写, 防抖 5s
  - apply_feed / apply_interact 走 yaml 钩子
  - 时间驱动衰减: 饱食度按小时 -0.833, 48h 不互动心情 -5
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.engine.state import PetState
from app.core.companion_config import companion_config
from app.core.save import SaveStore, _resolve_save_path


# ---- v23-4 升级公式 ----

def test_level_formula_uses_yaml():
    """升级公式 100 * level^1.5 走 yaml leveling. lv 2 需 282.8 EXP."""
    s = PetState()
    # lv 1: 0
    assert s.cal_level() == 1
    # lv 1 → 2: 需 base * 2^exp = 100 * 2^1.5 ≈ 282.8
    s.exp = 282
    assert s.cal_level() == 1
    s.exp = 283
    assert s.cal_level() == 2
    # lv 2 → 3: 100 * 3^1.5 ≈ 519.6
    s.exp = 519
    assert s.cal_level() == 2
    s.exp = 520
    assert s.cal_level() == 3


def test_level_max_caps_at_10():
    """等级上限 10, 不溢出."""
    s = PetState()
    s.exp = 999999
    assert s.cal_level() == 10


def test_level_up_need_uses_yaml():
    """level_up_need 走 yaml 公式."""
    s = PetState()
    s.level = 1
    expected = int(companion_config.leveling.base * (1 ** companion_config.leveling.exp))
    assert s.level_up_need() == expected


# ---- v23-4 存档路径 ----

def test_resolve_save_path_relative(tmp_path, monkeypatch):
    """相对路径 data/pet_state.json → cwd 解析 (父目录要存在)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    p = _resolve_save_path("data/pet_state.json")
    assert p.is_absolute()
    assert p == (tmp_path / "data" / "pet_state.json")


def test_resolve_save_path_absolute_keeps():
    """绝对路径保持不变."""
    abs_path = Path("C:/pet_state.json") if Path("C:/pet_state.json").is_absolute() else \
               Path.home() / "pet_state.json"
    p = _resolve_save_path(str(abs_path))
    assert p == abs_path


def test_savestore_uses_yaml_default_path(monkeypatch, tmp_path):
    """SaveStore 默认路径走 yaml persistence.file (data/pet_state.json)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    store = SaveStore(path=None, autosave_seconds=0)
    assert store.path == tmp_path / "data" / "pet_state.json"
    # 也确认 yaml 配置被读了
    assert store.write_on_update == companion_config.persistence.write_on_update
    assert store.write_min_interval == companion_config.persistence.write_min_interval_seconds


# ---- v23-4 写盘 + reload ----

def test_savestore_write_and_reload(tmp_path):
    """写盘 → 新 SaveStore 读盘: 数值完整恢复."""
    target = tmp_path / "data" / "pet_state.json"
    target.parent.mkdir(parents=True, exist_ok=True)

    store1 = SaveStore(path=target, autosave_seconds=0)
    s1 = PetState()
    s1.feeling = 73
    s1.strength_food = 81
    s1.exp = 250
    s1.level = 2
    s1.likability = 42
    store1.set_state(s1)
    store1.write_sync(s1)

    assert target.is_file()

    store2 = SaveStore(path=target, autosave_seconds=0)
    s2 = store2.load()
    assert s2.feeling == 73
    assert s2.strength_food == 81
    assert s2.exp == 250
    assert s2.level == 2
    assert s2.likability == 42


def test_savestore_mark_dirty_writes_immediately(tmp_path):
    """write_on_update=True + 防抖: mark_dirty 触发立即写 (1.5s 内)."""
    target = tmp_path / "data" / "pet_state.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    # 直接构造 SaveStore, 覆盖防抖为 0 让 mark_dirty 立即写
    store = SaveStore(path=target, autosave_seconds=0)
    store.write_min_interval = 0
    store.write_on_update = True

    s = PetState()
    s.feeling = 99
    store.set_state(s)
    store.mark_dirty()
    assert target.is_file(), "write_on_update=True 应立即写"
    import json
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["feeling"] == 99


def test_savestore_debounce_skips_rapid_writes(tmp_path):
    """防抖: 距离上次写 < min_interval, mark_dirty 不写第二次."""
    target = tmp_path / "data" / "pet_state.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    store = SaveStore(path=target, autosave_seconds=0)
    store.write_min_interval = 100   # 100s 防抖
    store.write_on_update = True
    s = PetState()
    store.set_state(s)

    # 第一次立即写
    s.feeling = 50
    store.mark_dirty()
    assert target.is_file()
    mtime1 = target.stat().st_mtime

    # 立刻第二次: 100s 防抖期内, 不写
    time.sleep(0.05)
    s.feeling = 60
    store.mark_dirty()
    mtime2 = target.stat().st_mtime
    assert mtime2 == mtime1, "防抖期内不应再写"
    assert store._dirty is True, "但 dirty 标记还在, 兜底会写"


# ---- v23-5 apply_feed / apply_interact ----

def test_apply_feed_uses_yaml():
    """apply_feed 走 yaml user_feed (satiety +15, mood +5)."""
    s = PetState(strength_food=50, feeling=50)
    s.apply_feed()
    assert s.strength_food == 65
    assert s.feeling == 55


def test_apply_interact_uses_yaml_and_records_ts():
    """apply_interact 走 yaml user_interact (mood +3, favor +2) + 记录 _last_interact_ts."""
    s = PetState(feeling=50, likability=50)
    before_ts = s._last_interact_ts
    s.apply_interact()
    assert s.feeling == 53
    assert s.likability == 52
    assert s._last_interact_ts > before_ts


# ---- v23-6 时间驱动衰减 ----

def test_time_decay_satiety_per_hour():
    """时间驱动: 饱食度按 -0.833/h 衰减."""
    s = PetState(strength_food=80)
    cfg = companion_config.time_decay
    # 模拟 1 小时
    s.strength_food = max(0.0, s.strength_food + cfg.satiety_per_hour * 1.0)
    assert s.strength_food == pytest.approx(80 - 0.833, abs=0.01)
    # 模拟 24 小时
    s2 = PetState(strength_food=80)
    s2.strength_food = max(0.0, s2.strength_food + cfg.satiety_per_hour * 24.0)
    assert s2.strength_food == pytest.approx(60.0, abs=0.1)


def test_time_decay_clamps_to_zero():
    """饱食度衰减不应跌破 0."""
    s = PetState(strength_food=5)
    cfg = companion_config.time_decay
    s.strength_food = max(0.0, s.strength_food + cfg.satiety_per_hour * 100)
    assert s.strength_food == 0.0
