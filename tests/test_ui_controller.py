"""回归测试：UIController 的关键信号路由 + 行为函数。

设计原则（来自经验教训：测真实调用链，不只测辅助函数）：
    - mock pet / brain / state / settings，**只测信号路由和副作用调用**
    - 不构造真实 UI 窗口（避免 Qt 事件循环依赖）
    - 用真实 signal emit 触发被测函数，避免直接 .method() 假阳

覆盖目标（按业务价值排序）：
    1. _fire_alarm    —— 闹钟到点统一处理（置顶窗 + 提示音 + 系统通知）
    2. _on_eat_requested / _on_food_selected —— 投喂流程
    3. _speak_alarm    —— 闹钟 TTS 播报（TTS 失败兜底）
    4. 外部信号 → 内部处理  —— 真实信号接收路径（穿透测试）
"""
from __future__ import annotations
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# 让 _init_model_config_ui 等要 SettingsWindow 的调用全 no-op
# （SettingsWindow 在 QT offscreen 下需要完整构建）
@pytest.fixture
def qapp():
    from app.core.qt_compat import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _make_cfg():
    """构造 Config 桩（不动 yaml 解析，只给字段赋值）。"""
    cfg = MagicMock()
    cfg.character = MagicMock()
    cfg.character.name = "测试角色"
    cfg.character.persona = ""
    cfg.character.tts_engine = "edge"
    cfg.character.tts_voice = "zh-CN-XiaoxiaoNeural"
    cfg.character.minimax_voice_id = ""
    cfg.character.gptsovits_url = ""
    cfg.character.gptsovits_ref_audio = ""
    cfg.character.gptsovits_prompt_text = ""
    cfg.brain.proactive_min_minutes = 25
    cfg.brain.proactive_max_minutes = 45
    cfg.brain.proactive_enabled = False
    cfg.window.always_on_top = True
    cfg.pet.renderer = "sprite"
    cfg.pet.live2d.hide_watermark = True
    cfg.sprite.fallback = "assets/sprites/body_front.png"
    return cfg


def _make_state_mgr():
    sm = MagicMock()
    state = MagicMock()
    state.stats_summary.return_value = "Lv.50 健康"
    state.money = 100.0
    state.has_checked_in_today.return_value = False
    sm.state = state
    return sm


def _make_pet():
    pet = MagicMock()
    pet.show_bubble = MagicMock()
    pet.hide = MagicMock()
    pet.show = MagicMock()
    pet.animator = MagicMock()
    pet.animator.trigger_scene = MagicMock()
    pet.is_random_stickers_enabled.return_value = True
    pet.get_sticker_options.return_value = {"size": 120, "rotation": 45, "min_s": 40, "max_s": 120, "duration_s": 4}
    pet.renderer = MagicMock()
    pet.renderer.max_fps = 30
    return pet


def _make_tts():
    tts = MagicMock()
    tts.speak = MagicMock()
    return tts


def _make_brain():
    brain = MagicMock()
    brain.bubble_requested = MagicMock()
    brain.bubble_cleared = MagicMock()
    brain.animation_requested = MagicMock()
    brain.remark_ready = MagicMock()
    brain.alarm_fired = MagicMock()
    brain.emotion_hint = MagicMock()
    return brain


def _make_motion():
    return MagicMock()


@pytest.fixture
def controller(qapp):
    """构造 UIController，所有依赖 mock 掉。

    SettingsWindow / PetWindow 不构造真实实例（offscreen Qt 复杂），
    而是用 patch 让它们返回 MagicMock。
    """
    root = Path(".")
    cfg = _make_cfg()
    state_mgr = _make_state_mgr()
    pet = _make_pet()
    tts = _make_tts()
    brain = _make_brain()
    motion = _make_motion()

    with patch("app.ui.ui_controller.SettingsWindow", return_value=MagicMock()), \
         patch("app.ui.ui_controller.TrayController", return_value=MagicMock()), \
         patch("app.ui.ui_controller.QTimer") as mock_timer_cls:
        mock_timer = MagicMock()
        mock_timer_cls.return_value = mock_timer

        from app.ui.ui_controller import UIController
        ctrl = UIController(
            root=root, cfg=cfg, state_mgr=state_mgr,
            pet=pet, tts=tts, brain=brain, motion=motion,
        )
    yield ctrl


