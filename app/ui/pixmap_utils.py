"""QPixmap 缩放工具：等比缩放 + 保 alpha + 去白边 + LRU 缓存。"""
from __future__ import annotations

import logging
from typing import Optional

from app.core.qt_compat import QImage, QPixmap, Qt

log = logging.getLogger(__name__)


# LRU 缓存：{(cache_key, width, height): scaled_pixmap}。
# 同一帧 pixmap 不要每帧重新缩放。
_scaled_cache: dict[tuple, QPixmap] = {}
_CACHE_MAX_SIZE = 8


def scale_pixmap_keep_alpha(
    pix: QPixmap,
    size,
    cache_key: Optional[tuple] = None,
) -> QPixmap:
    """等比缩放 QPixmap，保留 alpha 并消除白点。"""
    if pix.isNull():
        return pix

    if cache_key is not None and cache_key in _scaled_cache:
        return _scaled_cache[cache_key]

    try:
        import numpy as np
        img = pix.toImage().convertToFormat(QImage.Format.Format_ARGB32)
        scaled = img.scaled(
            size, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        # 扫白边像素 → 强制 a=0
        arr = np.array(scaled, copy=True)
        rgb = arr[..., :3]
        a = arr[..., 3]
        # 近白像素：RGB 全 >= 245
        white_mask = (
            (rgb[..., 0] >= 245)
            & (rgb[..., 1] >= 245)
            & (rgb[..., 2] >= 245)
        ) & (a > 0)
        arr[white_mask, 3] = 0
        from PIL import Image as _PI
        cleaned = _PI.fromarray(arr, "RGBA")
        result = QPixmap.fromImage(cleaned.toImage())
    except Exception:
        result = pix.scaled(
            size, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    if cache_key is not None:
        if len(_scaled_cache) >= _CACHE_MAX_SIZE:
            # 简单 LRU：删除最旧的一个
            oldest_key = next(iter(_scaled_cache))
            del _scaled_cache[oldest_key]
        _scaled_cache[cache_key] = result

    return result


def clear_pixmap_cache() -> None:
    """清空缩放缓存。"""
    _scaled_cache.clear()


__all__ = ["scale_pixmap_keep_alpha", "clear_pixmap_cache"]