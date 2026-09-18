"""桌宠自身相关工具：feed_self / play_animation / change_pet_emotion / say_to_user。"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from ._core import Tool, ToolRegistry

log = logging.getLogger(__name__)


def _fire_hook(hooks: dict, key: str, arg: str) -> None:
    cb = hooks.get(key)
    if cb:
        try:
            cb(arg)
        except Exception:  # noqa: BLE001
            log.exception("hook %s 失败", key)


def register(
    reg: ToolRegistry,
    *,
    state,
    items,
    hooks: Optional[dict[str, Callable[[str], None]]] = None,
) -> None:
    """注册桌宠自身工具（需要 state / items / hooks）。"""
    hooks = hooks or {}

    def feed_self(food_name: str = "") -> str:
        if items is None:
            return "错误：食物库未加载。"
        it = items.by_name(food_name.strip())
        if it is None:
            names = "、".join(x.name for x in items.items[:20])
            return f"没有叫「{food_name}」的食物。可选：{names}"
        from app.engine.works import apply_food
        if not apply_food(state, it):
            return f"钱不够（{it.price} 金币，现有 {state.money:.0f}），先去打工吧～"
        _fire_hook(hooks, "animation", "eat")
        return f"吃掉了「{it.name}」，花 {it.price} 金币。现在状态：{state.stats_summary()}"

    def play_animation(anim_name: str) -> str:
        """触发表情动画。可选：happy, sad, angry, shy, think, pride, fear, doubt, surprise, stretch, jump, spin, swim, file"""
        name = anim_name.strip().lower()
        _fire_hook(hooks, "animation", name)
        return f"已触发「{name}」动画"

    def change_pet_emotion(emotion: str) -> str:
        """切换桌宠情绪表情。可选：happy, sad, angry, shy, think, pride, fear, doubt, surprise"""
        name = emotion.strip().lower()
        _fire_hook(hooks, "animation", f"emotion_{name}")
        return f"已切换到「{name}」情绪"

    def say_to_user(text: str) -> str:
        _fire_hook(hooks, "bubble", text.strip())
        return "已对主人说。"

    reg.register(Tool(
        name="feed_self",
        description="用桌宠自己的金币买食物吃（食物名必须完全匹配 foods.json）。",
        parameters={
            "type": "object",
            "properties": {"food_name": {"type": "string"}},
            "required": ["food_name"],
        },
        fn=feed_self,
    ))
    reg.register(Tool(
        name="play_animation",
        description="触发表情/动作动画。可选：happy, sad, angry, shy, think, pride, fear, doubt, surprise, stretch, jump, spin, swim, file",
        parameters={
            "type": "object",
            "properties": {"anim_name": {"type": "string", "description": "动画名称"}},
            "required": ["anim_name"],
        },
        fn=play_animation,
    ))
    reg.register(Tool(
        name="change_pet_emotion",
        description="切换桌宠情绪表情。可选：happy, sad, angry, shy, think, pride, fear, doubt, surprise",
        parameters={
            "type": "object",
            "properties": {"emotion": {"type": "string", "description": "情绪名称"}},
            "required": ["emotion"],
        },
        fn=change_pet_emotion,
    ))
    reg.register(Tool(
        name="say_to_user",
        description="在桌宠头顶气泡里冒一句话（不用等主人打开聊天窗）。适合补充说明、打招呼。",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        fn=say_to_user,
    ))


__all__ = ["register"]