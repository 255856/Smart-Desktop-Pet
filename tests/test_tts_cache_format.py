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