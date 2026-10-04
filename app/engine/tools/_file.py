"""文件操作工具：list_desktop_files / read_text_file / read_file。

read_text_file: 沙箱内读纯文本。
read_file: 按扩展名分发到对应解析器（PDF / Word / Excel / CSV / 图片 OCR / md / html）。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from ._core import Tool, ToolRegistry

log = logging.getLogger(__name__)

_HOME = Path.home()
_MAX_BYTES = 8000   # 读文本的最大字符数


def _is_safe_path(p: Path) -> bool:
    """路径必须在用户主目录下。"""
    try:
        p_resolved = p.resolve()
        return str(p_resolved).startswith(str(_HOME.resolve()))
    except (OSError, RuntimeError):
        return False


def register(reg: ToolRegistry) -> None:
    """注册文件类工具。"""

    def list_desktop_files() -> str:
        """列出桌面文件（仅沙箱内）。"""
        desktop = Path(os.path.join(os.environ.get("USERPROFILE", ""), "Desktop"))
        if not desktop.is_dir():
            desktop = _HOME / "Desktop"
        if not desktop.is_dir():
            return "找不到桌面目录"
        files = sorted(desktop.iterdir(), key=lambda p: p.name)
        if not files:
            return "桌面是空的"
        lines = [f"{'📁' if p.is_dir() else '📄'} {p.name}" for p in files[:50]]
        total = len(files)
        if total > 50:
            lines.append(f"... 还有 {total - 50} 个文件")
        return f"桌面共 {total} 个文件：\n" + "\n".join(lines)

    def read_text_file(path: str, max_chars: int = _MAX_BYTES) -> str:
        """读取文本文件（沙箱内，最多 max_chars 字符，默认 8000）。"""
        if not path or not path.strip():
            return "错误：path 不能为空"
        p = Path(path).expanduser()
        if not p.exists():
            return f"文件不存在：{p}"
        if not p.is_file():
            return f"不是文件：{p}"
        if not _is_safe_path(p):
            return f"安全策略拒绝：路径必须在用户主目录下（{p}）"
        if p.stat().st_size > 1024 * 1024:    # 1 MB
            return f"文件太大（>{1} MB），用 read_file 走专门解析器"
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
            cap = max(200, min(int(max_chars if max_chars else _MAX_BYTES), 50000))
            if len(raw) > cap:
                return raw[:cap] + f"\n\n... (截断，原文 {len(raw)} 字符)"
            return raw
        except Exception as e:  # noqa: BLE001
            log.exception("read_text_file failed")
            return f"读取失败：{e}"

    def read_file(path: str = "", question: str = "") -> str:
        """按扩展名分发解析：PDF / Word / Excel / CSV / 图片 OCR / html / md。
        path 为空则尝试用 question 搜家目录（仅 .md/.txt）。
        沙箱内。
        """
        if not path or not path.strip():
            # 用 question 在家目录搜常见文档
            if question and question.strip():
                found = _locate_doc(question)
                if not found:
                    return f"未找到包含「{question}」的文档"
                path = str(found)
            else:
                return "错误：path 不能为空"
        p = Path(path).expanduser()
        if not p.exists():
            return f"文件不存在：{p}"
        if not p.is_file():
            return f"不是文件：{p}"
        if not _is_safe_path(p):
            return f"安全策略拒绝：路径必须在用户主目录下（{p}）"
        ext = p.suffix.lower()
        try:
            if ext == ".pdf":
                return _read_pdf(p)
            if ext in (".docx", ".doc"):
                return _read_docx(p)
            if ext in (".xlsx", ".xls"):
                return _read_xlsx(p)
            if ext == ".csv":
                return _read_csv(p)
            if ext in (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp"):
                return _read_image_ocr(p)
            if ext in (".md", ".markdown"):
                return _read_md(p)
            if ext in (".html", ".htm"):
                return _read_html(p)
            if ext in (".txt", ".json", ".yaml", ".yml", ".csv", ".tsv"):
                return _read_plain(p)
            return f"暂不支持该格式（{ext}）。已知：txt/md/pdf/docx/xlsx/csv/图片/HTML"
        except Exception as e:  # noqa: BLE001
            log.exception("read_file failed for %s", p)
            return f"读取失败：{e}"

    def _locate_doc(question: str) -> Path | None:
        """在家目录搜文件名含 question 的文档（优先 .md/.txt）。"""
        kw = question.lower()
        try:
            for ext in (".md", ".txt", ".docx", ".pdf"):
                for p in _HOME.rglob(f"*{ext}"):
                    if not p.is_file():
                        continue
                    name = p.stem.lower()    # 匹配文件名不含扩展名
                    if kw in name or kw in p.name.lower():
                        return p
        except (PermissionError, OSError):
            return None
        return None

    def _read_pdf(p: Path) -> str:
        try:
            from pypdf import PdfReader
        except ImportError:
            return "PDF 解析失败：未安装 pypdf（pip install pypdf）"
        try:
            reader = PdfReader(str(p))
            text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception as e:  # noqa: BLE001
            return f"PDF 解析失败：{e}"
        if not text.strip():
            return "PDF 内容为空（可能扫描版无 OCR 层）"
        if len(text) > _MAX_BYTES:
            text = text[:_MAX_BYTES] + f"\n\n... (截断，原文 {len(text)} 字符)"
        return text

    def _read_docx(p: Path) -> str:
        try:
            import docx
        except ImportError:
            return "DOCX 解析失败：未安装 python-docx（pip install python-docx）"
        try:
            d = docx.Document(str(p))
            text = "\n\n".join(par.text for par in d.paragraphs)
        except Exception as e:  # noqa: BLE001
            return f"DOCX 解析失败：{e}"
        if not text.strip():
            return "DOCX 内容为空"
        if len(text) > _MAX_BYTES:
            text = text[:_MAX_BYTES] + f"\n\n... (截断)"
        return text

    def _read_xlsx(p: Path) -> str:
        try:
            import openpyxl
        except ImportError:
            return "XLSX 解析失败：未安装 openpyxl（pip install openpyxl）"
        try:
            wb = openpyxl.load_workbook(str(p), read_only=True, data_only=True)
            lines = [f"# Sheet: {wb.sheetnames}"]
            for sheet_name in wb.sheetnames[:5]:
                ws = wb[sheet_name]
                lines.append(f"\n## {sheet_name}")
                for i, row in enumerate(ws.iter_rows(values_only=True), 1):
                    if i > 50:
                        lines.append("... (截断，前 50 行)")
                        break
                    # 把 None 替换为 ""
                    lines.append(" | ".join(str(c) if c is not None else "" for c in row[:20]))
        except Exception as e:  # noqa: BLE001
            return f"XLSX 解析失败：{e}"
        return "\n".join(lines)

    def _read_csv(p: Path) -> str:
        import csv
        try:
            with open(p, "r", encoding="utf-8", errors="replace", newline="") as f:
                lines = []
                reader = csv.reader(f)
                for i, row in enumerate(reader, 1):
                    if i > 200:
                        lines.append("... (截断，前 200 行)")
                        break
                    lines.append(" | ".join(str(c) for c in row))
        except Exception as e:  # noqa: BLE001
            return f"CSV 解析失败：{e}"
        return "\n".join(lines) or "(空)"

    def _read_image_ocr(p: Path) -> str:
        """复用 _ocr 工具的实现。"""
        from app.engine.tools._ocr import _ocr_simple
        return _ocr_simple(str(p))

    def _read_md(p: Path) -> str:
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            return f"读取失败：{e}"
        if len(raw) > _MAX_BYTES:
            return raw[:_MAX_BYTES] + f"\n\n... (截断)"
        return raw

    def _read_html(p: Path) -> str:
        try:
            from bs4 import BeautifulSoup
            raw = p.read_text(encoding="utf-8", errors="replace")
            soup = BeautifulSoup(raw, "html.parser")
            text = soup.get_text(separator="\n", strip=True)
        except ImportError:
            return "HTML 解析失败：未安装 bs4（pip install beautifulsoup4）"
        except Exception as e:  # noqa: BLE001
            return f"HTML 解析失败：{e}"
        if len(text) > _MAX_BYTES:
            return text[:_MAX_BYTES] + f"\n\n... (截断)"
        return text

    def _read_plain(p: Path) -> str:
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            return f"读取失败：{e}"
        if len(raw) > _MAX_BYTES:
            return raw[:_MAX_BYTES] + f"\n\n... (截断)"
        return raw

    reg.register(Tool(
        name="list_desktop_files",
        description="列出桌面上的文件和文件夹。",
        parameters={"type": "object", "properties": {}},
        fn=list_desktop_files,
    ))
    reg.register(Tool(
        name="read_text_file",
        description="读取纯文本文件（txt / json / yaml 等）。沙箱限定用户主目录，"
                    "超过 1MB 自动拒绝；超过 max_chars（默认 8000）会截断。",
        parameters={"type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "文件绝对路径"},
                        "max_chars": {"type": "integer", "minimum": 200, "maximum": 50000,
                                      "description": "最大返回字符数"},
                    },
                    "required": ["path"]},
        fn=read_text_file,
    ))
    reg.register(Tool(
        name="read_file",
        description="按扩展名分发解析：PDF / Word(docx) / Excel(xlsx) / CSV / "
                    "图片 OCR / Markdown / HTML / 纯文本。沙箱限定用户主目录。"
                    "path 可为空——空则用 question 在家目录搜 .md/.txt。",
        parameters={"type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "文件绝对路径，空则用 question 搜"},
                        "question": {"type": "string",
                                     "description": "用文件名关键词搜（path 为空时生效）"},
                    }},
        fn=read_file,
    ))


__all__ = ["register"]