"""桌宠数值配置 yaml 单元测试。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

# 让测试在没装 PyYAML 时也能 import (yaml 加载失败时走内置 _DEFAULTS)
try:
    import yaml  # noqa: F401
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

pytestmark = pytest.mark.skipif(not _HAS_YAML, reason="PyYAML 不可用")


# ---- fixtures ----

@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch):
    """每个测试前清掉 cached config, 防止上一次测试的 _user_yaml 影响。"""
    import app.core.companion_config as m
    m._cached = None
    # monkey patch _user_yaml_path 用临时目录
    yield
    m._cached = None


def _write_user_yaml(tmp_path: Path, content: str) -> Path:
    user_yaml = tmp_path / "companion_config.yaml"
    user_yaml.write_text(content, encoding="utf-8")
    return user_yaml


def _patch_user_path(monkeypatch, user_yaml_path: Path):
    """把 _user_yaml_path() 指向 tmp 路径。"""
    import app.core.companion_config as m
    monkeypatch.setattr(m, "_user_yaml_path", lambda: user_yaml_path)


# ---- 默认值测试 ----

def test_default_yaml_loads():
    """仓库默认 yaml 加载后 dataclass 字段正确。"""
    from app.core.companion_config import companion_config

    # 范围 + 初始值 (主人日志.txt)
    assert companion_config.satiety.initial == 80
    assert companion_config.mood.initial == 70
    assert companion_config.level.initial == 1
    assert companion_config.favor.initial == 50    # 好感 (替换 trust)

    # 升级公式
    assert companion_config.leveling.base == 100
    assert companion_config.leveling.exp == 1.5

    # 时间衰减
    assert companion_config.time_decay.satiety_per_hour == pytest.approx(-0.833)

    # 好感阈值
    assert companion_config.favor_rules.high_threshold == 80
    assert companion_config.favor_rules.low_threshold == 30


def test_event_hooks_default_values_match_log():
    """默认钩子权重跟 E:\\study\\日志.txt 一致。"""
    from app.core.companion_config import companion_config

    h = companion_config.event_hooks
    # 任务成功: 心情 +8, 经验 +20, 好感 +1
    assert h.task_success.mood == 8
    assert h.task_success.exp == 20
    assert h.task_success.favor == 1
    # 任务失败: 心情 -12, 好感 -3
    assert h.task_failure.mood == -12
    assert h.task_failure.favor == -3
    # 用户拒绝: 心情 -10, 好感 -2
    assert h.user_rejected.mood == -10
    assert h.user_rejected.favor == -2
    # 用户采纳: 心情 +6, 好感 +3
    assert h.user_accepted.mood == 6
    assert h.user_accepted.favor == 3
    # 用户喂食: 饱食 +15, 心情 +5
    assert h.user_feed.satiety == 15
    assert h.user_feed.mood == 5
    # 用户互动: 心情 +3, 好感 +2
    assert h.user_interact.mood == 3
    assert h.user_interact.favor == 2


def test_event_hooks_types_are_dataclass():
    """event_hooks 子字段必须是 _HookDeltas 实例 (不是 dict)。"""
    from app.core.companion_config import companion_config, _HookDeltas

    for hook_name in ["task_success", "task_failure", "user_rejected",
                      "user_accepted", "user_feed", "user_interact"]:
        hook = getattr(companion_config.event_hooks, hook_name)
        assert isinstance(hook, _HookDeltas), \
            f"{hook_name} 应该是 _HookDeltas, 实际是 {type(hook).__name__}"


def test_frozen_dataclass_immutable():
    """CompanionConfig frozen, 不能外部改字段 (避免运行时漂移)。"""
    from app.core.companion_config import companion_config

    with pytest.raises(Exception):  # FrozenInstanceError
        companion_config.favor.high_threshold = 100  # type: ignore[misc]


# ---- 用户覆盖测试 ----

def test_user_yaml_overrides_default(monkeypatch, tmp_path):
    """data/companion_config.yaml 覆盖默认 yaml。"""
    user_yaml = _write_user_yaml(tmp_path, """
satiety:
  initial: 50
favor_rules:
  high_threshold: 90
event_hooks:
  task_success:
    mood: 100
""")
    _patch_user_path(monkeypatch, user_yaml)
    from app.core.companion_config import load_companion_config
    cfg = load_companion_config()

    assert cfg.satiety.initial == 50                # 覆盖
    assert cfg.mood.initial == 70                   # 走默认 (yaml 没写)
    assert cfg.favor_rules.high_threshold == 90     # 覆盖
    assert cfg.favor_rules.low_threshold == 30      # 走默认
    assert cfg.event_hooks.task_success.mood == 100 # 覆盖
    assert cfg.event_hooks.task_success.exp == 20   # 走默认


def test_user_yaml_partial_does_not_break(monkeypatch, tmp_path):
    """用户 yaml 字段缺失时不抛, 走默认。"""
    user_yaml = _write_user_yaml(tmp_path, """
satiety:
  initial: 100
""")
    _patch_user_path(monkeypatch, user_yaml)
    from app.core.companion_config import load_companion_config
    cfg = load_companion_config()

    assert cfg.satiety.initial == 100
    # 缺字段走默认
    assert cfg.event_hooks.task_success.mood == 8


def test_user_yaml_bad_field_uses_default(monkeypatch, tmp_path):
    """用户 yaml 字段名错时不抛, 走默认。"""
    user_yaml = _write_user_yaml(tmp_path, """
satiety:
  bogus_field: 999
event_hooks:
  task_success:
    bogus: 999
""")
    _patch_user_path(monkeypatch, user_yaml)
    from app.core.companion_config import load_companion_config
    cfg = load_companion_config()

    # 都走默认
    assert cfg.satiety.initial == 80
    assert cfg.event_hooks.task_success.mood == 8


# ---- 缺失文件兜底测试 ----

def test_no_yaml_files_uses_defaults(monkeypatch, tmp_path):
    """仓库 yaml + 用户 yaml 都没了 → 走内置 _DEFAULTS (不抛)。"""
    _patch_user_path(monkeypatch, tmp_path / "nonexistent.yaml")
    # 把默认 yaml 路径也指向不存在的路径 (临时覆盖)
    import app.core.companion_config as m
    monkeypatch.setattr(m, "_default_yaml_path", lambda: tmp_path / "default_missing.yaml")
    m._cached = None
    from app.core.companion_config import load_companion_config
    cfg = load_companion_config()

    # 全是兜底值
    assert cfg.satiety.initial == 80
    assert cfg.event_hooks.task_success.mood == 8


# ---- 范围工具方法 ----

def test_range_clamp_clamps_to_bounds():
    """_Range.clamp() 把数值钳到 [min, max]。"""
    from app.core.companion_config import _Range

    r = _Range(minimum=0, maximum=100, initial=50)
    assert r.clamp(-10) == 0
    assert r.clamp(50) == 50
    assert r.clamp(150) == 100
