"""闹钟 / 倒计时到点的提示音。

用标准库 `winsound` 播放系统自带提示音，**零新增依赖**（winsound 属于
CPython 标准库，Windows 自带）。

为什么需要它：提醒到点时桌宠原本只弹一个气泡，主人往往在忙别的事、或者
桌宠缩在屏幕角落，完全注意不到——于是「设了提醒但好像没响」。提示音是
「到点一定有人听见」的最后一道保证。

非 Windows / winsound 不可用时全部静默降级：TTS 播报和置顶弹窗仍然会走。
"""
from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)

try:  # pragma: no cover - 平台相关
    import winsound  # type: ignore
    HAS_WINSOUND = True
except ImportError:  # pragma: no cover - 非 Windows
    winsound = None  # type: ignore
    HAS_WINSOUND = False

# 系统提示音别名（Windows 自带，无需自带音频文件）。
# 前两个是"闹钟"味最重的，第三个最柔和。
_ALIAS_CHIME = "AlarmClock"
_ALIAS_RING = "Ring"
_ALIAS_REMINDER = "Reminder"

# 同一时间只允许一个闹钟在响；新的会顶掉旧的（别叠加成噪音）
_lock = threading.Lock()
_current: "AlarmSound | None" = None


def available() -> bool:
    """当前环境能不能出声。"""
    return HAS_WINSOUND


class AlarmSound:
    """循环播放提示音，直到 `stop()`。

    用法::

        snd = AlarmSound()
        snd.start()      # 响
        ...
        snd.stop()       # 停
    """

    def __init__(self, alias: str = _ALIAS_CHIME, loops: int = 3):
        self.alias = alias
        self.loops = max(1, int(loops))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        """开始响。返回是否真的出声了（False = 本机无法播放）。"""
        global _current
        if not HAS_WINSOUND:
            log.debug("winsound 不可用，跳过提示音")
            return False
        # 先在锁内把「当前响着的」摘出来，再在锁外让它停。
        # 早期实现是在持锁状态下调用 prev.stop()，而 stop() 里又会拿同一把锁
        # （threading.Lock 不可重入）→ 直接死锁，测试整个卡住。
        with _lock:
            prev = _current
            _current = self
        if prev is not None and prev is not self:
            # 此刻 _current 已经是 self，prev.stop() 不会把 self 摘掉
            prev.stop()
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._play, daemon=True, name="alarm-sound")
        self._thread.start()
        return True

    def _play(self) -> None:
        try:
            for _ in range(self.loops):
                if self._stop.is_set():
                    return
                # SND_ALIAS: 用系统提示音；SND_ASYNC 让 UI 线程不被卡住
                winsound.PlaySound(
                    self.alias,
                    winsound.SND_ALIAS | winsound.SND_ASYNC,
                )
                # 等这一轮播完（系统提示音约 1~3 秒），期间可被 stop 打断
                if self._stop.wait(3.0):
                    return
        except Exception:  # noqa: BLE001
            log.debug("提示音播放异常", exc_info=True)
        finally:
            self._purge()

    def _purge(self) -> None:
        if not HAS_WINSOUND:
            return
        try:
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:  # noqa: BLE001
            pass

    def stop(self) -> None:
        """停止（幂等）。"""
        self._stop.set()
        self._purge()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=1.0)
        self._thread = None
        global _current
        with _lock:
            if _current is self:
                _current = None

    def is_playing(self) -> bool:
        return bool(self._thread and self._thread.is_alive())


def stop_all() -> None:
    """停掉所有正在响的提示音（退出时用）。"""
    global _current
    with _lock:
        cur = _current
        _current = None
    # 锁外再停：stop() 自己也要拿锁（早期实现同款死锁）
    if cur is not None:
        cur.stop()
    if HAS_WINSOUND:
        try:
            winsound.PlaySound(None, winsound.SND_PURGE)
        except Exception:  # noqa: BLE001
            pass


__all__ = ["AlarmSound", "available", "stop_all", "HAS_WINSOUND"]
