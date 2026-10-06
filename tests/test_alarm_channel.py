"""倒计时 / 闹钟到点的提醒通道回归测试。

背景：原先 `add_reminder` 到点只弹一个桌宠气泡，`countdown` 到点连气泡都可能
没有（set_say_hook 从没被主程序注入）——主人设了提醒却常常「好像没响」。
现在到点会走统一闹钟通道：置顶窗 + 循环提示音 + TTS 播报 + 系统通知。

这些测试锁住「到点一定会惊动人」这件事，以及各条通道的接线。
"""
from __future__ import annotations

import time

import pytest

from app.engine.tools import _timer
from app.engine.tools._core import Tool, ToolRegistry
from app.ui.alarm_window import AlarmPresenter, AlarmWindow, SNOOZE_MS
from app.voice import alarm_sound


# =============================================================================
# 1) 提示音
# =============================================================================

def test_alarm_sound_module_usable():
    """winsound 在 Windows 上是标准库，必须可用。"""
    assert alarm_sound.available() is True, "winsound 不可用则到点无声"


def test_alarm_sound_start_stop_is_idempotent():
    snd = alarm_sound.AlarmSound(loops=1)
    try:
        assert snd.start() is True
        # 重复 start 不应叠出第二个线程
        assert snd.start() is True
        assert snd.is_playing() is True
    finally:
        snd.stop()
    snd.stop()          # 幂等
    assert snd.is_playing() is False


def test_new_sound_supersedes_old():
    """两个闹钟同时响只是噪音，新的应顶掉旧的。"""
    a = alarm_sound.AlarmSound(loops=1)
    b = alarm_sound.AlarmSound(loops=1)
    try:
        a.start()
        b.start()
        time.sleep(0.1)
        assert a.is_playing() is False, "旧提示音应被顶掉"
    finally:
        a.stop()
        b.stop()
        alarm_sound.stop_all()


def test_stop_all_silences_everything():
    a = alarm_sound.AlarmSound(loops=5)
    a.start()
    alarm_sound.stop_all()
    assert a.is_playing() is False


# =============================================================================
# 2) 闹钟窗
# =============================================================================

def test_alarm_window_is_always_on_top(qapp):
    from app.core.qt_compat import Qt
    w = AlarmWindow("该起来活动啦", "⏰ 倒计时结束")
    try:
        flags = w.windowFlags()
        assert flags & Qt.WindowType.WindowStaysOnTopHint, "必须置顶"
        assert flags & Qt.WindowType.FramelessWindowHint, "做成无边框卡片"
    finally:
        w.close()


def test_alarm_window_show_and_dismiss(qapp):
    w = AlarmWindow("喝水", "⏰ 闹钟到点")
    fired = []
    w.dismissed.connect(lambda: fired.append("dismiss"))
    try:
        w.show_alarm("喝水", "⏰ 闹钟到点", sound=False)
        assert w.isVisible() is True
        w._on_ok()
        assert fired == ["dismiss"]
        assert w.isVisible() is False
    finally:
        w.close()


def test_alarm_window_snooze_emits_ms(qapp):
    w = AlarmWindow("休息一下", "⏰ 倒计时结束")
    got = []
    w.snoozed.connect(lambda ms: got.append(ms))
    try:
        w.show_alarm("休息一下", sound=False)
        w._on_snooze()
        assert got == [SNOOZE_MS]
    finally:
        w.close()


def test_alarm_window_close_stops_sound(qapp):
    """关掉窗口必须把提示音也停掉，否则进程退出后还在响。"""
    w = AlarmWindow("x", "y")
    w.show_alarm("x", "y", sound=True)
    w.close()
    assert w._sound.is_playing() is False


def test_alarm_window_keeps_rering_until_closed(qapp):
    """没确认就周期性再响——只响一次主人很容易刚好错过。

    这里用手动触发 _on_rering 代替真等 30 秒。
    """
    started = []
    w = AlarmWindow("别错过我", "⏰ 倒计时结束")
    w._sound.start = lambda: started.append(1)   # type: ignore[assignment]
    try:
        w.show_alarm("别错过我", sound=True)
        assert len(started) == 1, "刚弹出时应先响一轮"
        assert w._rering.isActive() is True, "窗开着就该周期性再响"
        w._on_rering()
        w._on_rering()
        assert len(started) == 3
    finally:
        w.close()
    assert w._rering.isActive() is False, "关掉后不该再响"


# =============================================================================
# 3) AlarmPresenter：置顶窗 + 播报
# =============================================================================

def test_presenter_shows_window_and_speaks(qapp):
    spoken = []
    p = AlarmPresenter(speak_fn=spoken.append)
    try:
        p.fire("30分钟到了", kind="⏰ 倒计时结束", sound=False, speak=True)
        assert p.is_showing() is True
        assert p.window.isVisible() is True
        assert spoken and "30分钟到了" in spoken[0]
    finally:
        p.shutdown()
    assert p.is_showing() is False


def test_presenter_empty_text_gets_default(qapp):
    spoken = []
    p = AlarmPresenter(speak_fn=spoken.append)
    try:
        p.fire("   ", sound=False, speak=True)
        assert "时间到啦" in spoken[0]
    finally:
        p.shutdown()


def test_presenter_survives_broken_speak_fn(qapp):
    """TTS 挂了不能连带把弹窗也带走。"""

    def boom(_):
        raise RuntimeError("TTS 炸了")

    p = AlarmPresenter(speak_fn=boom)
    try:
        p.fire("到点了", sound=False, speak=True)   # 不应抛
        assert p.is_showing() is True
    finally:
        p.shutdown()


