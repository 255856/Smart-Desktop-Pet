"""OCR：优先 Windows 自带（winsdk / winrt），回退 Tesseract。

2026-10-04 审计修复要点：
  - 原实现 import 的是 legacy 8.1 命名空间 `winrt.*`，而 requirements.txt 里
    声明（且被注释掉）的是 `winsdk` → 导入必然 ImportError，OCR 恒定失败；
  - `_ocr_windows` / `_ocr_via_powershell` 是不可达的死代码，且
    `_ocr_windows` 声明 `-> str` 却 `return None`，被调用时会返回字面量 "None"；
  - Tesseract 调用没有 timeout，坏图/超大图会把桌宠永久卡死；
  - description 宣称「优先走 System.Windows.Media.Ocr」，与实际后端不符。
"""
from __future__ import annotations

import logging
import os
import platform
import tempfile
from pathlib import Path

from ._core import Tool, ToolRegistry

log = logging.getLogger(__name__)

IS_WINDOWS = platform.system() == "Windows"

# OCR 后端都不可用时的统一文案。要说清「需要装什么」，
# 否则模型拿到一句含糊的失败后倾向自己编内容。
_NO_BACKEND = (
    "错误：OCR 不可用。Windows 内置 OCR 需要 pip install winsdk；"
    "另一条路是 pip install pytesseract 并另外安装 tesseract 二进制。"
    "两条都没装，所以无法识别这张图——请不要凭猜测描述图片内容。"
)


def _ocr_winsdk(image_path: str) -> str | None:
    """Windows 内置 OCR。winsdk / winrt 任一可用即可，都不可用返回 None。"""
    if not IS_WINDOWS:
        return None
    for ns in ("winsdk", "winrt"):
        try:
            if ns == "winsdk":
                from winsdk.windows.globalization import Language  # noqa: F401
                from winsdk.windows.graphics.imaging import BitmapDecoder
                from winsdk.windows.media.ocr import OcrEngine
                from winsdk.windows.storage import StorageFile
            else:
                from winrt.windows.graphics.imaging import BitmapDecoder  # noqa: F401
                from winrt.windows.media.ocr import OcrEngine  # noqa: F401
                from winrt.windows.storage import StorageFile  # noqa: F401
            break
        except ImportError:
            continue
    else:
        return None

    try:
        file = StorageFile.get_file_from_path_async(os.path.abspath(image_path))
        file = file.get() if hasattr(file, "get") else file
        stream = file.open_read_async()
        stream = stream.get() if hasattr(stream, "get") else stream
        decoder = BitmapDecoder.create_async(stream)
        decoder = decoder.get() if hasattr(decoder, "get") else decoder
        bitmap = decoder.get_software_bitmap_async()
        bitmap = bitmap.get() if hasattr(bitmap, "get") else bitmap
        engine = OcrEngine.try_create_from_user_profile_languages() \
            or OcrEngine.try_create_from_user_profile()
        result = engine.recognize_async(bitmap)
        result = result.get() if hasattr(result, "get") else result
        return (result.text or "").strip() or "(未识别到文字)"
    except Exception as e:  # noqa: BLE001
        log.warning("%s ocr failed: %s", ns, e)
        return None


def _ocr_tesseract(image_path: str) -> str | None:
    """Tesseract 兜底。装了返回文本，没装返回 None。"""
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(image_path) as img:
            # timeout 必加：pytesseract 超时抛的是自己的 RuntimeError，
            # 坏图/超大图会一直阻塞在 communicate() 上
            text = pytesseract.image_to_string(
                img, lang="chi_sim+eng", timeout=30)
        return text.strip() or "(未识别到文字)"
    except RuntimeError as e:      # pytesseract.TesseractError / 超时
        log.warning("tesseract timeout/error: %s", e)
        return "错误：OCR 超时或识别失败（图片可能过大或损坏）"
    except Exception as e:  # noqa: BLE001
        log.warning("tesseract failed: %s", e)
        return f"错误：OCR 失败（{type(e).__name__}）"


def _ocr(image_path: str) -> str:
    """按 Windows 内置 → Tesseract 的顺序尝试，都不行就如实说不行。"""
    for backend in (_ocr_winsdk, _ocr_tesseract):
        r = backend(image_path)
        if r is not None:
            return r
    return _NO_BACKEND


def register(reg: ToolRegistry) -> None:
    """注册 OCR 工具。"""

    def ocr_image(image_path: str = "") -> str:
        """对图片做 OCR（提取图中文字）。image_path 不传则截全屏后识别。"""
        tmp_path = None
        try:
            if not (image_path or "").strip():
                # 没传路径：自动截屏 + OCR
                if IS_WINDOWS:
                    try:
                        from PIL import ImageGrab
                        img = ImageGrab.grab()
                    except Exception as e:  # noqa: BLE001
                        return f"错误：自动截屏失败（{type(e).__name__}）"
                else:
                    import subprocess
                    fd, name = tempfile.mkstemp(suffix=".png")
                    os.close(fd)
                    tmp_path = name
                    try:
                        subprocess.run(["screencapture", "-x", name],
                                       check=False, timeout=10)
                    except Exception as e:  # noqa: BLE001
                        return f"错误：自动截屏失败（{type(e).__name__}）"
                    from PIL import Image
                    img = Image.open(name)
                fd, name = tempfile.mkstemp(suffix=".png")
                os.close(fd)
                tmp_path = name
                img.save(name)
                path = name
            else:
                path = image_path
            if not os.path.isfile(path):
                return f"错误：图片不存在：{path}"
            return _ocr(path)
        finally:
            # 原来这里不删临时文件，截一次屏就在 temp 里留一张
            if tmp_path:
                try:
                    Path(tmp_path).unlink(missing_ok=True)
                except OSError:
                    pass

    reg.register(Tool(name="ocr_image",
        description="对图片做 OCR（提取图中文字）。"
                    "image_path 空则自动截屏后识别。"
                    "需要先装 OCR 后端（winsdk 或 pytesseract+tesseract），"
                    "否则会明确告诉你不可用——不可用时请不要凭猜测描述图片。",
        parameters={"type": "object",
                    "properties": {
                        "image_path": {"type": "string",
                                       "description": "图片路径（PNG/JPG），空则自动截屏"},
                    }},
        fn=ocr_image))


__all__ = ["register"]
