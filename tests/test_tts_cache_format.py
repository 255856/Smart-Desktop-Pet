"""TTS 缓存格式嗅探 + 引擎 cache_ext 单测。"""
import tempfile
from pathlib import Path

from app.voice.gptsovits_tts import GPTSoVITSTTS
from app.voice.voice import TTS, _cache_format_matches


def test_cache_ext_defaults():
    """基类默认 mp3，GPT-SoVITS 子类必须为 wav（api_v2 返回 wav）。"""
    assert TTS.cache_ext == ".mp3"
    assert GPTSoVITSTTS.cache_ext == ".wav"


def test_cache_format_matches_wav():
    """RIFF/WAVE 头 → 期望 .wav 时 True，期望 .mp3 时 False。"""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False, mode="wb") as fp:
        fp.write(b"RIFF$\x00\x00\x00WAVEfmt ")
        path = Path(fp.name)
    try:
        assert _cache_format_matches(path, ".wav") is True
        assert _cache_format_matches(path, ".mp3") is False
    finally:
        path.unlink()


def test_cache_format_matches_mp3_id3():
    """ID3 头 → 期望 .mp3 时 True。"""
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False, mode="wb") as fp:
        fp.write(b"ID3\x04\x00\x00\x00\x00\x00\x00abc")
        path = Path(fp.name)
    try:
        assert _cache_format_matches(path, ".mp3") is True
        assert _cache_format_matches(path, ".wav") is False
    finally:
        path.unlink()


def test_cache_format_matches_mp3_sync():
    """MPEG sync 字节 (0xFFFB) → 期望 .mp3 时 True。"""
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False, mode="wb") as fp:
        fp.write(b"\xff\xfb\x90\x00" + b"\x00" * 100)
        path = Path(fp.name)
    try:
        assert _cache_format_matches(path, ".mp3") is True
    finally:
        path.unlink()


def test_cache_format_matches_empty():
    """空文件 / 损坏 → 任意期望都返回 False。"""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False, mode="wb") as fp:
        path = Path(fp.name)
    try:
        assert _cache_format_matches(path, ".wav") is False
        assert _cache_format_matches(path, ".mp3") is False
    finally:
        path.unlink()


def test_tts_speak_queue_serialization():
    """speak() 入队 + 唯一 worker 顺序消费（不会创建多个 thread 抢 mixer）。"""
    import threading
    from app.voice.voice import TTS

    class FakeTTS(TTS):
        """替换 _speak_blocking 直接记录调用顺序，避免真实 pygame 播放。"""
        def __init__(self):
            super().__init__(voice="fake")
            self.played: list[str] = []
            self._lock = threading.Lock()

        def _speak_blocking(self, text):  # type: ignore[override]
            with self._lock:
                self.played.append(text)

    tts = FakeTTS()
    # 入队 5 句
    for s in ["第一句", "第二句", "第三句", "第四句", "第五句"]:
        tts.speak(s)
    # 等 worker 处理
    tts._speak_queue.join()
    assert tts.played == ["第一句", "第二句", "第三句", "第四句", "第五句"]
    # worker 只启动一次
    assert tts._speak_worker_started is True


def test_chat_window_tts_drain_no_duplicate_queue():
    """_tts_drain_sentences 多次调用同一段不应重复入队（修复前会重复 N 次）。

    流式场景：_on_chunk 每收到一个 token 就调一次 _tts_drain_sentences(accumulated)，
    其中 accumulated 是「从开头到当前」的整段。如果每次从头扫描，已送过的整句
    会再次被 finditer 截出 + 入队。修复后用 _last_accumulated 增量切分。

    不真正构造 ChatWindow（依赖太多），改为直接调 _tts_drain_sentences 并传入
    同一 self / tts 的 mock，确保相同句子只入队一次。
    """
    from app.engine.tools._search import _format_results_md
    # 直接用 ChatWindow 类但只测它的 _tts_drain_sentences 方法（mock char_cfg + tts）
    import sys
    sys.path.insert(0, '.local-packages')
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)

    from app.core.config import LLMConfig, CharacterConfig
    from app.ui.chat_window import ChatWindow
    from app.voice.voice import TTS

    # 收集 speak + prepare 调用
    class CollectTTS(TTS):
        def __init__(self):
            super().__init__(voice="fake")
            self.spoken: list[str] = []
            self.prepareds: list[str] = []

        def speak(self, text):  # type: ignore[override]
            self.spoken.append(text)

        def prepare(self, text, timeout_s=30.0):  # type: ignore[override]
            self.prepareds.append(text)
            return True

    char_cfg = CharacterConfig(name="t", persona="p", tts_enabled=True)
    llm_cfg = LLMConfig(api_key="sk", model="m")
    cw = ChatWindow(llm_cfg, char_cfg, "assets/sprites", tts=CollectTTS())
    # 模拟流式：多次 chunk 累积同一段
    cw._tts_drain_sentences("主人你好呀。")
    cw._tts_drain_sentences("主人你好呀。今天天气不错。")
    cw._tts_drain_sentences("主人你好呀。今天天气不错！要不要出门。")
    cw._tts_drain_sentences("主人你好呀。今天天气不错！要不要出门。")
    cw._flush_tts_tail_to_prepare()
    # 等所有 prepare worker 完成（mock prepare 立即返回 True）
    import time
    deadline = time.monotonic() + 5
    while cw._sentence_workers and time.monotonic() < deadline:
        time.sleep(0.05)

    # 验证：每句只启动一次 prepare（不重复）
    prepared = cw.tts.prepareds
    # 关键是：同一句不应被启动 prepare 多次
    assert prepared.count("主人你好呀。") == 1, f"重复 prepare: {prepared}"
    # 第三次 chunk 时 "今天天气不错。" 已被 sanitize 替换为 "今天天气不错！"（累积文本变化）
    # —— 这里只验证「重复 prepare」被修：同一句话不应该出现 2 次以上
    assert len(prepared) <= 4, f"总 prepare 过多: {prepared}"