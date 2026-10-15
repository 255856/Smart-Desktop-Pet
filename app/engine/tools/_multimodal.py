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
from typing import Optional

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
    """不同 action 用不同 endpoint 路径 (主人截图 MiniMax 官网 image/video/
    audio endpoint 路径各不相同, OpenAI 兼容的 /images/generations 在 MiniMax
    上 404)。

    默认按 MiniMax 官网 2026-10-15 实测:
        image = image_generation  (官方文档 api.minimax.io/v1/image_generation)
        video = video_generation  (官方文档 api.minimax.io/v2/video_generation,
                                   异步 task_id, 见 generate_video 处理)
        audio = t2a_v2            (官方文档 api.minimax.io/v1/t2a_v2, TTS 同步)

    主人也可以覆盖: config.yaml multimodal.image_endpoint_path / video_endpoint_path /
    audio_endpoint_path, 跟其他多模态 provider (OpenAI / 智谱 / 阿里) 切。
    """
    cfg_path = _CONFIG.get(f"{action}_endpoint_path")
    if cfg_path:
        return str(cfg_path).lstrip("/")
    return _DEFAULT_ENDPOINTS.get(action, "image_generation")


_DEFAULT_ENDPOINTS: dict = {
    "image": "image_generation",
    "video": "video_generation",
    "audio": "t2a_v2",
}


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


def _extract_first_url(data_obj, kind: str) -> Optional[str]:
    """从 multimodal 响应里取第一个 URL 或 base64, 兼容多种 schema。

    MiniMax 官方 (image-01):
        data: {image_urls: ["https://..."], image_base64: ["..."]}
    MiniMax 代理 / OpenAI 兼容:
        data: [{url: "https://..."}, {b64_json: "..."}]
        data: [{"url": "..."}]
    其他 (智谱 / 阿里) 多半走 OpenAI 兼容。
    """
    if isinstance(data_obj, dict):
        # 官方 image: data.image_urls / data.image_base64
        urls = data_obj.get("image_urls")
        if isinstance(urls, list) and urls:
            return str(urls[0])
        if isinstance(urls, str) and urls:
            return urls
        b64s = data_obj.get("image_base64")
        if isinstance(b64s, list) and b64s:
            return "data:image/png;base64," + str(b64s[0])
        if isinstance(b64s, str) and b64s:
            return "data:image/png;base64," + b64s
    if isinstance(data_obj, list) and data_obj:
        first = data_obj[0]
        if isinstance(first, dict):
            url = first.get("url") or first.get("image_url") \
                or first.get("audio_url") or first.get("video_url") or ""
            if url:
                return str(url)
            b64 = first.get("b64_json") or first.get("image_base64") or ""
            if b64:
                mime = "audio/mpeg" if kind == "audio" else "image/png"
                return f"data:{mime};base64,{b64}"
    return None


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
    """根据 prompt 生成图片, 返回 http(s) URL (或 data:base64)。

    MiniMax 官方 (image-01) 用 aspect_ratio + response_format; OpenAI / 代理
    用 size; 兼容两种, 优先按 base_url 是不是 minimaxi.com 走 MiniMax schema。
    """
    if not prompt or not prompt.strip():
        raise RuntimeError("generate_image: prompt 不能为空")
    _check_or_raise("image")
    model = _cfg("image_model") or "image-01"
    is_minimax = "minimax" in _cfg("base_url").lower()

    if is_minimax:
        # MiniMax 官方 schema: aspect_ratio ("1:1"/"16:9") 替代 size; response_format=url
        aspect = kwargs.get("aspect_ratio") or _cfg("aspect_ratio", "1:1")
        body = {
            "model": model,
            "prompt": prompt.strip(),
            "n": int(kwargs.get("n", 1)),
            "aspect_ratio": aspect,
            "response_format": "url",
        }
    else:
        # OpenAI / 代理 schema
        size = str(kwargs.get("size") or _cfg("image_size", "1024x1024"))
        body = {
            "model": model,
            "prompt": prompt.strip(),
            "n": int(kwargs.get("n", 1)),
            "size": size,
        }
    log.info("multimodal.generate_image model=%s prompt=%s",
             model, prompt[:60])
    resp = _post_json(_build_endpoint_url("image"), body, _build_headers())
    url = _extract_first_url(resp.get("data"), "image")
    if not url:
        raise RuntimeError(
            f"image 响应里没拿到 URL/base64: {json.dumps(resp)[:300]}"
        )
    return url


