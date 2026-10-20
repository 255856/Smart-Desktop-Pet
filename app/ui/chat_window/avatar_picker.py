"""桌宠头像选择弹窗：分「预设」和「上传」两个 tab, 点一下应用。

路径:
- 预设: assets/avatars/presets/*.png
- 用户上传: assets/avatars/uploads/*.png
- 当前选中: assets/avatars/bot.png

每个 tab 用 FlowLayout 按图标实际宽度自动换行（头像是方图，
FlowLayout 等价于网格但不会留大空白）。
"""
from __future__ import annotations

import logging
from pathlib import Path

from app.core.qt_compat import (
    QFileDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QSizePolicy, QTabWidget, QToolButton, QVBoxLayout, QWidget,
    Qt, QDialog, Signal,
)
from app.ui.chat_window.tool_unlock_dialog import FlowLayout

log = logging.getLogger(__name__)

# 头像缩略图边长（正方形）
THUMB = 96


def _list_avatar_pngs(folder: Path) -> list[Path]:
    """列出一个目录下的所有 PNG/JPG 头像，按文件名排序。"""
    if not folder.is_dir():
        return []
    out: list[Path] = []
    for ext in ("*.png", "*.jpg", "*.jpeg"):
        out.extend(folder.glob(ext))
    return sorted(out)


