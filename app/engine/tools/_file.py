"""文件操作工具：list_desktop_files / read_text_file / read_file。

read_text_file: 沙箱内读纯文本。
read_file: 按扩展名分发到对应解析器（PDF / Word / Excel / CSV / 图片 OCR / md / html）。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from ._core import ToolRegistry, is_under_any
from ._decorator import tool

log = logging.getLogger(__name__)

_HOME = Path.home()
_MAX_BYTES = 8000   # 读文本的最大字符数


def _is_safe_path(p: Path) -> bool:
    """路径必须在用户主目录下。

    用 relative_to 做路径语义比较，避免 str.startswith 的前缀绕过
    （C:\\Users\\alice-backup 不属于 C:\\Users\\alice）。
    """
    return is_under_any(p, [_HOME])


# ---------------------------------------------------------------------------
# 文档检索 + 各格式解析器（module-level helpers,被 read_file 调用）
# ---------------------------------------------------------------------------

# 文档检索的有界配置：只扫常见文档目录，且检查文件数封顶。
# （原先对整个家目录 rglob，找不到时会扫完 AppData / 缓存 / venv，
# 在 CI runner 上表现为卡死、超时——3.11、3.12 假阳性的根因。）
_DOC_DIR_NAMES = ("Desktop", "Documents", "Downloads", "OneDrive")
_DOC_EXTS = (".md", ".txt", ".docx", ".pdf")
_SKIP_DIR_NAMES = frozenset({
    "appdata", "node_modules", ".git", "__pycache__",
    "site-packages", ".venv", "venv", "cache", "caches",
})
_SCAN_BUDGET = 2000
_BUDGET_EXHAUSTED = object()


def _locate_doc(question: str) -> Path | None:
    """在常见文档目录按文件名搜文档（浅层、有界，任何机器都快速返回）。

    扫家目录顶层文件 + 桌面 / 文档 / 下载 / OneDrive（递归），跳过
    AppData、node_modules、缓存等，检查文件数封顶 2000；找不到也要快。
    """
    kw = question.lower()
    state = {"budget": _SCAN_BUDGET}

    def _check(dirpath: str, filenames):
        for fn in filenames:
            low = fn.lower()
            if not low.endswith(_DOC_EXTS):
                continue
            state["budget"] -= 1
            if kw in low:
                cand = Path(dirpath) / fn
                if cand.is_file():
                    return cand
            if state["budget"] <= 0:
                return _BUDGET_EXHAUSTED
        return None

    # 1) 家目录顶层（只看文件，不递归）
    try:
        top_files = [n for n in os.listdir(_HOME)
                     if os.path.isfile(_HOME / n)]
        hit = _check(str(_HOME), top_files)
        if hit is _BUDGET_EXHAUSTED:
            return None
        if hit is not None:
            return hit
    except OSError:
        pass

    # 2) 常见文档目录递归（剪枝 + 预算）
    roots, seen = [], set()
    for name in _DOC_DIR_NAMES:
        d = _HOME / name
        if d.is_dir():
            key = os.path.normcase(os.path.abspath(d))
            if key not in seen:
                seen.add(key)
                roots.append(d)
    for root in roots:
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [
                    d for d in dirnames
                    if not d.startswith(".")
                    and d.lower() not in _SKIP_DIR_NAMES
                ]
                hit = _check(dirpath, filenames)
                if hit is _BUDGET_EXHAUSTED:
                    return None
                if hit is not None:
                    return hit
        except (PermissionError, OSError):
            continue
    return None


def _read_pdf(p: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return "错误：PDF 解析失败：未安装 pypdf（pip install pypdf）"
    try:
        reader = PdfReader(str(p))
        text = "\n\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as e:  # noqa: BLE001
        return f"错误：PDF 解析失败：{e}"
    if not text.strip():
        return "错误：PDF 内容为空（可能扫描版无 OCR 层）"
    if len(text) > _MAX_BYTES:
        text = text[:_MAX_BYTES] + f"\n\n... (截断，原文 {len(text)} 字符)"
    return text


def _read_docx(p: Path) -> str:
    try:
        import docx
    except ImportError:
        return "错误：DOCX 解析失败：未安装 python-docx（pip install python-docx）"
    try:
        d = docx.Document(str(p))
        text = "\n\n".join(par.text for par in d.paragraphs)
    except Exception as e:  # noqa: BLE001
        return f"错误：DOCX 解析失败：{e}"
    if not text.strip():
        return "错误：DOCX 内容为空"
    if len(text) > _MAX_BYTES:
        text = text[:_MAX_BYTES] + f"\n\n... (截断)"
    return text


def _read_xlsx(p: Path) -> str:
    try:
        import openpyxl
    except ImportError:
        return "错误：XLSX 解析失败：未安装 openpyxl（pip install openpyxl）"
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
        return f"错误：XLSX 解析失败：{e}"
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
        return f"错误：CSV 解析失败：{e}"
    return "\n".join(lines) or "(空)"


def _read_image_ocr(p: Path) -> str:
    """复用 _ocr 工具的实现。"""
    from app.engine.tools._ocr import _ocr_simple
    return _ocr_simple(str(p))


def _read_md(p: Path) -> str:
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        return f"错误：读取失败：{e}"
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
        return f"错误：读取失败：{e}"
    if len(raw) > _MAX_BYTES:
        return raw[:_MAX_BYTES] + f"\n\n... (截断)"
    return raw


# ---------------------------------------------------------------------------
# 工具定义
# ---------------------------------------------------------------------------

@tool(
    description="列出桌面上的文件和文件夹。",
    parameters={"type": "object", "properties": {}},
)
def list_desktop_files() -> str:
    """列出桌面文件（仅沙箱内）。"""
    desktop = Path(os.path.join(os.environ.get("USERPROFILE", ""), "Desktop"))
    if not desktop.is_dir():
        desktop = _HOME / "Desktop"
    if not desktop.is_dir():
        return "错误：找不到桌面目录"
    files = sorted(desktop.iterdir(), key=lambda p: p.name)
    if not files:
        return "桌面是空的"
    lines = [f"{'[目录]' if p.is_dir() else '[文件]'} {p.name}" for p in files[:50]]
    total = len(files)
    if total > 50:
        lines.append(f"... 还有 {total - 50} 个文件")
    return f"桌面共 {total} 个文件：\n" + "\n".join(lines)


@tool(
    description="读取纯文本文件（txt / json / yaml 等）。沙箱限定用户主目录，"
                "超过 1MB 自动拒绝；超过 max_chars（默认 8000）会截断。",
    parameters={"type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件绝对路径"},
                    "max_chars": {"type": "integer", "minimum": 200, "maximum": 50000,
                                  "description": "最大返回字符数"},
                },
                "required": ["path"]},
)
def read_text_file(path: str, max_chars: int = _MAX_BYTES) -> str:
    """读取文本文件（沙箱内，最多 max_chars 字符，默认 8000）。"""
    if not path or not path.strip():
        return "错误：path 不能为空"
    p = Path(path).expanduser()
    if not p.exists():
        return f"错误：文件不存在：{p}"
    if not p.is_file():
        return f"错误：不是文件：{p}"
    if not _is_safe_path(p):
        return f"错误：安全策略拒绝，路径必须在用户主目录下（{p}）"
    if p.stat().st_size > 1024 * 1024:    # 1 MB
        return f"错误：文件太大（>{1} MB），用 read_file 走专门解析器"
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
        cap = max(200, min(int(max_chars if max_chars else _MAX_BYTES), 50000))
        if len(raw) > cap:
            return raw[:cap] + f"\n\n... (截断，原文 {len(raw)} 字符)"
        return raw
    except Exception as e:  # noqa: BLE001
        log.exception("read_text_file failed")
        return f"错误：读取失败：{e}"


@tool(
    description="按扩展名分发解析：PDF / Word(docx) / Excel(xlsx) / CSV / "
                "图片 OCR / Markdown / HTML / 纯文本。沙箱限定用户主目录。"
                "path 可为空——空则用 question 在家目录搜 .md/.txt。",
    parameters={"type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件绝对路径，空则用 question 搜"},
                    "question": {"type": "string",
                                 "description": "用文件名关键词搜（path 为空时生效）"},
                }},
)
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
                return f"错误：未找到包含「{question}」的文档"
            path = str(found)
        else:
            return "错误：path 不能为空"
    p = Path(path).expanduser()
    if not p.exists():
        return f"错误：文件不存在：{p}"
    if not p.is_file():
        return f"错误：不是文件：{p}"
    if not _is_safe_path(p):
        return f"错误：安全策略拒绝，路径必须在用户主目录下（{p}）"
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
        return f"错误：暂不支持该格式（{ext}）。已知：txt/md/pdf/docx/xlsx/csv/图片/HTML"
    except Exception as e:  # noqa: BLE001
        log.exception("read_file failed for %s", p)
        return f"错误：读取失败：{e}"


# ---------------------------------------------------------------------------
# 文件编辑:edit_file / multi_edit(Phase 1.1 / 1.2)
# 跟 read_* 共享沙箱(_is_safe_path)
# ---------------------------------------------------------------------------


def _atomic_write_text(path: Path, text: str) -> None:
    """原子写文件:先写临时文件 + rename,避免写到一半被打断导致文件截断。

    rename 在同一文件系统下是原子操作(POSIX 保证)。
    Windows 上 Python 的 os.replace 跨目录也安全(只要 dst 不存在)。
    """
    import tempfile
    import os
    tmp = path.with_suffix(path.suffix + ".tmp_edit")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        # 失败时清掉临时文件,避免污染目录
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def _make_diff(path: str, before: str, after: str) -> str:
    """生成 unified diff 供 dry_run / 错误回退展示。"""
    import difflib
    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=2,
    )
    return "".join(diff)


@tool(
    description=(
        "精确替换文件中的字符串(原子写)。old_string 默认必须唯一匹配(防误伤);\n"
        "要全部替换传 replace_all=True。dry_run=True 时只返回 diff,不写盘。\n"
        "沙箱限定用户主目录;失败时返回清晰错误文案。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件绝对路径"},
            "old_string": {"type": "string", "description": "要替换的原内容(精确匹配)"},
            "new_string": {"type": "string", "description": "替换后的内容"},
            "replace_all": {
                "type": "boolean",
                "description": "True = 替换所有匹配(默认 False,要求唯一)",
            },
            "dry_run": {
                "type": "boolean",
                "description": "True = 只看 diff,不写盘(默认 False)",
            },
        },
        "required": ["path", "old_string", "new_string"],
    },
)
def edit_file(path: str, old_string: str, new_string: str,
              replace_all: bool = False, dry_run: bool = False) -> str:
    """精确替换文件中的字符串。

    行为:
      - 默认要求 old_string 唯一匹配(避免误伤) → 不唯一时返回错误并指出次数
      - replace_all=True 时全部替换,返回实际替换次数
      - dry_run=True 时只生成 diff,不写盘
      - 写盘走临时文件 + rename 原子操作
      - 沙箱:_is_safe_path,家目录之外直接拒
    """
    if not path or not path.strip():
        return "错误：path 不能为空"
    if not old_string:
        return "错误：old_string 不能为空(空字符串会匹配所有位置,易误伤)"
    p = Path(path).expanduser()
    if not p.exists():
        return f"错误：文件不存在：{p}"
    if not p.is_file():
        return f"错误：不是文件：{p}"
    if not _is_safe_path(p):
        return f"错误：安全策略拒绝，路径必须在用户主目录下({p})"
    try:
        before = p.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        return f"错误：读取失败：{e}"
    count = before.count(old_string)
    if count == 0:
        return f"错误：old_string 在文件中未找到({p});请确认内容精确(包含空白/换行)"
    if not replace_all and count > 1:
        return (f"错误：old_string 在文件中匹配 {count} 次,为防误伤默认拒绝替换。"
                f"加 replace_all=True 可强制全部替换,或用 multi_edit 一次性精确处理多个匹配。")
    after = before.replace(old_string, new_string) if replace_all else before.replace(old_string, new_string, 1)
    if before == after:
        return f"错误：替换前后内容相同,没真改什么({p})"
    if dry_run:
        return f"[dry_run] 不会写盘,只是预览 diff:\n{_make_diff(str(p), before, after)}"
    try:
        _atomic_write_text(p, after)
    except Exception as e:  # noqa: BLE001
        log.exception("edit_file write failed: %s", p)
        return f"错误：写盘失败：{e}"
    actual = count if replace_all else 1
    return f"已修改 {p}(替换 {actual} 处)\n{_make_diff(str(p), before, after)}"


@tool(
    description=(
        "对同一文件做多次精确替换,事务式(全成功或全失败,任一错就回滚)。\n"
        "edits 是 list[dict],每个 dict 包含 path(可选,默认沿用顶层 path)、old_string、new_string、replace_all(可选)。\n"
        "dry_run=True 时只生成最终 diff,不写盘。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "文件绝对路径(所有 edits 默认作用此文件)"},
            "edits": {
                "type": "array",
                "description": "edit 列表,每个 dict 含 old_string + new_string + 可选 replace_all",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_string": {"type": "string"},
                        "new_string": {"type": "string"},
                        "replace_all": {"type": "boolean"},
                    },
                    "required": ["old_string", "new_string"],
                },
            },
            "dry_run": {"type": "boolean", "description": "True = 只看 diff,不写盘"},
        },
        "required": ["path", "edits"],
    },
)
def multi_edit(path: str, edits: list, dry_run: bool = False) -> str:
    """对同一文件做多次精确替换(事务式)。

    每个 edit 的 old_string 默认必须唯一;replace_all=True 时全部替换。
    任一 edit 校验失败 → 全部回滚(实际是 dry_run 预校验全部通过后才真改)。
    """
    if not path or not path.strip():
        return "错误：path 不能为空"
    if not edits:
        return "错误：edits 不能为空"
    p = Path(path).expanduser()
    if not p.exists():
        return f"错误：文件不存在：{p}"
    if not p.is_file():
        return f"错误：不是文件：{p}"
    if not _is_safe_path(p):
        return f"错误：安全策略拒绝，路径必须在用户主目录下({p})"
    try:
        before = p.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        return f"错误：读取失败：{e}"
    # 第一遍:在副本上顺序校验 + 应用,任一失败立即报错
    working = before
    applied: list[tuple[int, str, str, str]] = []  # (count, old, new, kind)
    for i, e in enumerate(edits):
        if not isinstance(e, dict):
            return f"错误：edits[{i}] 不是 dict"
        old_s = e.get("old_string", "")
        new_s = e.get("new_string", "")
        repl_all = bool(e.get("replace_all", False))
        if not old_s:
            return f"错误：edits[{i}].old_string 不能为空"
        c = working.count(old_s)
        if c == 0:
            return f"错误：edits[{i}] old_string 在文件中未找到(可能前面的 edit 已改变行号/上下文)"
        if not repl_all and c > 1:
            return f"错误：edits[{i}] old_string 匹配 {c} 次,需要唯一(传 replace_all=True 或加更多上下文)"
        working = working.replace(old_s, new_s) if repl_all else working.replace(old_s, new_s, 1)
        applied.append((c if repl_all else 1, old_s, new_s, "all" if repl_all else "once"))
    if working == before:
        return "错误：所有 edits 应用后内容未变(等于 noop)"
    if dry_run:
        return f"[dry_run] 不会写盘,只是预览 diff:\n{_make_diff(str(p), before, working)}"
    try:
        _atomic_write_text(p, working)
    except Exception as e:  # noqa: BLE001
        log.exception("multi_edit write failed: %s", p)
        return f"错误：写盘失败：{e}"
    # 2026-10-15 修复：把 per-edit 摘要 + 协议分隔符放到 diff 之前,
    # 让模型「一眼就能看到」改了几处、每处改了什么。模型容易在「总结」时
    # 漏掉 diff 内容,把 per-edit 列表前置能强制它逐条复述。
    per_edit_lines = [
        f"  [编辑 #{i}] {'replace_all' if kind == 'all' else 'once'}(替换 {cnt} 处): {old!r} → {new!r}"
        for i, (cnt, old, new, kind) in enumerate(applied)
    ]
    summary_header = (
        f"已批量修改 {p},共 {len(applied)} 处替换:\n"
        + "\n".join(per_edit_lines)
        + "\n[--- diff ---]"
    )
    return f"{summary_header}\n{_make_diff(str(p), before, working)}"


def register(reg: ToolRegistry) -> None:
    """注册文件类工具。"""
    reg.register(list_desktop_files)
    reg.register(read_text_file)
    reg.register(read_file)
    reg.register(edit_file)
    reg.register(multi_edit)


__all__ = ["register"]