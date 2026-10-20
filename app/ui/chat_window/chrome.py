"""chat_window 窗口装饰辅助类。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.qt_compat import (
    QCursor, QEvent, QObject, QPoint, Qt, QToolTip,
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


class _AvatarHoverHook(QObject):
    """鼠标悬浮头像时弹 tooltip 显示桌宠状态。

    之前头像只是个静态图, 鼠标移上去什么都看不到;
    现在拿最新的 PetState 拼 stats_summary, 不用切到桌宠本体就能瞄一眼当前
    (等级 / 体力 / 饱食 / 心情 / 健康 / 好感 / 金币)。

    不吞事件 (返回 False) —— 让 _TitleBarDrag 仍然能拖动标题栏。
    """

    def __init__(self, chat_window: "ChatWindow"):
        super().__init__(chat_window)
        self._cw = chat_window

    def eventFilter(self, obj, ev) -> bool:
        t = ev.type()
        if t == QEvent.Type.Enter:
            text = self._build_tip()
            if text:
                QToolTip.showText(QCursor.pos(), text, obj)
        elif t == QEvent.Type.Leave:
            QToolTip.hideText()
        return False

    def _build_tip(self) -> str:
        try:
            sp = getattr(self._cw, "_state_provider", None)
            if not callable(sp):
                return ""
            state = sp()
        except Exception:  # noqa: BLE001
            return ""
        try:
            summary = str(state.stats_summary())
        except Exception:  # noqa: BLE001
            return ""
        return summary or ""


__all__ = ["_TitleBarDrag", "_AvatarHoverHook"]