class AvatarPickerDialog(QDialog):
    """头像选择弹窗。选定后 emit avatarSelected(str path)。"""

    avatarSelected = Signal(str)   # 选中的头像绝对路径

    def __init__(self, current_path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("选择桌宠头像")
        self.setMinimumSize(640, 480)
        self.resize(720, 560)
        # 跟工具解锁一致的窗口类型（Tool 浮动 + StaysOnTopHint + ApplicationModal），
        # 打开后其他窗口点不动
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setWindowModality(Qt.WindowModality.ApplicationModal)

        from app.ui.chat_window import AVATAR_PRESETS_DIR, AVATAR_UPLOADS_DIR
        self._presets_dir = AVATAR_PRESETS_DIR
        self._uploads_dir = AVATAR_UPLOADS_DIR
        self._current_path = current_path

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        # 顶部提示
        tip = QLabel("点选一个头像。预设与上传分两个 tab，可随时切换。")
        tip.setStyleSheet("color: #7a7a8c; font-size: 9pt;")
        root.addWidget(tip)

        # 两个 tab: 预设 / 上传
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_presets_tab(), "预设")
        self.tabs.addTab(self._build_uploads_tab(), "我上传的")
        root.addWidget(self.tabs, 1)

        # 底部：当前选中 + 关闭
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setObjectName("ghost_btn")
        cancel.clicked.connect(self.reject)
        bottom.addWidget(cancel)
        ok = QPushButton("关闭")
        ok.setObjectName("accent_btn")
        ok.clicked.connect(self.accept)
        bottom.addWidget(ok)
        root.addLayout(bottom)

        # 弹窗打开时, 如果当前选中头像在预设/上传里, 切到对应 tab
        self._select_current_tab()

    # ---- 预设 / 上传 tab ----
    def _build_presets_tab(self) -> QWidget:
        return self._build_grid_tab(
            self._presets_dir,
            empty_msg="预设目录里还没有头像。把 PNG 放进 assets/avatars/presets/ 即可。")

    def _build_uploads_tab(self) -> QWidget:
        return self._build_grid_tab(
            self._uploads_dir,
            empty_msg="还没上传过头像。点下方「上传头像」按钮选一张图片。")

    def _build_grid_tab(self, folder: Path, empty_msg: str) -> QWidget:
        host = QWidget()
        outer = QVBoxLayout(host)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)

        files = _list_avatar_pngs(folder)
        if not files:
            empty = QLabel(empty_msg)
            empty.setStyleSheet("color: #9a9aac; font-size: 9pt;")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            outer.addWidget(empty, 1)
        else:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            inner = QWidget()
            flow = FlowLayout(inner, spacing=12)
            for f in files:
                flow.addWidget(self._make_avatar_button(f))
            scroll.setWidget(inner)
            outer.addWidget(scroll, 1)

        # 上传 tab 多一个上传按钮
        if folder == self._uploads_dir:
            row = QHBoxLayout()
            upload_btn = QPushButton("上传头像…")
            upload_btn.setObjectName("ghost_btn")
            upload_btn.clicked.connect(lambda: self._on_upload(folder))
            row.addWidget(upload_btn)
            row.addStretch(1)
            # 提示当前选中
            self._selected_label = QLabel(self._current_status_text())
            self._selected_label.setStyleSheet("color: #7a7a8c; font-size: 9pt;")
            row.addWidget(self._selected_label)
            outer.addLayout(row)
        else:
            # 预设 tab 只显示当前选中
            row = QHBoxLayout()
            row.addStretch(1)
            self._selected_label = QLabel(self._current_status_text())
            self._selected_label.setStyleSheet("color: #7a7a8c; font-size: 9pt;")
            row.addWidget(self._selected_label)
            outer.addLayout(row)

        return host

    # ---- 单个头像按钮 ----
    def _make_avatar_button(self, path: Path) -> QToolButton:
        """做一个方形头像按钮: 缩略图 + 文件名（悬停 tooltip）。"""
        from app.core.qt_compat import QIcon
        btn = QToolButton()
        btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        btn.setIconSize(_size(THUMB))
        btn.setFixedSize(_size(THUMB + 8), _size(THUMB + 36))
        btn.setIcon(QIcon(str(path)))
        btn.setText(path.stem)
        # 缩略图超出图标显示范围时, label 文字会自动省略
        btn.setToolTip(str(path))
        # 当前选中加边框 (用 dynamicProperty 区分, hover 时换样式)
        if self._same_path(str(path), self._current_path):
            btn.setProperty("selected", True)
        btn.setStyleSheet(_CARD_BTN_QSS)
        btn.clicked.connect(lambda _=False, p=str(path): self._on_pick(p))
        return btn

    def _same_path(self, a: str, b: str) -> bool:
        if not a or not b:
            return False
        try:
            return Path(a).resolve() == Path(b).resolve()
        except Exception:  # noqa: BLE001
            return a == b

    # ---- 交互 ----
    def _on_pick(self, path: str) -> None:
        """点选后高亮 + emit + 关闭。"""
        self.avatarSelected.emit(path)
        self.accept()

    def _on_upload(self, folder: Path) -> None:
        """打开文件选择, 选 PNG/JPG 复制进 uploads/ 然后点选它。"""
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "选择头像图片",
            "",
            "Images (*.png *.jpg *.jpeg *.bmp *.gif)",
        )
        if not path_str:
            return
        src = Path(path_str)
        try:
            # 复制到 uploads/, 文件名加时间戳避免冲突
            import time as _t
            dst = folder / f"upload_{int(_t.time())}_{src.name}"
            dst.write_bytes(src.read_bytes())
        except Exception as e:  # noqa: BLE001
            log.warning("头像上传失败: %s", e)
            return
        # 重建当前 tab
        idx = self.tabs.indexOf(self.tabs.widget(self.tabs.currentIndex()))
        new_widget = self._build_uploads_tab()
        self.tabs.removeTab(1)
        self.tabs.insertTab(1, new_widget, "我上传的")
        self.tabs.setCurrentIndex(1)
        # 立即选中新上传
        self.avatarSelected.emit(str(dst))
        self.accept()

    def _select_current_tab(self) -> None:
        """如果当前选中头像在预设/上传里, 切到对应 tab。"""
        try:
            cur = Path(self._current_path).resolve()
        except Exception:  # noqa: BLE001
            return
        try:
            if cur.is_relative_to(self._presets_dir.resolve()):
                self.tabs.setCurrentIndex(0)
                return
        except Exception:  # noqa: BLE001
            pass
        try:
            if cur.is_relative_to(self._uploads_dir.resolve()):
                self.tabs.setCurrentIndex(1)
        except Exception:  # noqa: BLE001
            pass

    def _current_status_text(self) -> str:
        return f"当前：{self._current_path or '未选择'}"


def _size(v: int):
    from app.core.qt_compat import QSize
    return QSize(v, v)


# 头像按钮的样式: 普通态圆角白底, hover 加阴影, 选中态加 accent 边框
_CARD_BTN_QSS = """
QToolButton {
    background: #ffffff;
    border: 1.5px solid #e0dfe8;
    border-radius: 12px;
    padding: 4px;
    color: #4a3d6e;
    font-size: 8pt;
}
QToolButton:hover {
    border-color: #c4b5fd;
    background: #faf8ff;
}
QToolButton[selected="true"] {
    border: 2.5px solid #e89aae;
    background: #fff5f8;
}
"""