def test_snooze_really_refires(qapp):
    """回归：「稍后提醒」不能只是个装饰按钮。

    信号发出去没人收 = 主人点了稍后提醒，然后什么都不会发生——
    正是这次一直在修的那类「假装成功」。
    """
    fired = []
    p = AlarmPresenter(speak_fn=fired.append)
    try:
        # speak=True，这样初次到点 + 稍后重响各播报一次，可计数
        p.fire("起来走走", kind="⏰ 倒计时结束", sound=False, speak=True)
        assert len(fired) == 1
        assert p.window is not None
        p._on_snooze(1200)          # 1.2 秒后再响（真按钮是 5 分钟）
        assert p.has_pending_snooze() is True, "稍后提醒应排上重响计划"
        assert _wait_for(lambda: len(fired) >= 2, timeout=6), (
            f"稍后提醒没有真的再响：{fired}")
        assert "起来走走" in fired[-1]
    finally:
        p.shutdown()


def test_new_alarm_cancels_pending_snooze(qapp):
    """新的到点应顶掉旧的稍后提醒计划，别攒成连环提醒。"""
    p = AlarmPresenter(speak_fn=lambda t: None)
    try:
        p.fire("A", sound=False, speak=False)
        p._on_snooze(60000)
        assert p.has_pending_snooze() is True
        p.fire("B", sound=False, speak=False)
        assert p.has_pending_snooze() is False, "新到点应取消旧重响计划"
    finally:
        p.shutdown()


def test_dismiss_cancels_pending_snooze(qapp):
    p = AlarmPresenter(speak_fn=lambda t: None)
    try:
        p.fire("C", sound=False, speak=False)
        p._on_snooze(60000)
        p.dismiss()
        assert p.has_pending_snooze() is False
    finally:
        p.shutdown()


# =============================================================================
# 4) countdown 到点真的走闹钟通道
# =============================================================================

@pytest.fixture
def clean_hooks():
    """每个用例前后清干净全局 hook 和倒计时线程（它们是模块级状态）。"""
    _timer.set_say_hook(None)
    _timer.set_alarm_hook(None)
    _timer.cancel_all()
    yield
    _timer.set_say_hook(None)
    _timer.set_alarm_hook(None)
    _timer.cancel_all()      # cancel_countdown 会 join 线程，不留残余


def _wait_for(pred, timeout=5.0):
    """轮询等待；同时泵 Qt 事件循环，否则 QTimer 永远不会触发。"""
    from app.core.qt_compat import QApplication
    app = QApplication.instance()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if app is not None:
            app.processEvents()
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_countdown_has_notify_channel_when_alarm_hook_wired(clean_hooks):
    """接了闹钟 hook 后，countdown 必须敢说「到点会通知你」。"""
    _timer.set_alarm_hook(lambda text: None)
    assert _timer._has_notify_channel() is True
    out = _timer.countdown(seconds=1, message="t")
    assert "没有接入任何提醒通道" not in out, out
    assert "到点主人会收到提醒" in out, out


def test_countdown_honest_when_no_channel(clean_hooks):
    _timer.set_say_hook(None)
    _timer.set_alarm_hook(None)
    assert _timer._has_notify_channel() is False
    out = _timer.countdown(seconds=1, message="t")
    assert "没有接入任何提醒通道" in out, out
    assert "到点主人会收到提醒" not in out, out


def test_countdown_fires_alarm_hook(clean_hooks):
    """到点必须调 alarm hook（弹窗+响铃+播报），而不是只冒个气泡。"""
    alarms, bubbles = [], []
    _timer.set_alarm_hook(alarms.append)
    _timer.set_say_hook(bubbles.append)
    _timer.countdown(seconds=0.1, message="起来走走")
    assert _wait_for(lambda: alarms), f"alarm hook 未被调用 {alarms}"
    assert alarms and "起来走走" in alarms[0]
    assert bubbles == [], "有闹钟通道时不该再冒普通气泡"


def test_countdown_falls_back_to_say_hook(clean_hooks):
    bubbles = []
    _timer.set_say_hook(bubbles.append)
    _timer.countdown(seconds=0.1, message="只有气泡")
    assert _wait_for(lambda: bubbles), "无 alarm hook 时应退回 say hook"


def test_alarm_hook_exception_does_not_swallow_countdown(clean_hooks):
    def boom(_):
        raise RuntimeError("UI 通道炸了")

    _timer.set_alarm_hook(boom)
    _timer.countdown(seconds=0.1, message="x")
    # 线程里已 try 住，进程不该死；到点条目应已从活跃列表移除
    assert _wait_for(
        lambda: not [c for c in _timer._counters if c[2] == "x"]), \
        "到点后条目应被清理"


# =============================================================================
# 5) 工具注册与接线
# =============================================================================

def test_build_default_tools_wires_alarm_hook():
    """build_default_tools 必须把 alarm hook 接到 countdown 上。

    这正是 2026-10-04 审计里「set_say_hook 全仓只有 tests 调用过」那类
    接线缺失的回归防线——主程序不注入，倒计时就永远是哑的。
    """
    from app.engine.tools import build_default_tools

    class _S:
        def __getattr__(self, i):
            return lambda *a, **k: ""

    seen = []
    build_default_tools(state=_S(), reminders=_S(), memory=_S(), items=_S(),
                        hooks={"bubble": lambda t: None,
                               "animation": lambda n: None,
                               "hide_bubble": lambda: None,
                               "alarm": seen.append})
    assert callable(_timer._global_alarm_hook), "alarm hook 未接线"
    _timer.set_alarm_hook(None)   # 别污染别的用例


def test_countdown_tool_still_registered():
    from app.engine.tools import _timer as t
    reg = ToolRegistry()
    t.register(reg)
    names = set(reg.names())
    assert {"countdown", "list_countdowns", "cancel_countdown"} <= names
    schema = reg.get("countdown").parameters
    assert schema["required"] == ["seconds"]
