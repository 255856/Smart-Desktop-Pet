"""语音识别（ASR）模块 —— 麦克风录音 + faster-whisper 转文字。

设计：
    - 录音走独立线程（sounddevice.InputStream），不阻塞 Qt 主循环
    - 语音识别走独立线程（faster-whisper），首次加载模型较慢
    - 模型惰性加载且只加载一次，后续复用

依赖（可选安装，未安装时对应按钮/功能优雅降级）：
    pip install sounddevice faster-whisper
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

import numpy as np

from app.core.qt_compat import QObject, Signal

log = logging.getLogger(__name__)


def asr_available() -> bool:
    """sounddevice / faster-whisper 是否已安装（做 UI 降级判断）。

    用 find_spec 只查存在性、不执行模块——faster_whisper 的 import 会连带
    加载 ctranslate2 等重组件，在聊天窗口构造时执行会把 UI 卡住一两秒。
    """
    from importlib.util import find_spec
    try:
        return (find_spec("sounddevice") is not None
                and find_spec("faster_whisper") is not None)
    except Exception:  # noqa: BLE001
        return False


class SpeechRecognizer(QObject):
    """按住说话 → 语音转文字。

    用法：
        r = SpeechRecognizer(model_size="base", language="zh")
        r.recording_started.connect(...)     # 开始采音（UI 可显示「录音中」）
        r.text_ready.connect(on_text)        # 识别完成回掉，参数是文本
        r.error.connect(on_err)              # 识别失败回掉，参数是错误信息
        r.start_recording()
        r.stop_and_recognize()
    """

    recording_started = Signal()
    recording_finished = Signal()
    text_ready = Signal(str)
    error = Signal(str)

    SAMPLE_RATE = 16000          # whisper 标准采样率
    _AVERAGE_MAX_SECONDS = 20.0  # 单次最长录音（超过自动截断）

    def __init__(self, model_size: str = "base", language: str = "zh"):
        super().__init__()
        self.model_size = model_size
        self.language = language

        self._recorder_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._samples: list[np.ndarray] = []
        self._recorded_seconds = 0.0

        # 模型惰性加载（独立线程），只 load 一次
        self._model = None
        self._model_lock = threading.Lock()

    # ---------------- 录音 ----------------
    def start_recording(self) -> None:
        """开始采集麦克风。若已有录音线程则先停掉。"""
        if self._recorder_thread and self._recorder_thread.is_alive():
            return
        self._samples = []
        self._recorded_seconds = 0.0
        self._stop_event.clear()
        self._recorder_thread = threading.Thread(
            target=self._record_loop, daemon=True, name="asr-record")
        self._recorder_thread.start()
        self.recording_started.emit()

    def stop_and_recognize(self) -> None:
        """停止录音 → 后台线程转文字 → 发 text_ready(text)。"""
        if self._recorder_thread and self._recorder_thread.is_alive():
            self._stop_event.set()
            self._recorder_thread.join(timeout=3.0)
        self._recorder_thread = None
        self.recording_finished.emit()

        audio = self._current_audio()
        if audio.size == 0:
            self.text_ready.emit("")   # 空录音
            return

        threading.Thread(target=self._recognize_loop,
                         args=(audio,), daemon=True,
                         name="asr-recognize").start()

    def _record_loop(self) -> None:
        """录音线程体：把各块 float32 音频累积到 self._samples。"""
        try:
            import sounddevice as sd
        except Exception as e:  # noqa: BLE001
            self.error.emit(f"麦克风不可用：{e}")
            return
        try:
            with sd.InputStream(
                    samplerate=self.SAMPLE_RATE, channels=1,
                    dtype="float32", blocksize=0,
            ) as stream:
                while not self._stop_event.is_set():
                    data, _ = stream.read(4000)        # 4000 帧 16000Hz = 0.25s
                    if not self._stop_event.is_set():
                        self._samples.append(data.copy())
                        self._recorded_seconds += data.shape[0] / self.SAMPLE_RATE
                        if self._recorded_seconds >= self._AVERAGE_MAX_SECONDS:
                            break
        except Exception as e:  # noqa: BLE001
            if not self._stop_event.is_set():
                self.error.emit(f"录音出错：{e}")

    def _current_audio(self) -> np.ndarray:
        if not self._samples:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(self._samples).reshape(-1).astype(np.float32)

    # ---------------- 识别 ----------------
    def _recognize_loop(self, audio: np.ndarray) -> None:
        """识别线程体：惰性加载模型 + 转文字。"""
        try:
            model = self._get_model()
            segments, _info = model.transcribe(
                audio, language=self.language,
                beam_size=5, vad_filter=True,
            )
            text = "".join(s.text for s in segments).strip()
            self.text_ready.emit(text)
        except Exception as e:  # noqa: BLE001
            log.exception("ASR 识别失败")
            self.error.emit(f"语音识别失败：{e}")

    def _get_model(self):
        with self._model_lock:
            if self._model is None:
                _patch_hf_download()
                from faster_whisper import WhisperModel
                log.info("加载 faster-whisper 模型 %s (首次较慢)…", self.model_size)
                self._model = WhisperModel(
                    self.model_size, device="cpu", compute_type="int8")
            return self._model


def _patch_hf_download() -> None:
    """HuggingFace 国内下载补丁（仅影响 HF 下载，不影响全局 SSL 验证）。

    修复：不再修改 ssl._create_default_https_context（会禁用整个进程的 HTTPS 验证），
    改为只让 huggingface_hub 的内部 Session 关闭验证。
    """
    import os
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    # 不再禁用全局 SSL 验证 —— 改为局部处理
    import urllib3
    urllib3.disable_warnings()
    try:
        from huggingface_hub.utils._http import get_session
        get_session().verify = False
    except Exception:  # noqa: BLE001
        pass