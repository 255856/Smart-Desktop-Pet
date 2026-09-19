"""PetRenderer 工厂：根据配置选 sprite / live2d。

调用方（PetWindow）通过 create_renderer(cfg, ...) 拿到 PetRenderer 接口实现，
不用关心具体是 sprite 还是 live2d。

如果选了 live2d 但 PyQtWebEngine 没装，自动 fallback 到 sprite 并打 WARNING。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from app.animation.pet_renderer import PetRenderer

log = logging.getLogger(__name__)


def create_renderer(
    renderer_type: str,
    *,
    sprite_dir: Path,
    fallback_image: Optional[Path] = None,
    window_size,
    scale: float = 1.0,
    live2d_model_dir: Optional[Path] = None,
    live2d_hide_watermark: bool = True,
    live2d_random_exp_cfg: Optional[dict] = None,
    live2d_max_fps: int = 30,
) -> PetRenderer:
    """根据 cfg 创建对应渲染器。

    Args:
        renderer_type: "sprite" | "live2d"
        sprite_dir: sprite 资源目录（fallback_image 兜底图）
        fallback_image: sprite 模式下找不到动画时的兜底
        window_size: QSize，目标渲染区尺寸
        scale: sprite 预缩放系数；live2d 时传给 PIXI 缩放
        live2d_model_dir: live2d 模型根目录（含 model3.json）
        live2d_hide_watermark: live2d 模式下是否隐藏模型自带的 WaterMark 水印图层
        live2d_random_exp_cfg: 挂机随机表情 {enabled, min_s, max_s}（来自 config）

    Returns:
        PetRenderer 实例（sprite 总是成功；live2d 失败会回退到 sprite）
    """
    if renderer_type == "live2d":
        return _try_create_live2d(
            sprite_dir=sprite_dir,
            fallback_image=fallback_image,
            window_size=window_size,
            scale=scale,
            live2d_model_dir=live2d_model_dir,
            live2d_hide_watermark=live2d_hide_watermark,
            live2d_random_exp_cfg=live2d_random_exp_cfg,
            live2d_max_fps=live2d_max_fps,
        )
    # 默认 / 显式 sprite
    return _create_sprite(
        sprite_dir=sprite_dir,
        fallback_image=fallback_image,
        window_size=window_size,
        scale=scale,
    )


def _create_sprite(*, sprite_dir, fallback_image, window_size, scale) -> PetRenderer:
    """创建 sprite 渲染器（永远成功）。"""
    from app.animation.sprite_renderer import SpriteRenderer
    return SpriteRenderer(
        sprite_dir=sprite_dir,
        fallback_image=fallback_image,
        window_size=window_size,
        scale=scale,
    )


def _try_create_live2d(*, sprite_dir, fallback_image, window_size, scale,
                       live2d_model_dir, live2d_hide_watermark=True,
                       live2d_random_exp_cfg=None, live2d_max_fps=30) -> PetRenderer:
    """尝试创建 Live2D 渲染器；失败 fallback 到 sprite。"""
    if not live2d_model_dir:
        log.warning("renderer=live2d 但未配置 pet.live2d.model_dir；fallback 到 sprite")
        return _create_sprite(sprite_dir=sprite_dir, fallback_image=fallback_image,
                              window_size=window_size, scale=scale)
    if not live2d_model_dir.is_dir():
        log.warning("Live2D 模型目录不存在: %s；fallback 到 sprite", live2d_model_dir)
        return _create_sprite(sprite_dir=sprite_dir, fallback_image=fallback_image,
                              window_size=window_size, scale=scale)
    # 检查 model3.json 是否存在
    candidates = list(live2d_model_dir.glob("*.model3.json"))
    if not candidates:
        log.warning("Live2D 模型目录没有 model3.json: %s；fallback 到 sprite", live2d_model_dir)
        return _create_sprite(sprite_dir=sprite_dir, fallback_image=fallback_image,
                              window_size=window_size, scale=scale)

    try:
        from app.animation.live2d_renderer import Live2DRenderer, _WEB_ENGINE_AVAILABLE
    except ImportError as e:
        log.warning("Live2DRenderer import 失败: %s；fallback 到 sprite", e)
        return _create_sprite(sprite_dir=sprite_dir, fallback_image=fallback_image,
                              window_size=window_size, scale=scale)

    if not _WEB_ENGINE_AVAILABLE:
        log.warning("PyQtWebEngine 不可用；fallback 到 sprite。"
                    "安装 PyQtWebEngine 后可启用 Live2D: pip install PyQtWebEngine")
        return _create_sprite(sprite_dir=sprite_dir, fallback_image=fallback_image,
                              window_size=window_size, scale=scale)

    try:
        renderer = Live2DRenderer(
            model_dir=live2d_model_dir,
            widget_size=(window_size.width(), window_size.height()),
            scale=scale,
            hide_watermark=live2d_hide_watermark,
            random_exp_cfg=live2d_random_exp_cfg,
            max_fps=live2d_max_fps,
        )
        log.info("Live2DRenderer 已创建: %s", live2d_model_dir)
        return renderer
    except Exception as e:  # noqa: BLE001
        log.exception("Live2DRenderer 创建失败: %s；fallback 到 sprite", e)
        return _create_sprite(sprite_dir=sprite_dir, fallback_image=fallback_image,
                              window_size=window_size, scale=scale)


__all__ = ["create_renderer"]