"""自动省略的 QLabel：宽度不够时文字显示「…」。"""
from __future__ import annotations

from app.core.qt_compat import QLabel, Qt


class ElidedLabel(QLabel):
    """水平空间不足时自动省略的单行标签。"""

    def __init__(self, text: str = "", parent=None,
                 mode=Qt.TextElideMode.ElideRight):
        self._full_text = text
        self._elide_mode = mode
        super().__init__(parent)
        # 允许收缩到比文字更窄
        self.setMinimumWidth(0)
        self.setText(self._full_text)

    def full_text(self) -> str:
        return self._full_text

    def set_full_text(self, text: str) -> None:
        self._full_text = text
        self._update_elided()

    # QLabel.setText 被外部调用时也保留完整文本
    def setText(self, text: str) -> None:  # noqa: N802 (Qt 命名)
        self._full_text = text
        self._update_elided()

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        self._update_elided()
        super().resizeEvent(event)

    def _update_elided(self) -> None:
        fm = self.fontMetrics()
        elided = fm.elidedText(self._full_text, self._elide_mode,
                               max(self.width() - 1, 0))
        super().setText(elided)
        self.setToolTip(self._full_text if elided != self._full_text else "")


__all__ = ["ElidedLabel"]
