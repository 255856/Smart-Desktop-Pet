"""无边框窗口的标题栏拖动。

聊天窗和各个弹窗外壳（工具解锁 / 头像选择）都要能按住标题栏拖窗口，
逻辑一样，抽到这里共用。
"""
from __future__ import annotations

from app.core.qt_compat import QEvent, QObject, QPoint, Qt, event_global_pos


class TitleBarDrag(QObject):
    """按住标题区拖动整个窗口。"""

    def __init__(self, win):
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


__all__ = ["TitleBarDrag"]