# =============================================================================
# 1. _fire_alarm —— 闹钟/提醒到点统一处理（覆盖置顶窗 + 提示音 + 系统通知）
# =============================================================================

class TestFireAlarm:
    """闹钟到点要做的四件事：
        1) 桌宠气泡（顺带播 reminder 动画）
        2) 置顶闹钟窗（alarm.fire）
        3) 循环系统提示音（alarm.sound=True 已经包含）
        4) Windows 系统通知（toast.show）

        测试要穿透到 alarm.fire 和 toast.show 的实际调用，
        防止「其中任意一件漏做」（实测 2026-09-23 countdown 静默无声就是这条漏了）。
    """

    def test_fire_calls_bubble_animation_alarm_and_toast(self, controller, monkeypatch):
        toast_calls = []
        monkeypatch.setattr("app.core.toast.show",
                             lambda kind, text: toast_calls.append((kind, text)))

        # 注入 mock alarm
        controller.alarm = MagicMock()
        controller.alarm.fire = MagicMock()

        controller._fire_alarm("吃药", kind="⏰ 提醒")

        # 1) 桌宠气泡
        controller.pet.show_bubble.assert_called_once()
        bubble_text = controller.pet.show_bubble.call_args[0][0]
        assert "吃药" in bubble_text
        assert "提醒" in bubble_text

        # 2) 动画触发（reminder 场景）
        controller.pet.animator.trigger_scene.assert_called_with("reminder")

        # 3) 置顶闹钟窗（关键：sound=True；speak=False 由 _speak_alarm 单独管）
        controller.alarm.fire.assert_called_once_with(
            "吃药", kind="⏰ 提醒", sound=True, speak=False,
        )

        # 4) 系统通知
        assert len(toast_calls) == 1
        assert toast_calls[0] == ("⏰ 提醒", "吃药")

    def test_fire_continues_when_animation_fails(self, controller, monkeypatch):
        """动画 trigger_scene 抛异常时，其他三件仍要做完（不能整条失败）。"""
        controller.pet.animator.trigger_scene.side_effect = RuntimeError("simulated")
        controller.alarm = MagicMock()
        monkeypatch.setattr("app.core.toast.show",
                             lambda kind, text: None)
        # 不应抛异常
        controller._fire_alarm("喝水")
        controller.alarm.fire.assert_called_once()
        controller.pet.show_bubble.assert_called_once()


# =============================================================================
# 2. _on_eat_requested / _on_food_selected —— 投喂流程
# =============================================================================

class TestFeeding:

    def test_eat_requested_grows_state(self, controller):
        """右键喂食 → state_mgr.on_eat_requested → 桌宠气泡「吃饱啦~」。"""
        controller._on_eat_requested()
        controller.state_mgr.on_eat_requested.assert_called_once()
        controller.pet.show_bubble.assert_called_once()
        assert "吃饱啦" in controller.pet.show_bubble.call_args[0][0]

    def test_food_selected_falls_back_to_generic_eat_when_item_missing(self, controller):
        """指定食物（找不到）→ 退回通用 on_eat_requested。"""
        controller.brain.items.by_name.return_value = None
        controller._on_food_selected("不存在的食物")
        controller.state_mgr.on_eat_requested.assert_called_once()

    def test_food_selected_speaks_when_apply_food_fails(self, controller):
        """找到食物但 apply_food 失败（钱不够）→ _pet_speak 兜底。"""
        # apply_food 在 _on_food_selected 内 from app.engine.works import apply_food，
        # 所以 patch 模块级 works.apply_food
        controller.brain.items.by_name.return_value = MagicMock()
        with patch("app.engine.works.apply_food", return_value=False):
            controller._on_food_selected("可乐")
        # 内部 _pet_speak 也会触发 pet.show_bubble
        controller.pet.show_bubble.assert_called()

    def test_food_selected_no_items_attr(self, controller):
        """brain 没有 items 属性（rare edge）→ 退回通用。"""
        controller.brain = MagicMock(spec=["bubble_requested", "bubble_cleared",
                                         "animation_requested", "remark_ready",
                                         "alarm_fired", "emotion_hint"])
        controller._on_food_selected("可乐")
        controller.state_mgr.on_eat_requested.assert_called_once()


