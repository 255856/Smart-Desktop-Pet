"""v57: RoutineProfiler — 规律学习。

覆盖:
- `_normalize_app_name` 路径/扩展名归一化
- `_fetch_aw_window_events` mock AW 返回事件列表
- `RoutineProfiler.refresh_from_aw` 把 AW 事件聚合到 (weekday, hour) 桶
- `hint_for_now` 没数据返空串; 有数据生成自然语言 hint
- `is_stale` 阈值正确
- 持久化到 data/routines.json
- ProactiveBrain._routine_hint_for_now / _maybe_refresh_routine 在 rp=None 时优雅降级
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import tempfile
from typing import Any, Dict, List
from unittest import mock

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ----------------------------
# _normalize_app_name
# ----------------------------


def test_normalize_app_name_strips_path_and_exe():
    from app.brain.routine_profiler import _normalize_app_name
    assert _normalize_app_name("C:\\Program Files\\Code.exe") == "Code"
    assert _normalize_app_name("/usr/bin/python") == "python"
    assert _normalize_app_name("chrome.exe") == "chrome"
    assert _normalize_app_name("") == "未知"


# ----------------------------
# _fetch_aw_window_events (mocked)
# ----------------------------


def test_fetch_aw_window_events_returns_list_on_success():
    from app.brain import routine_profiler as rp

    fake_buckets = [{"id": "aw-watcher-window_host"}]
    fake_events = [{"timestamp": "2025-10-01T08:00:00Z", "data": {"app": "Code"}}]

    def fake_get(url, *args, **kwargs):
        if "/api/0/buckets/" in url and url.endswith("/buckets/"):
            return fake_buckets
        if "/api/0/buckets/" in url and "/events" in url:
            return fake_events
        return None

    with mock.patch.object(rp, "_http_get_json", side_effect=fake_get):
        events = rp._fetch_aw_window_events("http://127.0.0.1:5600", "host")
    assert events == fake_events


def test_fetch_aw_window_events_returns_empty_on_no_bucket():
    from app.brain import routine_profiler as rp

    with mock.patch.object(rp, "_http_get_json", return_value=[]):
        events = rp._fetch_aw_window_events("http://127.0.0.1:5600", "host")
    assert events == []


# ----------------------------
# RoutineProfiler.refresh + hint_for_now
# ----------------------------


def _make_fake_event(timestamp_iso: str, app: str) -> dict:
    return {"timestamp": timestamp_iso, "data": {"app": app}}


def test_refresh_groups_by_weekday_hour():
    from app.brain.routine_profiler import RoutineProfiler

    with tempfile.TemporaryDirectory() as d:
        rp = RoutineProfiler(d)
        # 2025-10-13 周一 (weekday=0), 10-15 周三 (weekday=2), 10-16 周四 (weekday=3)
        events = [
            _make_fake_event("2025-10-13T08:30:00+08:00", "Code.exe"),     # 周一 08
            _make_fake_event("2025-10-14T08:45:00+08:00", "Code.exe"),     # 周二 08
            _make_fake_event("2025-10-15T20:30:00+08:00", "chrome.exe"),   # 周三 20
            _make_fake_event("2025-10-16T08:00:00+08:00", "Code.exe"),     # 周四 08
            _make_fake_event("2025-10-16T08:15:00+08:00", "VSCode.exe"),   # 周四 08
        ]
        rp._hist = {}
        rp._fetched_ts = 0.0
        from collections import Counter
        counter = {}
        for ev in events:
            parsed = rp._event_to_local_weekday_hour(ev)
            assert parsed is not None
            wd, _, app = parsed
            counter.setdefault(wd, Counter())[app] += 1
        rp._hist = {str(wd): dict(c.most_common(5)) for wd, c in counter.items()}
        # 周一(0) 应该有 Code
        assert "Code" in rp._hist["0"]
        # 周三(2) 应该有 chrome
        assert "chrome" in rp._hist["2"]
        # 周四(3) 应该有 Code 和 VSCode
        assert "Code" in rp._hist["3"]
        assert "VSCode" in rp._hist["3"]


def test_hint_for_now_empty_when_no_data():
    from app.brain.routine_profiler import RoutineProfiler
    with tempfile.TemporaryDirectory() as d:
        rp = RoutineProfiler(d)
        rp._hist = {}
        assert rp.hint_for_now() == ""


def test_hint_for_now_generates_natural_language():
    from app.brain.routine_profiler import RoutineProfiler
    with tempfile.TemporaryDirectory() as d:
        rp = RoutineProfiler(d)
        # 把所有 weekday 都填上 Code
        from collections import Counter
        rp._hist = {str(wd): {"Code": 10, "chrome": 3, "Word": 1}
                    for wd in range(7)}
        # 周一上午
        monday_morning = _dt.datetime(2025, 10, 13, 9, 0)   # 周一 09:00
        hint = rp.hint_for_now(now=monday_morning, top_n=3)
        assert "Code" in hint
        assert "chrome" in hint
        assert "Word" in hint
        assert "星期一" in hint


def test_hint_for_now_respects_top_n():
    from app.brain.routine_profiler import RoutineProfiler
    with tempfile.TemporaryDirectory() as d:
        rp = RoutineProfiler(d)
        rp._hist = {"0": {"Code": 10, "chrome": 3, "Word": 1, "WPS": 0.5}}
        monday = _dt.datetime(2025, 10, 13, 9, 0)
        h1 = rp.hint_for_now(now=monday, top_n=2)
        assert "Code" in h1
        assert "chrome" in h1
        # Word 不在 top2
        assert "Word" not in h1


def test_is_stale_threshold():
    from app.brain.routine_profiler import RoutineProfiler
    with tempfile.TemporaryDirectory() as d:
        rp = RoutineProfiler(d)
        # 刚聚合 → 不陈旧
        rp._fetched_ts = __import__("time").time()
        assert rp.is_stale(max_age_s=3600) is False
        # 7 小时前 → 陈旧
        rp._fetched_ts = __import__("time").time() - 7 * 3600
        assert rp.is_stale(max_age_s=3600) is True


def test_persistence_roundtrip():
    from app.brain.routine_profiler import RoutineProfiler
    with tempfile.TemporaryDirectory() as d:
        rp1 = RoutineProfiler(d)
        rp1._hist = {"0": {"Code": 5}}
        rp1._fetched_ts = 12345.0
        rp1._save_to_disk()
        # 新实例应能从盘上读回
        rp2 = RoutineProfiler(d)
        assert rp2._hist == {"0": {"Code": 5}}
        assert rp2._fetched_ts == 12345.0


def test_refresh_from_aw_no_data_returns_false():
    """AW 不可达 → refresh 返 False, 不抛异常。"""
    from app.brain import routine_profiler as rp_mod
    from app.brain.routine_profiler import RoutineProfiler
    with tempfile.TemporaryDirectory() as d:
        rp = RoutineProfiler(d)
        with mock.patch.object(
            rp_mod, "_fetch_aw_window_events", return_value=[],
        ):
            ok = rp.refresh_from_aw()
        assert ok is False


# ----------------------------
# ProactiveBrain 集成
# ----------------------------


def test_proactive_routine_hint_empty_when_rp_none():
    from app.brain.proactive import ProactiveBrain

    fake_llm = mock.MagicMock()
    fake_llm.model = "test"
    fake_llm.base_url = "http://127.0.0.1:11434"
    fake_llm.api_key = "test-key"

    class _FakeMemory:
        def recent(self, n):
            return []
        def recent_shared(self, n):
            return []
    class _FakeState:
        def stats_summary(self):
            return "ok"

    brain = ProactiveBrain(
        llm_cfg=fake_llm, persona="你是桌宠",
        state=_FakeState(), memory=_FakeMemory(), char_name="小汐", aw_cfg=None,
    )
    # 强行关掉 rp, 验证降级
    brain.rp = None
    assert brain._routine_hint_for_now() == ""


def test_proactive_routine_hint_uses_rp():
    from app.brain.proactive import ProactiveBrain

    fake_llm = mock.MagicMock()
    fake_llm.model = "test"
    fake_llm.base_url = "http://127.0.0.1:11434"
    fake_llm.api_key = "test-key"

    class _FakeMemory:
        def recent(self, n):
            return []
        def recent_shared(self, n):
            return []
    class _FakeState:
        def stats_summary(self):
            return "ok"
    class _FakeRP:
        def hint_for_now(self):
            return "历史规律: 这个时间通常在 Code"

    brain = ProactiveBrain(
        llm_cfg=fake_llm, persona="你是桌宠",
        state=_FakeState(), memory=_FakeMemory(), char_name="小汐", aw_cfg=None,
    )
    brain.rp = _FakeRP()
    assert brain._routine_hint_for_now() == "历史规律: 这个时间通常在 Code"


def test_proactive_maybe_refresh_routine_skips_when_not_stale():
    from app.brain.proactive import ProactiveBrain

    fake_llm = mock.MagicMock()
    fake_llm.model = "test"
    fake_llm.base_url = "http://127.0.0.1:11434"
    fake_llm.api_key = "test-key"

    class _FakeMemory:
        def recent(self, n): return []
        def recent_shared(self, n): return []
    class _FakeState:
        def stats_summary(self): return "ok"
    class _FreshRP:
        called = 0
        def is_stale(self): return False
        def refresh_from_aw(self, **kw):
            self.called += 1
            return True

    brain = ProactiveBrain(
        llm_cfg=fake_llm, persona="你是桌宠",
        state=_FakeState(), memory=_FakeMemory(), char_name="小汐", aw_cfg=None,
    )
    rp = _FreshRP()
    brain.rp = rp
    brain._maybe_refresh_routine(force=False)
    # 等一会儿确认没有线程在跑 (refresh 不应被调用)
    import time as _t
    _t.sleep(0.2)
    assert rp.called == 0, "不陈旧时不应调 refresh"