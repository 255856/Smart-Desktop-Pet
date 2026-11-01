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
    """给无边框 QWidget 加上 8 方向边缘缩放。

    只在 root 窗口装一次事件过滤器，不再递归到子控件；
    MouseMove 用 30ms 节流，光标状态走 cache 避免重复 setOverrideCursor；
    manual 缩放期间临时禁用 shadow effect / 透明背景以避免每帧重绘阴影。
    """

    EDGE = 10  # 边缘感应宽度（像素）

    def __init__(self, win: QWidget, freeze_translucent: bool = True):
        super().__init__(win)
        self._win = win
        # 缩放期间要不要临时关掉 WA_TranslucentBackground。
        # 半透明窗口（chat / memory_panel / werewolf）传 False —— 关了会整块变黑；
        # 不透明窗口（settings / 默认）保留 True，省一次 alpha 合成。
        self._freeze_translucent = freeze_translucent
        self._attached = False
        # 手动缩放兜底状态：(edges, 初始几何, 按下全局点)；None 表示走系统缩放
        self._manual = None
        self._cursor_key = -1
        self._last_move_ms = 0
        # 手动缩放节流：~33ms 最多 setGeometry 一次（30fps，肉眼跟手）
        self._last_apply_ms = 0
        # manual 缩放期间被临时禁用的 effect / flags
        self._saved_effects: list[tuple] = []
        # attach 时给所有 descendant 装 eventFilter,用于 child 上的 release
        # 兜底清 overrideCursor;事件流处理时会先看 obj 是否在 _win 内。
        self._children: set = set()
        # 标题栏缓存（None = 没查过 / False = 没有标题栏）
        self._title_bar_cache = None

    def attach(self) -> "FramelessResizer":
        if self._attached:
            return self
        self._attached = True
        self._win.setMouseTracking(True)
        self._win.installEventFilter(self)
        # 给所有 child 也装 eventFilter,只为接 release 事件清 overrideCursor。
        # 标题栏拖完窗口时 release 发生在 child 上(_TitleBarDrag 吞掉事件不冒泡),
        # 必须从这里兜底,否则缩放光标会卡住。
        self._children = set(self._win.findChildren(QWidget))
        for child in self._children:
            child.installEventFilter(self)
        return self

    # ---- 边缘命中 ----
    def _title_bar_rect(self):
        """标题栏（objectName='titlebar'）的全局矩形；没有就 None。

        标题栏是**拖动把手**：鼠标在上面应该是「拖窗口」而不是「拖上边缘」。
        边缘判断若把标题栏那一条也算成上边缘，两者会抢事件
        （owner 报的「边缘判断有点问题」）。
        """
        if self._title_bar_cache is not False:
            if self._title_bar_cache is None:
                bar = None
                for w in self._win.findChildren(QWidget):
                    if w.objectName() == "titlebar":
                        bar = w
                        break
                self._title_bar_cache = bar
            bar = self._title_bar_cache
            if bar is None:
                return None
            g = bar.mapToGlobal(bar.rect().topLeft())
            return (g.x(), g.y(), bar.width(), bar.height())
        return None

    def _edge_at(self, gpos: QPoint):
        r = self._win.frameGeometry()
        m = self.EDGE
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
        # 标题栏那条不算上边缘（标题栏是拖动把手）
        if edges & Qt.TopEdge:
            bar = self._title_bar_rect()
            if bar is not None:
                bx, by, bw, bh = bar
                if bx <= x <= bx + bw and by <= y <= by + bh:
                    edges &= ~Qt.TopEdge
        return edges

    # ---- 光标 ----
    def _clear_override_cursor(self) -> None:
        """清掉所有残留 overrideCursor,把光标恢复到默认。"""
        while QApplication.overrideCursor() is not None:
            QApplication.restoreOverrideCursor()
        self._cursor_key = -1

    def _set_cursor(self, edges) -> None:
        key = int(edges) if edges else 0
        if key == self._cursor_key:
            return
        # key 变了说明鼠标从边缘进入 / 离开 —— 先清掉所有 overrideCursor,
        # 防止之前因为 WA_TranslucentBackground 下 Leave 不触发而残留的
        # 缩放光标一直跟着鼠标(典型场景:标题栏拖完窗口光标卡缩放图标)
        while QApplication.overrideCursor() is not None:
            QApplication.restoreOverrideCursor()
        self._cursor_key = key
        shape = _cursor_for_edge(edges) if edges else None
        if shape is not None:
            QApplication.setOverrideCursor(shape)

    # ---- 缩放期视觉优化 ----
    def _freeze_heavy_paint(self) -> None:
        """缩放期间临时禁用 shadow effect + 透明背景。

        QGraphicsDropShadowEffect(blurRadius=48) 每次 resize 都重绘模糊阴影，
        是 frameless 窗口拖动卡顿的主要开销。缩放结束（release）时 _thaw 恢复。
        """
        if self._saved_effects:
            return
        # 禁用所有子控件的 GraphicsEffect
        for w in self._win.findChildren(QWidget):
            fx = w.graphicsEffect()
            if fx is not None:
                self._saved_effects.append((w, fx))
                w.setGraphicsEffect(None)
        # 临时关掉透明背景 —— 缩放期间不需要 alpha 通道，重绘路径更短
        # 半透明窗口（freeze_translucent=False）保留 WA_TranslucentBackground，
        # 否则缩放瞬间整块变黑。普通不透明窗口走默认 True 省一次 alpha 合成。
        if self._freeze_translucent and \
                self._win.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground):
            self._saved_effects.append(
                ("translucent", Qt.WidgetAttribute.WA_TranslucentBackground))
            self._win.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)

    def _thaw_heavy_paint(self) -> None:
        if not self._saved_effects:
            return
        for item in self._saved_effects:
            if isinstance(item, tuple) and item[0] == "translucent":
                self._win.setAttribute(item[1], True)
            else:
                w, fx = item
                try:
                    w.setGraphicsEffect(fx)
                except RuntimeError:
                    pass  # widget 已销毁
        self._saved_effects.clear()

    # ---- 手动缩放兜底 ----
    def _manual_move(self, gpos: QPoint, press0: QPoint, geo0) -> None:
        dx = gpos.x() - press0.x()
        dy = gpos.y() - press0.y()
        edges = self._manual[0]
        left, top = geo0.left(), geo0.top()
        width, height = geo0.width(), geo0.height()
        min_w = self._win.minimumWidth()
        min_h = self._win.minimumHeight()
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
        if edges & Qt.RightEdge:
            width = width + dx
            if width < min_w:
                width = min_w
        if edges & Qt.BottomEdge:
            height = height + dy
            if height < min_h:
                height = min_h
        # 几何没变就别 setGeometry：每一次 setGeometry 都是一次整窗重绘
        # （半透明窗口 + 长聊天内容 = 明显卡顿）
        cur = self._win.geometry()
        if (cur.left(), cur.top(), cur.width(), cur.height()) == (
                left, top, width, height):
            return
        self._win.setGeometry(left, top, width, height)

    # ---- 事件 ----
    def eventFilter(self, obj, ev):  # noqa: C901
        # child 上的 release 事件:不修改事件流,只兜底清 overrideCursor。
        if obj is not self._win:
            if ev.type() == QEvent.Type.MouseButtonRelease:
                self._clear_override_cursor()
            return False
        t = ev.type()

        if t == QEvent.Type.MouseButtonPress and \
                ev.button() == Qt.MouseButton.LeftButton:
            edges = self._edge_at(event_global_pos(ev))
            if edges:
                self._manual = (edges, self._win.geometry(),
                                event_global_pos(ev))
                if self._do_system_resize(edges):
                    self._manual = None
                else:
                    # 走 manual 路径：先冻结 shadow / 透明背景以提速
                    self._freeze_heavy_paint()
                return True
            return False

        if t == QEvent.Type.MouseMove:
            if self._manual is not None and \
                    (ev.buttons() & Qt.MouseButton.LeftButton):
                edges, geo0, press0 = self._manual
                now_ms = getattr(ev, "timestamp", lambda: 0)()
                if now_ms - self._last_apply_ms < 33:
                    return True
                self._last_apply_ms = now_ms
                self._manual_move(event_global_pos(ev), press0, geo0)
                return True
            # 鼠标按键空闲时才更新光标:拖标题栏期间(LeftButton 按下但没走 manual)
            # 不应把光标切到缩放图标,否则拖完窗口光标还是缩放图标。
            if ev.buttons() & Qt.MouseButton.LeftButton:
                return False
            now_ms = getattr(ev, "timestamp", lambda: 0)()
            if now_ms - self._last_move_ms < 30:
                return False
            self._last_move_ms = now_ms
            self._set_cursor(self._edge_at(event_global_pos(ev)))
            return False

        if t == QEvent.Type.MouseButtonRelease:
            if self._manual is not None:
                self._manual = None
                # 恢复 shadow / 透明背景，触发一次完整重绘
                self._thaw_heavy_paint()
                self._win.update()
            # 任意 release 都兜底清 overrideCursor(WA_TranslucentBackground 窗口
            # 在透明区域不会触发 Leave,标题栏拖完窗口后光标容易卡在缩放图标,
            # 这里强制重置一次)。
            self._clear_override_cursor()
            self._set_cursor(self._edge_at(event_global_pos(ev)))
            return True

        if t == QEvent.Type.Leave:
            self._set_cursor(None)
            return False

        if t == QEvent.Type.Enter:
            # Enter 兜底清 overrideCursor:鼠标从窗口外进窗口时重置一次。
            self._clear_override_cursor()
            return False
        return False

    def _do_system_resize(self, edges) -> bool:
        handle = self._win.windowHandle()
        if handle is not None and hasattr(handle, "startSystemResize"):
            try:
                return bool(handle.startSystemResize(edges))
            except Exception:  # noqa: BLE001
                return False
        return False