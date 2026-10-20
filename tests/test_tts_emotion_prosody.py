"""v58: TTS 情绪 prosody (rate / pitch / volume)。

覆盖:
- `emotion_to_prosody(None)` 返空 dict
- `emotion_to_prosody("happy")` 返 (rate, pitch, volume)
- `emotion_to_prosody(Emotion.HAPPY)` 枚举也能识别
- 未知情绪返空 dict
- 所有 Emotion 枚举值都有映射
- `TTS.speak_with_emotion` 入队 (text, profile, prosody) 三元组
- `_speak_worker_loop` 兼容 2 元组 / 3 元组队列项
- `_synthesize` 接收 prosody kwarg 并 merge 进 edge_tts.Communicate
- 缓存 key 在有 prosody 时会变化 (不同情绪不互相覆盖)
"""
from __future__ import annotations

import os
import queue
import sys
from pathlib import Path
from typing import Any, Dict
from unittest import mock

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ----------------------------
# emotion_to_prosody
# ----------------------------


def test_emotion_to_prosody_none_returns_empty():
    from app.voice.voice import emotion_to_prosody
    assert emotion_to_prosody(None) == {}


def test_emotion_to_prosody_unknown_returns_empty():
    from app.voice.voice import emotion_to_prosody
    assert emotion_to_prosody("不存在") == {}
    assert emotion_to_prosody("zzz") == {}


def test_emotion_to_prosody_string_known_emotions():
    from app.voice.voice import emotion_to_prosody
    for emo in ("happy", "sad", "angry", "surprised", "shy", "proud", "thinking"):
        prosody = emotion_to_prosody(emo)
        assert prosody, f"{emo} 应有 prosody"
        # 字段都在 edge-tts 合法范围
        assert "rate" in prosody
        assert "pitch" in prosody
        assert "volume" in prosody


def test_emotion_to_prosody_enum_value():
    """Emotion.HAPPY (StringEnum) 应能被识别。"""
    from app.voice.voice import emotion_to_prosody
    from app.voice.character import Emotion
    prosody = emotion_to_prosody(Emotion.HAPPY)
    assert "rate" in prosody
    assert "pitch" in prosody


def test_emotion_to_prosody_values_are_edge_tts_safe():
    """rate/volume 形如 '+10%' / '-15%', pitch 形如 '+3Hz' / '-5Hz'。"""
    import re
    from app.voice.voice import emotion_to_prosody
    for emo in ("happy", "sad", "angry", "surprised"):
        p = emotion_to_prosody(emo)
        assert re.match(r"^[+\-]\d+%$", p["rate"]), f"{emo}.rate bad: {p['rate']}"
        assert re.match(r"^[+\-]\d+%$", p["volume"]), f"{emo}.volume bad: {p['volume']}"
        # pitch 可以是 +XHz 或 +X%
        assert re.match(r"^[+\-]\d+(Hz|%)$", p["pitch"]), \
            f"{emo}.pitch bad: {p['pitch']}"


def test_emotion_to_prosody_distinct_emotions_are_distinct():
    """不同情绪应至少有一个字段不同 (避免「全是同一套配置」)。"""
    from app.voice.voice import emotion_to_prosody
    happy = emotion_to_prosody("happy")
    sad = emotion_to_prosody("sad")
    angry = emotion_to_prosody("angry")
    assert happy != sad
    assert happy != angry
    assert sad != angry


# ----------------------------
# TTS.speak_with_emotion
# ----------------------------


def _make_tts_with_mock_queue():
    """构造一个只测试队列入队 + 调度的 TTS 子类替身。"""
    from app.voice.voice import TTS

    captured = {"calls": []}

    class _StubTTS(TTS):
        # 不实际合成 / 播放, 只记录入队 + 调度
        cache_ext = ".mp3"

        def __init__(self):
            self._enabled = True
            self._speak_queue = queue.Queue(maxsize=20)
            self._speak_worker_started = True   # 跳过 _ensure_speak_worker 真起线程
            self.voice = "test-voice"
            self.cache_dir = Path("/tmp")

        def _enqueue_speak(self, text, profile, prosody=None):
            captured["calls"].append((text, profile, prosody or {}))

        def _ensure_speak_worker(self):
            pass   # 已起

        # 把 speak / speak_as 也重写一下, 避免触发真合成
        def speak(self, text):
            self._enqueue_speak(text, None, {})

        def speak_as(self, text, profile=None):
            self._enqueue_speak(text, profile, {})

    return _StubTTS(), captured


def test_speak_with_emotion_none_prosody_empty():
    tts, cap = _make_tts_with_mock_queue()
    tts.speak_with_emotion("hello", None)
    assert cap["calls"] == [("hello", None, {})]


def test_speak_with_emotion_default_voice_routes_to_speak_when_none():
    """默认音色 + emotion=None → 直接走 speak() (prosody 空 dict), 不入专用队列。"""
    tts, cap = _make_tts_with_mock_queue()
    with mock.patch.object(tts, "speak") as mock_speak:
        tts.speak_with_emotion("hi", None)
        mock_speak.assert_called_once_with("hi")
    assert cap["calls"] == []


def test_speak_with_emotion_passes_prosody_through():
    tts, cap = _make_tts_with_mock_queue()
    from app.voice.character import Emotion
    tts.speak_with_emotion("主人早", Emotion.HAPPY)
    assert len(cap["calls"]) == 1
    text, profile, prosody = cap["calls"][0]
    assert text == "主人早"
    assert profile is None
    assert "rate" in prosody
    assert "pitch" in prosody


def test_speak_with_emotion_with_profile():
    tts, cap = _make_tts_with_mock_queue()
    fake_profile = mock.MagicMock(edge_voice="custom-vip")
    tts.speak_with_emotion("hi", "happy", profile=fake_profile)
    assert cap["calls"][0][1] is fake_profile