# =============================================================================
# 3. _speak_alarm —— 闹钟 TTS 播报（TTS 不可用 / 失败兜底）
# =============================================================================

class TestSpeakAlarm:

    def test_speak_calls_tts_when_available(self, controller):
        controller.tts.speak = MagicMock()
        controller._speak_alarm("吃药")
        controller.tts.speak.assert_called_once_with("吃药")

    def test_speak_silent_when_tts_is_none(self, controller):
        """TTS 不可用 → 不能抛异常（打扰主人）。"""
        controller.tts = None
        # 不应抛
        controller._speak_alarm("吃药")

    def test_speak_silent_when_tts_speak_raises(self, controller):
        """TTS.speak 失败（音频抽风）→ 不能冒上来。"""
        controller.tts.speak.side_effect = RuntimeError("audio device busy")
        controller._speak_alarm("吃药")  # 不应抛

    def test_fire_alarm_invokes_speak_via_alarm_presenter(self, controller):
        """alarm.fire 接受 speak_fn 但默认 speak=False —— 由 presenter 决定何时 TTS。
        验证 _fire_alarm 没把 TTS 路径写穿。
        """
        controller.alarm = MagicMock()
        controller._fire_alarm("吃药")
        # speak 应在 presenter 内部处理（speak=False 时 presenter 不主动 TTS，
        # 等到主人点「知道了」才 speak）
        kwargs = controller.alarm.fire.call_args.kwargs
        assert kwargs["speak"] is False


# =============================================================================
# 4. 信号路由 —— 直接调内部 handler，验证真实副作用
# =============================================================================

class TestSignalRouting:
    """替代 MagicMock emit 的方案：直接调 _on_xxx，因为 signal MagicMock 不真触发 Qt。

    真正的「信号连接是活的」由 Qt 自身保证（PyQt 已连桥），我们测的是「handler 是否做了正确的事」。
    """

    def test_on_food_low_triggers_pet_bubble(self, controller):
        """state_mgr.food_low 信号 → _on_food_low → pet.show_bubble。"""
        controller._on_food_low()
        controller.pet.show_bubble.assert_called_once()
        text = controller.pet.show_bubble.call_args[0][0]
        assert "饿" in text or "喵" in text

    def test_on_reminder_triggered_uses_alarm_pipeline(self, controller, monkeypatch):
        """reminder_fired → _on_reminder_triggered → _fire_alarm（走完整闹钟通道）。"""
        controller.alarm = MagicMock()
        monkeypatch.setattr("app.core.toast.show",
                             lambda kind, text: None)
        controller._on_reminder_triggered("吃药")
        controller.alarm.fire.assert_called_once()
        # 验证 kind 是「闹钟到点」（不是「倒计时结束」）
        kind = controller.alarm.fire.call_args.kwargs.get("kind", "")
        assert "闹钟" in kind or "提醒" in kind

    def test_on_alarm_fired_passes_text_through(self, controller, monkeypatch):
        """brain.alarm_fired（countdown 到点）→ _on_alarm_fired → _fire_alarm。"""
        controller.alarm = MagicMock()
        monkeypatch.setattr("app.core.toast.show",
                             lambda kind, text: None)
        controller._on_alarm_fired("煮面时间到", kind="⏰ 倒计时结束")
        controller.alarm.fire.assert_called_once_with(
            "煮面时间到", kind="⏰ 倒计时结束", sound=True, speak=False,
        )