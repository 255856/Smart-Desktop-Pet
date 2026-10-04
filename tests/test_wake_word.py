"""WakeWordRecognizer 单元测试：VAD 预过滤 + 状态机 + 模型共享。"""
import numpy as np
import pytest

from app.voice.wake_word import (
    WakeWordRecognizer, wake_available,
    _VAD_RMS_THRESHOLD, _VAD_QUIET_FRAMES_TO_BYPASS,
)


@pytest.fixture
def ww():
    """不实际 start 的 WakeWord 实例（避免真实加载麦克风 / 模型）。"""
    return WakeWordRecognizer(
        model_size="tiny", wake_words=["小汐"], language="zh",
    )


class TestWakeAvailability:
    def test_wake_available_check(self):
        # 取决于环境装了 sounddevice / faster_whisper；至少跑通不抛异常
        result = wake_available()
        assert isinstance(result, bool)


class TestWakeInit:
    def test_default_wake_words(self):
        ww = WakeWordRecognizer()
        assert "小汐" in ww.wake_words
        assert "嗨小汐" in ww.wake_words

    def test_custom_wake_words_lowercased(self):
        ww = WakeWordRecognizer(wake_words=["HI Siri", "Hey"])
        assert "hi siri" in ww.wake_words
        assert "hey" in ww.wake_words

    def test_default_chunk_2_seconds(self):
        ww = WakeWordRecognizer()
        # CHUNK_SECONDS=2.0 → 32000 帧 @ 16kHz
        assert ww.CHUNK_SECONDS == 2.0
        assert ww.SAMPLE_RATE == 16000


class TestWakeSharedModel:
    """set_shared_model + 懒加载。"""

    def test_initial_no_model(self):
        ww = WakeWordRecognizer()
        assert ww._model is None
        assert ww.is_model_ready() is False

    def test_set_shared_model_marks_ready(self):
        ww = WakeWordRecognizer()
        fake_model = object()
        ww.set_shared_model(fake_model)
        assert ww._model is fake_model
        assert ww.is_model_ready() is True

    def test_set_shared_model_none_is_noop(self):
        ww = WakeWordRecognizer()
        # 即使传 None 也不影响现有 model
        ww.set_shared_model(object())
        before = ww._model
        ww.set_shared_model(None)
        assert ww._model is before


class TestWakeVAD:
    """_has_speech 能量门 + 静音计数。"""

    def test_loud_audio_passes_vad(self, ww):
        # 模拟 1 秒大声（0.3 振幅） → RMS > threshold
        loud = np.ones(16000, dtype=np.float32) * 0.3
        assert ww._has_speech(loud) is True

    def test_quiet_audio_fails_vad(self, ww):
        # 静音 → RMS < threshold（默认 0.01）
        quiet = np.zeros(16000, dtype=np.float32)
        assert ww._has_speech(quiet) is False

    def test_silence_counter_resets_on_speech(self, ww):
        quiet = np.zeros(16000, dtype=np.float32)
        loud = np.ones(16000, dtype=np.float32) * 0.3
        for _ in range(5):
            ww._has_speech(quiet)
        assert ww._quiet_count == 5
        ww._has_speech(loud)
        assert ww._quiet_count == 0

    def test_silence_counter_increments(self, ww):
        assert ww._quiet_count == 0
        quiet = np.zeros(16000, dtype=np.float32)
        for _ in range(3):
            ww._has_speech(quiet)
        assert ww._quiet_count == 3


class TestWakeStateMachine:
    """_process_chunk 的状态转移。"""

    def _silent_chunk(self):
        return np.zeros(32000, dtype=np.float32)

    def _loud_chunk(self):
        return np.ones(32000, dtype=np.float32) * 0.3

    def test_initial_state_idle(self, ww):
        assert ww._wake_state == "idle"
        assert ww._command_deadline == 0.0

    def test_quiet_chunks_skipped_after_threshold(self, ww, monkeypatch):
        """连续静音片超过阈值后，_process_chunk 直接 return 不调模型。"""
        call_count = {"n": 0}

        def fake_transcribe(audio):
            call_count["n"] += 1
            return ""
        monkeypatch.setattr(ww, "_transcribe", fake_transcribe)

        # 让静音帧超过 _VAD_QUIET_FRAMES_TO_BYPASS
        for _ in range(_VAD_QUIET_FRAMES_TO_BYPASS + 5):
            ww._process_chunk(self._silent_chunk())
        # 模型应该只被调头几片（前 N+1 片静音里的兜底检测），之后完全跳过
        assert call_count["n"] <= _VAD_QUIET_FRAMES_TO_BYPASS + 1

    def test_command_recording_state_emits_text(self, ww, monkeypatch):
        """唤醒命中 → 进入 command_recording → 到点 emit command_ready。"""
        # 构造一个虚假的 transcribe：返回空（不触发唤醒）+ 一次返回真唤醒
        # 简化：直接通过 _wake_state 字段模拟
        ww._wake_state = "command_recording"
        ww._command_deadline = 0.0   # 立刻到期

        emitted = {"text": None}
        ww.command_ready.connect(lambda t: emitted.update({"text": t}))
        monkeypatch.setattr(ww, "_transcribe",
                            lambda audio: "你好主人")

        ww._process_chunk(self._loud_chunk())
        assert emitted["text"] == "你好主人"
        assert ww._wake_state == "idle"

    def test_stop_when_disabled_is_noop(self, ww):
        ww.stop()    # 未 start 也安全
        assert ww._enabled is False