def generate_video(prompt: str, **kwargs) -> str:
    """根据 prompt 生成视频, 返回 http(s) URL。

    MiniMax v2 video_generation 是异步 task_id (POST 返回 {task_id: "..."}),
    本实现同步 polling GET /v2/query/video_generation/{task_id}, 最多等
    timeout 秒拿到 succeeded content.url。

    非 MiniMax provider 默认走同步返回 URL (data[0].url / b64_json)。
    """
    if not prompt or not prompt.strip():
        raise RuntimeError("generate_video: prompt 不能为空")
    _check_or_raise("video")
    model = _cfg("video_model") or "MiniMax-H3"
    is_minimax = "minimax" in _cfg("base_url").lower()

    if is_minimax:
        # MiniMax v2 schema: content[] 结构 (text prompt 必须放进去)
        ratio = kwargs.get("ratio") or _cfg("video_ratio", "16:9")
        duration = int(kwargs.get("duration_seconds")
                       or _cfg("duration_seconds", 6))
        body = {
            "model": model,
            "content": [{"type": "text", "text": prompt.strip()}],
            "ratio": ratio,
            "duration": duration,
        }
        timeout_s = int(kwargs.get("timeout_s") or _cfg("video_timeout_s", 90))
        log.info("multimodal.generate_video model=%s duration=%s prompt=%s",
                 model, duration, prompt[:60])
        resp = _post_json(_build_endpoint_url("video"), body, _build_headers())
        task_id = resp.get("task_id") or ""
        if not task_id:
            raise RuntimeError(
                f"video 响应里没 task_id (MiniMax 异步任务): "
                f"{json.dumps(resp)[:300]}"
            )
        return _poll_video_task(task_id, timeout_s)

    # 非 MiniMax: 同步 schema
    duration = int(kwargs.get("duration_seconds")
                   or _cfg("duration_seconds", 6))
    body = {
        "model": model,
        "prompt": prompt.strip(),
        "duration_seconds": duration,
    }
    log.info("multimodal.generate_video model=%s duration=%s prompt=%s",
             model, duration, prompt[:60])
    resp = _post_json(_build_endpoint_url("video"), body, _build_headers())
    url = _extract_first_url(resp.get("data"), "video")
    if not url:
        url = resp.get("url") or resp.get("video_url") or ""
    if not url:
        raise RuntimeError(
            f"video 响应里没 url 字段: {json.dumps(resp)[:300]}"
        )
    return url


def _poll_video_task(task_id: str, timeout_s: int = 90) -> str:
    """polling MiniMax /v2/query/video_generation/{task_id} 直到 succeeded
    拿到 content.url, 或 timeout 抛错。

    v13 设计: 默认 90s 超时, 每 3s 轮询一次。MiniMax H3 6s 视频平均 30~60s 完成。
    """
    import urllib.parse
    base = _cfg("base_url").rstrip("/")
    # MiniMax v2 query path
    query_path = _CONFIG.get("video_query_path", "v2/query/video_generation")
    url = f"{base}/{query_path}/{urllib.parse.quote(task_id, safe='')}"
    headers = _build_headers()
    deadline = time.time() + max(5, timeout_s)
    poll_interval = int(_CONFIG.get("video_poll_interval_s", 3))
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read().decode("utf-8", errors="replace")
                body = json.loads(raw) if raw else {}
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            log.warning("video poll 第 %d 次失败: %s", attempt, e)
            time.sleep(poll_interval)
            continue
        task = body.get("task") or {}
        status = task.get("status") or ""
        if status == "succeeded":
            content = task.get("content") or {}
            url_out = content.get("url") or ""
            if url_out:
                return url_out
            # 备用: data.base_resp / 直接 file_id
            file_id = task.get("file_id") or ""
            if file_id:
                return f"{base}/files/{file_id}"
        elif status in ("failed", "cancelled", "expired"):
            err = task.get("error") or {}
            raise RuntimeError(
                f"video 任务 {status}: {err.get('message') or err}"
            )
        log.info("video poll 第 %d 次 status=%s, 等 %ds 再试",
                 attempt, status, poll_interval)
        time.sleep(poll_interval)
    raise RuntimeError(
        f"video 任务 {task_id} 等 {timeout_s}s 还没 succeeded, "
        f"主人可以加大 multimodal.video_timeout_s (默认 90s)。"
    )


def generate_audio(prompt: str, **kwargs) -> str:
    """根据 prompt 生成音频 / 音乐, 返回 http(s) URL 或 data:base64。

    MiniMax t2a_v2 (TTS) 同步返回; music_generation 是异步 task_id (类似
    video, 用 _poll_video_task 走 query endpoint, 复用路径)。
    """
    if not prompt or not prompt.strip():
        raise RuntimeError("generate_audio: prompt 不能为空")
    _check_or_raise("audio")
    model = _cfg("audio_model") or "speech-01"
    is_minimax = "minimax" in _cfg("base_url").lower()

    if is_minimax and "t2a_v2" in _endpoint_path("audio"):
        # TTS 同步: model + text + voice_id 等
        body = {
            "model": model,
            "text": prompt.strip(),
        }
    elif is_minimax and "music" in _endpoint_path("audio"):
        # music 异步 (跟 video 类似, 复用 _poll_video_task)
        body = {
            "model": model,
            "prompt": prompt.strip(),
        }
    else:
        body = {
            "model": model,
            "prompt": prompt.strip(),
        }
    log.info("multimodal.generate_audio model=%s prompt=%s",
             model, prompt[:60])
    resp = _post_json(_build_endpoint_url("audio"), body, _build_headers())

    # music 异步 task_id (跟 video 走同一 polling 路径)
    if is_minimax and "music" in _endpoint_path("audio"):
        task_id = resp.get("task_id") or ""
        if task_id:
            timeout_s = int(_CONFIG.get("audio_timeout_s", 90))
            return _poll_video_task(task_id, timeout_s)
        # 兜底继续尝试同步提取
    url = _extract_first_url(resp.get("data"), "audio")
    if not url:
        url = resp.get("url") or resp.get("audio_url") or ""
    if not url:
        raise RuntimeError(
            f"audio 响应里没 url 字段: {json.dumps(resp)[:300]}"
        )
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
