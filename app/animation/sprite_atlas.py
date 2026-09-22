"""精灵图集（Sprite Atlas）：把磁盘上的精灵组织成命名动画集合。"""
from __future__ import annotations

import logging
import random
import time
from pathlib import Path
from typing import Callable, Optional

from app.animation.animations import (
    Animation, AnimationPlayer, Frame,
    load_animation_from_dir, load_animation_set,
)
from app.animation.pet_renderer import PetRenderer
from app.core.qt_compat import QTimer

log = logging.getLogger(__name__)


class SpriteAtlas:
    """所有动画的集合 + 智能选择（支持按需加载）。"""

    # 所有动画组的字段名列表（用于 prescale 遍历）
    ANIMATION_GROUPS = [
        'idle', 'idle_default', 'idle_special',
        'touch_head', 'touch_body', 'walk_left', 'walk_right',
        'drag_1', 'drag_2',
        'sleep', 'sleep_2', 'sleep_3', 'eat_rice',
        'spin', 'stretch', 'jump', 'thinking', 'thinking_2', 'file',
        'playing_water',
        'emotion_happy', 'emotion_sad', 'emotion_angry', 'emotion_shy',
        'emotion_think', 'emotion_pride', 'emotion_fear', 'emotion_doubt',
        'emotion_surprise',
    ]

    # 需要加载的目录映射（按需加载用）
    _DIR_MAP = {
        'idle_default': ['Default'],
        'idle_special': ['Idle_shake', 'Idle_yawning', 'Idle_tail'],
        'touch_head': ['Touchhead'],
        'touch_body': ['Touchbody'],
        'walk_left': ['Left'],
        'walk_right': ['Right'],
        'drag_1': ['Drag_1'],
        'drag_2': ['Drag_2'],
        'sleep': ['Sleep'],
        'sleep_2': ['Sleep_2'],
        'sleep_3': ['sleep_3', 'Sleep_3'],
        'eat_rice': ['Eat_rice'],
        'spin': ['Spinaround'],
        'stretch': ['Stretch'],
        'jump': ['Jump'],
        'thinking': ['Thinking'],
        'thinking_2': ['Think_2', 'think_2', 'Thinking_2'],
        'file': ['File', 'file'],
        'emotion_happy': ['Emotion_happy'],
        'emotion_sad': ['Emotion_sad'],
        'emotion_angry': ['Emotion_anger'],
        'emotion_shy': ['Emotion_shy'],
        'emotion_think': ['Thinking'],
        'emotion_pride': ['Emotion_pride'],
        'emotion_fear': ['Emotion_fear'],
        'emotion_doubt': ['Emotion_doubt'],
        'emotion_surprise': ['Emotion_surprise'],
        'playing_water': ['Playing_in_water'],
    }

    def __init__(self, sprite_dir: str | Path, fallback_image: str | Path | None = None):
        self.sprite_dir = Path(sprite_dir)
        self._fallback_image = fallback_image
        self._loaded: set[str] = set()  # 已加载的组名
        self.fallback: Optional[Animation] = None

        # 存储 prescale 参数（按需加载时用于新加载的组）
        self._prescale_target_size = None
        self._prescale_scale_func = None
        self._override_duration_ms = None

        # 初始化所有组为空列表
        for name in self._DIR_MAP:
            setattr(self, name, [])
        self.idle: list[Animation] = []

        # 只立即加载 idle 和 fallback（其他组按需加载）
        self._load_group('idle_default')
        self._load_group('idle_special')
        self.idle = self.idle_default + self.idle_special

        # 加载 fallback
        if self.idle:
            self.fallback = self.idle[0]
        else:
            # 尝试从任意可用组加载
            for grp_name in self._DIR_MAP:
                self._load_group(grp_name)
                grp = getattr(self, grp_name, [])
                if grp:
                    self.fallback = grp[0]
                    break

        # 摘要日志
        self._log_summary()

    def _load_group(self, group_name: str) -> None:
        """按需加载一组动画。

        如果该组已加载则跳过。加载后自动应用之前设置的
        prescale 和 override_frame_duration_ms 参数。
        """
        if group_name in self._loaded:
            return
        names = self._DIR_MAP.get(group_name, [])
        if not names:
            return
        sd = self.sprite_dir
        if not sd.is_dir():
            return
        out: list[Animation] = []
        for n in names:
            a = load_animation_from_dir(sd / n, frame_duration_ms=50)
            if a:
                out.append(a)
        setattr(self, group_name, out)
        self._loaded.add(group_name)

        # 应用之前设置的 frame duration 覆盖
        if self._override_duration_ms is not None:
            for anim in out:
                for frame in anim.frames:
                    frame.duration_ms = self._override_duration_ms

        # 应用之前设置的 prescale（预缩放）
        if self._prescale_target_size is not None and self._prescale_scale_func is not None:
            for anim in out:
                for frame in anim.frames:
                    if frame.pixmap and not frame.pixmap.isNull():
                        frame.pixmap = self._prescale_scale_func(
                            frame.pixmap, self._prescale_target_size
                        )

        # 更新 idle 列表（仅当加载了 idle 相关组时）
        if group_name in ('idle_default', 'idle_special'):
            self.idle = self.idle_default + self.idle_special

    def ensure_loaded(self, group_name: str) -> None:
        """确保某个组已加载（公开接口）。

        PetAnimator 在访问动画组之前调用此方法，
        确保动画已按需加载。
        """
        self._load_group(group_name)

    def _log_summary(self) -> None:
        """输出加载摘要日志。"""
        loaded_groups = sorted(self._loaded)
        total = sum(len(getattr(self, g, [])) for g in loaded_groups)
        log.info(
            "SpriteAtlas: %d 个动画已加载 (组: %s, fallback=%s)",
            total, ', '.join(loaded_groups),
            self.fallback.name if self.fallback else 'none',
        )

    def any(self, group: list[Animation]) -> Optional[Animation]:
        return random.choice(group) if group else None

    def prescale(self, target_size, scale_func) -> None:
        """预缩放常用动画组的帧到目标尺寸。

        存储参数供后续按需加载的组使用。
        """
        self._prescale_target_size = target_size
        self._prescale_scale_func = scale_func
        # 预缩放所有已加载的 idle 变体（默认状态，最常播放）
        for anim in self.idle:
            for frame in anim.frames:
                if frame.pixmap and not frame.pixmap.isNull():
                    frame.pixmap = scale_func(frame.pixmap, target_size)
        # 其余已加载组预缩放**所有**动画（之前只缩放第一个，切换动画时卡顿）
        for group_name in self.ANIMATION_GROUPS:
            if group_name in ('idle', 'idle_default', 'idle_special'):
                continue
            if group_name not in self._loaded:
                continue
            group: list[Animation] = getattr(self, group_name, [])
            if not group:
                continue
            for anim in group:
                for frame in anim.frames:
                    if frame.pixmap and not frame.pixmap.isNull():
                        frame.pixmap = scale_func(frame.pixmap, target_size)
        if self.fallback is not None:
            for frame in self.fallback.frames:
                if frame.pixmap and not frame.pixmap.isNull():
                    frame.pixmap = scale_func(frame.pixmap, target_size)

    def override_frame_duration_ms(self, ms: int) -> None:
        """强制把所有已加载动画所有帧的 duration_ms 改成 ms。"""
        ms = max(1, int(ms))
        self._override_duration_ms = ms
        for group_name in self.ANIMATION_GROUPS:
            if group_name not in self._loaded:
                continue
            group: list[Animation] = getattr(self, group_name, [])
            for anim in group:
                for frame in anim.frames:
                    frame.duration_ms = ms
        if self.fallback is not None:
            for frame in self.fallback.frames:
                frame.duration_ms = ms


# 兼容：旧代码 `from app.animation.sprite_atlas import PetAnimator` 仍可用。
# 不能直接 import（会触发 sprite_atlas → sprite_renderer → sprite_atlas 循环），
# 用模块级 __getattr__ 懒加载。
def __getattr__(name):
    if name == "PetAnimator":
        from app.animation.sprite_renderer import PetAnimator
        return PetAnimator
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["SpriteAtlas", "PetAnimator"]
