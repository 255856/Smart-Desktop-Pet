"""chat_window 子模块：/ 命令自动补全。

QPlainTextEdit 没有原生 setCompleter；本类用 QListWidget 当 popup，
按 / 前缀过滤候选，Tab/Enter/Esc/鼠标点击 都能交互。
"""
from __future__ import annotations

from app.core.qt_compat import (
    QListWidget, QObject, QPlainTextEdit, QPoint, QTextCursor, Qt,
)


class _CommandCompleter(QObject):
    """监听 QPlainTextEdit 的按键事件，当输入行以 / 开头时弹候选列表。

    实现：
        - 监听 keyPress（Up/Down/Tab/Enter/Esc/字符键）
        - 用一个 QListWidget 当 popup
        - 匹配 prefix 的命令显示在 popup 里
        - Enter / Tab 接受第一个候选；Esc 关闭；鼠标点击也能选
    """

    def __init__(self, edit: QPlainTextEdit, candidates: list[str]):
        super().__init__(edit)
        self.edit = edit
        self.candidates = candidates
        self.popup = QListWidget()
        self.popup.setWindowFlags(Qt.WindowType.ToolTip)
        self.popup.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.popup.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.popup.setStyleSheet("""
            QListWidget {
                background: #ffffff;
                border: 1px solid #a78bfa;
                border-radius: 8px;
                padding: 4px;
                color: #2c2c38;
                font-size: 10pt;
            }
            QListWidget::item { padding: 4px 12px; border-radius: 4px; }
            QListWidget::item:selected { background: #ede9fe; color: #6d28d9; }
        """)
        self.popup.itemClicked.connect(self._accept)
        # 拦截事件
        edit.installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj is not self.edit:
            return False
        if event.type() == event.Type.KeyPress:
            key = event.key()
            if self.popup.isVisible():
                if key in (Qt.Key.Key_Down,):
                    self._move(1)
                    return True
                if key in (Qt.Key.Key_Up,):
                    self._move(-1)
                    return True
                if key in (Qt.Key.Key_Tab, Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    self._accept_current()
                    return True
                if key == Qt.Key.Key_Escape:
                    self.popup.hide()
                    return True
            # 文本变化时重新计算 popup
            if key == Qt.Key.Key_Slash or self._current_word().startswith("/"):
                self._refresh()
            elif key in (Qt.Key.Key_Space, Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
                self._refresh()
        return False

    def _current_word(self) -> str:
        """取光标所在行的第一个 token。"""
        cursor = self.edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.StartOfLine)
        cursor.movePosition(QTextCursor.MoveOperation.EndOfLine,
                            QTextCursor.MoveMode.KeepAnchor)
        line = cursor.selectedText()
        # 取第一个 token（空格前）
        for i, ch in enumerate(line):
            if ch in (" ", "\n", "\t"):
                return line[:i]
        return line

    def _refresh(self) -> None:
        word = self._current_word()
        if not word.startswith("/"):
            self.popup.hide()
            return
        prefix = word.lower()
        matches = [c for c in self.candidates if c.lower().startswith(prefix)]
        if not matches:
            self.popup.hide()
            return
        self.popup.clear()
        self.popup.addItems(matches)
        self.popup.setCurrentRow(0)
        # 定位在输入框下方
        rect = self.edit.cursorRect()
        global_pos = self.edit.mapToGlobal(rect.bottomLeft())
        self.popup.move(global_pos + QPoint(0, 4))
        popup_w = max(180, self.popup.sizeHintForColumn(0) + 24)
        self.popup.setFixedWidth(popup_w)
        self.popup.setFixedHeight(min(180, self.popup.sizeHintForRow(0) * len(matches) + 16))
        self.popup.show()

    def _move(self, delta: int) -> None:
        n = self.popup.count()
        if n == 0:
            return
        cur = (self.popup.currentRow() + delta) % n
        self.popup.setCurrentRow(cur)

    def _accept_current(self) -> None:
        item = self.popup.currentItem()
        if item is not None:
            self._apply_text(item.text())
        self.popup.hide()

    def _accept(self, item) -> None:
        self._apply_text(item.text())
        self.popup.hide()

    def _apply_text(self, full: str) -> None:
        """把当前行的第一个 token 替换成 full（保留后续内容）。"""
        cursor = self.edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.StartOfLine)
        cursor.movePosition(QTextCursor.MoveOperation.EndOfLine,
                            QTextCursor.MoveMode.KeepAnchor)
        line = cursor.selectedText()
        # 找到第一个空格位置
        rest = ""
        for i, ch in enumerate(line):
            if ch in (" ", "\n", "\t"):
                rest = line[i:]
                break
        cursor.insertText(full + rest)
        self.edit.setTextCursor(cursor)


__all__ = ["_CommandCompleter"]