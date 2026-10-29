"""usage 进 trace 的接线测试（Stage 0.2）。

聊天窗的 usage handler 原来直接 return —— usage 不入 trace。
现在 usage 会落到 trace，同时状态栏的累加逻辑一点没变。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".local-packages"))
sys.path.insert(0, str(ROOT))

from app.core.qt_compat import QApplication  # noqa: E402
_app = QApplication.instance() or QApplication(sys.argv)

import pytest  # noqa: E402

from app.brain.llm_client import LLMConfig  # noqa: E402
from app.core.config import CharacterConfig  # noqa: E402


@pytest.fixture(autouse=True)
def _no_asr_warmup(monkeypatch):
    """聊天窗构造完会用 QTimer 预热 faster-whisper 模型 —— 测试里别碰它
    （本机 ctranslate2 加载会直接崩进程）。"""
    from app.ui.chat_window import ChatWindow
    monkeypatch.setattr(ChatWindow, "_warmup_asr_model", lambda self: None)


class _FakeTrace:
    def __init__(self):
        self.events = []

    def record(self, rid, kind, payload):
        self.events.append((rid, kind, payload))


def _make_window():
    char_cfg = CharacterConfig(name="台账测试娘", tts_enabled=False)
    llm_cfg = LLMConfig(model="test", api_key="test",
                        base_url="http://localhost:11434/v1")
    from app.ui.chat_window import ChatWindow
    return ChatWindow(llm_cfg, char_cfg, "assets/sprites")


def _usage_payload():
    return {
        "event": "usage",
        "prompt_tokens": 120,
        "completion_tokens": 7,
        "total_tokens": 127,
        "cache_hit_tokens": 100,
        "cache_miss_tokens": 20,
        "cache_write_tokens": 0,
        "got_real": True,
        "cache_hit_rate": 0.8333,
    }


def test_usage_event_reaches_trace():
    """B5: usage 不再被丢掉，进 trace。"""
    win = _make_window()
    trace = _FakeTrace()
    win.trace = trace
    win._trace_run_id = "rtest123"

    win._on_meta("meta", _usage_payload())

    assert len(trace.events) == 1
    rid, kind, recorded = trace.events[0]
    assert rid == "rtest123"
    assert kind == "usage"
    assert recorded["total_tokens"] == 127
    assert recorded["cache_hit_tokens"] == 100
    assert recorded["cache_miss_tokens"] == 20
    assert recorded["cache_write_tokens"] == 0


def test_status_bar_accumulation_unchanged():
    """状态栏累加逻辑一点没变（prompt/completion/total 照旧累加）。"""
    win = _make_window()
    win.trace = _FakeTrace()
    win._trace_run_id = "rtest123"
    win._accum_tokens = 0
    win._accum_prompt = 0
    win._accum_completion = 0

    win._on_meta("meta", _usage_payload())

    assert win._accum_prompt == 120
    assert win._accum_completion == 7
    assert win._accum_tokens == 127


def test_usage_without_trace_does_not_raise():
    """没有 trace（或 trace 抛异常）时不能把异常抛进聊天链路。"""
    win = _make_window()
    win.trace = None
    win._trace_run_id = None
    win._accum_tokens = 0
    win._on_meta("meta", _usage_payload())
    assert win._accum_tokens == 127

    class _Boom:
        def record(self, rid, kind, payload):
            raise RuntimeError("trace 挂了")

    win.trace = _Boom()
    win._accum_tokens = 0
    win._on_meta("meta", _usage_payload())
    assert win._accum_tokens == 127


def test_non_usage_meta_still_goes_to_trace():
    """普通 meta 事件照旧进 trace（usage 分支没有把这条路堵死）。"""
    win = _make_window()
    trace = _FakeTrace()
    win.trace = trace
    win._trace_run_id = "rtest123"

    win._on_meta("meta", {"event": "force_retry", "reason": "x"})

    assert len(trace.events) == 1
    assert trace.events[0][1] == "meta"
    assert trace.events[0][2]["event"] == "force_retry"
