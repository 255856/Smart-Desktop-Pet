"""ASR 模块单元测试（不依赖真实麦克风）。"""
import pytest
from app.voice.asr import asr_available


class TestASR:
    def test_asr_available(self):
        """检查 ASR 可用性检测（不要求一定可用）"""
        available = asr_available()
        assert isinstance(available, bool)
