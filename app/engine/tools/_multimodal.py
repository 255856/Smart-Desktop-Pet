"""多模态生成工具 (image / video) —— 适配层 (v11)。

2026-10-15 v11 升级: 不再写死 OpenAI 调 gpt-image-1, 改走 multimodal_router:
  - 主人 /api/<name> 选一个 API provider (openai / MiniMax / 智谱 / 阿里百炼 / ...)
  - chat 里点 🎨 生成图片 / 🎬 生成视频 → tool fn 调 router.route(action, prompt)
  - router 根据当前 active provider 找 handler, 没设就报"主人先 /api/<name>",
    选了但不支持就报"当前 API 不支持 X, 切到 Y 后重试"

新增 provider: 在 app/brain/multimodal_providers/ 加一个 .py 跟 openai.py
/MiniMax.py 同样 shape, 然后 multimodal_providers/__init__.py:setup_all() 加
一行 import + setup()。不用改本文件。
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def generate_image(prompt: str, **kwargs) -> str:
    """根据 prompt 生成图片, 返回 http(s) URL。

    v11: 走 multimodal_router.route("image", prompt, **kwargs),
    让主人 /api/<name> 决定调哪个 provider。
    """
    from app.brain.multimodal_router import route
    return route("image", prompt, **kwargs)


def generate_video(prompt: str, **kwargs) -> str:
    """根据 prompt 生成视频, 返回 http(s) URL。

    v11: 走 multimodal_router.route("video", prompt, **kwargs)。
    """
    from app.brain.multimodal_router import route
    return route("video", prompt, **kwargs)


def generate_audio(prompt: str, **kwargs) -> str:
    """根据 prompt 生成音频 / 音乐, 返回 http(s) URL。

    v11 新增: 之前 v9 留了 stub 引导主人接, 现在走 router 同样模式。
    """
    from app.brain.multimodal_router import route
    return route("audio", prompt, **kwargs)


# ------------------------------------------------------------------
# register —— 让 build_default_tools 能 pick up
# ------------------------------------------------------------------
def register(reg, **_kwargs) -> None:
    """注册到 ToolRegistry。parameters 用最简 JSON Schema。

    v11: 同时自动初始化 multimodal_providers (让 router 知道有哪些可选 API),
    避免 chat_window 没显式 init 时 router 是空的。
    """
    try:
        from app.brain.multimodal_providers import setup_all
        setup_all()
    except Exception:  # noqa: BLE001
        log.exception("multimodal_providers.setup_all 失败, 路由可能找不到 provider")

    from ._core import Tool

    reg.register(Tool(
        name="generate_image",
        description=(
            "根据 prompt 生成图片, 返回 http(s) URL。\n"
            "参数: prompt (str) — 描述要生成的图片。\n"
            "路由: 走当前激活的 API provider (/api/<name> 切换), "
            "没设会报「主人先选 API」。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "prompt": {"type": "string",
                           "description": "要生成的图片描述"},
                "size": {"type": "string", "default": "1024x1024",
                         "description": "图片尺寸, 如 1024x1024 / 1536x1024"},
            },
            "required": ["prompt"],
        },
        fn=generate_image,
    ))

    reg.register(Tool(
        name="generate_video",
        description=(
            "根据 prompt 生成短视频, 返回 http(s) URL。\n"
            "参数: prompt (str) — 描述要生成的视频。\n"
            "路由: 走当前激活的 API provider, 不支持视频就报「切到 Y 重试」。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "prompt": {"type": "string",
                           "description": "要生成的视频描述"},
                "duration_seconds": {"type": "integer", "default": 4},
            },
            "required": ["prompt"],
        },
        fn=generate_video,
    ))

    reg.register(Tool(
        name="generate_audio",
        description=(
            "根据 prompt 生成音频 / 音乐, 返回 http(s) URL。\n"
            "路由: 走当前激活的 API provider。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "prompt": {"type": "string",
                           "description": "要生成的音频描述"},
            },
            "required": ["prompt"],
        },
        fn=generate_audio,
    ))


__all__ = ["register", "generate_image", "generate_video", "generate_audio"]
