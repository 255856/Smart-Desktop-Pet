"""语音唤醒：始终监听麦克风，命中唤醒词（默认「小汐」）后回调信号。

性能设计：
- VAD 能量门：能量低于阈值直接跳过 whisper，省 CPU 90%+
- 静音缓存：连续 N 片静音时不调模型
- shared_model：复用 chat_window 已加载的 faster-whisper（避免重复 load）
- 模型懒加载：后台线程加载，不阻塞 UI
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

import numpy as np

from app.core.qt_compat import QObject, Signal

log = logging.getLogger(__name__)

# 必须在 import faster_whisper / ctranslate2 之前设（解决 c10.dll + SSL 问题）。
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("KMP_WARNINGS", "0")
os.environ.setdefault("HF_HUB_DISABLE_SSL_VERIFICATION", "1")


# VAD 阈值（float32 音频）
_VAD_RMS_THRESHOLD = 0.01    # 低于此值认为静音
_VAD_QUIET_FRAMES_TO_BYPASS = 3    # 连续 N 片静音后跳过模型


class WakeWordRecognizer(QObject):
    """始终监听唤醒词。

    信号：
        wake_triggered()         # 命中唤醒词；UI 可弹出"我在~"
        command_ready(text)      # 唤醒后录到主人命令并识别出文字（直接 emit 给 chat_window）
        error(msg)               # 后端错误
        state_changed(str)       # "listening" | "wake_detected" | "command_recording"
                                 #            | "disabled" | "error" — UI 显示状态

    用法：
        ww = WakeWordRecognizer(model_size="base", wake_words=["小汐"])
        ww.set_shared_model(asr._model)   # 复用 ASR 已加载模型
        ww.state_changed.connect(lambda s: status_label.setText(s))
        ww.wake_triggered.connect(lambda: popup.show_bubble("我在~"))
        ww.command_ready.connect(lambda text: chat_window.handle_voice_command(text))
        ww.start()
    """

    state_changed = Signal(str)
    wake_triggered = Signal()
    command_ready = Signal(str)
    error = Signal(str)

    SAMPLE_RATE = 16000
    CHUNK_SECONDS = 2.0    # 提到 2 秒：单片推理负载低
    POST_WAKE_RECORD_SECONDS = 8.0

    def __init__(self, *, model_size: str = "tiny",   # tiny 默认：~30MB / 推理快
                 wake_words: Optional[list[str]] = None,
                 language: str = "zh",
                 shared_model=None):
        """shared_model: 可选传入已加载的 WhisperModel（避免重复加载）。"""
        super().__init__()
        self.model_size = model_size
        self.language = language
        self.wake_words = [w.lower() for w in (wake_words or ["小汐", "嗨小汐", "hey 小汐", "hey"])]
        self._model = shared_model
        self._model_lock = threading.Lock()
        self._model_loaded = shared_model is not None
        self._enabled = False
        self._stop_event = threading.Event()
        self._wake_state = "idle"
        self._command_deadline = 0.0
        self._quiet_count = 0    # 连续静音片计数

    def set_shared_model(self, model) -> None:
        """由主程序注入已加载的 WhisperModel，避免重复 load。"""
        if model is not None:
            self._model = model
            self._model_loaded = True

    def start(self) -> None:
        if self._enabled:
            return
        self._enabled = True
        self._stop_event.clear()
        threading.Thread(target=self._listen_loop, daemon=True,
                         name="wake-word-listen").start()
        log.info("WakeWordRecognizer 启动，唤醒词=%s", self.wake_words)
        self.state_changed.emit("listening")

    def stop(self) -> None:
        if not self._enabled:
            return
        self._enabled = False
        self._stop_event.set()
        log.info("WakeWordRecognizer 停止")
        self.state_changed.emit("disabled")

    def _listen_loop(self) -> None:
        try:
            import sounddevice as sd
        except ImportError:
            self.error.emit("sounddevice 未安装，无法监听唤醒")
            self.state_changed.emit("error")
            return
        try:
            chunk_size = int(self.SAMPLE_RATE * self.CHUNK_SECONDS)
            with sd.InputStream(
                samplerate=self.SAMPLE_RATE, channels=1, dtype="float32",
                blocksize=chunk_size,
            ) as stream:
                while not self._stop_event.is_set():
                    data, _ = stream.read(chunk_size)
                    if data.size == 0:
                        continue
                    audio = data.reshape(-1).astype(np.float32)
                    self._process_chunk(audio)
        except Exception as e:  # noqa: BLE001
            log.exception("WakeWordRecognizer 监听出错")
            self.error.emit(f"唤醒监听出错：{e}")
            self.state_changed.emit("error")

    def _has_speech(self, audio: np.ndarray) -> bool:
        """VAD 能量门：RMS > 阈值 + 静音计数。"""
        rms = float(np.sqrt(np.mean(audio ** 2)))
        if rms < _VAD_RMS_THRESHOLD:
            self._quiet_count += 1
            return False
        self._quiet_count = 0
        return True

    def _process_chunk(self, audio: np.ndarray) -> None:
        now = time.time()
        # 1) 命令录制阶段：到点 → 转写最后一片 → emit
        if self._wake_state == "command_recording" and now >= self._command_deadline:
            self._wake_state = "idle"
            self.state_changed.emit("listening")
            if self._has_speech(audio):
                text = self._transcribe(audio)
                if text:
                    self.command_ready.emit(text)
            return
        # 2) 命令录制中（未到期）：什么都不做
        if self._wake_state != "idle":
            return
        # 3) 唤醒检测：VAD 预过滤
        if not self._has_speech(audio):
            # 连续静音片超过阈值 → 跳过模型推理（省 CPU）
            if self._quiet_count > _VAD_QUIET_FRAMES_TO_BYPASS:
                return
            # 第一两片静音做兜底检测（防 VAD 把弱唤醒词漏掉）
        text = self._transcribe(audio).lower()
        if not text:
            return
        for w in self.wake_words:
            if w in text:
                log.info("wake word matched: %r (heard: %r)", w, text)
                self._wake_state = "wake_detected"
                self.wake_triggered.emit()
                self.state_changed.emit("wake_detected")
                self._command_deadline = now + self.POST_WAKE_RECORD_SECONDS
                self._wake_state = "command_recording"
                self.state_changed.emit("command_recording")
                self._quiet_count = 0
                return

    def _transcribe(self, audio: np.ndarray) -> str:
        """后台线程 + 懒加载模型：首次 inference 才加载，避免启动阻塞。"""
        try:
            model = self._get_model()
            segments, _info = model.transcribe(
                audio, language=self.language,
                beam_size=1, vad_filter=False,
            )
            return "".join(s.text for s in segments).strip()
        except Exception as e:  # noqa: BLE001
            log.debug("wake transcribe failed: %s", e)
            return ""

    def _get_model(self):
        with self._model_lock:
            if self._model is None:
                _patch_hf_download()
                from faster_whisper import WhisperModel
                log.info("WakeWordRecognizer 加载 faster-whisper %s (首次)…", self.model_size)
                self._model = WhisperModel(
                    self.model_size, device="cpu", compute_type="int8",
                    local_files_only=True)
                self._model_loaded = True
                log.info("WakeWordRecognizer 模型加载完成")
            return self._model

    def is_model_ready(self) -> bool:
        """主程序查询：模型已就绪（避免对话第一句走 ASR 时卡）。"""
        return self._model is not None and self._model_loaded


def _patch_hf_download() -> None:
    """HF 国内镜像 + 本地缓存优先：本地有 cache 就直接用，不查远程 API。"""
    import os
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")


def wake_available() -> bool:
    """sounddevice + faster_whisper 都装了才能唤醒。"""
    from importlib.util import find_spec
    try:
        return (find_spec("sounddevice") is not None
                and find_spec("faster_whisper") is not None)
    except Exception:  # noqa: BLE001
        return False