"""SettingsWindow 开发者模式单元测试。"""
import pytest
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def settings_window(qapp, monkeypatch):
    """构造一个最小可用的 SettingsWindow (省略 chat_cfg / pet_window 这些可选 deps)."""
    from app.ui.settings_window import SettingsWindow
    # SettingsWindow 需要一些非空 cfg; 我们给最小 None char_cfg,
    # 让它走 _build_tab_visual 等分支需要的 None fallback.
    sw = SettingsWindow(parent=None, char_cfg=None,
                        renderer=None, sticker_enabled=False,
                        always_on_top=False, live2d_state=None)
    yield sw
    sw.close()
    sw.deleteLater()


def test_signals_exist(qapp):
    """developer_mode_changed + dev_state_pushed 信号应在 SettingsWindow 上。"""
    from app.ui.settings_window import SettingsWindow
    sw = SettingsWindow(parent=None, char_cfg=None, renderer=None,
                        sticker_enabled=False, always_on_top=False,
                        live2d_state=None)
    # PyQt Signal 不能用 hasattr, 用属性访问
    assert hasattr(SettingsWindow, "developer_mode_changed")
    assert hasattr(SettingsWindow, "dev_state_pushed")
    sw.close()
    sw.deleteLater()
    print("[OK] developer_mode_changed + dev_state_pushed signals exist")


def test_developer_mode_starts_disabled(settings_window):
    """默认 developer_mode = False (关)."""
    assert settings_window.developer_mode is False
    print("[OK] SettingsWindow.developer_mode defaults False")


def test_chk_developer_mode_checkbox_exists(settings_window):
    """控制 Tab 应有 chk_developer_mode 控件, 默认 uncheck + 初始 False。"""
    sw = settings_window
    assert hasattr(sw, "chk_developer_mode")
    assert sw.chk_developer_mode.isChecked() is False
    print("[OK] chk_developer_mode present + unchecked")


def test_setting_developer_mode_true_enables_dev_panel(settings_window):
    """_set_developer_mode(True) 显示 _dev_panel + 调用 preview + emit。"""
    sw = settings_window
    # 听 emit dev_state_pushed
    captured = {}
    sw.dev_state_pushed.connect(
        lambda snap: captured.setdefault("snap", snap)
    )
    sw._set_developer_mode(True)
    assert sw.developer_mode is True
    assert hasattr(sw, "_dev_panel")
    assert sw._dev_panel.isVisibleTo(sw), (
        "dev_panel should be visible after _set_developer_mode(True)"
    )
    # dev_state_pushed 应 emit 一次 (默认 snapshot)
    assert "snap" in captured, (
        "_set_developer_mode(True) 应调 _emit_dev_state"
    )
    snap = captured["snap"]
    for key in ("strength", "strength_food", "strength_drink",
                "feeling", "health", "likability", "level"):
        assert key in snap, f"dev_state snapshot 应包含 {key}"
    print(f"[OK] _set_developer_mode(True) shows panel + emits (snap keys={list(snap.keys())})")


def test_setting_developer_mode_false_hides_dev_panel(settings_window):
    """_set_developer_mode(False) 隐藏 _dev_panel。"""
    sw = settings_window
    sw._set_developer_mode(True)
    assert sw._dev_panel.isVisibleTo(sw)
    sw._set_developer_mode(False)
    assert sw.developer_mode is False
    assert not sw._dev_panel.isVisibleTo(sw), (
        "_set_developer_mode(False) 应隐藏 _dev_panel"
    )
    print("[OK] _set_developer_mode(False) hides panel")


def test_dev_preview_shows_quantize_and_decisions(settings_window):
    """_refresh_dev_preview 应更新量化标签 + 决策预览。"""
    sw = settings_window
    sw._set_developer_mode(True)
    # 模拟 HIGH/HIGH/HIGH
    sliders = sw._dev_sliders
    sliders["feeling"].setValue(90)
    sliders["strength_food"].setValue(90)
    sliders["level"].setValue(18)
    # 触发 preview (slider 已自动 emit + refresh, 这里再 refresh 一次稳)
    sw._refresh_dev_preview()
    text_quant = sw.lbl_dev_quantize.text()
    text_dec = sw.lbl_dev_decisions.text()
    assert "HIGH" in text_quant, (
        f"应有 HIGH 量化. 实际: {text_quant}"
    )
    assert "lock_screen" in text_dec, (
        f"决策预览应含常见工具 lock_screen. 实际: {text_dec}"
    )
    assert "→" in text_dec, (
        f"决策预览应有 → 箭头. 实际: {text_dec}"
    )
    print(f"[OK] _refresh_dev_preview 量化='{text_quant}', 决策预览含 ... lock_screen")


def test_preset_button_changes_sliders_and_emits(settings_window):
    """预设按钮 (满状态 / 低状态 / 中状态 等) 改 sliders + emit。"""
    sw = settings_window
    sw._set_developer_mode(True)
    captured = []
    sw.dev_state_pushed.connect(lambda snap: captured.append(snap))
    # 模拟点击 "满状态" 预设 (button index = 0)
    # 找按钮文字匹配
    from app.ui.settings_window import QPushButton
    btn = None
    for child in sw.findChildren(QPushButton):
        if child.text() == "满状态":
            btn = child
            break
    assert btn is not None, "找不到 '满状态' 预设按钮"
    btn.click()
    assert len(captured) >= 1, "预设按钮应 emit dev_state_pushed"
    snap = captured[-1]
    assert snap["feeling"] == 90
    assert snap["strength_food"] == 90
    assert snap["level"] == 18
    # slider value 同步
    assert sw._dev_sliders["feeling"].value() == 90
    assert sw._dev_sliders["strength_food"].value() == 90
    assert sw._dev_sliders["level"].value() == 18
    print(f"[OK] 满状态预设: sliders={90}/{90}/18 + emit OK")


