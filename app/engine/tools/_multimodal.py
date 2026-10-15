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


# module-level config, 由 setup_config() 注入 (chat_window 构造时调一次)
# v15: 整个 config dict 注入 (含 llm / multimodal 段), 默认从 llm 段读
# base_url / api_key 跟 chat 共享 — 主人 plan 是统一 key, 不用再配一份。
_CONFIG: dict = {}


def setup_config(cfg: dict) -> None:
    """注入配置。cfg 是整个 config.yaml dict (含 llm / multimodal / ... 段),
    之后 multimodal tool 优先从 cfg.multimodal 读, fallback 到 cfg.llm。

    v15 简化 (主人实测 2026-10-15: 「config.yaml 里的配置好像在复杂化」):
      - base_url / api_key 默认从 llm 段读 (主人 plan 是统一 key)
      - endpoint_path 不再让用户配, 按 base_url 域名自动判断
      - 3 个 model 字段 (image / video / audio) 是 multimodal 段唯一必填项
    """
    global _CONFIG
    _CONFIG = dict(cfg or {})


def _cfg(key: str, default: str = "") -> str:
    """读 config 字段 (v15 兼容顺序):
      1) 顶层字段 (旧 v12-v14 测试 / 直接 setup_config({"base_url": ...}))
      2) multimodal 段字段 (新 v15 习惯)
      3) llm 段字段 (base_url / api_key 跟 chat 共享)
      4) env 变量 (MULTIMODAL_BASE_URL 等)
    """
    # 1) 顶层 (兼容老测试)
    v = _CONFIG.get(key)
    if v:
        return str(v).strip()
    # 2) multimodal 段
    mm = _CONFIG.get("multimodal") or {}
    v = mm.get(key) if isinstance(mm, dict) else None
    if v:
        return str(v).strip()
    # 3) llm 段
    llm = _CONFIG.get("llm") or {}
    v = llm.get(key) if isinstance(llm, dict) else None
    if v:
        return str(v).strip()
    # 4) env 变量
    env = os.environ.get("MULTIMODAL_" + key.upper())
    if env:
        return env.strip()
    return default


def _is_enabled() -> bool:
    enabled = _CONFIG.get("multimodal", {}).get("enabled", True)
    if isinstance(enabled, str):
        return enabled.strip().lower() not in ("false", "0", "no", "")
    return bool(enabled)


def _base_domain() -> str:
    """base_url 的 hostname 部分, 用来判断走 MiniMax 官方 schema 还是 OpenAI 兼容。"""
    base = _cfg("base_url", "").lower()
    if "minimax" in base:
        return "minimax"
    if "openai" in base:
        return "openai"
    return "other"


def _endpoint_path(action: str) -> str:
    """v15 简化: 按 base_url 域名自动判断 endpoint 路径, 不再让用户配
    endpoint_path 字段 (主人反馈「config.yaml 里的配置好像在复杂化」)。

    规则:
      minimax (含 minimax.cn / minimax.io / minimaxi.com / minimaxi.chat)
        image = image_generation
        video = video_generation  (异步 task_id, 见 generate_video)
        audio = t2a_v2
      openai / 其他
        全部走 OpenAI 兼容的 /images/generations (image / video / audio 同路径)
    """
    domain = _base_domain()
    if domain == "minimax":
        return {
            "image": "image_generation",
            # MiniMax H3 视频必须用 v2 端点（v1 报"该模型请使用 /v2"）
            "video": "v2/video_generation",
            "audio": "t2a_v2",
        }.get(action, "image_generation")
    # OpenAI 兼容 (含主人想用其他 provider)
    return {
        "image": "images/generations",
        "video": "videos/generations",
        "audio": "audio/speech",
    }.get(action, "images/generations")


def _post_json(url: str, body: dict, headers: dict, timeout: int = 120) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace") if e.fp else ""
        # v16: 1004 错 (login fail) 通常是 key 跟 base_url 不匹配 (plan key
        # 走 plan 域, pay-as-you-go 走另一个域), 提示主人去 MiniMax 后台
        # 看 plan 接入指南的 base_url, 别只看 stack trace。
        if '"statuscode":1004' in body_text or 'login fail' in body_text.lower():
            raise RuntimeError(
                "MiniMax 401 鉴权失败 (statuscode=1004, login fail): "
                "API key 跟 base_url 不匹配。\n\n"
                "  - 主人用的是 MiniMax Plan 订阅 key (sk-cp- 开头), "
                "应该走 plan 接入指南里给的 base_url, 不是任意 minimaxi 域都通。\n"
                "  - 主人去 MiniMax 后台「套餐详情」或「接入指南」找 plan 专属的 base_url, "
                "填到 config.yaml 的 llm.base_url 字段。\n"
                "  - 主人截图红线写得很清楚: 「本 API Key 适用于按量付费... "
                "使用 M Plan 或 Credits 资源时, 请改用套餐详情中的订阅 Key」"
                f"\n\n  原始响应: {body_text[:200]}"
            ) from e
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


