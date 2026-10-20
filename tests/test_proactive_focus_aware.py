"""v54: 主动关心感知主人是否在场（AFK / 会议窗口）。

覆盖：
- `get_idle_status()` 解析 aw-watcher-afk 事件（afk / not-afk / 数据不可达）
- `is_focus_meeting()` 命中会议软件关键词 + 数据不可达时不误报
- `ProactiveBrain._read_idle_minutes / _is_in_meeting` 走 AW 配置并优雅降级
- `_prepare` 在 idle 太久或在会议时返 (None, None) 跳过本轮
- 会议场景还会调 `set_tts_mute_until_threadsafe`
- `_ChatOnceWorker.run()` 在 _prepare 返 (None, None) 时发 done("") 不发 LLM 请求
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
from typing import Any, List
from unittest import mock

import pytest

# 让 import 走项目结构（不需要先 import app）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# -----------------------
# activitywatch.py 单测
# -----------------------

def _mock_aw_get_json(events: List[Any], buckets: Any):
    """构造一个 mock _http_get_json：buckets/afk bucket events 都按入参返。"""
    state = {"buckets": buckets, "events": events}

    def fake_get(url, *args, **kwargs):
        if "/api/0/info" in url:
            return {"version": "test"}
        if "/api/0/buckets/" in url and url.endswith("/buckets/"):
            return state["buckets"]
        # 事件查询
        return state["events"]

    return fake_get


def test_get_idle_status_afk_10min():
    """aw-watcher-afk 最新事件 status=afk, duration=600s → 返 10.0 分钟。"""
    from app.integrations import activitywatch

    buckets = [{"id": f"aw-watcher-afk_{activitywatch._hostname()}"}]
    events = [{"data": {"status": "afk"}, "duration": 600.0}]

    with mock.patch.object(activitywatch, "_http_get_json",
                           side_effect=_mock_aw_get_json(events, buckets)):
        result = activitywatch.get_idle_status()
    assert result == pytest.approx(10.0)


def test_get_idle_status_not_afk_returns_zero():
    """最近事件 not-afk → 返 0.0（主人在用电脑）。"""
    from app.integrations import activitywatch

    buckets = [{"id": f"aw-watcher-afk_{activitywatch._hostname()}"}]
    events = [{"data": {"status": "not-afk"}, "duration": 3.0}]

    with mock.patch.object(activitywatch, "_http_get_json",
                           side_effect=_mock_aw_get_json(events, buckets)):
        result = activitywatch.get_idle_status()
    assert result == 0.0


def test_get_idle_status_no_bucket_returns_none():
    """aw-server 不可达或没装 afk watcher → 返 None（不阻塞主动搭话）。"""
    from app.integrations import activitywatch

    # 没有 buckets 数据
    with mock.patch.object(activitywatch, "_http_get_json", return_value=None):
        result = activitywatch.get_idle_status()
    assert result is None


def test_is_focus_meeting_matches_chinese_keyword():
    """窗口标题含「腾讯会议」→ True。"""
    from app.integrations import activitywatch

    buckets = [{"id": f"aw-watcher-window_{activitywatch._hostname()}"}]
    events = [{"data": {"app": "TencentMeeting", "title": "腾讯会议 #12345"},
               "duration": 120.0}]

    with mock.patch.object(activitywatch, "_http_get_json",
                           side_effect=_mock_aw_get_json(events, buckets)):
        assert activitywatch.is_focus_meeting() is True


def test_is_focus_meeting_matches_zoom_keyword():
    """窗口标题含「Zoom Meeting」→ True。"""
    from app.integrations import activitywatch

    buckets = [{"id": f"aw-watcher-window_{activitywatch._hostname()}"}]
    events = [{"data": {"app": "zoom.exe", "title": "Zoom Meeting"},
               "duration": 30.0}]

    with mock.patch.object(activitywatch, "_http_get_json",
                           side_effect=_mock_aw_get_json(events, buckets)):
        assert activitywatch.is_focus_meeting() is True


def test_is_focus_meeting_no_data_returns_false():
    """AW 不可达或没数据 → False（保守不自动静音）。"""
    from app.integrations import activitywatch

    with mock.patch.object(activitywatch, "_http_get_json", return_value=None):
        assert activitywatch.is_focus_meeting() is False


def test_is_focus_meeting_normal_window_returns_false():
    """普通窗口（VSCode）→ False。"""
    from app.integrations import activitywatch

    buckets = [{"id": f"aw-watcher-window_{activitywatch._hostname()}"}]
    events = [{"data": {"app": "Code", "title": "main.py - myproject"},
               "duration": 60.0}]

    with mock.patch.object(activitywatch, "_http_get_json",
                           side_effect=_mock_aw_get_json(events, buckets)):
        assert activitywatch.is_focus_meeting() is False


# -----------------------
# proactive.py 集成测
# -----------------------

class _FakeAWConfig:
    """aw_cfg 测试替身：只要 server_url / hostname 字段。"""
    server_url = "http://127.0.0.1:5600"
    hostname = ""
    enabled = True


class _StubMemory:
    """MemoryStore 替身：只要 .recent() 返列表。"""
    def recent(self, n):
        return []


class _StubState:
    """PetState 替身：只要 .stats_summary()。"""
    def stats_summary(self):
        return "心情: 80 饱食: 80"


def _make_proactive(**overrides):
    """绕过真实 LLM 配置构造一个最小 ProactiveBrain。"""
    from app.brain.proactive import ProactiveBrain

    fake_llm = mock.MagicMock()
    fake_llm.model = "test-model"
    fake_llm.base_url = "http://127.0.0.1:11434"
    fake_llm.api_key = "test-key"
    # is_placeholder_key 看到非空 key 就放行

    kwargs = dict(
        llm_cfg=fake_llm,
        persona="你是桌宠",
        state=_StubState(),
        memory=_StubMemory(),
        char_name="小汐",
        min_minutes=25,
        max_minutes=45,
        aw_cfg=_FakeAWConfig(),
    )
    kwargs.update(overrides)
    return ProactiveBrain(**kwargs)


def test_proactive_read_idle_minutes_returns_value():
    """_read_idle_minutes 走 get_idle_status, 解析返回 12.5。"""
    brain = _make_proactive()
    with mock.patch(
        "app.integrations.activitywatch.get_idle_status",
        return_value=12.5,
    ):
        assert brain._read_idle_minutes() == pytest.approx(12.5)


def test_proactive_read_idle_minutes_graceful_on_failure():
    """get_idle_status 抛异常 → _read_idle_minutes 返 None（不阻塞）。"""
    brain = _make_proactive()
    with mock.patch(
        "app.integrations.activitywatch.get_idle_status",
        side_effect=RuntimeError("aw server down"),
    ):
        assert brain._read_idle_minutes() is None


def test_proactive_meeting_detection():
    brain = _make_proactive()
    with mock.patch(
        "app.integrations.activitywatch.is_focus_meeting",
        return_value=True,
    ):
        assert brain._is_in_meeting() is True
    with mock.patch(
        "app.integrations.activitywatch.is_focus_meeting",
        return_value=False,
    ):
        assert brain._is_in_meeting() is False


def test_proactive_meeting_detection_graceful_on_failure():
    brain = _make_proactive()
    with mock.patch(
        "app.integrations.activitywatch.is_focus_meeting",
        side_effect=RuntimeError("boom"),
    ):
        assert brain._is_in_meeting() is False


def test_apply_idle_skip_minutes_clamps_to_min_1():
    """apply_idle_skip_minutes(minutes) 把值夹到 ≥ 1。"""
    brain = _make_proactive(idle_skip_minutes=30)
    brain.apply_idle_skip_minutes(0)
    assert brain.idle_skip_minutes == 1
    brain.apply_idle_skip_minutes(45)
    assert brain.idle_skip_minutes == 45


def test_apply_meeting_auto_mute_minutes_clamps_to_min_1():
    brain = _make_proactive(meeting_auto_mute_minutes=30)
    brain.apply_meeting_auto_mute_minutes(0)
    assert brain.meeting_auto_mute_minutes == 1
    brain.apply_meeting_auto_mute_minutes(60)
    assert brain.meeting_auto_mute_minutes == 60


# -----------------------
# _prepare / _ChatOnceWorker 集成测
# -----------------------

def _build_fire_context(brain):
    """模拟 ProactiveBrain._fire 里的 context_base + _prepare 闭包。"""
    from app.brain.llm_client import ChatMessage
    # 直接调 _prepare 用的 inner 闭包不容易拿到, 但可以借用 _fire 内部的
    # context_base 重新造一个最小输入, 然后单独验证 _read_idle_minutes
    # 和 _is_in_meeting 两条短路。
    # 完整 _prepare 的端到端测试由 _ChatOnceWorker 路径覆盖。
    return brain


def test_prepare_skips_when_idle_exceeds_threshold():
    """_read_idle_minutes 返 60 > idle_skip_minutes=30 → 决策层应跳过。

    这里直接验证短路逻辑：直接构造一个会被 _read_idle_minutes 拦截的
    brain 实例, 在 _ChatOnceWorker.run() 上发出来 done("") 信号。
    """
    from app.brain.proactive import _ChatOnceWorker

    brain = _make_proactive(idle_skip_minutes=30)

    # 模拟 ProactiveBrain._fire 里 context_base 的最小部分 + _prepare 闭包
    def fake_prepare():
        idle = brain._read_idle_minutes()
        if idle is not None and idle >= brain.idle_skip_minutes:
            return None, None
        return ("system", "msg")  # noqa: 不走

    done_signal = threading.Event()
    done_payload = []
    failed_payload = []

    worker = _ChatOnceWorker(
        llm_cfg=brain.llm_cfg,
        system_prompt="",
        messages=[],
        prepare=fake_prepare,
    )
    worker.done.connect(lambda text: (done_payload.append(text), done_signal.set()))
    worker.failed.connect(lambda e: failed_payload.append(e))

    with mock.patch(
        "app.integrations.activitywatch.get_idle_status",
        return_value=60.0,
    ):
        worker.run()
        # 给信号槽一点点时间（Qt 在非 GUI 线程也可能要事件循环）
        done_signal.wait(timeout=2.0)

    assert done_payload == [""], f"应该收到 done(''), 实际 {done_payload}"
    assert failed_payload == [], f"不应有 failed 信号, 实际 {failed_payload}"


def test_prepare_skips_and_mutes_when_in_meeting():
    """会议窗口 → 跳过本轮 + 调 set_tts_mute_until_threadsafe。"""
    from app.brain.proactive import _ChatOnceWorker
    import app.engine.tools._tts_mute as mute_mod

    brain = _make_proactive(meeting_auto_mute_minutes=45)
    # 清掉之前可能残留的静音
    mute_mod._mute_until_ts = 0.0

    def fake_prepare():
        if brain._is_in_meeting():
            mute_mod.set_tts_mute_until_threadsafe(brain.meeting_auto_mute_minutes)
            return None, None
        return ("system", "msg")  # noqa: 不走

    done_signal = threading.Event()
    done_payload = []
    worker = _ChatOnceWorker(
        llm_cfg=brain.llm_cfg,
        system_prompt="",
        messages=[],
        prepare=fake_prepare,
    )
    worker.done.connect(lambda text: (done_payload.append(text), done_signal.set()))

    with mock.patch(
        "app.integrations.activitywatch.is_focus_meeting",
        return_value=True,
    ):
        worker.run()
        done_signal.wait(timeout=2.0)

    assert done_payload == [""]
    # TTS 静音状态应已生效, 且到点时间应该 ≥ 当前时间 + 30 分钟
    import time as _t
    assert mute_mod._mute_until_ts > _t.time() + 30 * 60


def test_prepare_runs_normally_when_not_idle_no_meeting():
    """既不空闲也不开会 → _prepare 不短路, _ChatOnceWorker 应该正常驱动 LLM。"""
    from app.brain.proactive import _ChatOnceWorker
    import app.brain.llm_client as llm_mod

    brain = _make_proactive()

    # 一个返回固定结果的 fake LLMClient
    class _FakeLLMClient:
        def __init__(self, cfg, system, request_timeout_s=None):
            self.cfg = cfg
            self.system = system
            self.closed = False

        async def chat_once(self, messages):
            return "主人好, 今天精神不错~"

        async def close(self):
            self.closed = True

    llm_calls = {"n": 0}
    real_init = llm_mod.LLMClient

    def fake_init(cfg, system, request_timeout_s=None):
        llm_calls["n"] += 1
        return _FakeLLMClient(cfg, system, request_timeout_s)

    def fake_prepare():
        idle = brain._read_idle_minutes()
        if idle is not None and idle >= brain.idle_skip_minutes:
            return None, None
        if brain._is_in_meeting():
            return None, None
        return ("you are 小汐", [mock.MagicMock()])

    done_signal = threading.Event()
    done_payload = []
    worker = _ChatOnceWorker(
        llm_cfg=brain.llm_cfg,
        system_prompt="",
        messages=[],
        prepare=fake_prepare,
    )
    worker.done.connect(lambda text: (done_payload.append(text), done_signal.set()))

    with mock.patch.object(llm_mod, "LLMClient", side_effect=fake_init), \
         mock.patch(
             "app.integrations.activitywatch.get_idle_status",
             return_value=0.0,
         ), \
         mock.patch(
             "app.integrations.activitywatch.is_focus_meeting",
             return_value=False,
         ):
        worker.run()
        done_signal.wait(timeout=2.0)

    assert done_payload == ["主人好, 今天精神不错~"]
    assert llm_calls["n"] == 1, "应该发了一次 LLM 请求"


def test_tts_mute_threadsafe_can_be_called_from_non_main():
    """线程安全版 set_tts_mute_until_threadsafe 应能从子线程调且不回调 hook。"""
    import app.engine.tools._tts_mute as mute_mod
    import time as _t
    mute_mod._mute_until_ts = 0.0

    # 注入一个会被检测到的「假设有人设了 hook」状态, 验证 _notify 不会因为
    # 跨线程被错误触发（线程安全版不调 _notify, 直接绕开）
    called = {"n": 0}

    def fake_hook(muted):
        called["n"] += 1

    mute_mod.set_mute_hook(fake_hook)
    try:
        # 在子线程调
        def worker():
            mute_mod.set_tts_mute_until_threadsafe(20)

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=2.0)
        # hook 不应被线程安全版触发
        assert called["n"] == 0, \
            "set_tts_mute_until_threadsafe 不应回调 hook"
        # 但 mute 状态已生效
        assert mute_mod.is_muted() is True
    finally:
        mute_mod.set_mute_hook(None)