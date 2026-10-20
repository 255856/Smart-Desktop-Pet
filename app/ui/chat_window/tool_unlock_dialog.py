"""工具解锁对话框：顶部等级路线轴 + 解锁进度 + 逐级工具明细。

布局用 FlowLayout（按 chip 实际宽度自动换行），不用固定列数，
避免长工具名（MCP 的 mcp_aw_aw_get_recent_activities 之类）被裁切或溢出。
"""
from __future__ import annotations

import re

from typing import Optional

from app.core.qt_compat import (
    QBrush, QColor, QDialog, QFont, QFontMetrics, QFrame, QHBoxLayout, QLabel,
    QLayout, QPainter, QPen, QPushButton, QRect, QScrollArea, QSize, QSizePolicy,
    QToolButton, QToolTip, QVBoxLayout, QWidget, Qt, Signal, event_global_pos,
    event_local_pos,
)


# ---------------------------------------------------------------- FlowLayout
class FlowLayout(QLayout):
    """按控件实际宽度自动换行的横排布局。

    QHBoxLayout 不支持换行，QGridLayout 需要预先知道列数。工具名长度差异很大
    （web_search 10 字符 vs aw_get_recent_activities 24 字符），固定列数必然
    出错 —— 会把长名裁掉或挤出边界，所以按 sizeHint 逐个塞，放不下就换行。

    实现参考 Qt 官方 "Flow Layout" 示例：接管 geometry 分配，
    不往 QLayout 里 addWidget（那会触发 QHBoxLayout 的自动布局）。
    """

    def __init__(self, parent=None, spacing: int = 5) -> None:
        super().__init__(parent)
        self._items: list = []
        self._h = 0
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(spacing)

    # ---- QLayout 接口 ----
    def addItem(self, item) -> None:  # noqa: N802 (Qt 命名)
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, idx):  # noqa: N802 (Qt 命名)
        return self._items[idx] if 0 <= idx < len(self._items) else None

    def takeAt(self, idx):  # noqa: N802 (Qt 命名)
        return self._items.pop(idx) if 0 <= idx < len(self._items) else None

    def expandingDirections(self):  # noqa: N802 (Qt 命名)
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 (Qt 命名)
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 (Qt 命名)
        return self._calc(width)[0]

    def setGeometry(self, rect) -> None:  # noqa: N802 (Qt 命名)
        super().setGeometry(rect)
        h, _rows = self._calc(rect.width())
        self._h = h
        x = rect.x()
        y = rect.y()
        line_h = 0
        for item in self._items:
            w = item.sizeHint().width()
            ih = item.sizeHint().height()
            if x + w > rect.right() + 1 and x > rect.x():
                x = rect.x()
                y = y + line_h + self.spacing()
                line_h = 0
            item.setGeometry(
                QRect(x, y, w, max(ih, line_h) if line_h else ih))
            x = x + w + self.spacing()
            line_h = max(line_h, ih)

    def sizeHint(self):  # noqa: N802 (Qt 命名)
        return self.minimumSize()

    def minimumSize(self):  # noqa: N802 (Qt 命名)
        sz = QSize()
        for item in self._items:
            sz = sz.expandedTo(item.sizeHint())
        m = self.contentsMargins()
        return QSize(sz.width() + m.left() + m.right(),
                     max(self._h, sz.height()) + m.top() + m.bottom())

    # ---- 内部 ----
    def _calc(self, width: int) -> tuple[int, int]:
        """按宽度算 (总高, 行数)。"""
        m = self.contentsMargins()
        avail = max(1, width - m.left() - m.right())
        x = m.left()
        y = m.top()
        line_h = 0
        rows = 1
        for item in self._items:
            w = item.sizeHint().width()
            h = item.sizeHint().height()
            if x + w > avail + m.left() and x > m.left():
                x = m.left()
                y += line_h + self.spacing()
                line_h = 0
                rows += 1
            x += w + self.spacing()
            line_h = max(line_h, h)
        return y + line_h + m.bottom(), rows


