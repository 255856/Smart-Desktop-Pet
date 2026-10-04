"""截图 OCR：用 Windows 自带 OCR（System.Drawing + WinRT.Media.Ocr）或 Tesseract 兜底。"""
from __future__ import annotations

import logging
import os
import platform
import subprocess
import tempfile

from ._core import Tool, ToolRegistry

log = logging.getLogger(__name__)

IS_WINDOWS = platform.system() == "Windows"


def _ocr_windows(image_path: str) -> str:
    """Windows 10+ 自带 OCR（WinRT.Media.Ocr）。"""
    try:
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.globalization import Language
        from winrt.windows.storage import StorageFile
        from winrt.windows.graphics.imaging import BitmapDecoder
    except ImportError:
        return None  # 调 Tesseract 兜底

    try:
        engine = OcrEngine.create_for_user_profile(
            OcrEngine.try_create_from_user_profile().recognizer_language
            if False else None  # 用默认 zh-Hans-CN
        )
        # 简化：直接用系统 OCR
        from winrt.windows.media.ocr import OcrEngine as Ocr
        from winrt.windows.globalization import Language
        from winrt.windows.graphics.imaging import BitmapDecoder as BD
        from PIL import Image

        img = Image.open(image_path)
        # Windows.Media.Ocr 期望 SoftwareBitmap
        # 通过 PowerShell 调用是更简单路径——下面 fallback 走 ps
        return _ocr_via_powershell(image_path)
    except Exception:
        return _ocr_via_powershell(image_path)


def _ocr_via_powershell(image_path: str) -> str:
    """通过 PowerShell 调 Windows.Media.Ocr。PowerShell 5.1+ 自带 System.Drawing。"""
    ps = (
        "Add-Type -AssemblyName System.Runtime.WindowsRuntime;"
        "Add-Type -AssemblyName System.Drawing;"
        "$null = [Windows.Media.Ocr.OcrEngine, Windows.Media.Ocr.OcrResult, "
        "  Windows.Globalization.Language, Windows.Graphics.Imaging.BitmapDecoder, Windows.Storage.StorageFile]"
        # 真正调用——略复杂，我们用更轻量的 Tesseract 兜底
    )
    # 跳过 WinRT 复杂 API，直接 fallback Tesseract
    return _ocr_tesseract(image_path)


def _ocr_tesseract(image_path: str) -> str:
    """Tesseract 兜底（用户自行 pip install pytesseract）。"""
    try:
        import pytesseract
        from PIL import Image
        text = pytesseract.image_to_string(Image.open(image_path), lang="chi_sim+eng")
        return text.strip() or "(未识别到文字)"
    except ImportError:
        return "OCR 不可用：Windows 自带 WinRT 路径失败，且未安装 pytesseract"
    except Exception as e:  # noqa: BLE001
        return f"OCR 失败：{e}"


def _ocr_simple(image_path: str) -> str:
    """最简实现：先试 Windows 自带（pywinrt），失败用 Tesseract。"""
    if not IS_WINDOWS:
        return _ocr_tesseract(image_path)
    # Windows: 用 UIA / WPF 路径调 System.Windows.Media.Ocr
    try:
        import winrt.windows.media.ocr as ocr
        from winrt.windows.storage import StorageFile
        from winrt.windows.graphics.imaging import BitmapDecoder
        file = StorageFile.get_file_from_path_async(image_path).get()
        stream = file.open_read_async().get()
        decoder = BitmapDecoder.create_async(stream).get()
        bitmap = decoder.get_software_bitmap_async().get()
        engine = ocr.OcrEngine.create_for_user_profile()
        result = engine.recognize_async(bitmap).get()
        return result.text.strip() or "(未识别到文字)"
    except ImportError:
        return _ocr_tesseract(image_path)
    except Exception as e:  # noqa: BLE001
        log.warning("winrt ocr failed: %s", e)
        return _ocr_tesseract(image_path)


def register(reg: ToolRegistry) -> None:
    """注册 OCR 工具。"""

    def ocr_image(image_path: str = "") -> str:
        """对图片做 OCR。image_path 不传则截全屏后识别（需要先 take_screenshot）。"""
        if not image_path or not image_path.strip():
            # 没传路径：自动截屏 + OCR
            if IS_WINDOWS:
                try:
                    from PIL import ImageGrab
                    img = ImageGrab.grab()
                except Exception as e:  # noqa: BLE001
                    return f"自动截屏失败：{e}"
            else:
                try:
                    import subprocess
                    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                    tmp.close()
                    subprocess.run(["screencapture", "-x", tmp.name], check=False)
                    from PIL import Image
                    img = Image.open(tmp.name)
                except Exception as e:  # noqa: BLE001
                    return f"自动截屏失败：{e}"
            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            img.save(tmp.name)
            path = tmp.name
        else:
            path = image_path
        if not os.path.isfile(path):
            return f"图片不存在：{path}"
        return _ocr_simple(path)

    reg.register(Tool(name="ocr_image",
        description="对图片做 OCR（提取图中文字）。"
                    "image_path 空则自动截屏后识别。"
                    "Windows 优先走 System.Windows.Media.Ocr，未安装 pywinrt 时回退 Tesseract。",
        parameters={"type": "object",
                    "properties": {
                        "image_path": {"type": "string",
                                       "description": "图片路径（PNG/JPG），空则自动截屏"},
                    }},
        fn=ocr_image))


__all__ = ["register"]