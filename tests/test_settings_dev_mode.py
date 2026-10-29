# -*- coding: utf-8 -*-
"""开发者模式：手改桌宠状态验决策。

设置面板删了之后，这条能力搬到了控制台 —— 控制台的「设置」Tab 能改
开发者模式开关，主程序收到 `dev_state` 动作就改 PetState。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ── 开发者模式开关 ─────────────────────────────────────────────────────────
def test_developer_mode_is_a_setting():
    """开发者模式是一个设置项（控制台能勾）。"""
    from app.core.pet_settings import SETTING_SCHEMA
    spec = SETTING_SCHEMA.get("developer_mode")
    assert spec is not None, "开发者模式不在设置项清单里"
    assert spec["type"] == "bool"
    assert spec["group"] == "行为"


def test_developer_mode_persists(tmp_path):
    """勾了要能保持（重启别又关掉）。"""
    from app.core.pet_settings import SettingsService
    from app.core.settings_store import SettingsStore
    store = SettingsStore(tmp_path / "settings.json")
    svc = SettingsService(store)
    svc.update({"developer_mode": True})
    assert store.get("developer_mode") is True


def test_developer_mode_change_is_applied(tmp_path):
    """改了设置 → 主程序要知道（开发者模式是主程序的运行时状态）。"""
    from app.core.pet_settings import SettingsService
    from app.core.settings_store import SettingsStore
    store = SettingsStore(tmp_path / "settings.json")
    svc = SettingsService(store)
    applied = []
    svc.apply_hook = lambda values: applied.append(dict(values))
    svc.update({"developer_mode": True})
    assert applied == [{"developer_mode": True}]


# ── 手改状态（dev_state 动作） ───────────────────────────────────────────
def _ctrl_with_state():
    """一个够用 UIController：只借 _on_dev_state_pushed 与 _control_action。"""
    from app.ui.ui_controller import UIController

    class _Ctrl(UIController):
        def __init__(self):  # noqa: D107 - 只借方法，不跑 Qt 初始化
            self.developer_mode = False
            self.state = type("_S", (), {
                "on_change": None,
                "normalize": lambda self: None,
                "level": 1,
                "strength": 100.0,
                "strength_food": 100.0,
                "strength_drink": 100.0,
                "feeling": 80.0,
                "health": 100.0,
                "likability": 0.0,
            })()
            self.state_mgr = None
            self.brain = None
    return _Ctrl()


def test_dev_state_action_reaches_the_state():
    """控制台推一组状态数值 → PetState 真被改了。"""
    ctrl = _ctrl_with_state()
    result = ctrl._control_action("dev_state", {
        "snapshot": {"feeling": 95, "strength_food": 30, "level": 12}})
    assert result["ok"] is True
    assert ctrl.state.feeling == 95
    assert ctrl.state.strength_food == 30
    assert ctrl.state.level == 12


def test_dev_state_action_without_snapshot_fails():
    """没给快照要报错（别静默成功）。"""
    ctrl = _ctrl_with_state()
    result = ctrl._control_action("dev_state", {})
    assert result["ok"] is False
    assert "状态快照" in result["error"]


def test_unknown_action_is_reported():
    """不认识的要说清楚，别装成功。"""
    ctrl = _ctrl_with_state()
    result = ctrl._control_action("没有这个动作", {})
    assert result["ok"] is False
    assert "未知动作" in result["error"]


def test_dashboard_exposes_dev_state_action():
    """控制台的设置 Tab 要能发运行时动作（dev_state 走这条通道）。"""
    src = (ROOT / "app" / "web" / "dashboard.py").read_text(encoding="utf-8")
    assert "/api/settings/action" in src
    # 动作名由前端传，主程序那边认得 dev_state
    assert "action" in src
    ctrl_src = (ROOT / "app" / "ui" / "ui_controller.py").read_text(encoding="utf-8")
    assert "dev_state" in ctrl_src, "主程序不认 dev_state 动作"