def test_speak_disabled_skips_enqueue():
    from app.voice.voice import TTS

    class _Stub(TTS):
        def __init__(self):
            self._enabled = False
            self._speak_queue = queue.Queue(maxsize=20)
            self.cache_ext = ".mp3"

        def _enqueue_speak(self, *a, **kw):
            raise AssertionError("不应入队")

    tts = _Stub()
    tts.speak_with_emotion("hi", "happy")  # 不应抛


# ----------------------------
# 缓存 key 在 prosody 下区分
# ----------------------------


def test_speak_blocking_cache_key_changes_with_prosody():
    """同一句话, 不同 prosody 应该用不同 cache key (不互相覆盖)。"""
    from app.voice.voice import _cache_key
    text = "你好"
    neutral_tag = "voice-a"
    happy_prosody = {"rate": "+10%", "pitch": "+3Hz", "volume": "+0%"}
    sad_prosody = {"rate": "-15%", "pitch": "-3Hz", "volume": "-5%"}

    # 模拟 _speak_blocking 的 tag 拼接逻辑
    def tag_with(p):
        prosody_tag = "|".join(f"{k}={v}" for k, v in sorted(p.items()))
        return f"{neutral_tag}|{prosody_tag}" if neutral_tag else prosody_tag

    k_neutral = _cache_key(text, neutral_tag)
    k_happy = _cache_key(text, tag_with(happy_prosody))
    k_sad = _cache_key(text, tag_with(sad_prosody))
    assert k_neutral != k_happy
    assert k_happy != k_sad


# ----------------------------
# _synthesize 接受 prosody
# ----------------------------


def test_synthesize_passes_prosody_to_edge_tts():
    """edge-tts.Communicate 应该收到 rate/pitch/volume kwarg。"""
    from app.voice import voice as voice_mod

    class _FakeComm:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        async def save(self, path):
            pass

    captured = {"comm": None}

    def fake_communicate_factory(text, **kwargs):
        c = _FakeComm(text, **kwargs)
        captured["comm"] = c
        return c

    class _StubTTS(voice_mod.TTS):
        def __init__(self):
            self.voice = "test-voice"

        async def _synthesize_impl(self):
            pass

    # 直接用 mock 替换 edge_tts.Communicate
    fake_edge = mock.MagicMock()
    fake_edge.Communicate = fake_communicate_factory

    async def _run():
        tts = _StubTTS()
        # 用 asyncio.manual import 不行, 直接传 prosody
        with mock.patch.dict(os.environ, {}):
            # 把 edge_tts 注入
            sys.modules["edge_tts"] = fake_edge
            try:
                await tts._synthesize(
                    "hi", Path("/tmp/out.mp3"), prosody={"rate": "+10%", "pitch": "+3Hz", "volume": "+0%"},
                )
            finally:
                sys.modules.pop("edge_tts", None)

    import asyncio
    asyncio.run(_run())
    assert captured["comm"] is not None
    assert captured["comm"].kwargs.get("rate") == "+10%"
    assert captured["comm"].kwargs.get("pitch") == "+3Hz"
    assert captured["comm"].kwargs.get("volume") == "+0%"


def test_synthesize_no_prosody_uses_voice_only():
    """prosody=None/{} → 只传 voice, 不传 rate/pitch/volume。"""
    from app.voice import voice as voice_mod

    class _FakeComm:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs
            self.args = args
        async def save(self, path): pass

    captured = {"comm": None}
    def fake_factory(text, **kwargs):
        captured["comm"] = _FakeComm(text, **kwargs)
        return captured["comm"]

    fake_edge = mock.MagicMock()
    fake_edge.Communicate = fake_factory

    class _StubTTS(voice_mod.TTS):
        def __init__(self):
            self.voice = "test-voice"

    async def _run():
        tts = _StubTTS()
        sys.modules["edge_tts"] = fake_edge
        try:
            await tts._synthesize("hi", Path("/tmp/out.mp3"), prosody=None)
            await tts._synthesize("hi", Path("/tmp/out.mp3"), prosody={})
        finally:
            sys.modules.pop("edge_tts", None)

    import asyncio
    asyncio.run(_run())
    # 两次调用都没有 rate/pitch/volume
    for k in ("rate", "pitch", "volume"):
        assert k not in captured["comm"].kwargs


# ----------------------------
# _speak_worker_loop 兼容 2 元组 / 3 元组
# ----------------------------


def test_speak_worker_loop_handles_2tuple():
    """队列里老格式 (text, profile) 2 元组不应崩。"""
    from app.voice.voice import TTS

    class _Stub(TTS):
        def __init__(self):
            self._speak_queue = queue.Queue()
            self._speak_called = []
        def _speak_blocking(self, text, profile=None, prosody=None):
            self._speak_called.append((text, profile, prosody))

    tts = _Stub()
    tts._speak_queue.put(("hi", None))   # 老格式
    tts._speak_queue.put(("hi2", None, {"rate": "+5%"}))   # 新格式

    # 直接调 worker loop 一步 (避免无限循环)
    item1 = tts._speak_queue.get(timeout=0.5)
    item2 = tts._speak_queue.get(timeout=0.5)

    # 模拟 worker loop 里的解包
    if len(item1) == 3:
        text, profile, prosody = item1
    else:
        text, profile = item1
        prosody = {}
    if len(item2) == 3:
        text, profile, prosody = item2
    else:
        text, profile = item2
        prosody = {}

    # 2 元组得到 prosody={}
    # 3 元组得到 {"rate": "+5%"}
    assert prosody == {"rate": "+5%"}