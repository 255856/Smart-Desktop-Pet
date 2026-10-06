"""chat_window 子模块：窗口装饰相关辅助类。

- _TitleBarDrag：无边框窗口的鼠标拖动支持
- _InputFocusWatcher：输入框获焦时高亮边框（Qt QSS 不支持 :focus-within）
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.qt_compat import (
    QEvent, QFrame, QObject, QPoint, Qt,
    event_global_pos,
)

if TYPE_CHECKING:
    from app.ui.chat_window import ChatWindow


class _TitleBarDrag(QObject):
    """无边框窗口标题栏拖动：按住标题区 / 头像即可拖动整个窗口。"""

    def __init__(self, win: "ChatWindow"):
        super().__init__(win)
        self._win = win
        self._offset = QPoint()
        self._dragging = False

    def eventFilter(self, obj, ev) -> bool:
        t = ev.type()
        if t == QEvent.Type.MouseButtonPress and \
                ev.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._offset = (event_global_pos(ev)
                            - self._win.frameGeometry().topLeft())
            return True
        if t == QEvent.Type.MouseMove and self._dragging and \
                (ev.buttons() & Qt.MouseButton.LeftButton):
            self._win.move(event_global_pos(ev) - self._offset)
            return True
        if t == QEvent.Type.MouseButtonRelease:
            self._dragging = False
            return True
        return False


class _InputFocusWatcher(QObject):
    """输入框获得 / 失去焦点时高亮输入容器边框（Qt QSS 不支持 :focus-within）。"""

    def __init__(self, container: QFrame):
        super().__init__(container)
        self._container = container

    def eventFilter(self, obj, ev) -> bool:
        if ev.type() == QEvent.Type.FocusIn:
            self._container.setStyleSheet(
                "QFrame { background: #ffffff; border: 2px solid #a99bfa; "
                "border-radius: 16px; }")
        elif ev.type() == QEvent.Type.FocusOut:
            self._container.setStyleSheet("")
        return False


__all__ = ["_TitleBarDrag", "_InputFocusWatcher"]