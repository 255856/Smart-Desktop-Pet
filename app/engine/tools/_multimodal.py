"""多模态生成工具 (image / video / audio) —— config-driven (v12)。

2026-10-15 v12 简化: 不再 v11 的 router + 多 provider 注册, 改走单一组
config.yaml 配置:

    multimodal:
      enabled: true
      base_url: https://api.minimaxi.com/v1    # 默认跟 llm 同
      api_key: sk-cp-...                        # 默认从 llm.api_key 读
      image_model: MiniMax-image-01
      video_model: MiniMax-H3
      audio_model: MiniMax-speech-01
      image_size: 1024x1024
      duration_seconds: 4

主人实测设计 (2026-10-15 第五轮):
  "我看了 minmax 官网请求地址不同, 所以我觉得可以把这种参数与普通的 api
   地址放在一起由用户填写" —— 跟 chat api 一样填在 config.yaml 一段, 多模态
  tool 就根据输入的参数继续生成。不需要 v11 的 /api/<name> 路由切换。

v11 的 multimodal_router / multimodal_providers/ 抽象代码保留 (以后
扩展多 provider 用得上), 本文件改成 config-driven 单一组参数。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)


# module-level config, 由 setup_config() 注入 (chat_window 在 build tools
# 之前调一次)。
_CONFIG: dict = {}


def setup_config(cfg: dict) -> None:
    """注入 multimodal 配置, 之后 tool fn 都从这里读。"""
    global _CONFIG
    _CONFIG = dict(cfg or {})


def _cfg(key: str, default: str = "") -> str:
    """读 config 字段, 跟环境变量回退 (env 优先是 PyQt5 风格, 这里 cfg 优先)。"""
    v = _CONFIG.get(key)
    if v:
        return str(v).strip()
    env = os.environ.get("MULTIMODAL_" + key.upper())
    if env:
        return env.strip()
    return default


def _is_enabled() -> bool:
    enabled = _CONFIG.get("enabled", True)
    if isinstance(enabled, str):
        return enabled.strip().lower() not in ("false", "0", "no", "")
    return bool(enabled)


def _endpoint_path(action: str) -> str:
    """不同 action 用不同 endpoint 路径 (主人截图 MiniMax 官网 image/video
    endpoint 路径不同)。默认 image = /images/generations, video / audio 由
    config.video_endpoint_path / audio_endpoint_path 覆盖, 没填就用同一组
    OpenAI 兼容的 /images/generations 兜底。
    """
    cfg_path = _CONFIG.get(f"{action}_endpoint_path")
    if cfg_path:
        return str(cfg_path).lstrip("/")
    # 默认按 OpenAI 兼容约定
    return "images/generations"


def _post_json(url: str, body: dict, headers: dict, timeout: int = 120) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace") if e.fp else ""
        raise RuntimeError(
            f"HTTP {e.code} {e.reason}: {body_text[:500]}"
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"网络错误: {e.reason}") from e


def _uploads_dir() -> Path:
    """base64 响应落盘到 assets/uploads/ 时用。"""
    d = Path(__file__).resolve().parent.parent.parent.parent / "assets" / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _build_endpoint_url(action: str) -> str:
    base = _cfg("base_url").rstrip("/")
    return f"{base}/{_endpoint_path(action)}"


def _build_headers() -> dict:
    return {
        "Authorization": f"Bearer {_cfg('api_key')}",
        "Content-Type": "application/json",
    }


def _check_or_raise(action: str) -> None:
    if not _is_enabled():
        raise RuntimeError(
            "multimodal 没启用, 配 config.yaml 加 multimodal.enabled=true, "
            "或者完全删除该段 (默认 enabled=true)。"
        )
    if not _cfg("base_url"):
        raise RuntimeError(
            "multimodal.base_url 没填 (config.yaml 的 multimodal 段), "
            "跟 llm.base_url 同源就行。"
        )
    if not _cfg("api_key"):
        raise RuntimeError(
            "multimodal.api_key 没填 (config.yaml 的 multimodal 段), "
            "跟 llm.api_key 同源就行。"
        )


def generate_image(prompt: str, **kwargs) -> str:
    """根据 prompt 生成图片, 返回 http(s) URL (或落盘后的 file://)。"""
    if not prompt or not prompt.strip():
        raise RuntimeError("generate_image: prompt 不能为空")
    _check_or_raise("image")
    model = _cfg("image_model") or _cfg("image_model_default", "gpt-image-1")
    size = str(kwargs.get("size") or _cfg("image_size", "1024x1024"))
    body = {
        "model": model,
        "prompt": prompt.strip(),
        "n": int(kwargs.get("n", 1)),
        "size": size,
    }
    log.info("multimodal.generate_image model=%s size=%s prompt=%s",
             model, size, prompt[:60])
    resp = _post_json(_build_endpoint_url("image"), body, _build_headers())
    data = resp.get("data") or []
    if not data:
        raise RuntimeError(f"响应里没 data 字段: {json.dumps(resp)[:300]}")
    first = data[0]
    if isinstance(first, dict):
        url = first.get("url") or ""
        if url:
            return url
        b64 = first.get("b64_json") or ""
        if b64:
            fn = f"multimodal_{int(time.time())}.png"
            (_uploads_dir() / fn).write_bytes(base64.b64decode(b64))
            return "file:///" + str(_uploads_dir() / fn).replace("\\", "/").lstrip("/")
    raise RuntimeError(f"响应格式不认识: {json.dumps(first)[:300]}")


def generate_video(prompt: str, **kwargs) -> str:
    """根据 prompt 生成视频, 返回 http(s) URL。"""
    if not prompt or not prompt.strip():
        raise RuntimeError("generate_video: prompt 不能为空")
    _check_or_raise("video")
    model = _cfg("video_model") or _cfg("video_model_default", "video-model")
    duration = int(kwargs.get("duration_seconds") or _cfg("duration_seconds", 4))
    body = {
        "model": model,
        "prompt": prompt.strip(),
        "duration_seconds": duration,
    }
    log.info("multimodal.generate_video model=%s duration=%s prompt=%s",
             model, duration, prompt[:60])
    resp = _post_json(_build_endpoint_url("video"), body, _build_headers())
    # 不同 provider video 响应 schema 差异大, 通用提取
    url = (
        resp.get("url")
        or resp.get("video_url")
        or (resp.get("data") or [{}])[0].get("url")
        or ""
    )
    if not url:
        raise RuntimeError(f"video 响应里没 url 字段: {json.dumps(resp)[:300]}")
    return url


def generate_audio(prompt: str, **kwargs) -> str:
    """根据 prompt 生成音频 / 音乐, 返回 http(s) URL。"""
    if not prompt or not prompt.strip():
        raise RuntimeError("generate_audio: prompt 不能为空")
    _check_or_raise("audio")
    model = _cfg("audio_model") or _cfg("audio_model_default", "audio-model")
    body = {
        "model": model,
        "prompt": prompt.strip(),
    }
    log.info("multimodal.generate_audio model=%s prompt=%s", model, prompt[:60])
    resp = _post_json(_build_endpoint_url("audio"), body, _build_headers())
    url = (
        resp.get("url")
        or resp.get("audio_url")
        or (resp.get("data") or [{}])[0].get("url")
        or ""
    )
    if not url:
        raise RuntimeError(f"audio 响应里没 url 字段: {json.dumps(resp)[:300]}")
    return url


def register(reg, **_kwargs) -> None:
    """注册到 ToolRegistry. cfg 走 setup_config() module-level 注入 (上面)。"""
    from ._core import Tool

    reg.register(Tool(
        name="generate_image",
        description=(
            "根据 prompt 生成图片, 返回 http(s) URL。\n"
            "参数: prompt (str) — 描述要生成的图片。\n"
            "配置: config.yaml 的 multimodal 段 (base_url / api_key / image_model)。"
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
            "配置: config.yaml 的 multimodal 段 (base_url / api_key / video_model)。"
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
            "配置: config.yaml 的 multimodal 段 (audio_model)。"
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


__all__ = [
    "register", "setup_config",
    "generate_image", "generate_video", "generate_audio",
]