# ---------------------------------------------------------------- 风险配色
_RISK_COLOR = {
    "SAFE": "#2f8f1e",
    "LOW": "#1f6fc4",
    "MEDIUM": "#b8860b",
    "HIGH": "#c2521a",
    "IRREVERSIBLE": "#c02626",
    "UNKNOWN": "#7a7a8c",
}
_RISK_LABEL = {
    "SAFE": "只读",
    "LOW": "轻改",
    "MEDIUM": "中等",
    "HIGH": "高危",
    "IRREVERSIBLE": "危险",
    "UNKNOWN": "扩展",
}


def _risk_color(risk: str) -> str:
    return _RISK_COLOR.get(risk, _RISK_COLOR["UNKNOWN"])


def _risk_label(risk: str) -> str:
    return _RISK_LABEL.get(risk, "扩展")


# ---------------------------------------------------------------- 工具功能说明
_MD_MARK = re.compile(r"[*`]")


def _short_desc(text: str, limit: int = 110) -> str:
    """把工具 description 压成 tooltip 里读得完的一句话。

    description 是写给 LLM 看的，里面混着 **粗体**、参数说明和换行，直接塞进
    Qt tooltip 会变成一大坨加粗号的黄框。这里先剥掉 Markdown 标记、压空白，
    再优先在句号/分号处收，超长才硬截断。
    """
    s = _MD_MARK.sub("", text or "").replace("\r", " ").replace("\n", " ")
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return "暂无功能说明"
    for sep, tail in (("。", "。"), (";", "；"), (";", "；"), (". ", ".")):
        idx = s.find(sep)
        if 18 <= idx <= limit:
            return s[:idx] + tail
    if len(s) <= limit:
        return s
    return s[:limit].rstrip() + "…"


