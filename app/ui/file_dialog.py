"""文件拖入桌宠时的弹窗：吃掉（回收站）/ 转换（格式转换）。

设计：
    - 拖入任意文件 → 弹出文件操作弹窗
    - 「吃掉」：文件移到回收站 + 播放吃饭动画
    - 「转换」：弹出格式转换菜单，支持常见格式转换
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from app.core.qt_compat import (
    QDialog, QDialogButtonBox, QFileDialog, QInputDialog, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
    Qt, QVBoxLayout, QWidget,
)

log = logging.getLogger(__name__)

# 支持的转换格式映射：{源后缀: {目标后缀: 转换函数}}
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

# 可以"吃掉"的文件类型（常见文档和图片）
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
    """把文件移到回收站（Windows 用 shell32 API，其他平台用 shutil.move 到 ~/.trash）。"""
    try:
        import ctypes
        SHFileOperationW = ctypes.windll.shell32.SHFileOperationW
    except (AttributeError, OSError):
        # 非 Windows 或无 shell32：退化为 move 到回收站目录
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
    """非 Windows 平台：移动到用户回收站目录。"""
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
            # GIF 首帧 → 静态格式
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
    """获取文件支持的转换格式列表。返回 [(显示名, 后缀), ...]"""
    src_suffix = path.suffix.lower()
    conversions = _SUPPORTED_CONVERSIONS.get(src_suffix, {})
    display_names = {
        ".png": "PNG 图片", ".jpg": "JPG 图片", ".jpeg": "JPEG 图片",
        ".webp": "WebP 图片", ".bmp": "BMP 图片", ".gif": "GIF 动画",
        ".txt": "纯文本", ".md": "Markdown",
    }
    return [(display_names.get(ext, ext.upper()), ext) for ext in conversions.keys()]


class FileOperationDialog(QDialog):
    """文件操作弹窗：吃掉 / 转换。"""

    def __init__(self, file_path: Path, parent: QWidget = None):
        super().__init__(parent)
        self.file_path = file_path
        self.result_action: str = ""  # "eat" | "convert" | ""
        self.convert_suffix: str = ""
        self._build_ui()

    def _build_ui(self) -> None:
        self.setWindowTitle(f"📄 {self.file_path.name}")
        self.setModal(True)
        self.setMinimumWidth(360)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # 文件信息
        info = QLabel()
        try:
            size_kb = self.file_path.stat().st_size / 1024
            size_text = f"{size_kb:.1f} KB"
        except OSError:
            size_text = "未知"
        info.setText(
            f"📄 <b>{self.file_path.name}</b><br/>"
            f"📁 {self.file_path.parent}<br/>"
            f"📦 {size_text}"
        )
        info.setStyleSheet("font-size: 10pt; color: #333; padding: 8px;")
        layout.addWidget(info)

        # 操作按钮
        btn_row = QHBoxLayout()

        self.btn_eat = QPushButton("😋 吃掉！")
        self.btn_eat.setToolTip("把文件移到回收站，桌宠播放吃文件动画")
        self.btn_eat.setStyleSheet(
            "background: qlineargradient(x1:0,y1:0,x2:1,y2:1,"
            "stop:0 #f472b6, stop:1 #db2777); color: #ffffff; "
            "border-radius: 12px; padding: 10px 24px; font-weight: 700; font-size: 11pt; "
            "border: none;")
        self.btn_eat.clicked.connect(self._on_eat)
        btn_row.addWidget(self.btn_eat)

        self.btn_convert = QPushButton("🔄 格式转换")
        self.btn_convert.setToolTip("转换文件格式")
        self.btn_convert.setStyleSheet(
            "background: qlineargradient(x1:0,y1:0,x2:1,y2:1,"
            "stop:0 #8b5cf6, stop:1 #6d28d9); color: #ffffff; "
            "border-radius: 12px; padding: 10px 24px; font-weight: 700; font-size: 11pt; "
            "border: none;")
        self.btn_convert.clicked.connect(self._on_convert)
        btn_row.addWidget(self.btn_convert)

        layout.addLayout(btn_row)

        # 取消
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setStyleSheet(
            "border: 1px solid #ddd; border-radius: 8px; padding: 8px; color: #666;")
        self.btn_cancel.clicked.connect(self.reject)
        layout.addWidget(self.btn_cancel, alignment=Qt.AlignCenter)

    def _on_eat(self) -> None:
        self.result_action = "eat"
        self.accept()

    def _on_convert(self) -> None:
        conversions = get_available_conversions(self.file_path)
        if not conversions:
            QMessageBox.warning(self, "转换",
                f"不支持该文件类型的转换（{self.file_path.suffix}）\n"
                "支持的格式：PNG/JPG/WebP/BMP/GIF/TXT/MD")
            return

        # 弹出格式选择
        names = [c[0] for c in conversions]
        chosen, ok = QInputDialog.getText(
            self, "选择目标格式",
            "要转换为什么格式？",
            QLineEdit.EchoMode.Normal,
            " → ".join(names),
        )
        if ok and chosen:
            # 解析选择（默认值用 " → " 分隔，用户可能输入数字或选择）
            self.result_action = "convert"
            self.convert_suffix = ""
            # 尝试匹配
            for name, ext in conversions:
                if name.lower() in chosen.lower() or ext.lower() in chosen.lower():
                    self.convert_suffix = ext
                    break
            if not self.convert_suffix and conversions:
                self.convert_suffix = conversions[0][1]
            self.accept()


def show_file_dialog(file_path: Path, parent: QWidget = None) -> tuple[str, str]:
    """显示文件操作弹窗，返回 (action, convert_suffix)。"""
    dlg = FileOperationDialog(file_path, parent)
    dlg.exec_()
    return dlg.result_action, dlg.convert_suffix
