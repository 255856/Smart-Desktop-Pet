"""视觉输入 —— 把图片转成 OpenAI 风格 multimodal message 的 helper。

OpenAI-compatible vision API 协议(也适用于 DeepSeek / MiniMax / Ollama 部分模型):
    messages = [
      {"role": "user",
       "content": [
         {"type": "text", "text": "这张图是什么？"},
         {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
       ]}
    ]

支持格式:
  - file path (.png / .jpg / .jpeg / .bmp / .gif / .webp)
  - raw bytes (已经读到内存的图片)
  - QPixmap / QImage (Qt 对象,可选 —— 见 encode_pixmap)

尺寸防御:
  - 单图 base64 后大小上限 8 MiB,超过截断式报错(不让 LLM 端挂掉)
  - 推荐调用方先压缩到 ~1024px 长边,避免 token 浪费
"""
from __future__ import annotations

import base64
import logging
import mimetypes
from pathlib import Path
from typing import Union

log = logging.getLogger(__name__)


# 单图 base64 编码后大小上限 —— 超过就拒绝(让 LLM 端拒收比让它吃光 context 友好)
MAX_IMAGE_B64_BYTES = 8 * 1024 * 1024

# 支持的 mime(用 mimetypes 推不出来时回退)
_DEFAULT_MIME = "image/png"

_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
}


def _guess_mime(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in _MIME_BY_EXT:
        return _MIME_BY_EXT[ext]
    mt, _ = mimetypes.guess_type(str(path))
    return mt or _DEFAULT_MIME


def image_path_to_data_url(path: Union[str, Path]) -> str:
    """把磁盘图片读出来,转成 data URL(base64 内联)。

    Raises:
        FileNotFoundError: 文件不存在
        ValueError: 编码后超 MAX_IMAGE_B64_BYTES
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"image not found: {p}")
    raw = p.read_bytes()
    if len(raw) > MAX_IMAGE_B64_BYTES:
        # 注意:read_bytes 是 raw bytes,base64 编码后会涨 ~33%
        # 上限是 base64 后大小,所以 raw 上限 = 8MiB / 1.33 ≈ 6MiB
        # 用 raw 上限 6MiB 当 sentinel 更直观
        raise ValueError(
            f"image too large: {len(raw)} bytes > 6 MiB raw "
            f"(~{len(raw) * 4 // 3} bytes base64). "
            f"Resize first."
        )
    b64 = base64.b64encode(raw).decode("ascii")
    mime = _guess_mime(p)
    return f"data:{mime};base64,{b64}"


def bytes_to_data_url(raw: bytes, mime: str = "image/png") -> str:
    """raw bytes → data URL(已读过内存的图片用)。"""
    if len(raw) > MAX_IMAGE_B64_BYTES:
        raise ValueError(
            f"image too large: {len(raw)} bytes base64"
        )
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def build_multimodal_content(
    text: str, image_data_urls: list[str],
) -> list[dict]:
    """构造 OpenAI 风格的 multimodal content 列表。

    返回的 list 可以直接当 messages[].content。
    """
    parts: list[dict] = []
    if text:
        parts.append({"type": "text", "text": text})
    for url in image_data_urls:
        parts.append({"type": "image_url", "image_url": {"url": url}})
    if not parts:
        # 退化:完全空就给个空 text,避免某些后端拒绝 content=[]
        parts.append({"type": "text", "text": ""})
    return parts


def encode_local_images(paths: list[Union[str, Path]]) -> list[str]:
    """批量转 path → data URL,跳过读失败的(打 WARNING,不抛)。"""
    out: list[str] = []
    for p in paths:
        try:
            out.append(image_path_to_data_url(p))
        except (FileNotFoundError, ValueError) as e:
            log.warning("vision: 跳过 %s: %s", p, e)
    return out


__all__ = [
    "MAX_IMAGE_B64_BYTES",
    "image_path_to_data_url",
    "bytes_to_data_url",
    "build_multimodal_content",
    "encode_local_images",
]