# -*- coding: utf-8 -*-
"""Live2D「触发场景动作」配置：场景 → 外观组合（表情 + 发型 + 配件/手势/特殊）。

设计：
    - 固定场景（BUILTIN_SCENES）跨模型通用：6 种聊天情绪、AI 思考、睡觉、醒来、
      待机、开机、摸头/身体、双击、拖拽、收到提醒、闲置 30 分钟、深夜时段。
    - 每个场景的外观组合 SceneBundle 可包含：表情（单选）、发型（单选）、
      若干 toggle 叠加项（配件/手势/特殊，多选）；留空 = 该场景不改变这一项。
    - 自定义动作（CustomAction）由用户在设置面板命名并配置，初始为空；
      可绑定一个工具动作键（hook），右键菜单「玩一下」始终可手动触发。
    - 配置按模型各存一份：data/live2d_scenes/<模型目录名>.json（不同模型动作/外观
      不一致，互不串）。用户在 UI 里的覆盖优先；未覆盖时用模型 YAML triggers 推导
      的默认值（开箱即用），并提供「恢复默认」。

只服务于 live2d 渲染器；sprite 渲染器不加载本配置。
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 固定场景：(scene_id, 中文标题, 类型 persist=持续 / transient=一次性, 分组)
# ---------------------------------------------------------------------------
SCENE_GROUP_CHAT = "聊天情绪"
SCENE_GROUP_STATE = "状态"
SCENE_GROUP_INTERACT = "互动"

# 常见情绪只列 6 种（用户拍板）；scene_id 与通用情绪键的映射见 _CHAT_ALIAS_KEY
BUILTIN_SCENES: list[tuple[str, str, str, str]] = [
    # 聊天情绪（回复结束 / 主动搭话带情绪时触发）
    ("chat_happy",     "开心",                  "persist",  SCENE_GROUP_CHAT),
    ("chat_shy",       "害羞",                  "persist",  SCENE_GROUP_CHAT),
    ("chat_angry",     "生气",                  "persist",  SCENE_GROUP_CHAT),
    ("chat_sad",       "难过",                  "persist",  SCENE_GROUP_CHAT),
    ("chat_surprised", "惊讶",                  "persist",  SCENE_GROUP_CHAT),
    ("chat_thinking",  "思考",                  "persist",  SCENE_GROUP_CHAT),
    # 持续状态
    ("thinking",       "AI 思考中",             "persist",  SCENE_GROUP_STATE),
    ("sleeping",       "睡觉",                  "persist",  SCENE_GROUP_STATE),
    ("wake",           "醒来",                  "persist",  SCENE_GROUP_STATE),
    ("idle",           "待机（恢复自然）",      "persist",  SCENE_GROUP_STATE),
    ("startup",        "开机初始外观",          "persist",  SCENE_GROUP_STATE),
    ("late_night",     "深夜时段（22:00–6:00）", "persist", SCENE_GROUP_STATE),
    ("idle_lonely",    "许久未理我（30 分钟）",  "persist",  SCENE_GROUP_STATE),
    # 一次性互动
    ("touch_head",     "摸头",                  "transient", SCENE_GROUP_INTERACT),
    ("touch_body",     "摸身体",                "transient", SCENE_GROUP_INTERACT),
    ("double_click",   "双击",                  "transient", SCENE_GROUP_INTERACT),
    ("reminder",       "收到提醒",              "transient", SCENE_GROUP_INTERACT),
    ("dragging",       "拖拽中",                "persist",   SCENE_GROUP_INTERACT),
]

# chat 场景 → 模型 emotion_aliases 的通用情绪键
_CHAT_ALIAS_KEY = {
    "chat_happy": "happy",
    "chat_shy": "shy",
    "chat_angry": "angry",
    "chat_sad": "sad",
    "chat_surprised": "surprise",
    "chat_thinking": "thinking",
}

# 一次性动作默认时长（毫秒）；用户未配 hold_ms 时兜底
DEFAULT_TRANSIENT_HOLD_MS = 1600

# 自定义动作可绑定的工具动作键（hook → 中文标签）。
# 与 brain_controller._do_play_animation 里的名字保持一致；空 hook=仅手动播放。
TOOL_ACTION_HOOKS: list[tuple[str, str]] = [
    ("eat", "喂食"),
    ("file", "打开文件（开工）"),
    ("spin", "转圈"),
    ("stretch", "伸懒腰"),
    ("jump", "跳跃"),
    ("swim", "游泳"),
    ("tongue", "吐舌"),
    ("cheek", "比耶"),
]

# 五大类外观的分组（UI 弹窗按此分组）
APPEARANCE_GROUPS = [
    ("emotion", "表情"),
    ("hairstyle", "发型"),
    ("special", "特殊"),
    ("gear", "配件"),
    ("gesture", "手势"),
]


@dataclass
class SceneBundle:
    """一个场景触发时应用的外观组合。所有字段留空 = 不改变该项。"""

    emotion: str = ""        # 表情（face/emotion 组）条目名
    hairstyle: str = ""      # 发型条目名；"__default__"=默认发型
    toggles: list[str] = field(default_factory=list)  # 配件/手势/特殊条目名（多选）
    hold_ms: int = 0         # 一次性场景的保持时长（0=持续或用默认）

    def is_empty(self) -> bool:
        return not (self.emotion or self.hairstyle or self.toggles)

    def to_dict(self) -> dict:
        return {"emotion": self.emotion, "hairstyle": self.hairstyle,
                "toggles": list(self.toggles), "hold_ms": int(self.hold_ms or 0)}

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "SceneBundle":
        if not d:
            return cls()
        return cls(
            emotion=str(d.get("emotion") or ""),
            hairstyle=str(d.get("hairstyle") or ""),
            toggles=[str(x) for x in (d.get("toggles") or [])],
            hold_ms=int(d.get("hold_ms") or 0),
        )


def _classify_item(profile, name: str) -> Optional[str]:
    """把一个条目名归类为 emotion / hairstyle / toggle；模型里没有则 None。"""
    item = profile.find_item(name) if profile is not None else None
    if item is None:
        return None
    cat = profile.category(item.category)
    if cat is None:
        return None
    if cat.emotion:
        return "emotion"
    if cat.hairstyle:
        return "hairstyle"
    return "toggle"


class SceneStore:
    """单个模型的场景配置：用户覆盖 + 自定义动作，落盘 JSON。"""

    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path, profile=None):
        self.path = Path(path)
        self.profile = profile
        # 用户覆盖的固定场景外观：scene_id -> SceneBundle（仅存有非空覆盖的）
        self.overrides: dict[str, SceneBundle] = {}
        # 自定义动作（扁平 dict）：{id,name,hook,emotion,hairstyle,toggles,hold_ms}
        self.custom_actions: list[dict] = []
        self.load()

    def set_profile(self, profile) -> None:
        """渲染器加载 profile 后回填（默认值推导依赖它）。"""
        self.profile = profile

    # ------------------------------------------------------------ 默认值
    def default_bundle(self, scene_id: str) -> Optional[SceneBundle]:
        """从模型 YAML triggers 推导默认外观；模型没配则 None。"""
        p = self.profile
        if p is None:
            return None

        # 聊天情绪 → 通用情绪别名映射的模型表情
        if scene_id in _CHAT_ALIAS_KEY:
            target = p.emotion_aliases.get(_CHAT_ALIAS_KEY[scene_id])
            if target and p.find_item(target) is not None:
                return SceneBundle(emotion=target)
            return None

        if scene_id == "thinking":
            if p.thinking_expr or p.thinking_items:
                return SceneBundle(
                    emotion=p.thinking_expr or "",
                    toggles=[n for n in p.thinking_items if p.find_item(n)])
            return None

        if scene_id in ("touch_head", "touch_body"):
            if p.touch_expr and p.find_item(p.touch_expr) is not None:
                return SceneBundle(emotion=p.touch_expr,
                                   hold_ms=int(p.touch_hold_ms or 700))
            return None

        if scene_id == "double_click":
            spec = p.actions.get("tongue")
            if spec is not None and spec.kind == "item":
                return self._bundle_from_action(spec)
            return None

        # 睡觉/醒来/待机/拖拽/开机/提醒/深夜/闲置：默认不额外改变外观
        # （睡觉由 sleep_params 专门处理；待机/醒来回自然）。
        return None

    def _bundle_from_action(self, spec) -> Optional[SceneBundle]:
        """把 profile 的 ActionSpec（kind=item）转成外观组合。"""
        kind = _classify_item(self.profile, spec.item)
        if kind is None:
            return None
        b = SceneBundle(hold_ms=int(spec.duration_ms or DEFAULT_TRANSIENT_HOLD_MS))
        if kind == "emotion":
            b.emotion = spec.item
        elif kind == "hairstyle":
            b.hairstyle = spec.item
        else:
            b.toggles = [spec.item]
        return b

    # ------------------------------------------------------------ 读取
    def effective_bundle(self, scene_id: str) -> Optional[SceneBundle]:
        """实际生效的组合：用户覆盖优先，否则默认；都没有则 None。"""
        if scene_id in self.overrides:
            return self.overrides[scene_id]
        return self.default_bundle(scene_id)

    def is_overridden(self, scene_id: str) -> bool:
        return scene_id in self.overrides

    def bundle_for_preview(self, scene_id: str) -> SceneBundle:
        """给 UI 用：始终返回一个 bundle（无配置则空 bundle）。"""
        b = self.effective_bundle(scene_id)
        return b if b is not None else SceneBundle()

    # ------------------------------------------------------------ 固定场景编辑
    def set_bundle(self, scene_id: str, bundle: SceneBundle) -> None:
        """覆盖某个固定场景的外观（空 bundle = 显式清空该场景所有外观）。"""
        self.overrides[scene_id] = bundle
        self.save()

    def reset_scene(self, scene_id: str) -> None:
        """恢复单个场景到模型默认。"""
        self.overrides.pop(scene_id, None)
        self.save()

    # ------------------------------------------------------------ 自定义动作
    def add_custom_action(self, name: str, hook: str, bundle: SceneBundle,
                          hold_ms: int = DEFAULT_TRANSIENT_HOLD_MS) -> dict:
        cid = "a" + uuid.uuid4().hex[:8]
        action = {
            "id": cid, "name": name.strip() or "新动作",
            "hook": (hook or "").strip().lower(),
            "emotion": bundle.emotion, "hairstyle": bundle.hairstyle,
            "toggles": list(bundle.toggles), "hold_ms": int(hold_ms or 0),
        }
        self.custom_actions.append(action)
        self.save()
        return action

    def update_custom_action(self, cid: str, *, name: str = None, hook: str = None,
                             bundle: "SceneBundle|None" = None,
                             hold_ms: int = None) -> None:
        for a in self.custom_actions:
            if a["id"] != cid:
                continue
            if name is not None:
                a["name"] = name.strip() or a["name"]
            if hook is not None:
                a["hook"] = (hook or "").strip().lower()
            if bundle is not None:
                a["emotion"] = bundle.emotion
                a["hairstyle"] = bundle.hairstyle
                a["toggles"] = list(bundle.toggles)
            if hold_ms is not None:
                a["hold_ms"] = int(hold_ms or 0)
            break
        self.save()

    def remove_custom_action(self, cid: str) -> bool:
        before = len(self.custom_actions)
        self.custom_actions = [a for a in self.custom_actions if a["id"] != cid]
        if len(self.custom_actions) != before:
            self.save()
            return True
        return False

    def custom_by_hook(self, hook: str) -> Optional[dict]:
        """按工具动作键找自定义动作（play_animation 优先匹配用户配置）。"""
        hook = (hook or "").strip().lower()
        for a in self.custom_actions:
            if a.get("hook") and a["hook"] == hook:
                return a
        return None

    def custom_by_id(self, cid: str) -> Optional[dict]:
        for a in self.custom_actions:
            if a["id"] == cid:
                return a
        return None

    @staticmethod
    def action_bundle(action: dict) -> SceneBundle:
        """自定义动作扁平 dict → SceneBundle。"""
        return SceneBundle(
            emotion=action.get("emotion", ""),
            hairstyle=action.get("hairstyle", ""),
            toggles=list(action.get("toggles", [])),
        )

    # ------------------------------------------------------------ 持久化
    def to_json(self) -> dict:
        return {
            "version": self.SCHEMA_VERSION,
            "model": self.profile.name if self.profile else "",
            "saved_at": time.time(),
            "scenes": {sid: b.to_dict() for sid, b in self.overrides.items()},
            "custom_actions": list(self.custom_actions),
        }

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.to_json(), ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(self.path)
        except Exception:  # noqa: BLE001
            log.exception("Live2D 场景配置保存失败: %s", self.path)

    def load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.overrides = {
                str(sid): SceneBundle.from_dict(d)
                for sid, d in (data.get("scenes") or {}).items()
            }
            self.custom_actions = [
                a for a in (data.get("custom_actions") or [])
                if isinstance(a, dict) and a.get("id")
            ]
        except Exception:  # noqa: BLE001
            log.exception("Live2D 场景配置读取失败: %s", self.path)
            self.overrides = {}
            self.custom_actions = []
