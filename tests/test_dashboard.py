"""v39: settings_window 顶部心情仪表盘, 验证 _build_dashboard 跟 update_dashboard 联动.

- dashboard 包含 pet_dashboard / dash_name / dash_mood_chip / dash_mood_block / dash_stats / dash_level
- update_dashboard 把 state 量化映射到 UI:
    - mood HIGH -> 樱粉 + 心情好
    - mood MID  -> 蜜桃 + 心情一般
    - mood LOW  -> 雾蓝 + 心情低落
- dev mode 滑块 (state.on_change) 触发 update_dashboard, 4 维数值实时刷新
- 没 state -> 显示 - (兜底)
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from app.engine.state import PetState
from app.ui import ui_style
from app.ui.settings_window import SettingsWindow
from app.core.qt_compat import (
    QApplication, QLabel, QFrame, QProgressBar, Qt,
)


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _make_state(feeling=80, food=100, favor=0, level=1, exp=0):
    s = PetState()
    s.feeling = feeling
    s.strength_food = food
    s.likability = favor
    s.level = level
    s.exp = exp
    s.normalize()
    return s


def test_dashboard_widgets_exist(qapp):
    """dashboard 含 4 段 7+ 个 widget (跟设计一致)."""
    from app.core.config import LLMConfig, CharacterConfig
    sw = SettingsWindow(parent=None, char_cfg=CharacterConfig(name="\u5c0f\u6c50"))
    dash = sw.dashboard
    assert dash.objectName() == "pet_dashboard"
    assert isinstance(sw.dash_name, QLabel)
    assert isinstance(sw.dash_mood_chip, QLabel)
    assert isinstance(sw.dash_mood_block, QFrame)
    assert isinstance(sw.dash_level, QLabel)
    assert isinstance(sw.dash_exp, QProgressBar)
    # 4 维状态条
    assert set(sw.dash_stats.keys()) == {"feeling", "strength_food", "likability", "strength"}
    for k, (lbl, val) in sw.dash_stats.items():
        assert isinstance(lbl, QLabel)
        assert isinstance(val, QLabel)
    print("[OK] v39: dashboard 含 7+ widget (name/mood/chip/block/level/exp/4 stat)")


def test_dashboard_default_no_state(qapp):
    """没 state 时, 4 维 + 心情 + 等级都显示 -. update_dashboard 跑不出错."""
    from app.core.config import LLMConfig, CharacterConfig
    sw = SettingsWindow(parent=None, char_cfg=CharacterConfig(name="\u5c0f\u6c50"))
    sw.update_dashboard()
    for k, (lbl, val) in sw.dash_stats.items():
        # 用 em-dash 兜底 (跟 PetState 状态条 0 时的显示风格一致)
        assert val.text() in ("-", "\u2014"), (
            f"v39: 无 state 时 {k} 应显示 -, 实际 {val.text()!r}"
        )
    assert sw.dash_mood_chip.text() in ("-", "\u2014")
    assert sw.dash_level.text() in ("-", "\u2014")
    print("[OK] v39: 无 state 兜底, 4 维 + 心情 + 等级全 -")


def test_dashboard_mood_high_renders_cherry_pink(qapp):
    """心情 HIGH (feeling=80) -> 心情好 + 樱粉."""
    from app.core.config import LLMConfig, CharacterConfig
    sw = SettingsWindow(parent=None, char_cfg=CharacterConfig(name="\u5c0f\u6c50"))
    state = _make_state(feeling=80)
    sw.attach_state(state)
    sw.update_dashboard()
    assert sw.dash_mood_chip.text() == "心情好", (
        f"v39: mood HIGH 应显示'心情好', 实际 {sw.dash_mood_chip.text()!r}"
    )
    style = sw.dash_mood_block.styleSheet()
    assert ui_style.MOOD_HIGH in style, (
        f"v39: 心情色块背景应含 MOOD_HIGH 樱粉, 实际 style: {style!r}"
    )
    print("[OK] v39: mood HIGH -> '心情好' + 樱粉 (#E89AAE)")


def test_dashboard_mood_low_renders_misty_blue(qapp):
    """心情 LOW (feeling=0) -> 心情低落 + 雾蓝."""
    from app.core.config import LLMConfig, CharacterConfig
    sw = SettingsWindow(parent=None, char_cfg=CharacterConfig(name="\u5c0f\u6c50"))
    state = _make_state(feeling=0)
    sw.attach_state(state)
    sw.update_dashboard()
    assert sw.dash_mood_chip.text() == "心情低落", (
        f"v39: mood LOW 应显示'心情低落', 实际 {sw.dash_mood_chip.text()!r}"
    )
    style = sw.dash_mood_block.styleSheet()
    assert ui_style.MOOD_LOW in style, (
        f"v39: 心情色块背景应含 MOOD_LOW 雾蓝, 实际 style: {style!r}"
    )
    print("[OK] v39: mood LOW -> '心情低落' + 雾蓝 (#B8C5D6)")


def test_dashboard_4stat_values_match_state(qapp):
    """4 维状态数值跟 state 字段一致."""
    from app.core.config import LLMConfig, CharacterConfig
    sw = SettingsWindow(parent=None, char_cfg=CharacterConfig(name="\u5c0f\u6c50"))
    state = _make_state(feeling=42, food=58, favor=77, level=3, exp=33)
    sw.attach_state(state)
    sw.update_dashboard()
    assert sw.dash_stats["feeling"][1].text() == "42"
    assert sw.dash_stats["strength_food"][1].text() == "58"
    assert sw.dash_stats["likability"][1].text() == "77"
    assert sw.dash_stats["strength"][1].text() == "100"  # PetState 默认 strength=100
    assert sw.dash_level.text() == "3"
    assert sw.dash_exp.value() == 33
    print("[OK] v39: 4 维 + 等级 + 经验 跟 state 字段一致")


def test_dashboard_follows_state_on_change(qapp):
    """state.on_change 触发 update_dashboard, dev mode 滑块 / 喂食 后实时刷新."""
    from app.core.config import LLMConfig, CharacterConfig
    sw = SettingsWindow(parent=None, char_cfg=CharacterConfig(name="\u5c0f\u6c50"))
    state = _make_state(feeling=80)  # HIGH
    sw.attach_state(state)
    sw.update_dashboard()
    assert sw.dash_mood_chip.text() == "心情好"
    # 改 state, 触发 on_change
    state.feeling = 0
    state.normalize()
    if state.on_change:
        state.on_change()
    assert sw.dash_mood_chip.text() == "心情低落", (
        f"v39: state.on_change 后心情应变低落, 实际 {sw.dash_mood_chip.text()!r}"
    )
    print("[OK] v39: state.on_change 触发 update_dashboard, 心情实时变")
