"""多模态生成 API 路由 —— 解决「不同 model API 与对应多模态工具之间的适配」。

2026-10-15 v11: 主人实测设计 ——
有的 API 提生图 (OpenAI gpt-image-1), 有的提生视频 (MiniMax H3), 有的全有,
有的都没有。主人 /api/<name> 选 API 后, 多模态 tool 走这个 router:
  - 当前选用的 API 声明支持 image_generation → 调对应 provider 的 image handler
  - 不支持 → 如实告诉主人 (包含 "切到 X 后重试" 的指引)
  - 主人没选 → 同样给清晰提示 (让主人 /api/<name> 选一个)

跟 chat_window 的 /skills/<name> 激活 (v9b) 平行:
  - /skills/<name> 激活 "行为 skill" (system prompt 补丁) — v9 现有
  - /api/<name>   激活 "API provider" (multimodal tool 路由目标) — v11 新加
两者互不冲突, 同一 session 可以同时激活: 比如 /api/MiniMax 让 H3 走 MiniMax 视频,
同时 /skills/poem 让桌宠用七言绝句回答。

设计要点:
  - ApiCapability 数据类描述该 provider 提哪些能力。
  - 每个 provider 自己注册 handler (gen_image / gen_video / gen_audio callable)。
  - router.route() 走: active provider → 查 capability → 调 handler → 抛清晰错。
  - 全部 module-level 单例 (避免跟 Qt 实例化链路耦合)。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger(__name__)


# 三个 action 名, 跟 multimodal tool / provider handler 对齐
ACTION_IMAGE = "image"
ACTION_VIDEO = "video"
ACTION_AUDIO = "audio"


@dataclass
class ApiCapability:
    """单个 API provider 提的能力声明 + 描述。

    fields:
      name             - provider 唯一名字 (e.g. "openai" / "MiniMax")
      display_name     - 给主人看的名字 (e.g. "OpenAI gpt-image-1")
      supports_image   - 能不能生图
      supports_video   - 能不能生视频
      supports_audio   - 能不能生音频 / 音乐
      model_image      - 该 provider 的 image model 名字 (e.g. "gpt-image-1")
      model_video      - 视频 model 名字 (e.g. "MiniMax-H3")
      model_audio      - 音频 model 名字
      notes            - 自由文本, 描述该 provider 注意点 / 套餐限制
    """
    name: str
    display_name: str = ""
    supports_image: bool = False
    supports_video: bool = False
    supports_audio: bool = False
    model_image: str = ""
    model_video: str = ""
    model_audio: str = ""
    notes: str = ""


# provider 内部注册表
_PROVIDERS: dict[str, ApiCapability] = {}
_HANDLERS: dict[str, dict[str, Callable[..., str]]] = {}
_ACTIVE: Optional[str] = None


def register(
    cap: ApiCapability,
    *,
    gen_image: Optional[Callable[..., str]] = None,
    gen_video: Optional[Callable[..., str]] = None,
    gen_audio: Optional[Callable[..., str]] = None,
) -> None:
    """注册一个 provider。capability 跟 handler 解耦, handler 可选。

    注意: cap.supports_image=True 但 gen_image=None, 路由时会报
    "声明支持但没接实现" 错, 引导主人去填实现。
    """
    if not cap.name:
        raise ValueError("ApiCapability.name 不能为空")
    if cap.name in _PROVIDERS:
        log.warning("multimodal_router: 重复注册 provider %r, 覆盖", cap.name)
    _PROVIDERS[cap.name] = cap
    handlers: dict[str, Callable[..., str]] = {}
    if gen_image is not None:
        handlers[ACTION_IMAGE] = gen_image
    if gen_video is not None:
        handlers[ACTION_VIDEO] = gen_video
    if gen_audio is not None:
        handlers[ACTION_AUDIO] = gen_audio
    _HANDLERS[cap.name] = handlers
    log.info(
        "multimodal_router: 注册 provider %r (image=%s video=%s audio=%s)",
        cap.name, cap.supports_image, cap.supports_video, cap.supports_audio,
    )


def unregister(name: str) -> None:
    global _ACTIVE
    _PROVIDERS.pop(name, None)
    _HANDLERS.pop(name, None)
    if _ACTIVE == name:
        _ACTIVE = None


def set_active(name: Optional[str]) -> tuple[bool, str]:
    """设置当前激活的 provider。返回 (success, message)。"""
    global _ACTIVE
    if name is None:
        _ACTIVE = None
        return True, "已清空当前激活的 API provider"
    if name not in _PROVIDERS:
        return False, (
            f"未知 API provider `{name}`。\n"
            f"已注册的: {', '.join(sorted(_PROVIDERS.keys())) or '(空)'}。"
        )
    prev = _ACTIVE
    _ACTIVE = name
    cap = _PROVIDERS[name]
    if prev == name:
        return True, f"`{name}` 已经是当前激活的 API provider。"
    if prev:
        return True, f"已把 API provider 从 `{prev}` 切到 `{name}`。"
    return True, (
        f"已激活 API provider: **{cap.display_name or name}**"
        f"（image: {'✓' if cap.supports_image else '✗'}, "
        f"video: {'✓' if cap.supports_video else '✗'}, "
        f"audio: {'✓' if cap.supports_audio else '✗'}）"
    )


def get_active() -> Optional[str]:
    return _ACTIVE


def list_providers() -> list[str]:
    return sorted(_PROVIDERS.keys())


def get_capability(name: str) -> Optional[ApiCapability]:
    return _PROVIDERS.get(name)


def list_capabilities() -> dict[str, ApiCapability]:
    return dict(_PROVIDERS)


# action 跟 capability field 之间的映射
_ACTION_TO_CAP = {
    ACTION_IMAGE: "supports_image",
    ACTION_VIDEO: "supports_video",
    ACTION_AUDIO: "supports_audio",
}


def _hint_for_unsupported(action: str, current: str, cap: ApiCapability) -> str:
    """构造 "当前 API 不支持 X, 切到 Y 重试" 的友好提示。"""
    # 找替代者: 同样 action 支持的其它 provider
    alternatives: list[str] = []
    field_name = _ACTION_TO_CAP.get(action)
    for n, c in _PROVIDERS.items():
        if n == current:
            continue
        if field_name and getattr(c, field_name, False):
            alternatives.append(n)
    msg = (
        f"当前 API provider `{cap.display_name or current}` 不支持"
        f"`generate_{action}`。"
    )
    if cap.notes:
        msg += f"\n（说明: {cap.notes}）"
    if alternatives:
        msg += f"\n\n切到以下 provider 重试: {' / '.join(alternatives)}。"
        msg += f"\n（用 `/api/{alternatives[0]}` 切换）"
    else:
        msg += "\n\n目前没有其它 provider 声明支持这个能力, 需要主人先在 router 里接一个。"
    return msg


def route(action: str, prompt: str, **kwargs) -> str:
    """核心: 根据当前激活的 provider 把多模态生成请求路由到对应 handler。

    异常:
      RuntimeError - 主人还没激活 / 选的不支持 / 选的支持但没接 / 底层 HTTP 错
    """
    if not prompt or not prompt.strip():
        raise RuntimeError(f"route({action}): prompt 不能为空")

    if _ACTIVE is None:
        names = list_providers()
        if not names:
            raise RuntimeError(
                "没注册任何 API provider, 调 generate_<x> 之前先在 "
                "app/brain/multimodal_providers/ 里 register。"
            )
        raise RuntimeError(
            f"主人还没激活 API provider, 选一个先:\n"
            f"{chr(10).join('  • ' + n for n in names)}\n"
            f"激活方式: /api/<name> （例如 `/api/{names[0]}`）"
        )

    cap = _PROVIDERS.get(_ACTIVE)
    if cap is None:
        raise RuntimeError(
            f"当前激活 `{_ACTIVE}` 但 provider registry 里找不到, "
            f"可能是被 unregister 了。"
        )

    field_name = _ACTION_TO_CAP.get(action)
    if not field_name:
        raise RuntimeError(f"未知的 action {action!r}（应是 image / video / audio 之一）")
    if not getattr(cap, field_name, False):
        raise RuntimeError(_hint_for_unsupported(action, _ACTIVE, cap))

    handlers = _HANDLERS.get(_ACTIVE, {})
    fn = handlers.get(action)
    if fn is None:
        raise RuntimeError(
            f"API provider `{cap.display_name or _ACTIVE}` 声明支持 "
            f"generate_{action} 但没接 handler, "
            f"去 app/brain/multimodal_providers/{_ACTIVE}.py 补一下实现。"
        )

    log.info("multimodal_router: route %s via %s (prompt=%s)",
             action, _ACTIVE, prompt[:60])
    return fn(prompt, **kwargs)


def clear() -> None:
    """清空整个 registry (测试用)。"""
    _PROVIDERS.clear()
    _HANDLERS.clear()
    global _ACTIVE
    _ACTIVE = None


__all__ = [
    "ACTION_IMAGE", "ACTION_VIDEO", "ACTION_AUDIO",
    "ApiCapability",
    "register", "unregister", "set_active", "get_active",
    "list_providers", "get_capability", "list_capabilities",
    "route", "clear",
]
