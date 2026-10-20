"""统一的弹窗外壳：无边框 + 圆角卡片 + 自绘标题栏。

聊天窗、工具解锁弹窗、头像选择弹窗共用这一套外壳和 ui_style 配色，
保证弹窗跟主窗口看起来是一家人，而不是各自一套 QSS + 系统原生标题栏。

结构:
    透明窗口
      └─ window_card (圆角卡片 + 柔阴影)
           ├─ titlebar (自绘：标题 / 副标题 / 关闭)
           └─ dialog_body (子类往 body_layout 里放内容)
"""
from __future__ import annotations

from typing import Optional

from app.core.qt_compat import (
    QColor, QDialog, QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel,
    QToolButton, QVBoxLayout, QWidget, Qt,
)
from app.ui import ui_style
from app.ui.window_drag import TitleBarDrag


class FramelessDialog(QDialog):
    """无边框弹窗外壳。

    子类往 self.body_layout 里放内容；标题 / 副标题由外壳统一渲染。
    """

    def __init__(
        self,
        parent=None,
        *,
        title: str = "",
        subtitle: str = "",
        width: int = 720,
        height: int = 520,
        min_width: int = 560,
        min_height: int = 400,
        extra_flags: Optional[Qt.WindowType] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog
        if extra_flags is not None:
            flags |= extra_flags
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumSize(min_width, min_height)
        self.resize(width, height)
        self.setStyleSheet(ui_style.DIALOG_QSS)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 18)
        root.setSpacing(0)

        card = QFrame(self)
        card.setObjectName("window_card")
        card.setStyleSheet(ui_style.WINDOW_CARD_QSS)
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(ui_style.WINDOW_SHADOW_BLUR)
        shadow.setOffset(0, ui_style.WINDOW_SHADOW_OFFSET)
        shadow.setColor(QColor(
            ui_style.WINDOW_SHADOW_COLOR_R,
            ui_style.WINDOW_SHADOW_COLOR_G,
            ui_style.WINDOW_SHADOW_COLOR_B,
            ui_style.WINDOW_SHADOW_ALPHA))
        card.setGraphicsEffect(shadow)

        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.setSpacing(0)
        root.addWidget(card, 1)

        # ---- 自绘标题栏 ----
        header = QFrame(card)
        header.setObjectName("titlebar")
        header.setStyleSheet(ui_style.TITLEBAR_QSS)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(16, 9, 8, 9)
        hl.setSpacing(4)

        drag_zone = QWidget(header)
        dz = QVBoxLayout(drag_zone)
        dz.setContentsMargins(0, 0, 0, 0)
        dz.setSpacing(1)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("titlebar_title")
        dz.addWidget(self.title_label)
        self.subtitle_label = QLabel(subtitle)
        self.subtitle_label.setObjectName("titlebar_subtitle")
        dz.addWidget(self.subtitle_label)
        hl.addWidget(drag_zone, 1)

        self.btn_min = QToolButton()
        self.btn_min.setObjectName("win_btn")
        self.btn_min.setText("─")
        self.btn_min.setToolTip("最小化")
        self.btn_min.clicked.connect(self.showMinimized)
        hl.addWidget(self.btn_min)

        self.btn_max = QToolButton()
        self.btn_max.setObjectName("win_btn")
        self.btn_max.setText("▢")
        self.btn_max.setToolTip("最大化")
        self.btn_max.clicked.connect(self._toggle_maximized)
        hl.addWidget(self.btn_max)

        self.btn_close = QToolButton()
        self.btn_close.setObjectName("win_btn_close")
        self.btn_close.setText("X")
        self.btn_close.setToolTip("关闭")
        self.btn_close.clicked.connect(self.reject)
        hl.addWidget(self.btn_close)
        card_layout.addWidget(header)

        # 标题栏拖动
        self._drag_filter = TitleBarDrag(self)
        for w in (drag_zone, self.title_label, self.subtitle_label):
            w.installEventFilter(self._drag_filter)

        # ---- 内容区 ----
        self.body = QWidget(card)
        self.body.setObjectName("dialog_body")
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(16, 12, 16, 14)
        self.body_layout.setSpacing(10)
        card_layout.addWidget(self.body, 1)

    def _toggle_maximized(self) -> None:
        """最大化 / 还原切换。"""
        if self.isMaximized():
            self.showNormal()
            self.btn_max.setText("▢")
            self.btn_max.setToolTip("最大化")
        else:
            self.showMaximized()
            self.btn_max.setText("[ ]")
            self.btn_max.setToolTip("还原")


__all__ = ["FramelessDialog"]
