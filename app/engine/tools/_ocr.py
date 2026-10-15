"""OCR：优先 Windows 自带（winsdk / winrt），回退 Tesseract。

2026-10-04 审计修复要点：
  - 原实现 import 的是 legacy 8.1 命名空间 `winrt.*`，而 requirements.txt 里
    声明（且被注释掉）的是 `winsdk` → 导入必然 ImportError，OCR 恒定失败；
  - `_ocr_windows` / `_ocr_via_powershell` 是不可达的死代码，且
    `_ocr_windows` 声明 `-> str` 却 `return None`，被调用时会返回字面量 "None"；
  - Tesseract 调用没有 timeout，坏图/超大图会把桌宠永久卡死；
  - description 宣称「优先走 System.Windows.Media.Ocr」，与实际后端不符。

2026-10-15 v17 主人实测反馈「识图怎么解决」(分析一下这张图片):
  - ocr_image 只提文字, 不满足「理解图片内容」的需求
  - 新增 understand_image 工具: 走 MiniMax M3 chat completion 多模态 (image content),
    让模型理解图片主体 / 场景 / 含义; OCR 仍保留给"提取图中文字"用
"""
from __future__ import annotations

import json
import logging
import os
import platform
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from ._core import ToolRegistry
from ._decorator import tool

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


# v17: 视觉理解 — 调 MiniMax M3 chat completion 多模态 (image_url data URL)
def _vision(image_path: str, prompt: str) -> str:
    """v17 主人实测反馈「识图怎么解决」 — 调 MiniMax M3 多模态 chat completion,
    把图片 base64 内联到 image_url 字段, 让模型理解图片内容 (主体 / 场景 / 含义)。

    v16 复用 _multimodal._CONFIG, base_url / api_key 从 llm 段共享 (主人 plan 是统一 key)。
    """
    # 1) 读图 + base64 内联
    from app.brain.vision import (
        image_path_to_data_url,
        build_multimodal_content,
    )
    try:
        data_url = image_path_to_data_url(image_path)
    except FileNotFoundError:
        return f"错误：图片不存在：{image_path}"
    except ValueError as e:
        return f"错误：图片处理失败: {e}"

    # 2) 从 _multimodal._CONFIG 读 base_url / api_key / model
    #    (v16 复用: llm 段共享, 兜底 multimodal 段)
    try:
        from . import _multimodal
        base_url = _multimodal._cfg("base_url", "").rstrip("/")
        api_key = _multimodal._cfg("api_key", "")
        # 视觉用主 chat 用的 M3 model, 跟 chat 保持一致
        model = _multimodal._cfg("model", "MiniMax-M3")
    except Exception:  # noqa: BLE001
        base_url = ""
        api_key = ""
        model = "MiniMax-M3"

    if not base_url or not api_key:
        return (
            "错误：vision 缺 base_url / api_key, 主人去 config.yaml llm 段填。"
            "（v16 复用 _multimodal 配置）"
        )

    # 3) POST chat completion with image content
    url = f"{base_url}/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": build_multimodal_content(
                    prompt or "请描述这张图片的内容。",
                    [data_url],
                ),
            }
        ],
        "stream": False,
        "max_tokens": 1024,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            obj = json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace") if e.fp else ""
        # v16: 1004 错给清晰 plan key 提示
        if '"statuscode":1004' in body_text or 'login fail' in body_text.lower():
            return (
                "vision 401 鉴权失败 (MiniMax 1004): API key 跟 base_url 不匹配。\n"
                "  主人用的是 MiniMax Plan 订阅 key (sk-cp- 开头), "
                "应该走 plan 接入指南里给的 base_url, 不是任意 minimaxi 域都通。\n"
                "  主人去 MiniMax 后台「套餐详情」找 plan 专属的 base_url, "
                "填到 config.yaml 的 llm.base_url 字段。"
            )
        return f"vision HTTP {e.code}: {body_text[:300]}"
    except urllib.error.URLError as e:
        return f"vision 网络错误: {e.reason}"

    choices = obj.get("choices") or []
    if not choices:
        return f"vision 响应里没 choices: {json.dumps(obj)[:300]}"
    msg = choices[0].get("message") or {}
    content = msg.get("content") or ""
    return content.strip() or "(模型没返回内容)"


def register(reg: ToolRegistry) -> None:
    """注册 OCR + 视觉理解工具。"""

    @tool(
        description="对图片做 OCR（提取图中文字）。"
                    "image_path 空则自动截屏后识别。"
                    "需要先装 OCR 后端（winsdk 或 pytesseract+tesseract），"
                    "否则会明确告诉你不可用——不可用时请不要凭猜测描述图片。"
                    "「理解图片内容 / 描述图片」请改用 understand_image, 那个不需要装 OCR 后端。",
        parameters={"type": "object",
                    "properties": {
                        "image_path": {"type": "string",
                                       "description": "图片路径（PNG/JPG），空则自动截屏"},
                    }},
    )
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

    @tool(
        description="理解 / 描述一张图片的内容 (主体 / 场景 / 含义), 走 MiniMax M3 多模态 chat completion。"
                    "跟 ocr_image 的区别: 这个理解图片整体, 不需要装 winsdk / tesseract;"
                    "ocr_image 专门提取图中文字。"
                    "用法: understand_image(image_path='/path/to/img.png', prompt='描述图片里的内容')"
                    "image_path 必须传本地图片路径。",
        parameters={"type": "object",
                    "properties": {
                        "image_path": {"type": "string",
                                       "description": "图片绝对路径 (PNG/JPG/WebP)"},
                        "prompt": {"type": "string",
                                   "description": "问模型的问题, 如「描述图片内容」「这是什么」「图里有什么文字」"},
                    },
                    "required": ["image_path"]},
    )
    def understand_image(image_path: str, prompt: str = "请描述这张图片的内容。") -> str:
        """v17: 调 MiniMax M3 多模态 chat completion 理解图片内容。"""
        if not (image_path or "").strip():
            return "错误: image_path 不能为空, 主人传图片绝对路径"
        if not os.path.isfile(image_path):
            return f"错误: 图片不存在: {image_path}"
        return _vision(image_path, prompt)

    reg.register(ocr_image)
    reg.register(understand_image)


__all__ = ["register"]
