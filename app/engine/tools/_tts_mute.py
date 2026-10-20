"""TTS 静音：临时关掉朗读，到点自动恢复。"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from ._core import ToolRegistry
from ._decorator import tool

log = logging.getLogger(__name__)

# 全局静音截止时间戳（0 = 未静音）
_mute_until_ts: float = 0.0
_lock = threading.Lock()

# hook：让主程序收到 mute / unmute 事件
_mute_hook: Optional[Callable[[bool], None]] = None


def set_mute_hook(fn: Optional[Callable[[bool], None]]) -> None:
    """由主程序注入：fn(muted: bool)。"""
    global _mute_hook
    _mute_hook = fn


def is_muted() -> bool:
    """当前是否处于静音窗口期。"""
    return time.time() < _mute_until_ts


def _notify():
    if callable(_mute_hook):
        try:
            _mute_hook(is_muted())
        except Exception:  # noqa: BLE001
            log.exception("mute hook failed")


def set_tts_mute_until(minutes: float = 0) -> str:
    """把 TTS 静音 minutes 分钟（最长 8 小时）。0 = 默认 30。"""
    if minutes is None or minutes == 0:
        minutes = 30.0
    try:
        m = float(minutes)
    except (TypeError, ValueError):
        return f"错误：minutes 必须是数字（{minutes!r}）"
    if m <= 0:
        return "错误：minutes 必须 > 0"
    m = min(m, 480)
    global _mute_until_ts
    with _lock:
        _mute_until_ts = time.time() + m * 60
    _notify()
    return f"已静音 TTS {m:g} 分钟（回复只显示气泡不朗读）"


def unmute_tts() -> str:
    """立即取消 TTS 静音。"""
    global _mute_until_ts
    with _lock:
        _mute_until_ts = 0.0
    _notify()
    return "已取消静音"


def set_tts_mute_until_threadsafe(minutes: float = 0) -> str:
    """线程安全版本：不回调 hook（hook 是 Qt 槽，跨线程调不安全）。

    给 ProactiveBrain 的 worker 线程用。状态本身会更新；UI 下一次操作时
    通过 is_muted() 自己感知新状态。如果当前没有 hook 被设置（仓库里
    目前就没有），行为跟普通 set_tts_mute_until 完全一致。
    """
    if minutes is None or minutes == 0:
        minutes = 30.0
    try:
        m = float(minutes)
    except (TypeError, ValueError):
        return f"错误：minutes 必须是数字（{minutes!r}）"
    if m <= 0:
        return "错误：minutes 必须 > 0"
    m = min(m, 480)
    global _mute_until_ts
    with _lock:
        _mute_until_ts = time.time() + m * 60
    return f"已静音 TTS {m:g} 分钟（回复只显示气泡不朗读）"


def get_tts_mute_status() -> str:
    """查询当前静音状态 / 剩余时间。"""
    global _mute_until_ts
    with _lock:
        until = _mute_until_ts
    if until == 0 or until <= time.time():
        return "当前未静音"
    remain = int((until - time.time()) / 60)
    return f"当前静音中，剩 ~{remain} 分钟"


def register(reg: ToolRegistry) -> None:
    """注册静音 / 取消静音工具。"""

    @tool(
        description="把 TTS 静音 N 分钟（最长 8 小时）。"
                    "静音期间桌宠回复只显示气泡，不朗读。"
                    "适合开会、自习、不想被打扰时用。",
        parameters={"type": "object",
                    "properties": {
                        "minutes": {"type": "number", "minimum": 0.1, "maximum": 480,
                                   "description": "静音分钟数，0 = 默认 30，最长 480"},
                    }},
    )
    def set_tts_mute_until(minutes: float = 0) -> str:
        """把 TTS 静音 minutes 分钟（最长 8 小时）。0 = 默认 30。"""
        if minutes is None or minutes == 0:
            minutes = 30.0
        try:
            m = float(minutes)
        except (TypeError, ValueError):
            return f"错误：minutes 必须是数字（{minutes!r}）"
        if m <= 0:
            return "错误：minutes 必须 > 0"
        m = min(m, 480)
        global _mute_until_ts
        with _lock:
            _mute_until_ts = time.time() + m * 60
        _notify()
        return f"已静音 TTS {m:g} 分钟（回复只显示气泡不朗读）"

    @tool(description="立即取消 TTS 静音。",
          parameters={"type": "object", "properties": {}})
    def unmute_tts() -> str:
        """立即取消 TTS 静音。"""
        global _mute_until_ts
        with _lock:
            _mute_until_ts = 0.0
        _notify()
        return "已取消静音"

    @tool(description="查询 TTS 静音状态。",
          parameters={"type": "object", "properties": {}})
    def get_tts_mute_status() -> str:
        """查询当前静音状态 / 剩余时间。"""
        global _mute_until_ts
        with _lock:
            until = _mute_until_ts
        if until == 0 or until <= time.time():
            return "当前未静音"
        remain = int((until - time.time()) / 60)
        return f"当前静音中，剩 ~{remain} 分钟"

    reg.register(set_tts_mute_until)
    reg.register(unmute_tts)
    reg.register(get_tts_mute_status)


__all__ = ["register", "is_muted", "set_mute_hook", "set_tts_mute_until_threadsafe"]