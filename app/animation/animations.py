"""VPet 风格动画系统

核心概念：
    Frame       单张帧（QPixmap + duration_ms）
    Animation   一组帧 + 循环策略
    Player      帧序列播放器（按时间推进，自动循环/一次性）

VPet 同款特性：
    - 每帧独立 duration（125ms / 250ms / 500ms ...）
    - 多循环变体（同一状态 3 个不同序列，避免重复感）
    - 一次性动画（touch 反应）播完回到 idle
    - 状态优先级：高优先级动画可打断低优先级
"""
from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from app.core.qt_compat import QPixmap

log = logging.getLogger(__name__)


@dataclass
class Frame:
    """单张精灵帧。"""
    pixmap: QPixmap
    duration_ms: int = 125          # VPet 同款命名约定 _125 表示 125ms
    original: Optional[QPixmap] = None  # 原始未缩放的 pixmap（缩放桌宠时用此还原）


class Animation:
    """一组帧 + 循环策略。"""

    LOOP = "loop"          # 无限循环
    ONCE = "once"          # 播一次后回到 last_loop
    PING_PONG = "pingpong"  # 来回循环

    def __init__(self, name: str, frames: list[Frame], mode: str = LOOP):
        self.name = name
        self.frames = frames
        self.mode = mode
        if not frames:
            log.warning("Animation '%s' has no frames", name)

    def is_valid(self) -> bool:
        return bool(self.frames)

    def total_duration_ms(self) -> int:
        return sum(f.duration_ms for f in self.frames)

    def __len__(self) -> int:
        return len(self.frames)


class AnimationPlayer:
    """帧序列播放器。

    用法：
        player = AnimationPlayer()
        player.play(animation)         # 切到新动画
        player.play_one_shot(reaction) # 播一次性动画，播完回到 last_loop
        player.tick()                  # 由 QTimer.timeout 调用，推进一帧
        player.current_pixmap()        # 当前帧 QPixmap

    cross-fade：
        set_crossfade_ms(200) 开启——切换 animation 时把"上一帧"叠合过渡。
        get_pending_crossfade() 返回 (pixmap, alpha)，paintEvent 据此叠合。
    """

    def __init__(self):
        self._current: Optional[Animation] = None
        self._last_loop: Optional[Animation] = None
        self._frame_idx = 0
        self._frame_elapsed = 0  # 当前帧已 elapsed 的时间（毫秒）
        self._direction = 1
        self._finished_cb: Optional[Callable] = None
        # cross-fade 状态（PR-crossfade：豆包生成图 pose 不连贯时让切换不抖）
        self._crossfade_ms = 0
        self._prev_pixmap: Optional[QPixmap] = None
        self._prev_alpha: float = 0.0
        self._crossfade_start_t: float = 0.0

    def play(self, anim: Animation,
             on_finished: Optional[Callable] = None) -> None:
        if not anim or not anim.is_valid():
            return
        # PR-crossfade：记下上一帧，让 paintEvent 在 crossfade_ms 内做淡出。
        # 同一 anim 不重设 prev（避免同一动画内帧间都做 crossfade——只有切换时做）。
        prev_anim = self._current
        prev_pix = self.current_pixmap()
        is_same = (prev_anim is anim)
        if (not is_same) and prev_pix is not None and self._crossfade_ms > 0:
            self._prev_pixmap = prev_pix
            self._crossfade_start_t = time.monotonic()
            self._prev_alpha = 1.0
        self._current = anim
        # PR-bugfix："动画越来越快"——同动画不重置 frame_idx / _frame_elapsed。
        # 否则 set_idle() 每 30-75 s 调一次 play(idle[0]) 都会把动画从第 0 帧
        # 重启，看起来像"动画循环加速"。
        if not is_same:
            self._frame_idx = 0
            self._frame_elapsed = 0
            self._direction = 1
        if anim.mode != Animation.ONCE:
            self._last_loop = anim
            self._finished_cb = None
        else:
            self._finished_cb = on_finished

    def play_one_shot(self, anim: Animation,
                      on_finished: Optional[Callable] = None) -> None:
        if not anim or not anim.is_valid():
            if on_finished:
                on_finished()
            return
        anim = Animation(anim.name, anim.frames, Animation.ONCE)
        self.play(anim, on_finished=on_finished)

    def back_to_idle(self) -> None:
        if self._last_loop:
            self.play(self._last_loop)

    def tick(self, dt_ms: int = 33) -> bool:
        """推进动画一帧。dt_ms 是自上次调用以来经过的毫秒数。

        Returns:
            bool: True 表示帧发生了变化（需要重绘），False 表示仍在同一帧内
        """
        if not self._current or not self._current.frames:
            return False
        old_frame_idx = self._frame_idx
        self._frame_elapsed += dt_ms
        frame = self._current.frames[self._frame_idx]
        if self._frame_elapsed >= frame.duration_ms:
            self._advance()
            return True
        return False

    def _advance(self) -> None:
        anim = self._current
        old_frame_idx = self._frame_idx
        # 先保存旧帧的 duration（可能在 ONCE 播完 back_to_idle 后 anim 改变）
        old_frame_duration = anim.frames[old_frame_idx].duration_ms
        if anim.mode == Animation.LOOP:
            self._frame_idx = (self._frame_idx + 1) % len(anim.frames)
        elif anim.mode == Animation.ONCE:
            if self._frame_idx + 1 >= len(anim.frames):
                cb = self._finished_cb
                self.back_to_idle()
                if cb:
                    cb()
                return
            self._frame_idx += 1
        elif anim.mode == Animation.PING_PONG:
            next_idx = self._frame_idx + self._direction
            if next_idx >= len(anim.frames):
                self._direction = -1
                next_idx = self._frame_idx + self._direction
            elif next_idx < 0:
                self._direction = 1
                next_idx = self._frame_idx + self._direction
            self._frame_idx = next_idx
        # carry-over：减去已消耗的帧 duration，剩余时间留给下一帧
        self._frame_elapsed -= old_frame_duration

    def current_pixmap(self) -> Optional[QPixmap]:
        if not self._current or not self._current.frames:
            return None
        return self._current.frames[self._frame_idx].pixmap

    def current_animation(self) -> Optional[Animation]:
        return self._current

    # -------------------------- cross-fade --------------------------
    def set_crossfade_ms(self, ms: int) -> None:
        """开启 cross-fade（毫秒）。0 = 关闭。"""
        self._crossfade_ms = max(0, int(ms))

    def crossfade_ms(self) -> int:
        return self._crossfade_ms

    def get_pending_crossfade(self) -> Optional[tuple[QPixmap, float]]:
        """返回 (上一帧 pixmap, alpha ∈ [0, 1])——正在淡出的帧。
        若 cross-fade 结束或未开启，返回 None。"""
        if self._prev_pixmap is None or self._crossfade_ms <= 0:
            return None
        elapsed_ms = (time.monotonic() - self._crossfade_start_t) * 1000
        if elapsed_ms >= self._crossfade_ms:
            self._prev_pixmap = None
            self._prev_alpha = 0.0
            return None
        # alpha 从 1 → 0 线性衰减
        alpha = max(0.0, 1.0 - elapsed_ms / self._crossfade_ms)
        return self._prev_pixmap, alpha


