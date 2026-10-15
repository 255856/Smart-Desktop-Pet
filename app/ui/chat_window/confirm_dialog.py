"""危险工具确认弹窗 —— 自定义 QDialog。

为什么不用 QMessageBox:
- 2026-10-15 用户实测:chat_window 是 FramelessWindowHint + WA_TranslucentBackground
  (为了做悬浮桌宠效果),用 QMessageBox(self) 继承父窗口的 window flags
  → Qt 把它当 transient child → 在某些时序下被父窗口的 frame dismiss
  → 用户看到「只闪一下」就消失。
- 用 QMessageBox(None) 改成独立 top-level 也不稳:headless / no-desktop
  session 下 Qt 内部逻辑会立即 close box。

修法:写自定义 QDialog,显式控制所有 window flags,不依赖父窗口的 flags,
  保证弹窗在 frameless 父窗口下也能稳定显示。
"""
from __future__ import annotations

import logging
from typing import Optional

from app.core.qt_compat import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    Qt, QSize, pyqtSlot,
)

log = logging.getLogger(__name__)


class _DangerConfirmDialog(QDialog):
    """危险工具确认弹窗 —— 独立 top-level QDialog,Yes/No 按钮。

    协议:
      - exec() 返回 int: 1 = Yes (Accepted), 0 = No (Rejected)
      - 超时(通过 _close_confirm_box 调 reject())返回 0
      - WA_DeleteOnClose 防止内存泄漏
    """

    def __init__(self, name: str, args_pretty: str, parent=None):
        # 关键:parent=None 让 QDialog 完全独立,不继承 chat_window 的
        # FramelessWindowHint / WA_TranslucentBackground。
        super().__init__(parent)
        # 显式控制所有 window flags,不让 Qt 帮我们猜
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowSystemMenuHint
            | Qt.WindowType.WindowTitleHint
        )
        # 关掉后自动释放
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        # 模态性:ApplicationModal 阻塞整个 app 的输入直到用户点按钮
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        # 工具名 + 参数展示
        self._name = name
        self._args_pretty = args_pretty or "  (无)"

        # 中文标签
        from app.ui.chat_window.utils import _DANGEROUS_LABELS
        label = _DANGEROUS_LABELS.get(name, name)
        self.setWindowTitle("危险工具确认")
        self.setMinimumSize(QSize(420, 200))

        # --- 布局:图标 + 文本,下方 Yes / No 按钮 ---
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        # 标题行(粗体红字)
        title = QLabel(f"⚠  桌宠想要调用危险工具:{label}")
        title.setStyleSheet(
            "font-size:13pt; font-weight:600; color:#b91c1c;"
            "padding-bottom:4px;"
        )
        layout.addWidget(title)

        # 参数(纯文本 monospace)
        body = QLabel(
            "参数:\n"
            f"{self._args_pretty}\n\n"
            "是否允许?(默认 No —— 不点 Yes 就不会执行)"
        )
        body.setStyleSheet(
            "color:#1f2937; font-size:10.5pt; line-height:1.55;"
            "background:#f9fafb; border:1px solid #e5e7eb; border-radius:6px;"
            "padding:10px 12px;"
        )
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(body, 1)

        # 按钮
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch(1)

        self._no_btn = QPushButton("✗  拒绝 (默认)")
        self._no_btn.setMinimumWidth(120)
        self._no_btn.setMinimumHeight(34)
        self._no_btn.setStyleSheet(
            "QPushButton{background:#e5e7eb;color:#1f2937;border:none;border-radius:6px;}"
            "QPushButton:hover{background:#d1d5db;}"
            "QPushButton:pressed{background:#9ca3af;}"
        )
        self._no_btn.clicked.connect(self._on_no)
        # Tab order 第一(No 是默认,跟 QMessageBox 一致)
        btn_row.addWidget(self._no_btn)

        self._yes_btn = QPushButton("✓  允许执行")
        self._yes_btn.setMinimumWidth(120)
        self._yes_btn.setMinimumHeight(34)
        self._yes_btn.setStyleSheet(
            "QPushButton{background:#b91c1c;color:#fff;border:none;border-radius:6px;font-weight:600;}"
            "QPushButton:hover{background:#991b1b;}"
            "QPushButton:pressed{background:#7f1d1d;}"
        )
        self._yes_btn.clicked.connect(self._on_yes)
        btn_row.addWidget(self._yes_btn)

        layout.addLayout(btn_row)
        # 默认焦点在 No 上(防误点)
        self._no_btn.setDefault(True)
        self._no_btn.setAutoDefault(True)
        # Esc 键 = 拒绝
        self._no_btn.setShortcut("Esc")
        # Enter 键 = 允许(明确让 Enter 是 Yes,跟 QMessageBox 一致)
        self._yes_btn.setShortcut("Return")

    @pyqtSlot()
    def _on_yes(self):
        log.info("danger confirm: user pressed YES for tool=%r", self._name)
        self.accept()

    @pyqtSlot()
    def _on_no(self):
        log.info("danger confirm: user pressed NO for tool=%r", self._name)
        self.reject()

    def reject(self) -> None:
        """让超时路径 _close_confirm_box 也能干净关掉,统一走 Rejected(0)。"""
        log.info("danger confirm: rejected (timeout or close) for tool=%r", self._name)
        super().reject()


__all__ = ["_DangerConfirmDialog"]
