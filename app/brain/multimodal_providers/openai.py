"""OpenAI 多模态 provider —— image generation (gpt-image-1 / dall-e-3)。

2026-10-15 v11: 这是 v10 _multimodal.generate_image 的 provider 化版本,
注册到 multimodal_router, 让 /api/openai 激活后, chat 里的 🎨 生成图片
直接走 OpenAI image generations endpoint。

video 不在 OpenAI 公开 API 里, 所以本 provider supports_video=False,
选了 /api/openai 调 /生成视频会被 router 报清晰提示 + 推 MiniMax H3。
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

from app.brain.multimodal_router import ApiCapability, register

log = logging.getLogger(__name__)


def _endpoint() -> str:
    base = os.environ.get("MULTIMODAL_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    return f"{base}/images/generations"


def _api_key() -> str:
    k = os.environ.get("MULTIMODAL_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    return k.strip().strip('"').strip("'")


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
            f"OpenAI HTTP {e.code} {e.reason}: {body_text[:500]}"
        ) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"OpenAI 网络错误: {e.reason}") from e


def _uploads_dir() -> Path:
    """base64 响应落盘到 assets/uploads/ 时用, file:// 引用保持 v10 一致。"""
    d = Path(__file__).resolve().parent.parent.parent / "assets" / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def gen_image(prompt: str, *, size: str = "1024x1024", n: int = 1, model: str = "") -> str:
    """OpenAI image generation. 返回 http(s) URL; 返 b64_json 时落盘到本地。"""
    if not prompt or not prompt.strip():
        raise RuntimeError("openai.gen_image: prompt 不能为空")
    key = _api_key()
    if not key:
        raise RuntimeError(
            "OPENAI_API_KEY / MULTIMODAL_API_KEY 没设。\n"
            "设置方法: setx OPENAI_API_KEY \"sk-...\" 重启桌宠, "
            "或在 config.yaml 里加 multimodal.api_key。"
        )
    used_model = (model or os.environ.get("MULTIMODAL_IMAGE_MODEL", "gpt-image-1")).strip()
    body = {
        "model": used_model,
        "prompt": prompt.strip(),
        "n": n,
        "size": size,
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    log.info("openai.gen_image model=%s size=%s prompt=%s", used_model, size, prompt[:60])
    resp = _post_json(_endpoint(), body, headers)
    data = resp.get("data") or []
    if not data:
        raise RuntimeError(f"OpenAI 响应里没 data: {json.dumps(resp)[:300]}")
    first = data[0]
    if isinstance(first, dict):
        url = first.get("url") or ""
        if url:
            return url
        b64 = first.get("b64_json") or ""
        if b64:
            fn = f"openai_{int(time.time())}.png"
            (_uploads_dir() / fn).write_bytes(base64.b64decode(b64))
            return "file:///" + str(_uploads_dir() / fn).replace("\\", "/").lstrip("/")
    raise RuntimeError(f"OpenAI 响应格式不认识: {json.dumps(first)[:300]}")


# ------------------------------------------------------------------
# 注册到 router
# ------------------------------------------------------------------
def setup() -> None:
    """调用一次, 把本 provider 装到 router。"""
    cap = ApiCapability(
        name="openai",
        display_name="OpenAI (gpt-image-1 / dall-e-3)",
        supports_image=True,
        supports_video=False,  # OpenAI 公开 API 不生视频
        supports_audio=False,
        model_image=os.environ.get("MULTIMODAL_IMAGE_MODEL", "gpt-image-1"),
        notes=(
            "走 OpenAI 兼容 image generations endpoint。"
            "环境变量: OPENAI_API_KEY / MULTIMODAL_BASE_URL / MULTIMODAL_IMAGE_MODEL。"
        ),
    )
    register(cap, gen_image=gen_image)