# ========================= 精灵图加载器 =========================

def load_animation_from_dir(directory: str | Path,
                              frame_duration_ms: int = 125,
                              max_frames: int = 0) -> Optional[Animation]:
    """从一个目录加载动画。

    支持文件名命名约定：
        xxx_0_125.png   → frame_idx=0, duration=125ms (VPet 格式)
        xxx_000_125.png → frame_idx=0, duration=125ms (VPet 格式，3 位序号)
        rmbg-1.png      → 按文件名排序，duration=frame_duration_ms
        rmbg-001.png    → 提取序号 001，duration=frame_duration_ms

    VPet 同款帧率：默认 50ms/帧 (20 FPS)，配合每动作 ~81 张的密集动画。
    最小帧率限制：30ms (≈33 FPS)，避免动画过快导致"跳帧"感，但不强制 12 FPS。
    此前设的 83ms（12 FPS）会让 retune-ms 50 的 20 fps 被强制降回 12 fps，已改为 30。
    """
    MIN_FRAME_DURATION_MS = 30  # 最小 30ms = 33 FPS 上限（不强制 12 FPS）

    p = Path(directory)
    if not p.is_dir():
        return None
    files = []
    for ext in ('*.png', '*.jpg', '*.jpeg', '*.gif'):
        files.extend(p.glob(ext))
    if not files:
        return None

    items: list[tuple[int, int, Path]] = []
    for f in files:
        stem = f.stem
        # 优先匹配 VPet 格式：xxx_序号_持续时间
        m = re.search(r'_(\d+)_(\d+)$', stem)
        if m:
            idx = int(m.group(1))
            dur = int(m.group(2))
            # 应用最小持续时间限制
            dur = max(dur, MIN_FRAME_DURATION_MS)
        else:
            # 尝试提取纯序号：rmbg-001 → 1, rmbg-1 → 1
            m2 = re.search(r'[-_](\d+)$', stem)
            if m2:
                idx = int(m2.group(1))
            else:
                idx = len(items)
            dur = frame_duration_ms
        items.append((idx, dur, f))

    items.sort(key=lambda x: (x[0], x[2].name))
    if max_frames > 0:
        items = items[:max_frames]

    frames: list[Frame] = []
    for idx, dur, f in items:
        pix = QPixmap(str(f))
        if pix.isNull():
            continue
        frames.append(Frame(pixmap=pix, duration_ms=dur, original=pix))

    if not frames:
        return None
    return Animation(name=p.name, frames=frames, mode=Animation.LOOP)


def load_animation_set(base_dir: str | Path, category: str,
                       subdirs: list[str],
                       frame_duration_ms: int = 50) -> list[Animation]:
    """从 base_dir/category/{subdir}/ 加载多个变体动画。"""
    base = Path(base_dir) / category
    results = []
    for sub in subdirs:
        anim = load_animation_from_dir(base / sub, frame_duration_ms)
        if anim:
            results.append(anim)
    return results


def random_loop(anims: list[Animation]) -> Optional[Animation]:
    """从一组动画里随机挑一个（用于多循环变体）。"""
    valid = [a for a in anims if a and a.is_valid()]
    return random.choice(valid) if valid else None