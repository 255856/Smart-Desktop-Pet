# -*- coding: utf-8 -*-
"""小游戏「触发场景 → 动作」配置（所有小游戏共用一套；渲染器无关）。

五子棋、狼人杀等所有小游戏在同一情形（开始 / 胜利 / 失败）时，共用这里的
同一份配置；每个场景映射到一组候选动作，运行时由 ui_controller 按当前渲染器
能力（jump / spin / stretch / swim 为通用动作，tongue / cheek 等视模型而定）
按顺序取第一个可用动作播放。配置持久化到 data/game_actions.json，
配置入口在「设置 → Live2D」里。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# 候选动作：(动作键, 中文名)；none 表示该场景不做动作
GAME_ACTION_CHOICES: list[tuple[str, str]] = [
    ("jump",    "跳跃"),
    ("spin",    "转圈"),
    ("stretch", "伸懒腰"),
    ("swim",    "游泳"),
    ("tongue",  "吐舌"),
    ("cheek",   "比耶"),
    ("none",    "不做动作"),
]
ACTION_LABELS: dict[str, str] = dict(GAME_ACTION_CHOICES)
VALID_ACTION_KEYS = {k for k, _ in GAME_ACTION_CHOICES}

# 通用游戏场景：(event_id, 中文标题, 默认动作候选)
# 所有小游戏共用：开始 / 胜利 / 失败三种情形
GAME_EVENTS: list[tuple[str, str, list[str]]] = [
    ("game_start", "游戏开始（进入后保持）",      ["jump", "spin"]),
    ("game_win",   "游戏胜利（获胜 / 夺冠）",      ["jump", "spin", "cheek"]),
    ("game_lose",  "游戏失败（落败 / 认输）",      ["cheek", "stretch"]),
]

DEFAULT_ACTIONS: dict[str, list[str]] = {
    eid: list(acts) for eid, _, acts in GAME_EVENTS
}


@dataclass
class GameActionStore:
    """游戏场景 → 动作候选的持久化配置（所有小游戏共用）。"""

    path: Optional[Path] = field(default=None)
    enabled: bool = True
    actions: dict[str, list[str]] = field(
        default_factory=lambda: {k: list(v) for k, v in DEFAULT_ACTIONS.items()})

    def __post_init__(self) -> None:
        if self.path is not None:
            self.path = Path(self.path)
            self.load()

    # ------------------------------------------------------------ 查询
    def for_event(self, event_id: str) -> list[str]:
        """该场景实际播放的动作（剔除 none 与非法键，保持候选顺序）。"""
        return [a for a in self.actions.get(event_id, [])
                if a and a != "none" and a in VALID_ACTION_KEYS]

    @staticmethod
    def action_label(key: str) -> str:
        return ACTION_LABELS.get(key, key)

    def summary(self, event_id: str) -> str:
        acts = self.for_event(event_id)
        if not acts:
            return "不做动作"
        return "、".join(self.action_label(a) for a in acts)

    # ------------------------------------------------------------ 编辑
    def set_event(self, event_id: str, actions: list[str]) -> None:
        cleaned = [a for a in actions if a in VALID_ACTION_KEYS and a != "none"]
        self.actions[event_id] = cleaned
        self.save()

    def set_enabled(self, on: bool) -> None:
        self.enabled = bool(on)
        self.save()

    def reset(self) -> None:
        self.enabled = True
        self.actions = {k: list(v) for k, v in DEFAULT_ACTIONS.items()}
        self.save()

    # ------------------------------------------------------------ 持久化
    def to_json(self) -> dict:
        return {
            "version": 3,
            "enabled": bool(self.enabled),
            "actions": dict(self.actions),
        }

    def load(self) -> None:
        if not self.path or not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.enabled = bool(data.get("enabled", True))
            raw = data.get("actions") or {}
            # 只认当前通用场景；旧版细分场景（和棋 / 攻势等）键自动作废
            for eid, default in DEFAULT_ACTIONS.items():
                vals = raw.get(eid, default)
                if isinstance(vals, list):
                    cleaned = [str(a) for a in vals if str(a) in VALID_ACTION_KEYS]
                    self.actions[eid] = cleaned
        except Exception:  # noqa: BLE001
            log.exception("游戏动作配置读取失败：%s", self.path)
            self.actions = {k: list(v) for k, v in DEFAULT_ACTIONS.items()}

    def save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self.to_json(), ensure_ascii=False, indent=2),
                encoding="utf-8")
            tmp.replace(self.path)
        except Exception:  # noqa: BLE001
            log.exception("游戏动作配置保存失败：%s", self.path)
