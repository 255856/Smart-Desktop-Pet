"""桌宠自身相关工具：feed_self / play_animation / change_pet_emotion / say_to_user。"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from ._core import Tool, ToolRegistry

log = logging.getLogger(__name__)

# 动画 / 情绪白名单：与下面各工具 description 里列的一致。
# 之前不校验直接 fire hook 并回报「已触发」，导致传入不存在的名字时
# 桌宠会理直气壮地说「好的，已经帮你跳舞啦~」——实际什么都没发生，
# 而且 ToolRegistry 返回值不含失败词，agent 的矛盾检测也拦不住。
ANIMATIONS = ("happy", "sad", "angry", "shy", "think", "pride", "fear",
              "doubt", "surprise", "stretch", "jump", "spin", "swim", "file")
EMOTIONS = ("happy", "sad", "angry", "shy", "think", "pride", "fear",
            "doubt", "surprise")


def _fire_hook(hooks: dict, key: str, arg: str) -> bool:
    """投递 hook。返回是否真的送出去了。

    原实现返回 None 且吞掉异常，调用方无条件回报「已触发/已切换/已对主人说」，
    于是 UI 侧没响应时桌宠仍然对主人报成功——「假装成功」的又一处来源。
    这里如实回传成败，让调用方能说真话。
    """
    cb = hooks.get(key)
    if not cb:
        return False
    try:
        cb(arg)
        return True
    except Exception:  # noqa: BLE001
        log.exception("hook %s 失败", key)
        return False


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
            return f"错误：没有叫「{food_name}」的食物。可选：{names}"
        from app.engine.works import apply_food
        if not apply_food(state, it):
            return (f"错误：钱不够（需要 {it.price} 金币，现有 "
                    f"{state.money:.0f}），没吃掉任何东西。")
        _fire_hook(hooks, "animation", "eat")
        return f"吃掉了「{it.name}」，花 {it.price} 金币。现在状态：{state.stats_summary()}"

    def play_animation(anim_name: str) -> str:
        """触发表情动画。可选：happy, sad, angry, shy, think, pride, fear, doubt, surprise, stretch, jump, spin, swim, file"""
        name = (anim_name or "").strip().lower()
        if not name:
            return f"错误：动画名不能为空。可选：{', '.join(ANIMATIONS)}"
        if name not in ANIMATIONS:
            # 如实报错：不要假装触发成功，否则 agent 会顺着报喜
            return (f"错误：没有叫「{name}」的动画，未触发任何动作。"
                    f"可选：{', '.join(ANIMATIONS)}")
        if not _fire_hook(hooks, "animation", name):
            return (f"错误：动画「{name}」没能投递给界面（UI 通道不可用），"
                    f"桌宠实际没有做出这个动作。")
        return f"已触发「{name}」动画"

    def change_pet_emotion(emotion: str) -> str:
        """切换桌宠情绪表情。可选：happy, sad, angry, shy, think, pride, fear, doubt, surprise"""
        name = (emotion or "").strip().lower()
        if not name:
            return f"错误：情绪名不能为空。可选：{', '.join(EMOTIONS)}"
        if name not in EMOTIONS:
            return (f"错误：没有「{name}」这种情绪，未切换。"
                    f"可选：{', '.join(EMOTIONS)}")
        if not _fire_hook(hooks, "animation", f"emotion_{name}"):
            return f"错误：情绪「{name}」没能投递给界面，实际没有切换。"
        return f"已切换到「{name}」情绪"

    def say_to_user(text: str) -> str:
        content = (text or "").strip()
        if not content:
            return "错误：要说的内容不能为空"
        if not _fire_hook(hooks, "bubble", content):
            return "错误：气泡通道不可用，这句话没能显示给主人。"
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