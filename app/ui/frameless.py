"""无边框窗口的边缘缩放组件。"""
from __future__ import annotations

from app.core.qt_compat import (
    QApplication, QEvent, QObject, QPoint, Qt, QWidget,
    event_global_pos,
)


def _cursor_for_edge(edges: "Qt.Edges"):
    """8 条边/角 → 系统缩放光标。"""
    left = bool(edges & Qt.LeftEdge)
    right = bool(edges & Qt.RightEdge)
    top = bool(edges & Qt.TopEdge)
    bottom = bool(edges & Qt.BottomEdge)
    if (left and top) or (right and bottom):
        return Qt.CursorShape.SizeFDiagCursor
    if (right and top) or (left and bottom):
        return Qt.CursorShape.SizeBDiagCursor
    if left or right:
        return Qt.CursorShape.SizeHorCursor
    if top or bottom:
        return Qt.CursorShape.SizeVerCursor
    return None


class FramelessResizer(QObject):
    """给无边框 QWidget 加上 8 方向边缘缩放。"""

    def __init__(self, win: QWidget, edge: int = 7):
        super().__init__(win)
        self._win = win
        self._edge = edge
        self._attached = False
        # 手动缩放兜底状态：(edges, 初始几何, 按下全局点)；None 表示走系统缩放
        self._manual = None
        self._cursor_key = -1  # 当前 override 光标对应的 edges 位

    # ---- 安装 ----
    def attach(self) -> "FramelessResizer":
        if self._attached:
            return self
        self._attached = True
        self._install(self._win)
        self._win.installEventFilter(self)
        return self

    def _install(self, w: QWidget) -> None:
        w.setMouseTracking(True)
        w.installEventFilter(self)
        for child in w.findChildren(QWidget):
            child.setMouseTracking(True)
            child.installEventFilter(self)

    # ---- 边缘命中 ----
    def _edge_at(self, gpos: QPoint):
        r = self._win.frameGeometry()
        m = self._edge
        x, y = gpos.x(), gpos.y()
        edges = Qt.Edges()
        if x <= r.left() + m:
            edges |= Qt.LeftEdge
        if x >= r.right() - m:
            edges |= Qt.RightEdge
        if y <= r.top() + m:
            edges |= Qt.TopEdge
        if y >= r.bottom() - m:
            edges |= Qt.BottomEdge
        return edges

    # ---- 光标 ----
    def _set_cursor(self, edges) -> None:
        key = int(edges) if edges else 0
        if key == self._cursor_key:
            return
        while QApplication.overrideCursor() is not None:
            QApplication.restoreOverrideCursor()
        self._cursor_key = key
        shape = _cursor_for_edge(edges) if edges else None
        if shape is not None:
            QApplication.setOverrideCursor(shape)

    # ---- 手动缩放兜底 ----
    def _manual_move(self, gpos: QPoint, press0: QPoint, geo0) -> None:
        dx = gpos.x() - press0.x()
        dy = gpos.y() - press0.y()
        edges = self._manual[0]
        left, top = geo0.left(), geo0.top()
        width, height = geo0.width(), geo0.height()
        min_w = self._win.minimumWidth()
        min_h = self._win.minimumHeight()
        # 左/上边缘：移动 origin 并反向改宽高，缩到最小时锁定 origin
        if edges & Qt.LeftEdge:
            width = width - dx
            if width < min_w:
                left = geo0.left() + (geo0.width() - min_w)
                width = min_w
            else:
                left = geo0.left() + dx
        if edges & Qt.TopEdge:
            height = height - dy
            if height < min_h:
                top = geo0.top() + (geo0.height() - min_h)
                height = min_h
            else:
                top = geo0.top() + dy
        # 右/下边缘：只改宽高
        if edges & Qt.RightEdge:
            width = width + dx
            if width < min_w:
                width = min_w
        if edges & Qt.BottomEdge:
            height = height + dy
            if height < min_h:
                height = min_h
        self._win.setGeometry(left, top, width, height)

    # ---- 事件 ----
    def eventFilter(self, obj, ev):  # noqa: C901
        t = ev.type()
        # 动态子控件（QScrollArea / Tab 切换后才创建的控件）
        if t == QEvent.Type.ChildAdded:
            child = getattr(ev, "child", None)
            if isinstance(child, QWidget):
                self._install(child)
            return False

        if t == QEvent.Type.MouseButtonPress and \
                ev.button() == Qt.MouseButton.LeftButton:
            edges = self._edge_at(event_global_pos(ev))
            if edges:
                # 先准备好手动兜底数据
                self._manual = (edges, self._win.geometry(),
                                event_global_pos(ev))
                if self._do_system_resize(edges):
                    self._manual = None
                return True  # 命中边缘：吞掉，标题拖拽不再触发
            return False

        if t == QEvent.Type.MouseMove:
            if self._manual is not None and \
                    (ev.buttons() & Qt.MouseButton.LeftButton):
                edges, geo0, press0 = self._manual
                self._manual_move(event_global_pos(ev), press0, geo0)
                return True
            # 非拖拽：仅更新光标
            self._set_cursor(self._edge_at(event_global_pos(ev)))
            return False

        if t == QEvent.Type.MouseButtonRelease and self._manual is not None:
            self._manual = None
            return True

        if t == QEvent.Type.Leave and obj is self._win:
            self._set_cursor(None)
        return False

    def _do_system_resize(self, edges) -> bool:
        handle = self._win.windowHandle()
        if handle is not None and hasattr(handle, "startSystemResize"):
            try:
                return bool(handle.startSystemResize(edges))
            except Exception:  # noqa: BLE001
                return False
        return False
