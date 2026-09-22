"""屏幕感知：获取当前活跃窗口信息，让桌宠了解用户正在做什么。"""
from __future__ import annotations

import ctypes
import logging
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class WindowInfo:
    """窗口信息。"""
    title: str
    process_name: str
    hwnd: int


class ScreenAware:
    """桌面窗口感知。"""

    def __init__(self):
        self._last_info: Optional[WindowInfo] = None
        self._last_check: float = 0.0
        self._cache_ttl = 2.0  # 2 秒缓存，避免频繁调用 ctypes

    def get_active_window(self) -> Optional[WindowInfo]:
        """获取当前活跃窗口信息（带缓存）。"""
        now = time.time()
        if (
            self._last_info is not None
            and now - self._last_check < self._cache_ttl
        ):
            return self._last_info
        try:
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            if not hwnd:
                self._last_check = now
                return None
            length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value

            # 获取进程名
            process_name = self._get_process_name(hwnd)

            info = WindowInfo(title=title, process_name=process_name, hwnd=hwnd)
            self._last_info = info
            self._last_check = now
            return info
        except Exception as e:
            log.debug("获取窗口信息失败：%s", e)
            return None

    def _get_process_name(self, hwnd: int) -> str:
        """通过 hwnd 获取进程名。"""
        try:
            pid = ctypes.c_ulong(0)
            ctypes.windll.user32.GetWindowThreadProcessId(
                hwnd, ctypes.byref(pid)
            )
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value
            )
            if not handle:
                return "unknown"
            try:
                buf = ctypes.create_unicode_buffer(260)
                size = ctypes.c_ulong(len(buf))
                ctypes.windll.kernel32.QueryFullProcessImageNameW(
                    handle, 0, buf, ctypes.byref(size)
                )
                name = buf.value.split("\\")[-1]
                return name.lower() if name else "unknown"
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            return "unknown"

    def get_context_summary(self) -> str:
        """生成当前屏幕上下文的简短描述（用于注入 system prompt）。"""
        info = self.get_active_window()
        if not info:
            return ""
        return f"用户当前正在使用 {info.process_name}，窗口标题：{info.title}"

    def clear_cache(self) -> None:
        """清除缓存，强制下次重新获取。"""
        self._last_info = None
        self._last_check = 0.0