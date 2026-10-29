# -*- coding: utf-8 -*-
"""跨线程调用主线程（Qt 控件只能在主线程碰）。

控制服务是 HTTP 线程，它要改桌宠的界面（缩放、透明度、置顶…）——
直接改会崩：Qt 控件不是线程安全的。

做法：主线程里放一个 QObject，用 Signal 把活儿投递回去，等它干完再回话。
"""
from __future__ import annotations

import logging
import threading

from app.core.qt_compat import QObject, Signal

log = logging.getLogger(__name__)

#: 一次调用最多等多久（主线程卡住时不至于把 HTTP 线程挂死）
DEFAULT_TIMEOUT = 30.0


class MainThreadInvoker(QObject):
    """把一个可调用对象丢回主线程执行，并等结果。"""

    #: 内部用：带一个可调用对象
    invoke = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.invoke.connect(self._exec)

    def _exec(self, fn) -> None:
        # 这个槽在主线程跑（QObject 的线程亲和性 = 主线程）
        try:
            fn()
        except Exception:  # noqa: BLE001
            log.exception("主线程调用失败")

    def call(self, fn, timeout: float = DEFAULT_TIMEOUT):
        """在主线程跑 ``fn()``，返回它的结果。

        主线程抛的异常原样抛回来（HTTP 层会转成错误响应）。
        """
        box: dict = {}
        done = threading.Event()

        def _run():
            try:
                box["value"] = fn()
            except Exception as e:  # noqa: BLE001
                box["error"] = e
            finally:
                done.set()

        self.invoke.emit(_run)
        if not done.wait(timeout):
            raise TimeoutError("主线程忙，等超时了")
        if "error" in box:
            raise box["error"]
        return box.get("value")
