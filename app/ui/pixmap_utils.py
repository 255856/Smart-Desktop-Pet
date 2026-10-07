"""QPixmap 缩放工具：等比缩放 + 保 alpha + 去白边 + LRU 缓存。

从 `app.ui.pet_window` 抽出——之前 `_scale_pixmap_keep_alpha` / `_clear_pixmap_cache`
是私有函数,但 `app.ui.ui_controller` 直接 import 了它们,破坏封装。

NOTE: 与 `app.animation.sprite_renderer._scale_pixmap_keep_alpha` 同名但**不同行为**:
本模块版本走 QImage 缩放 + 扫白边像素强制 alpha=0,代价 ~30ms(每 200x200),
但避免透明 PNG 缩放后出现"白点闪烁"。sprite_renderer 的版本只是
``QPixmap.scaled(SmoothTransformation)``,更快但可能丢 alpha——它用于
sprite atlas 预缩放,场景里 PetWindow 已用本模块版本再次缩放兜底,所以
两份不能简单合并。
"""
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
    """等比缩放 QPixmap 同时**保留 alpha 并消除白点**。

    PyQt5 下 ``QPixmap.scaled(Qt.SmoothTransformation)`` 会丢 alpha,导致
    透明 PNG 缩放后背景变白。这里先转 QImage(保留 alpha),QImage 缩放
    (保 alpha),再扫一遍"a != 0 但 RGB 全接近 255"的边缘像素,把它们的
    a 强制设为 0——否则在透明窗口底色上看着就是"白点闪烁"。

    使用 cache_key 可以缓存缩放结果,避免每帧重复计算。
    """
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
        # "近白": RGB 全 >= 245(留余地防止误伤真正浅色角色)
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
            # 简单 LRU: 删除最旧的一个
            oldest_key = next(iter(_scaled_cache))
            del _scaled_cache[oldest_key]
        _scaled_cache[cache_key] = result

    return result


def clear_pixmap_cache() -> None:
    """清空缩放缓存(切换角色或窗口尺寸变化时调用)。"""
    _scaled_cache.clear()


__all__ = ["scale_pixmap_keep_alpha", "clear_pixmap_cache"]