def test_slider_drag_emits_state_with_snapshot(settings_window):
    """直接改 slider value (模拟拖动) → emit dev_state_pushed。"""
    sw = settings_window
    sw._set_developer_mode(True)
    captured = []
    sw.dev_state_pushed.connect(lambda snap: captured.append(snap))
    sw._dev_sliders["feeling"].setValue(50)
    sw._dev_sliders["strength_food"].setValue(50)
    sw._dev_sliders["level"].setValue(8)
    assert len(captured) >= 3
    last = captured[-1]
    assert last["feeling"] == 50
    assert last["strength_food"] == 50
    assert last["level"] == 8
    print("[OK] slider 拖动 emit snapshot")


def test_settings_update_state_view_keeps_dev_panel_in_sync(settings_window):
    """_update_state_view(state) 应同步 progress bar + (开发者模式下) sliders。"""
    sw = settings_window
    sw._set_developer_mode(True)
    from app.engine.state import PetState
    ps = PetState()
    ps.feeling = 70
    ps.strength_food = 60
    ps.level = 5
    sw._update_state_view(ps)
    # sliders 跟 state 同步
    assert sw._dev_sliders["feeling"].value() == 70
    assert sw._dev_sliders["strength_food"].value() == 60
    assert sw._dev_sliders["level"].value() == 5
    # value labels 同步
    assert sw._dev_value_labels["feeling"].text() == "70"
    assert sw._dev_value_labels["strength_food"].text() == "60"
    print("[OK] _update_state_view 同步 sliders + value labels")


def test_v226_strategy_preview_panel_exists(settings_window):
    """dev_panel 应有 strategy 预览 label。"""
    sw = settings_window
    sw._set_developer_mode(True)
    assert hasattr(sw, "lbl_dev_strategy"), (
        "v22-6 应加 lbl_dev_strategy label"
    )
    print("[OK] v22-6: lbl_dev_strategy 控件存在")


def test_v226_strategy_preview_updates_with_sliders(settings_window):
    """拖 slider 时策略预览应跟上 (跟 _strategy_guidance 输出一致)。"""
    sw = settings_window
    sw._set_developer_mode(True)
    # HIGH sat+mid mood → "状态好" 探索段
    sw._dev_sliders["feeling"].setValue(85)
    sw._dev_sliders["strength_food"].setValue(85)
    sw._dev_sliders["level"].setValue(10)
    txt_high = sw.lbl_dev_strategy.text()
    assert "策略预览" in txt_high, f"应有策略预览头. 实际: {txt_high}"
    assert "状态好" in txt_high or "主动建议" in txt_high, (
        f"HIGH sat+mood 应有探索段. 实际: {txt_high}"
    )

    # LOW sat+low mood → 保守段
    sw._dev_sliders["feeling"].setValue(20)
    sw._dev_sliders["strength_food"].setValue(20)
    sw._dev_sliders["level"].setValue(2)
    txt_low = sw.lbl_dev_strategy.text()
    assert "情绪低落" in txt_low or "简短" in txt_low, (
        f"LOW mood 应有保守段. 实际: {txt_low}"
    )

    # 平稳态 (MID sat + MID mood) → (无 — 状态平稳无策略注入)
    sw._dev_sliders["feeling"].setValue(50)
    sw._dev_sliders["strength_food"].setValue(50)
    sw._dev_sliders["level"].setValue(8)


# ---- trace 页 3 段 UI ----

def test_v237_trace_panel_exists(settings_window):
    """dev_panel 应有 trace 视图 (调前/调后 2 段色块)。"""
    sw = settings_window
    sw._set_developer_mode(True)
    assert hasattr(sw, "_dev_trace_view"), "v23-7 应有 _dev_trace_view"
    assert hasattr(sw, "_dev_trace_scroll"), "v23-7 应有 _dev_trace_scroll (QScrollArea)"
    print("[OK] v23-7: trace 视图控件存在")


def test_v237_trace_panel_renders_decision_and_outcome(tmp_path, monkeypatch, settings_window):
    """trace 视图从 companion_log.txt 读 + 渲染 decision/outcome 块。"""
    import json
    from app.brain._companion_log import trace_decision, trace_outcome

    # 重定向 trace 路径到 tmp
    log_path = tmp_path / "trace.log"
    monkeypatch.setattr(
        "app.brain._companion_log._default_log_path",
        lambda: log_path,
    )

    # 写一条 decision + 一条 outcome
    trace_decision(
        thread_id="t", tool="set_brightness", risk=2, decision="REFUSE",
        sat="LOW", mood="LOW", level="LOW",
    )
    trace_outcome(
        thread_id="t", outcome="success", tool="set_brightness",
        feeling_delta=8.0, food_delta=0.0, favor_delta=1.0, exp_delta=20.0,
        level_before=1, level_after=2, success=True,
    )

    sw = settings_window
    sw._set_developer_mode(True)
    sw._refresh_dev_trace()

    html = sw._dev_trace_view.text()
    assert "set_brightness" in html, f"trace 应有工具名. 实际: {html}"
    assert "REFUSE" in html, f"decision 应展示. 实际: {html}"
    # outcome 块绿边框
    assert "#3a8c4d" in html, f"outcome 应有绿边框. 实际: {html}"
    # decision 块蓝边框
    assert "#2e6da4" in html, f"decision 应有蓝边框. 实际: {html}"
    print("[OK] v23-7: trace 渲染 decision + outcome 块")
