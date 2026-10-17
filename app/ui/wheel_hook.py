"""Windows 低级鼠标钩子：让非激活桌宠也能收滚轮。"""
from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from typing import Callable, Optional

log = logging.getLogger(__name__)

WH_MOUSE_LL = 14
WM_MOUSEWHEEL = 0x020A
WM_MOUSEHWHEEL = 0x020E

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


# LRESULT CALLBACK LowLevelMouseProc(int nCode, WPARAM wParam, LPARAM lParam)
_HOOKPROC = ctypes.WINFUNCTYPE(
    ctypes.c_long, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)


def _signed_short(v: int) -> int:
    """HIWORD（有符号 16 位）→ Python 整数。"""
    v = v & 0xFFFF
    return v - 0x10000 if v >= 0x8000 else v


class WheelHook:
    """低级鼠标滚轮钩子。

    on_wheel(screen_x, screen_y, delta_y, delta_x) -> bool：
        返回 True 表示该滚轮已被桌宠消费（吞掉，不传给前台）；
        返回 False 表示放行。
    """

    def __init__(self, on_wheel: Callable[[int, int, int, int], bool]):
        self._on_wheel = on_wheel
        self._hook: Optional[int] = None
        self._proc = None  # 保持引用，否则被 GC 后回调崩溃

    def install(self) -> bool:
        if self._hook is not None:
            return True
        try:
            def _callback(n_code, w_param, l_param):
                # 任何异常都必须放行，绝不能卡住系统鼠标
                try:
                    if n_code >= 0 and w_param in (WM_MOUSEWHEEL, WM_MOUSEHWHEEL):
                        data = ctypes.cast(
                            l_param, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
                        if w_param == WM_MOUSEWHEEL:
                            dy = _signed_short(data.mouseData >> 16)
                            dx = 0
                        else:
                            dy = 0
                            dx = _signed_short(data.mouseData >> 16)
                        if dy != 0 or dx != 0:
                            if self._on_wheel(data.pt.x, data.pt.y, dy, dx):
                                return 1
                except Exception:  # noqa: BLE001
                    log.exception("滚轮钩子回调异常，已放行")
                return _user32.CallNextHookEx(self._hook, n_code, w_param, l_param)

            self._proc = _HOOKPROC(_callback)
            mod = _kernel32.GetModuleHandleW(None)
            self._hook = _user32.SetWindowsHookExW(
                WH_MOUSE_LL, self._proc, mod, 0)
            if not self._hook:
                log.warning("SetWindowsHookExW(WH_MOUSE_LL) 失败：%s",
                            ctypes.get_last_error())
                self._proc = None
                return False
            return True
        except Exception:  # noqa: BLE001
            log.exception("安装滚轮钩子失败")
            self._hook = None
            self._proc = None
            return False

    def uninstall(self) -> None:
        if self._hook is not None:
            try:
                _user32.UnhookWindowsHookEx(self._hook)
            except Exception:  # noqa: BLE001
                log.exception("卸载滚轮钩子失败")
            self._hook = None
            self._proc = None

    @property
    def installed(self) -> bool:
        return self._hook is not None
