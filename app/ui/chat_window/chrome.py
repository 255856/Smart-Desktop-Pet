"""chat_window 子模块：窗口装饰相关辅助类。

- _TitleBarDrag：无边框窗口的鼠标拖动支持

历史：`_InputFocusWatcher` 在 FocusIn 时把输入容器边框改成紫色 (#a99bfa)，
主人 2026-10-15 实测：「每次点击输入框会有一个紫色线圈不需要」，
所以把整个 watcher 删了，输入容器边框永远保持中性灰。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.qt_compat import (
    QEvent, QObject, QPoint, Qt,
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


__all__ = ["_TitleBarDrag"]