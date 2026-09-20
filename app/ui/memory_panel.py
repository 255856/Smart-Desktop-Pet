# -*- coding: utf-8 -*-
"""长期记忆管理面板：搜索 / 类别筛选 / 新增 / 删除。

从聊天窗标题栏「记忆」按钮或托盘菜单打开。直接操作 MemoryStore（增删实时落盘），
增删后发出 changed 信号，让聊天窗刷新副标题的「N 记忆」计数。

列表走 UI 层子串过滤 + 类别筛选（不调用 memory.search，避免浏览时 touch 改变
记忆热度、也避免每次按键都重算语义检索）。
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from app.core.qt_compat import (
    QComboBox, QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QLineEdit,
    QColor, QObject, QEvent, QPushButton, QScrollArea, QSizePolicy, Qt,
    QToolButton, QVBoxLayout, QWidget, QDialog, Signal,
)
from app.ui import ui_style

log = logging.getLogger(__name__)

# 记忆类别 id -> 中文标签
CATEGORY_LABELS = {
    "preference": "偏好",
    "fact": "事实",
    "event": "事件",
    "skill": "技能",
    "person": "人物",
    "other": "其他",
}
# 类别小标签配色（文字 / 底色 / 边框）
CATEGORY_COLORS = {
    "preference": ("#be185d", "#fce7f3", "#fbcfe8"),
    "fact":       ("#1d4ed8", "#dbeafe", "#bfdbfe"),
    "event":      ("#b45309", "#fef3c7", "#fde68a"),
    "skill":      ("#047857", "#d1fae5", "#a7f3d0"),
    "person":     ("#6d28d9", "#ede9fe", "#ddd6fe"),
    "other":      ("#475569", "#f1f5f9", "#e2e8f0"),
}

# 重要性档位：显示名 -> 数值
IMPORTANCE_LEVELS = (
    ("低 ★", 0.3),
    ("中 ★★", 0.5),
    ("高 ★★★", 0.8),
)

_MEMORY_QSS = f"""
QWidget {{
    font-family: {ui_style.FONT};
    font-size: 10pt;
    color: {ui_style.TEXT};
}}
QWidget#memory_root {{ background: transparent; }}
QLineEdit {{
    background: #ffffff;
    border: 1.5px solid #e3e1f0;
    border-radius: 10px;
    padding: 7px 12px;
    font-size: 10pt;
    selection-background-color: #c4b5fd;
}}
QLineEdit:focus {{ border-color: {ui_style.ACCENT_LT}; background: #fefeff; }}
QComboBox {{
    background: #ffffff;
    border: 1.5px solid #e3e1f0;
    border-radius: 10px;
    padding: 6px 10px;
    font-size: 9pt;
    color: {ui_style.TEXT};
}}
QComboBox:hover {{ border-color: {ui_style.ACCENT_LT}; }}
QComboBox QAbstractItemView {{
    background: #fff; border: 1px solid #e3e1f0;
    selection-background-color: {ui_style.ACCENT_BG};
    selection-color: {ui_style.ACCENT_DK};
    outline: none;
}}
QPushButton {{
    background: #ffffff;
    border: 1.5px solid #e0e0ee;
    border-radius: 10px;
    padding: 7px 16px;
    font-weight: 600;
    font-size: 9pt;
}}
QPushButton:hover {{ border-color: {ui_style.ACCENT_LT}; color: {ui_style.ACCENT_DK}; background: {ui_style.ACCENT_BG}; }}
QPushButton#add_btn {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #a78bfa, stop:1 #7c6cf0);
    color: #fff; border: none; padding: 7px 22px; border-radius: 10px;
}}
QPushButton#add_btn:hover {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #9a7ef8, stop:1 #6a58e0); }}
QPushButton#del_btn {{
    background: transparent; border: none; color: #c2c2d0;
    font-size: 13pt; font-weight: 700; padding: 2px 8px;
}}
QPushButton#del_btn:hover {{ color: #ef4444; background: #fee2e2; border-radius: 8px; }}
QScrollArea#mem_scroll {{ background: transparent; border: none; }}
QWidget#mem_list_host {{ background: transparent; }}
QFrame#mem_row {{
    background: #ffffff;
    border: 1px solid #eceaf5;
    border-radius: 12px;
}}
QFrame#mem_row:hover {{ border-color: #d8d2f7; background: #fcfbff; }}
QFrame#add_card {{
    background: #f7f5ff;
    border: 1.5px dashed #c9c0f3;
    border-radius: 12px;
}}
QScrollBar:vertical {{ background: transparent; width: 6px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #d5d2e6; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {ui_style.ACCENT_LT}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
"""


def _relative_time(ts: float) -> str:
    """时间戳 → 简短相对时间。"""
    days = int((time.time() - ts) // 86400)
    if days <= 0:
        return "今天"
    if days == 1:
        return "昨天"
    if days < 7:
        return f"{days}天前"
    if days < 30:
        return f"{days // 7}周前"
    if days < 365:
        return f"{days // 30}月前"
    return f"{days // 365}年前"


class _DragFilter(QObject):
    """按住标题栏拖动整个无边框对话框。"""

    def __init__(self, win: "MemoryDialog"):
        super().__init__(win)
        self._win = win
        self._drag_pos = None

    def eventFilter(self, obj, evt):
        if evt.type() == QEvent.Type.MouseButtonPress:
            self._drag_pos = evt.globalPosition().toPoint() - self._win.frameGeometry().topLeft() \
                if hasattr(evt, "globalPosition") else evt.globalPos() - self._win.pos()
        elif evt.type() == QEvent.Type.MouseMove and self._drag_pos is not None:
            if hasattr(evt, "globalPosition"):
                self._win.move(evt.globalPosition().toPoint() - self._drag_pos)
            else:
                self._win.move(evt.globalPos() - self._drag_pos)
        elif evt.type() == QEvent.Type.MouseButtonRelease:
            self._drag_pos = None
        return False


class MemoryDialog(QDialog):
    """长期记忆管理面板（非模态，可边聊边开）。"""

    changed = Signal()   # 新增 / 删除后发出

    def __init__(self, memory, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.memory = memory
        self.setObjectName("memory_root")
        self.setWindowTitle("长期记忆")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet(_MEMORY_QSS)
        self.resize(480, 620)
        self.setMinimumWidth(420)
        self._build_ui()
        self.refresh()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 14)
        root.setSpacing(0)

        card = QFrame(self)
        card.setObjectName("window_card")
        card.setStyleSheet(ui_style.WINDOW_CARD_QSS)
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(44)
        shadow.setOffset(0, 10)
        shadow.setColor(QColor(90, 78, 200, 50))
        card.setGraphicsEffect(shadow)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        root.addWidget(card)

        # ---- 标题栏 ----
        header = QFrame(card)
        header.setObjectName("titlebar")
        header.setStyleSheet(ui_style.TITLEBAR_QSS)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(16, 10, 10, 10)
        hl.setSpacing(8)
        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        t = QLabel("🧠 长期记忆")
        t.setObjectName("titlebar_title")
        self.count_label = QLabel("")
        self.count_label.setObjectName("titlebar_subtitle")
        title_box.addWidget(t)
        title_box.addWidget(self.count_label)
        hl.addLayout(title_box)
        hl.addStretch(1)
        btn_close = QToolButton()
        btn_close.setObjectName("win_btn_close")
        btn_close.setText("✕")
        btn_close.setToolTip("关闭")
        btn_close.clicked.connect(self.close)
        hl.addWidget(btn_close)
        cl.addWidget(header)
        self._drag = _DragFilter(self)
        header.installEventFilter(self._drag)

        body = QVBoxLayout()
        body.setContentsMargins(14, 12, 14, 12)
        body.setSpacing(10)
        cl.addLayout(body, 1)

        # ---- 搜索 + 类别筛选 ----
        row = QHBoxLayout()
        row.setSpacing(8)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索记忆内容…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self.refresh)
        row.addWidget(self.search_edit, 1)
        self.cat_combo = QComboBox()
        self.cat_combo.addItem("全部类别", "")
        for cid, label in CATEGORY_LABELS.items():
            self.cat_combo.addItem(label, cid)
        self.cat_combo.currentIndexChanged.connect(self.refresh)
        self.cat_combo.setFixedWidth(96)
        row.addWidget(self.cat_combo)
        body.addLayout(row)

        # ---- 记忆列表（滚动）----
        self.scroll = QScrollArea()
        self.scroll.setObjectName("mem_scroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list_host = QWidget()
        self.list_host.setObjectName("mem_list_host")
        self.list_layout = QVBoxLayout(self.list_host)
        self.list_layout.setContentsMargins(2, 2, 6, 2)
        self.list_layout.setSpacing(8)
        self.list_layout.addStretch(1)
        self.scroll.setWidget(self.list_host)
        body.addWidget(self.scroll, 1)

        self.empty_label = QLabel("还没有记住任何事，在下面添加一条吧～")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setStyleSheet("color: #a0a0b4; font-size: 10pt; padding: 20px;")
        self.empty_label.hide()
        body.addWidget(self.empty_label)

        # ---- 新增区 ----
        add_card = QFrame()
        add_card.setObjectName("add_card")
        av = QVBoxLayout(add_card)
        av.setContentsMargins(12, 10, 12, 12)
        av.setSpacing(8)
        self.new_edit = QLineEdit()
        self.new_edit.setPlaceholderText("记一条，例如：主人不喜欢吃香菜")
        self.new_edit.returnPressed.connect(self._on_add)
        av.addWidget(self.new_edit)
        row2 = QHBoxLayout()
        row2.setSpacing(8)
        self.new_cat = QComboBox()
        for cid, label in CATEGORY_LABELS.items():
            self.new_cat.addItem(label, cid)
        self.new_cat.setCurrentIndex(1)  # 默认「事实」
        self.new_cat.setFixedWidth(96)
        row2.addWidget(self.new_cat)
        self.new_imp = QComboBox()
        for label, val in IMPORTANCE_LEVELS:
            self.new_imp.addItem(label, val)
        self.new_imp.setCurrentIndex(1)  # 默认「中」
        self.new_imp.setFixedWidth(96)
        row2.addWidget(self.new_imp)
        row2.addStretch(1)
        btn_add = QPushButton("添加")
        btn_add.setObjectName("add_btn")
        btn_add.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_add.clicked.connect(self._on_add)
        row2.addWidget(btn_add)
        av.addLayout(row2)
        body.addWidget(add_card)

    # -------------------------------------------------------------- 数据
    def _filtered_items(self):
        """UI 层子串过滤 + 类别筛选，按时间倒序（不触发检索 touch）。"""
        kw = self.search_edit.text().strip().lower()
        cat = self.cat_combo.currentData()
        items = self.memory.all()
        if cat:
            items = [i for i in items if i.category == cat]
        if kw:
            items = [i for i in items if kw in i.content.lower()]
        items.sort(key=lambda i: i.created_at, reverse=True)
        return items

    def refresh(self) -> None:
        """重建列表。"""
        # 清掉旧行（保留末尾 stretch）
        while self.list_layout.count() > 1:
            it = self.list_layout.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        items = self._filtered_items()
        total = self.memory.count()
        self.count_label.setText(f"共 {total} 条" + (f" · 筛选出 {len(items)} 条"
                                                      if self.search_edit.text().strip() or
                                                      self.cat_combo.currentData() else ""))
        self.empty_label.setVisible(len(items) == 0)
        self.scroll.setVisible(len(items) > 0)
        for it in items:
            self.list_layout.insertWidget(self.list_layout.count() - 1,
                                          self._make_row(it))

    def _make_row(self, item) -> QFrame:
        """单行记忆：类别标签 + 内容 + 星级 + 时间 + 删除。"""
        row = QFrame()
        row.setObjectName("mem_row")
        h = QHBoxLayout(row)
        h.setContentsMargins(12, 9, 8, 9)
        h.setSpacing(9)

        cid = item.category if item.category in CATEGORY_LABELS else "other"
        fg, bg, border = CATEGORY_COLORS[cid]
        cat_tag = QLabel(CATEGORY_LABELS[cid])
        cat_tag.setFixedWidth(34)
        cat_tag.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cat_tag.setStyleSheet(
            f"background:{bg}; color:{fg}; border:1px solid {border};"
            "border-radius:8px; padding:2px 0; font-size:8pt; font-weight:700;")
        h.addWidget(cat_tag)

        content = QLabel(item.content)
        content.setWordWrap(True)
        content.setStyleSheet("color:#2c2c38; font-size:10pt; border:none; background:transparent;")
        content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        h.addWidget(content, 1)

        stars = QLabel("★" * max(1, round(item.importance * 5)))
        stars.setStyleSheet("color:#f5b642; font-size:9pt; border:none; background:transparent;")
        stars.setFixedWidth(54)
        stars.setAlignment(Qt.AlignmentFlag.AlignCenter)
        h.addWidget(stars)

        time_lbl = QLabel(_relative_time(item.created_at))
        time_lbl.setStyleSheet("color:#a9a9bc; font-size:8pt; border:none; background:transparent;")
        time_lbl.setFixedWidth(44)
        time_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        h.addWidget(time_lbl)

        del_btn = QPushButton("✕")
        del_btn.setObjectName("del_btn")
        del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        del_btn.setToolTip("删除这条记忆")
        del_btn.clicked.connect(lambda _=False, mid=item.id: self._on_delete(mid))
        h.addWidget(del_btn)
        return row

    # -------------------------------------------------------------- 操作
    def _on_add(self) -> None:
        text = self.new_edit.text().strip()
        if not text:
            return
        cat = self.new_cat.currentData() or "fact"
        imp = float(self.new_imp.currentData() or 0.5)
        before = self.memory.count()
        self.memory.add(text, category=cat, importance=imp)
        if self.memory.count() == before:
            # 完全相同内容已存在 → 给出提示（不重复记）
            self.new_edit.setPlaceholderText("这条内容已经记住啦～")
        self.new_edit.clear()
        self.refresh()
        self.changed.emit()

    def _on_delete(self, mid: str) -> None:
        self.memory.remove(mid)
        self.refresh()
        self.changed.emit()