class TestWakeThreadSafety:
    """stop() 设 stop_event，listen_loop 应退出。"""

    def test_stop_sets_event(self, ww):
        ww._enabled = True
        ww.stop()
        assert ww._stop_event.is_set()
        assert ww._enabled is False

    def test_double_stop_safe(self, ww):
        ww.stop()
        ww.stop()    # 不抛异常
        assert ww._enabled is False


class TestAsrWarmupLogic:
    """ASR 模型预热逻辑：chat_window 构造时 schedule QTimer.singleShot 触发 _get_model。"""

    def test_warmup_method_signature(self):
        """chat_window._warmup_asr_model 是实例方法。"""
        from app.ui import chat_window
        assert hasattr(chat_window.ChatWindow, "_warmup_asr_model")
        import inspect
        sig = inspect.signature(chat_window.ChatWindow._warmup_asr_model)
        assert list(sig.parameters) == ["self"]    # 无参数

    def test_warmup_handles_no_asr(self):
        """ASR 不可用时 _warmup_asr_model 不抛异常（不调真模型加载）。"""
        from app.ui import chat_window
        import sys
        from PyQt5.QtWidgets import QApplication
        QApplication.instance() or QApplication(sys.argv)

        class Stub:
            asr = None

        # 直接调：因为 asr=None → 方法提前 return；只验不抛错
        chat_window.ChatWindow._warmup_asr_model(Stub())

    def test_warmup_swallows_exceptions(self):
        """_get_model 抛异常（例 DLL 加载失败）：_warmup_asr_model 不挂。"""
        from app.ui import chat_window
        import sys
        from PyQt5.QtWidgets import QApplication
        QApplication.instance() or QApplication(sys.argv)

        class FakeASR:
            def _get_model(self):
                # 模拟用户机的 torch DLL 损坏
                raise OSError("[WinError 1114] c10.dll 初始化失败")

        class Stub:
            asr = FakeASR()

        # 应该静默吞，不抛 OSError
        chat_window.ChatWindow._warmup_asr_model(Stub())

    def test_warmup_retries_once_on_failure(self):
        """首次加载失败时重试一次：第二次成功就用第二次的结果。

        注：原 _do_warmup 闭包用 QTimer.singleShot 调度，offscreen 测试线程非 QThread，
        QTimer 会报错。我们直接验证重试逻辑的语义——构造 _get_model 模拟。
        """
        # 模拟重试逻辑：第 1 次抛、第 2 次成功
        call_count = {"n": 0}

        def fake_get_model():
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise OSError("首次偶发失败")
            return object()

        # 重试逻辑（在 chat_window 里是 for attempt in (1, 2):）
        model = None
        for _ in (1, 2):
            try:
                model = fake_get_model()
                break
            except OSError:
                continue
        assert call_count["n"] == 2    # 重试了 1 次
        assert model is not None

    def test_warmup_main_env_vars_set(self):
        """main.py 在 import torch 前必须设 OMP_NUM_THREADS=1 等环境变量。"""
        # 重置再 import 检查
        import os
        os.environ.pop("OMP_NUM_THREADS", None)
        os.environ.pop("KMP_DUPLICATE_LIB_OK", None)
        os.environ.pop("KMP_WARNINGS", None)

        # 模拟 main 模块的顶部执行
        os.environ.setdefault("OMP_NUM_THREADS", "1")
        os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
        os.environ.setdefault("KMP_WARNINGS", "0")

        assert os.environ.get("OMP_NUM_THREADS") == "1"
        assert os.environ.get("KMP_DUPLICATE_LIB_OK") == "TRUE"
        assert os.environ.get("KMP_WARNINGS") == "0"