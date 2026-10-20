"""文件拖入桌宠时的弹窗：吃掉（回收站）/ 转换（格式转换）。"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Optional

from app.core.qt_compat import (
    QFrame, QHBoxLayout, QLabel, QPushButton,
    Qt, QVBoxLayout, QWidget,
)
from app.ui.dialog_shell import FramelessDialog

log = logging.getLogger(__name__)

# 格式转换映射：{源后缀: {目标后缀: 转换方式}}
_SUPPORTED_CONVERSIONS: dict[str, dict[str, str]] = {
    ".png": {".jpg": "PIL:PNG->JPG", ".webp": "PIL:PNG->WEBP", ".bmp": "PIL:PNG->BMP", ".gif": "PIL:PNG->GIF"},
    ".jpg": {".png": "PIL:JPG->PNG", ".webp": "PIL:JPG->WEBP", ".bmp": "PIL:JPG->BMP", ".gif": "PIL:JPG->GIF"},
    ".jpeg": {".png": "PIL:JPG->PNG", ".webp": "PIL:JPG->WEBP", ".bmp": "PIL:JPG->BMP"},
    ".webp": {".png": "PIL:WEBP->PNG", ".jpg": "PIL:WEBP->JPG", ".bmp": "PIL:WEBP->BMP"},
    ".bmp": {".png": "PIL:BMP->PNG", ".jpg": "PIL:BMP->JPG"},
    ".gif": {".png": "PIL:GIF->PNG", ".jpg": "PIL:GIF->JPG"},
    ".txt": {".md": "TEXT:TXT->MD"},
    ".md": {".txt": "TEXT:MD->TXT"},
}

# 可以"吃掉"的文件类型
_EATABLE_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".ico",
    ".txt", ".md", ".py", ".pyc", ".pyo",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".csv", ".json", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2",
    ".html", ".htm", ".css", ".js", ".ts",
    ".mp3", ".mp4", ".avi", ".mkv", ".mov", ".wav",
    ".log", ".bak", ".tmp",
}


def is_eatable(path: Path) -> bool:
    """判断文件是否可以被"吃掉"。"""
    return path.suffix.lower() in _EATABLE_SUFFIXES


def send_to_recycle_bin(path: Path) -> bool:
    """把文件移到回收站。"""
    try:
        import ctypes
        SHFileOperationW = ctypes.windll.shell32.SHFileOperationW
    except (AttributeError, OSError):
        # 非 Windows：fallback 到 ~/.trash
        return _fallback_recycle(path)

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", ctypes.c_void_p),
            ("wFunc", ctypes.c_uint),
            ("pFrom", ctypes.c_wchar_p),
            ("pTo", ctypes.c_wchar_p),
            ("fFlags", ctypes.c_uint16),
            ("fAnySizes", ctypes.c_int),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", ctypes.c_wchar_p),
        ]

    FO_DELETE = 3
    FOF_ALLOWUNDO = 0x40
    FOF_NOCONFIRMATION = 0x10
    FOF_SILENT = 0x4

    src = str(path.resolve()) + "\0"  # null-terminated
    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = src
    op.pTo = None
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT

    result = SHFileOperationW(ctypes.byref(op))
    return result == 0


def _fallback_recycle(path: Path) -> bool:
    """非 Windows：移到 ~/.trash。"""
    trash_dir = Path.home() / ".trash"
    trash_dir.mkdir(parents=True, exist_ok=True)
    dest = trash_dir / path.name
    counter = 1
    while dest.exists():
        dest = trash_dir / f"{path.stem}_{counter}{path.suffix}"
        counter += 1
    try:
        shutil.move(str(path), str(dest))
        log.info("回收站(fallback): %s -> %s", path, dest)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("回收站失败：%s", e)
        return False


def convert_file(src_path: Path, target_suffix: str) -> Optional[Path]:
    """转换文件到目标格式。返回输出路径，失败返回 None。"""
    src_suffix = src_path.suffix.lower()

    # PIL 图片转换
    if src_suffix in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"):
        try:
            from PIL import Image
            img = Image.open(src_path)
            # GIF 取首帧
            if src_suffix == ".gif" and target_suffix in (".png", ".jpg", ".webp", ".bmp"):
                img = img.convert("RGBA" if target_suffix == ".png" else "RGB")
            # JPG 不支持 RGBA
            if target_suffix in (".jpg", ".jpeg") and img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            dest = src_path.with_suffix(target_suffix)
            counter = 1
            while dest.exists():
                dest = src_path.with_name(f"{src_path.stem}_{counter}{target_suffix}")
                counter += 1
            img.save(dest)
            log.info("图片转换：%s -> %s", src_path, dest)
            return dest
        except Exception as e:  # noqa: BLE001
            log.warning("图片转换失败：%s", e)
            return None

    # 文本格式转换（txt <-> md）
    if src_suffix == ".txt" and target_suffix == ".md":
        try:
            content = src_path.read_text(encoding="utf-8", errors="replace")
            dest = src_path.with_suffix(".md")
            counter = 1
            while dest.exists():
                dest = src_path.with_name(f"{src_path.stem}_{counter}.md")
                counter += 1
            dest.write_text(content, encoding="utf-8")
            return dest
        except Exception as e:  # noqa: BLE001
            log.warning("文本转换失败：%s", e)
            return None

    if src_suffix == ".md" and target_suffix == ".txt":
        try:
            content = src_path.read_text(encoding="utf-8", errors="replace")
            dest = src_path.with_suffix(".txt")
            counter = 1
            while dest.exists():
                dest = src_path.with_name(f"{src_path.stem}_{counter}.txt")
                counter += 1
            dest.write_text(content, encoding="utf-8")
            return dest
        except Exception as e:  # noqa: BLE001
            log.warning("文本转换失败：%s", e)
            return None

    log.warning("不支持的转换：%s -> %s", src_path.suffix, target_suffix)
    return None


def get_available_conversions(path: Path) -> list[tuple[str, str]]:
    """返回支持的转换格式 [(显示名, 后缀), ...]。"""
    src_suffix = path.suffix.lower()
    conversions = _SUPPORTED_CONVERSIONS.get(src_suffix, {})
    display_names = {
        ".png": "PNG 图片", ".jpg": "JPG 图片", ".jpeg": "JPEG 图片",
        ".webp": "WebP 图片", ".bmp": "BMP 图片", ".gif": "GIF 动画",
        ".txt": "纯文本", ".md": "Markdown",
    }
    return [(display_names.get(ext, ext.upper()), ext) for ext in conversions.keys()]


class FileOperationDialog(FramelessDialog):
    """文件操作弹窗：移到回收站 / 转换格式。

    沿用 FramelessDialog 外壳（无边框 + 圆角卡片 + 自绘标题栏）, 跟聊天窗、
    工具解锁窗、头像弹窗是同一套外观。
    """

    def __init__(self, file_path: Path, parent: QWidget = None):
        super().__init__(
            parent,
            title=file_path.name,
            subtitle="文件操作",
            width=460,
            height=380,
            min_width=420,
            min_height=340,
        )
        self.file_path = file_path
        self.result_action: str = ""  # "eat" / "convert" / ""
        self.convert_suffix: str = ""
        self._confirming_eat = False
        self._build_ui()

    # ---------------------------------------------------------------- UI
    def _build_ui(self) -> None:
        body = self.body_layout

        # ---- 文件信息卡 ----
        card = QFrame()
        card.setObjectName("file_info_card")
        card.setStyleSheet(
            "QFrame#file_info_card { background: #fbfafd; border: 1px solid #eceaf5;"
            " border-radius: 12px; }")
        card.setMinimumHeight(84)
        ch = QHBoxLayout(card)
        ch.setContentsMargins(14, 12, 14, 12)
        ch.setSpacing(12)

        # 系统文件图标（不是 emoji, 跟随系统主题, 跟资源管理器一致）
        icon = QLabel()
        icon.setPixmap(_file_icon(self.file_path, 44))
        icon.setFixedSize(44, 44)
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ch.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)

        col = QVBoxLayout()
        col.setSpacing(3)
        # 三行信息放一个富文本 label 里 —— 三个独立 QLabel 在窄布局里会被压扁叠字
        import html as _html
        info = QLabel(
            f"<span style='font-size:11pt; font-weight:600; color:#3d3d50;'>"
            f"{_html.escape(self.file_path.name)}</span><br/>"
            f"<span style='font-size:9pt; color:#8a8a9c;'>"
            f"{_html.escape(_elide_middle(str(self.file_path.parent), 46))}</span><br/>"
            f"<span style='font-size:9pt; color:#8a8a9c;'>"
            f"{_html.escape(self._meta_text())}</span>")
        info.setTextFormat(Qt.TextFormat.RichText)
        info.setToolTip(str(self.file_path))
        info.setWordWrap(False)
        # 三行富文本: 必须给够高度, 否则布局会把它压成一行 (路径/大小直接看不见)
        info.setMinimumHeight(58)
        col.addWidget(info)
        ch.addLayout(col, 1)
        body.addWidget(card)

        # ---- 可转换格式（直接列出来让人点, 不用打字选）----
        self._conversions = get_available_conversions(self.file_path)
        if self._conversions:
            cap = QLabel("转换为")
            cap.setStyleSheet(
                "font-size: 9pt; color: #8a8a9c; background: transparent;")
            body.addWidget(cap)
            chip_row = QHBoxLayout()
            chip_row.setSpacing(6)
            for name_txt, ext in self._conversions:
                btn = QPushButton(ext.lstrip(".").upper())
                btn.setObjectName("chip")
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.setToolTip(f"转换为 {name_txt}")
                # QSS 的 padding 不一定反映在 sizeHint 里, 留点余量免得文字被裁
                btn.setMinimumWidth(btn.sizeHint().width() + 10)
                btn.clicked.connect(
                    lambda _=False, e=ext: self._on_convert_pick(e))
                chip_row.addWidget(btn)
            chip_row.addStretch(1)
            body.addLayout(chip_row)
            note = QLabel("转换会生成副本, 原文件保留")
            note.setStyleSheet(
                "font-size: 8pt; color: #a8a8b8; background: transparent;")
            body.addWidget(note)
        else:
            hint = QLabel("该格式暂不支持转换")
            hint.setStyleSheet(
                "font-size: 9pt; color: #a8a8b8; background: transparent;")
            body.addWidget(hint)

        body.addStretch(1)

        # ---- 底部按钮 ----
        self._btn_row = QHBoxLayout()
        self._btn_row.setSpacing(8)
        body.addLayout(self._btn_row)

        self.btn_locate = QPushButton("在文件夹中显示")
        self.btn_locate.setObjectName("ghost_btn")
        self.btn_locate.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_locate.clicked.connect(self._on_locate)
        self._btn_row.addWidget(self.btn_locate)
        self._btn_row.addStretch(1)

        self._cancel_btn = QPushButton("取消")
        self._cancel_btn.setObjectName("ghost_btn")
        self._cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cancel_btn.clicked.connect(self.reject)
        self._btn_row.addWidget(self._cancel_btn)

        self.btn_eat = QPushButton("移到回收站")
        self.btn_eat.setObjectName("accent_btn")
        self.btn_eat.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_eat.setToolTip("文件会移到回收站, 可从回收站还原")
        self.btn_eat.clicked.connect(self._on_eat)
        self._btn_row.addWidget(self.btn_eat)

    def _meta_text(self) -> str:
        """大小 / 类型 / 修改时间（拿不到就只显示能拿到的）。"""
        parts: list[str] = []
        try:
            st = self.file_path.stat()
            parts.append(_human_size(st.st_size))
            parts.append(_kind_label(self.file_path))
            import datetime as _dt
            parts.append(
                _dt.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"))
        except OSError:
            parts.append("文件已不存在")
        return " · ".join(parts)

    # ------------------------------------------------------------ 交互
    def _on_eat(self) -> None:
        """移到回收站 —— 破坏性操作, 先要一次明确确认。"""
        if not self._confirming_eat:
            self._confirming_eat = True
            self.btn_eat.setText("确认移到回收站")
            self.btn_eat.setObjectName("danger_btn")   # 危险色
            self.btn_eat.style().unpolish(self.btn_eat)
            self.btn_eat.style().polish(self.btn_eat)
            self._cancel_btn.setText("返回")
            return
        self.result_action = "eat"
        self.accept()

    def _on_convert_pick(self, ext: str) -> None:
        """点格式 chip = 直接转换。

        原来是弹个输入框让用户**打字**选格式（"要转换为什么格式？"），
        打错字就静默转成第一个 —— 现在列出来让人点, 不可能选错。
        """
        self.result_action = "convert"
        self.convert_suffix = ext
        self.accept()

    def _on_locate(self) -> None:
        """在系统文件管理器里定位到该文件。"""
        try:
            from PyQt5.QtCore import QUrl
            from PyQt5.QtGui import QDesktopServices
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(self.file_path.parent)))
        except Exception:  # noqa: BLE001
            log.debug("打开所在文件夹失败", exc_info=True)


# ---------------------------------------------------------------- 小工具
def _file_icon(path: Path, size: int):
    """取系统文件图标（QFileIconProvider, 跟随系统主题, 不是 emoji）。"""
    from PyQt5.QtCore import QFileInfo
    from PyQt5.QtWidgets import QFileIconProvider
    try:
        provider = QFileIconProvider()
        return provider.icon(QFileInfo(str(path))).pixmap(size, size)
    except Exception:  # noqa: BLE001
        from PyQt5.QtGui import QPixmap
        return QPixmap(size, size)


def _elide_middle(text: str, keep: int = 40) -> str:
    """路径过长时中间省略, 保留头尾（头是盘符/用户, 尾是文件名）。"""
    text = str(text)
    if len(text) <= keep:
        return text
    head = keep // 2
    tail = keep - head
    return f"{text[:head]}...{text[-tail:]}"


def _human_size(size: int) -> str:
    """字节数转人类可读。"""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _kind_label(path: Path) -> str:
    """文件类型的中文说明。"""
    table = {
        ".txt": "纯文本", ".md": "Markdown", ".py": "Python 脚本",
        ".png": "PNG 图片", ".jpg": "JPG 图片", ".jpeg": "JPEG 图片",
        ".webp": "WebP 图片", ".bmp": "BMP 图片", ".gif": "GIF 图片",
        ".ico": "图标", ".pdf": "PDF 文档", ".json": "JSON",
        ".csv": "CSV 表格", ".zip": "压缩包", ".log": "日志",
    }
    suffix = path.suffix.lower()
    return table.get(suffix, suffix.lstrip(".").upper() or "未知类型")


def show_file_dialog(file_path: Path, parent: QWidget = None) -> tuple[str, str]:
    """显示文件操作弹窗，返回 (action, convert_suffix)。"""
    dlg = FileOperationDialog(file_path, parent)
    dlg.exec_()
    return dlg.result_action, dlg.convert_suffix
