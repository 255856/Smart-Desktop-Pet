"""多模态生成工具 (image / video)。

2026-10-15 v10: 主人实测要求 — 「你去实现多模态的工具调用」。
设计:
  - 默认走 OpenAI 兼容协议 (gpt-image-1 / dall-e-3) —— 任何 OpenAI 兼容
    endpoint 都可接 (Replicate / SiliconFlow / 智谱 / MiniMax 等)。
  - base_url / api_key / model 全部走环境变量, 不写死:
      MULTIMODAL_BASE_URL     默认 https://api.openai.com/v1
      MULTIMODAL_API_KEY      默认走 OPENAI_API_KEY
      MULTIMODAL_IMAGE_MODEL  默认 gpt-image-1
      MULTIMODAL_VIDEO_MODEL  默认 (空, 见 generate_video 实现)
  - tool fn 返回 http(s) URL 字符串, 跟 _on_media_action 的 markdown image
    渲染路径直连。
  - 缺 key / 网络错时抛 RuntimeError, chat_window 那层 catch 渲染给主人看。

video 留接口但不内置真 provider —— 因为视频生成各家 API 差异太大
(MiniMax H3 / Pika / Replicate / RunwayML / 智谱 CogVideoX), 让主人在配置
里挑一个 (或下个 Phase 4.* 接上)。
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request

log = logging.getLogger(__name__)


# 通用 OpenAI 兼容 image generations endpoint
def _endpoint() -> str:
    base = os.environ.get("MULTIMODAL_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    return f"{base}/images/generations"


def _api_key() -> str:
    k = os.environ.get("MULTIMODAL_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    return k.strip().strip('"').strip("'")


def _image_model() -> str:
    return os.environ.get("MULTIMODAL_IMAGE_MODEL", "gpt-image-1")


def _video_model() -> str:
    return os.environ.get("MULTIMODAL_VIDEO_MODEL", "").strip()


def _post_json(url: str, body: dict, headers: dict, timeout: int = 120) -> dict:
    """POST 一个 JSON body, 拿回 JSON 响应。失败抛 RuntimeError。"""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers=headers, method="POST",
    )
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


# ------------------------------------------------------------------
# generate_image —— 真接 OpenAI 兼容 image generations endpoint
# ------------------------------------------------------------------
def generate_image(
    prompt: str,
    *,
    model: Optional[str] = None,
    size: str = "1024x1024",
    n: int = 1,
) -> str:
    """根据 prompt 生成图片, 返回 http(s) URL。

    适配 OpenAI gpt-image-1 / dall-e-3 / SiliconFlow Qwen-Image / 智谱 CogView
    等 OpenAI 兼容 endpoint。返回的 URL 是 1 小时内有效的临时链接。

    异常:
      RuntimeError — 缺 key / 网络错 / 响应里没 URL
    """
    if not prompt or not prompt.strip():
        raise RuntimeError("generate_image: prompt 不能为空")
    key = _api_key()
    if not key:
        raise RuntimeError(
            "MULTIMODAL_API_KEY 没设 (或在 OPENAI_API_KEY 里也找不到)。\n"
            "设置方法: setx MULTIMODAL_API_KEY \"sk-...\" 然后重启桌宠, "
            "或在 config.yaml 里加 multimodal.api_key 项。"
        )
    body = {
        "model": model or _image_model(),
        "prompt": prompt.strip(),
        "n": n,
        "size": size,
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    log.info("multimodal: generate_image model=%s size=%s prompt=%s",
             body["model"], size, prompt[:60])
    resp = _post_json(_endpoint(), body, headers)
    # OpenAI 兼容响应: data: [{ url: "..." , b64_json: "..." }]
    data = resp.get("data") or []
    if not data:
        raise RuntimeError(
            f"模型响应里没有 data 字段: {json.dumps(resp)[:300]}"
        )
    first = data[0]
    if isinstance(first, dict):
        url = first.get("url") or ""
        if url:
            return url
        # 部分 provider 不返回 url, 返 b64_json —— 这里落盘到 assets/uploads
        # 然后用 file:// URL 嵌图, 跟 v10 上传文件路径一致。
        b64 = first.get("b64_json") or ""
        if b64:
            from pathlib import Path as _P
            import base64 as _b64
            d = _P(__file__).resolve().parent.parent.parent.parent / "assets" / "uploads"
            d.mkdir(parents=True, exist_ok=True)
            fn = f"gpt_{int(time.time())}.png"
            (d / fn).write_bytes(_b64.b64decode(b64))
            return "file:///" + str(d / fn).replace("\\", "/").lstrip("/")
    raise RuntimeError(
        f"模型响应格式不认识: {json.dumps(first)[:300]}"
    )


# ------------------------------------------------------------------
# generate_video —— 接口留好, 默认 stub 抛清晰错误, 等主人接真 provider
# ------------------------------------------------------------------
def generate_video(
    prompt: str,
    *,
    model: Optional[str] = None,
    duration_seconds: int = 4,
    resolution: str = "720p",
) -> str:
    """根据 prompt 生成视频, 返回 http(s) URL。

    默认 stub —— 没接真 provider 时抛 RuntimeError 告诉主人怎么接。
    接法 (任选其一):
      - MiniMax video / 字节豆包视频: 改这个函数体, 调他们的 HTTP API。
      - Replicate: 用 replicate 库, 把 model_id 改成 cogvideox / animate-diff 等。
      - 智谱 CogVideoX: 用 zhipuai SDK, cogvideox 接口。

    异常:
      RuntimeError — 还没接 / 缺 key / 网络错
    """
    if not prompt or not prompt.strip():
        raise RuntimeError("generate_video: prompt 不能为空")
    chosen = (model or _video_model()).strip()
    if not chosen:
        raise RuntimeError(
            "视频生成还没接真 provider。\n\n"
            "接法 (任选):\n"
            "  1. MiniMax video / 字节豆包视频 — 改 app/engine/tools/_multimodal.py "
            "里的 generate_video 函数体, 调对应 HTTP API。\n"
            "  2. Replicate — `pip install replicate`, 选 cogvideox / "
            "animate-diff 等 model, 在这里调 replicate.run(model_id, input={...})。\n"
            "  3. 智谱 CogVideoX — pip install zhipuai, 调 zhipuai.model_api "
            "里的 cogvideox 接口。\n\n"
            "完成后, 这里 raise NotImplementedError 那行删掉, 改 return URL。\n"
            "环境变量: MULTIMODAL_VIDEO_MODEL (默认空) + provider 自己的 api_key。"
        )
    # 选了 model 但还没实现, 跟空一样给清晰错误
    raise RuntimeError(
        f"generate_video: 选了 model {chosen!r} 但还没接实现, "
        f"改 app/engine/tools/_multimodal.py:generate_video 函数体。"
    )


# ------------------------------------------------------------------
# register —— 让 build_default_tools 能 pick up
# ------------------------------------------------------------------
def register(reg, **_kwargs) -> None:
    """注册到 ToolRegistry。parameters 用最简 JSON Schema。"""
    from ._core import Tool

    reg.register(Tool(
        name="generate_image",
        description=(
            "根据 prompt 生成图片, 返回 http(s) URL。\n"
            "参数: prompt (str) — 描述要生成的图片。"
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
            "参数: prompt (str) — 描述要生成的视频。"
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


__all__ = ["register", "generate_image", "generate_video"]