def _download_to_uploads(url_or_data: str, kind: str) -> str:
    """v14: 把 generate_* 返回的 URL / data:base64 落盘到项目 assets/uploads/,
    返回 file:/// 绝对路径。这样 chat_store 持久化的是项目内绝对路径,
    重启后 file 还在, 渲染时 _inline_local_images_to_data_url 能找到。

    主人实测 2026-10-15: 「桌宠返回的文件, 图片之类的也应该存在文件夹中」—
    落盘后不受 MiniMax 24h URL 过期影响, 跨重启稳定。

    kind 决定后缀 (image / video / audio)。支持三种输入:
      - http(s)://   → urlopen 下载
      - data:<mime>;base64,XXX  → 解码 XXX
      - file:/// 已经本地, 直接转 file:/// 绝对路径返回 (不重复复制)
    """
    if url_or_data.startswith("file:///"):
        return url_or_data  # 已经在本地, 不动

    ext_map = {
        "image": "png",      # 通用后缀, 真实后缀从 URL/MIME 推断会更准但 chat 渲染不挑
        "video": "mp4",
        "audio": "mp3",
    }
    ext = ext_map.get(kind, "bin")
    fn = f"multimodal_{kind}_{int(time.time() * 1000)}.{ext}"
    target = _uploads_dir() / fn

    if url_or_data.startswith("data:"):
        # data:image/png;base64,XXX
        try:
            b64 = url_or_data.split(",", 1)[1]
            target.write_bytes(base64.b64decode(b64))
        except (IndexError, ValueError, OSError) as e:
            raise RuntimeError(f"data: URL 落盘失败: {e}") from e
    elif url_or_data.startswith(("http://", "https://")):
        try:
            req = urllib.request.Request(url_or_data, headers={
                "User-Agent": "desktop-pet/1.0"
            })
            with urllib.request.urlopen(req, timeout=120) as r:
                target.write_bytes(r.read())
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as e:
            raise RuntimeError(
                f"下载 multimodal {kind} 失败 ({url_or_data[:80]}): {e}"
            ) from e
    else:
        raise RuntimeError(
            f"multimodal {kind} 收到不认识的结果 (不是 http(s):// / data: / file://): "
            f"{url_or_data[:80]}"
        )

    abs_path = str(target).replace("\\", "/").lstrip("/")
    return f"file:///{abs_path}"


def _build_endpoint_url(action: str) -> str:
    # video 端点用 video_base_url（MiniMax v2 不带 /v1），其他走 base_url
    if action == "video":
        video_base = _cfg("video_base_url")
        if video_base:
            base = video_base.rstrip("/")
        else:
            # fallback：base_url 自动剥 /v1（image/chat 走 OpenAI /v1 兼容）
            base = _cfg("base_url").rstrip("/").removesuffix("/v1")
    else:
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
    # v14: 落盘到项目 assets/uploads/, 返回 file:// 路径
    return _download_to_uploads(url, "image")


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
    # MiniMax v2 视频端点 base URL 不带 /v1（image / chat 走 /v1）
    video_base = _cfg("video_base_url") or _cfg("base_url").rstrip("/").removesuffix("/v1")

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
            # MiniMax H3 视频必填 resolution —— 试 480p / 768p, 不支持 720p/1080p
            "resolution": kwargs.get("resolution") or _cfg("video_resolution", "480p"),
        }
        timeout_s = int(kwargs.get("timeout_s") or _cfg("video_timeout_s", 90))
        log.info("multimodal.generate_video model=%s duration=%s resolution=%s prompt=%s",
                 model, duration, body["resolution"], prompt[:60])
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
    # 同 _build_endpoint_url("video") 逻辑：MiniMax v2 不带 /v1
    video_base = _cfg("video_base_url")
    if video_base:
        base = video_base.rstrip("/")
    else:
        base = _cfg("base_url").rstrip("/").removesuffix("/v1")
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
        # TTS 同步: model + text + voice_setting.voice_id (MiniMax t2a_v2 嵌套 schema)
        body = {
            "model": model,
            "text": prompt.strip(),
            "voice_setting": {
                "voice_id": kwargs.get("voice_id")
                or _cfg("audio_voice_id", "female-yujie"),
            },
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
            # v14: polling 拿到 URL 后落盘
            url = _poll_video_task(task_id, timeout_s)
            return _download_to_uploads(url, "audio")
        # 兜底继续尝试同步提取
    # MiniMax t2a_v2 同步返回: data.audio 是 hex 字符串（不是 URL）
    data = resp.get("data") or {}
    if isinstance(data, dict):
        # audio 是 hex 字符串（之前 _extract_first_url 不认）—— 落盘为 mp3
        hex_audio = data.get("audio")
        if isinstance(hex_audio, str) and hex_audio:
            import binascii as _binascii
            try:
                fn = f"multimodal_audio_{int(time.time())}.mp3"
                (_uploads_dir() / fn).write_bytes(_binascii.unhexlify(hex_audio))
                return "file:///" + str(_uploads_dir() / fn).replace("\\", "/").lstrip("/")
            except Exception as e:
                log.warning("解析 t2a_v2 hex 音频失败：%s", e)
    url = _extract_first_url(data, "audio")
    if not url:
        url = resp.get("url") or resp.get("audio_url") or ""
    if not url:
        raise RuntimeError(
            f"audio 响应里没 url 字段: {json.dumps(resp)[:300]}"
        )
    # v14: 落盘
    return _download_to_uploads(url, "audio")


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