# ---------------------------------------------------------------- 等级路线轴
class LevelTimeline(QWidget):
    """10 级解锁路线的 X 轴总览。

    轴上排 10 个节点，奇数级标在轴上方、偶数级标在下方交错排布 —— 同一侧只
    放 5 个，每个标签分到的横向宽度翻倍，中文奖励标题才不会被截没。

    悬浮节点弹 tooltip（完整标题 + 工具数 + 解锁状态），点击节点把下方卡片
    列表滚到对应等级。
    """

    levelClicked = Signal(int)

    _MARGIN = 30
    _AXIS_Y = 60
    _HEIGHT = 116

    _STATE_STYLE = {
        # 状态: (节点色, 文字色, 光晕色)
        "done": ("#5aa845", "#3f7a34", "#e2f0dc"),
        "current": ("#e8a33d", "#b06f10", "#fbeacb"),
        "locked": ("#c6c5d2", "#8f8f9e", "#eeedf3"),
    }
    _STATE_TEXT = {"done": "已解锁", "current": "当前等级", "locked": "未解锁"}

    def __init__(self, levels: list[tuple[int, str, int, str]],
                 parent=None) -> None:
        """
        levels: [(等级, 奖励标题, 该级工具数, 状态)]，状态取 done/current/locked
        """
        super().__init__(parent)
        self._levels = levels
        self._hover = -1
        self._px: list[int] = []
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(self._HEIGHT)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    # ---- 绘制 ----
    def paintEvent(self, _evt) -> None:  # noqa: N802 (Qt 命名)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        n = len(self._levels)
        if n == 0:
            return
        w, y_ax = self.width(), self._AXIS_Y
        step = (w - 2 * self._MARGIN) / (n - 1) if n > 1 else 0
        self._px = [int(self._MARGIN + i * step) for i in range(n)]

        # 轴线：已解锁段实色，锁定段浅灰
        cur_x = self._px[min(max(self._cur_index(), 0), n - 1)]
        p.setPen(QPen(QColor("#b9dcac"), 3))
        p.drawLine(self._px[0], y_ax, cur_x, y_ax)
        p.setPen(QPen(QColor("#e2e1ea"), 3))
        p.drawLine(cur_x, y_ax, self._px[-1], y_ax)

        base = self.font()
        for i, (lvl, title, _cnt, state) in enumerate(self._levels):
            x = self._px[i]
            dot_c, txt_c, halo_c = self._STATE_STYLE.get(
                state, self._STATE_STYLE["locked"])
            above = (i % 2 == 0)          # Lv.1 / Lv.3 / … 在轴上方
            is_current = (state == "current")

            if is_current or i == self._hover:
                r = 16 if is_current else 12
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(QColor(halo_c)))
                p.drawEllipse(x - r, y_ax - r, r * 2, r * 2)

            # 节点圆点：已解锁实心 + 白心，当前等级描边加粗
            r = 8 if is_current else 5
            p.setPen(QPen(QColor(dot_c), 3 if is_current else 2))
            p.setBrush(QBrush(QColor(dot_c if state == "done" else "#ffffff")))
            p.drawEllipse(x - r, y_ax - r, r * 2, r * 2)
            if state == "done":
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QBrush(QColor("#ffffff")))
                p.drawEllipse(x - 2, y_ax - 2, 4, 4)

            # 引线
            p.setPen(QPen(QColor(dot_c), 1))
            p.drawLine(x, y_ax - 10 if above else y_ax + 10,
                       x, y_ax - 18 if above else y_ax + 18)

            # 两行文字：Lv.N + 奖励标题，横向 clamp 在控件内
            lw = max(int(step) - 2, 48) if step else w
            lx = max(0, min(x - lw // 2, w - lw))
            if above:
                lv_rect, ti_rect = QRect(lx, 14, lw, 16), QRect(lx, 30, lw, 14)
            else:
                lv_rect, ti_rect = QRect(lx, 80, lw, 16), QRect(lx, 96, lw, 14)

            f_lv = QFont(base)
            f_lv.setPointSize(9)
            f_lv.setBold(True)
            p.setFont(f_lv)
            p.setPen(QColor(txt_c))
            p.drawText(lv_rect, Qt.AlignmentFlag.AlignCenter, f"Lv.{lvl}")

            f_ti = QFont(base)
            f_ti.setPointSize(8)
            f_ti.setBold(is_current)
            p.setFont(f_ti)
            p.setPen(QColor(txt_c))
            p.drawText(ti_rect, Qt.AlignmentFlag.AlignCenter,
                       _elide(title, f_ti, lw - 8))

    # ---- 交互 ----
    def mouseMoveEvent(self, evt) -> None:  # noqa: N802
        idx = self._nearest(event_local_pos(evt).x())
        if idx != self._hover:
            self._hover = idx
            self.update()
        if idx >= 0:
            lvl, title, cnt, state = self._levels[idx]
            QToolTip.showText(
                event_global_pos(evt),
                f"Lv.{lvl}　{title}\n{cnt} 个工具 · {self._STATE_TEXT.get(state, '')}",
                self)

    def mousePressEvent(self, evt) -> None:  # noqa: N802
        idx = self._nearest(event_local_pos(evt).x())
        if idx >= 0:
            self.levelClicked.emit(self._levels[idx][0])

    def mouseLeaveEvent(self, _evt) -> None:  # noqa: N802
        if self._hover != -1:
            self._hover = -1
            self.update()
        QToolTip.hideText()

    # ---- 内部 ----
    def _cur_index(self) -> int:
        for i, item in enumerate(self._levels):
            if item[3] == "current":
                return i
        return 0

    def _nearest(self, x: int) -> int:
        if not self._px:
            return -1
        best, best_d = 0, 1 << 30
        for i, px in enumerate(self._px):
            d = abs(px - x)
            if d < best_d:
                best, best_d = i, d
        half = (self._px[1] - self._px[0]) // 2 if len(self._px) > 1 else 30
        return best if best_d <= max(half, 20) else -1


def _elide(text: str, font: "QFont", width: int) -> str:
    return QFontMetrics(font).elidedText(
        text or "", Qt.TextElideMode.ElideRight, max(20, width))


def build_tool_unlock_dialog(
    *,
    level: int,
    unlocked_display: set[str],
    level_to_tools: dict[int, list[tuple[str, str, str]]],
    rewards: dict[int, str],
    celebrate: dict[int, str],
    parent: Optional[QWidget] = None,
) -> QDialog:
    """构造工具解锁对话框。

    level: 当前等级
    unlocked_display: 当前等级已解锁的工具**显示名**集合
                      (MCP 用 short_mcp_name 后的短名，与 level_to_tools 的 key 一致)
    level_to_tools: {等级: [(显示名, 风险等级名, 功能说明), ...]}
    rewards: {等级: 奖励标题}
    celebrate: {等级: 真实升级时的祝贺语}
    """
    dlg = QDialog(parent)
    dlg.setWindowTitle("工具解锁")
    dlg.setMinimumSize(860, 640)
    dlg.resize(920, 700)
    dlg.setObjectName("unlock_root")
    # v37: 改用 ui_style.UNLOCK_QSS (GOLD/MINT/LOCKED 三态 + 渐变背景),
    # 跟主系统 token 一致, 4 个窗现在都走同一套颜色定义
    from app.ui import ui_style
    dlg.setStyleSheet(ui_style.UNLOCK_QSS)
    # 窗口类型: Tool (浮动工具窗, 不在任务栏) + WindowStaysOnTopHint (始终最上层)。
    # 跟主聊天窗 / 设置窗 / 桌宠窗都是普通 Window 类型, 这里特意不一样 —— 一眼能分辨。
    # 注意: setWindowFlags 会清掉 setWindowModality, 必须先设 flags 再设 modality。
    dlg.setWindowFlags(
        Qt.WindowType.Window
        | Qt.WindowType.Tool
        | Qt.WindowType.WindowStaysOnTopHint
    )
    dlg.setWindowModality(Qt.WindowModality.ApplicationModal)

    root = QVBoxLayout(dlg)
    root.setContentsMargins(0, 0, 0, 0)
    root.setSpacing(0)

    # ---- 自定义标题栏 (无边框窗口没系统标题栏)
    # 跟 chat_window / settings_window 一致: 标题 + 副标题 (左) | min/max/close (右)
    # 也可按住拖动整窗
    from app.ui.chat_window.chrome import _TitleBarDrag
    titlebar = QFrame()
    titlebar.setObjectName("unlock_titlebar")
    titlebar.setFixedHeight(46)
    thbox = QHBoxLayout(titlebar)
    thbox.setContentsMargins(18, 8, 6, 8)
    thbox.setSpacing(8)

    title_col = QVBoxLayout()
    title_col.setSpacing(0)
    title = QLabel("工具解锁")
    title.setObjectName("header_title")
    title_col.addWidget(title)
    subtitle = QLabel("每升 1 级解锁新工具")
    subtitle.setObjectName("header_subtitle")
    title_col.addWidget(subtitle)
    thbox.addLayout(title_col, 1)

    btn_min = QToolButton()
    btn_min.setText("─")
    btn_min.setObjectName("title_btn_min")
    btn_min.setToolTip("最小化")
    btn_min.setFixedSize(30, 28)
    btn_min.clicked.connect(dlg.showMinimized)

    btn_max = QToolButton()
    btn_max.setText("▢")
    btn_max.setObjectName("title_btn_max")
    btn_max.setToolTip("最大化")
    btn_max.setFixedSize(30, 28)
    def _toggle_max() -> None:
        if dlg.isMaximized():
            dlg.showNormal()
            btn_max.setText("▢")
            btn_max.setToolTip("最大化")
        else:
            dlg.showMaximized()
            btn_max.setText("❐")
            btn_max.setToolTip("还原")
    btn_max.clicked.connect(_toggle_max)

    btn_close = QToolButton()
    btn_close.setText("X")
    btn_close.setObjectName("title_btn_close")
    btn_close.setToolTip("关闭")
    btn_close.setFixedSize(30, 28)
    btn_close.clicked.connect(dlg.reject)

    thbox.addWidget(btn_min)
    thbox.addWidget(btn_max)
    thbox.addWidget(btn_close)
    root.addWidget(titlebar)
    # 标题栏可拖动整窗 (复用 chat_window 的 _TitleBarDrag)
    titlebar.installEventFilter(_TitleBarDrag(dlg))

    # 内容区沿用主系统的 UNLOCK_QSS, 内容距顶 14 像素
    content = QFrame()
    content_layout = QVBoxLayout(content)
    content_layout.setContentsMargins(22, 14, 22, 14)
    content_layout.setSpacing(8)
    root.addWidget(content, 1)

    # 风险图例
    legend = QHBoxLayout()
    legend.setSpacing(10)
    legend.addWidget(QLabel("工具风险："))
    for rk in ("SAFE", "LOW", "MEDIUM", "HIGH", "IRREVERSIBLE"):
        c = _risk_color(rk)
        dot = QLabel(f"{_risk_label(rk)}")
        dot.setStyleSheet(
            f"color: {c}; font-size: 8pt; border: 1px solid {c};"
            " border-radius: 8px; padding: 1px 7px; background: #ffffff;")
        dot.setToolTip(rk)
        dot.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        legend.addWidget(dot)
    legend.addStretch(1)
    content_layout.addLayout(legend)

    # ---- 等级路线轴（10 级总览，奇偶级上下交错）----
    timeline_levels = []
    for lvl in sorted(level_to_tools.keys()):
        state = ("current" if lvl == level
                 else ("done" if lvl < level else "locked"))
        timeline_levels.append(
            (int(lvl), rewards.get(int(lvl), ""),
             len(level_to_tools.get(lvl, [])), state))
    timeline = LevelTimeline(timeline_levels)
    content_layout.addWidget(timeline)

    # ---- 进度 ----
    total_cfg = sum(len(v) for v in level_to_tools.values())
    total_tools = total_cfg or len(unlocked_display)
    # level_to_tools 里存的是显示名（MCP 用短名），所以这里也用显示名集合计数
    got = sum(1 for names in level_to_tools.values() for n, _, _ in names
              if n in unlocked_display)

    prog_row = QHBoxLayout()
    prog_row.setSpacing(10)
    prog_text = QLabel(f"当前等级  Lv.{level} / 10    已解锁  {got} / {total_tools}  个工具")
    prog_text.setObjectName("progress_text")
    prog_row.addWidget(prog_text)
    prog_row.addStretch(1)
    content_layout.addLayout(prog_row)

    # 进度条（用 QLabel 做纯 CSS 条，避免额外依赖 QProgressBar 样式）
    bar_outer = QFrame()
    bar_outer.setFixedHeight(8)
    bar_outer.setStyleSheet(
        "QFrame { background: #e4e1ee; border: none; border-radius: 4px; }")
    bar_layout = QHBoxLayout(bar_outer)
    bar_layout.setContentsMargins(0, 0, 0, 0)
    bar_layout.setSpacing(0)
    pct = (got / total_tools * 100.0) if total_tools else 0.0
    if pct > 0:
        # fill 必须内联上色：QLabel 是 QFrame 子类，bar_outer 的 `QFrame {...}`
        # 会一起染灰它，优先级高于对话框里的 QLabel#progress_fill
        fill = QLabel()
        fill.setFixedHeight(8)
        fill.setStyleSheet(
            "QLabel { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,"
            " stop:0 #8b7ae8, stop:1 #6d5cd0); border-radius: 4px; }")
        bar_layout.addWidget(fill, max(1, int(pct)))
    bar_layout.addStretch(1)
    content_layout.addWidget(bar_outer)

    next_lv = level + 1 if level < 10 else None
    if next_lv is not None:
        nxt = level_to_tools.get(next_lv, [])
        names = ", ".join(n for n, _, _ in nxt[:5])
        more = f" 等 {len(nxt)} 个" if len(nxt) > 5 else (" 个" if nxt else "")
        hint = QLabel(
            f"下一级 Lv.{next_lv} 解锁 {len(nxt)}{more}"
            + (f"：{names}" if names else "")
        )
        hint.setObjectName("next_hint")
        hint.setWordWrap(True)
        root.addWidget(hint)
    else:
        hint = QLabel("已满级，全部能力（含危险工具）均已解锁。")
        hint.setObjectName("next_hint")
        root.addWidget(hint)

    # ---- 卡片列表 ----
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setStyleSheet("""
        QScrollArea { border: none; background: transparent; }
        QScrollArea > QWidget > QWidget { background: transparent; }
    """)
    inner = QWidget()
    inner.setStyleSheet("background: transparent;")
    cards = QVBoxLayout(inner)
    cards.setContentsMargins(2, 6, 8, 6)
    cards.setSpacing(8)

    for lvl in sorted(level_to_tools.keys()):
        tools = level_to_tools.get(lvl, [])
        unlocked_now = lvl <= level
        is_current = lvl == level
        if is_current:
            card = QFrame(); card.setObjectName("card_current")
            lv_key, badge_bg, badge_txt, badge_bold = (
                "lv_current", "#e8a33d", "当前等级", "700")
        elif unlocked_now:
            card = QFrame(); card.setObjectName("card_done")
            lv_key, badge_bg, badge_txt, badge_bold = (
                "lv_done", "#5aa845", "已解锁", "400")
        else:
            card = QFrame(); card.setObjectName("card_locked")
            lv_key, badge_bg, badge_txt, badge_bold = (
                "lv_locked", "#a8a8b8", "未解锁", "400")
        card.setObjectName(f"{card.objectName()}_lv{int(lvl)}")

        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 9, 14, 11)
        cl.setSpacing(6)

        head = QHBoxLayout()
        head.setSpacing(8)
        lv = QLabel(f"Lv.{lvl}")
        lv.setObjectName(lv_key)
        head.addWidget(lv)
        # 徽章用内联 setStyleSheet —— QSS 里 #badge_* 规则会被 chip 的全局规则干扰
        badge = QLabel(badge_txt)
        badge.setStyleSheet(
            f"background: {badge_bg}; color: #ffffff; font-size: 8pt;"
            f" font-weight: {badge_bold}; border-radius: 8px; padding: 2px 9px;")
        badge.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        head.addWidget(badge)

        reward_txt = rewards.get(lvl, "")
        if reward_txt:
            rw = QLabel(reward_txt)
            rw.setObjectName(
                "reward_current" if is_current
                else ("reward_done" if unlocked_now else "reward_locked"))
            head.addWidget(rw)
        head.addStretch(1)
        cnt = QLabel(f"{len(tools)} 个工具")
        cnt.setObjectName("cnt")
        head.addWidget(cnt)
        cl.addLayout(head)

        if tools:
            flow = FlowLayout(spacing=5)
            for nm, risk, desc in tools:
                chip = QLabel(nm)
                chip.setObjectName("chip")
                c = _risk_color(risk)
                chip.setStyleSheet(
                    f"QLabel#chip {{ font-size: 9pt; padding: 3px 8px;"
                    f" border-radius: 9px; background: #ffffff;"
                    f" border: 1px solid {c}; color: {c}; }}")
                chip.setToolTip(f"{nm}\n{_short_desc(desc)}\n"
                                f"风险：{_risk_label(risk)}")
                chip.setSizePolicy(QSizePolicy.Policy.Fixed,
                                   QSizePolicy.Policy.Fixed)
                flow.addWidget(chip)
            holder = QWidget()
            holder.setLayout(flow)
            holder.setStyleSheet("background: transparent;")
            holder.setSizePolicy(QSizePolicy.Policy.Preferred,
                                 QSizePolicy.Policy.MinimumExpanding)
            cl.addWidget(holder)

        if unlocked_now:
            msg = celebrate.get(lvl, "")
            if msg:
                m = QLabel(msg)
                m.setObjectName("celebrate")
                m.setWordWrap(True)
                cl.addWidget(m)

        cards.addWidget(card)

    cards.addStretch(1)
    scroll.setWidget(inner)
    content_layout.addWidget(scroll, 1)

    # 点路线轴节点 → 卡片列表滚到对应等级
    def _jump_to_level(lv: int) -> None:
        target = inner.findChild(QWidget, f"card_done_lv{lv}") \
            or inner.findChild(QWidget, f"card_current_lv{lv}") \
            or inner.findChild(QWidget, f"card_locked_lv{lv}")
        if target is not None:
            scroll.ensureWidgetVisible(target, 0, 24)

    timeline.levelClicked.connect(_jump_to_level)

    # ---- 关闭 ----
    btn_row = QHBoxLayout()
    btn_row.addStretch(1)
    close_btn = QPushButton("关闭")
    close_btn.setObjectName("close_btn")
    close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
    close_btn.clicked.connect(dlg.accept)
    btn_row.addWidget(close_btn)
    content_layout.addLayout(btn_row)

    return dlg