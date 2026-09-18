"""精灵图集（Sprite Atlas）：把磁盘上的精灵组织成命名动画集合。

新的英文目录结构：
    sprites/
    ├── Default/          (默认待机)
    ├── Idle_shake/       (歪头)
    ├── Idle_yawning/     (打哈欠)
    ├── Idle_tail/        (摇尾巴)
    ├── Touchhead/        (摸头反应)
    ├── Touchbody/        (摸身体反应)
    ├── Left/             (向左走)
    ├── Right/            (向右走)
    ├── Sleep/            (睡觉)
    ├── Spinaround/       (转圈圈)
    ├── Stretch/          (伸懒腰)
    ├── Jump/             (起跳)
    ├── Thinking/         (思考)
    ├── Drag_1/           (拖动动画 1)
    ├── Drag_2/           (拖动动画 2，紧跟 Drag_1)
    └── Emotion_*/        (各种情绪)
"""
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


class PetAnimator:
    """更高层的"我现在想做什么"语义封装。"""

    # 特殊待机触发间隔：60 秒（每分钟随机触发一次）
    SPECIAL_IDLE_INTERVAL_S = 60

    def __init__(self, atlas: SpriteAtlas, player: AnimationPlayer,
                 on_animation_changed: Optional[Callable] = None):
        self.atlas = atlas
        self.player = player
        self._on_animation_changed = on_animation_changed
        self._last_group_getter: Optional[Callable[[], Optional[Animation]]] = None
        self._last_idle_set_at: float = 0.0
        # 主待机动画（从 idle_default 中随机选择一个）
        self._current_idle: Optional[Animation] = None
        if atlas.idle_default:
            self._current_idle = random.choice(self.atlas.idle_default)
        # 特殊待机定时器（每分钟触发）
        self._special_idle_timer: Optional[QTimer] = None
        self._setup_special_idle_timer()
        # 拖动动画链：drag_1 播完后自动播 drag_2
        self._drag_chain_active: bool = False
        # 睡觉状态（用于 state.tick 判断是否跳过衰减）
        self._sleeping: bool = False

    def _setup_special_idle_timer(self) -> None:
        """设置定时器，每分钟触发一次特殊待机动画。"""
        self._special_idle_timer = QTimer()
        self._special_idle_timer.setSingleShot(True)
        self._special_idle_timer.timeout.connect(self._trigger_special_idle)
        self._reschedule_special_idle()

    def _reschedule_special_idle(self) -> None:
        """重新调度特殊待机定时器（60 秒间隔）。"""
        if not self.atlas.idle_special:
            return
        interval_ms = self.SPECIAL_IDLE_INTERVAL_S * 1000
        self._special_idle_timer.setInterval(interval_ms)
        self._special_idle_timer.start()

    def _trigger_special_idle(self) -> None:
        """播放一次特殊待机动画（歪头/打哈欠/摇尾巴），播完回到主待机。"""
        if self._sleeping or not self.atlas.idle_special:
            return
        # 只在当前动画是主待机时才触发
        cur = self.player.current_animation()
        if cur is not None and cur not in self.atlas.idle_default:
            self._reschedule_special_idle()
            return
        a = random.choice(self.atlas.idle_special)
        if a:
            once_anim = Animation(a.name, a.frames, Animation.ONCE)
            self.player.play(once_anim, on_finished=self.set_idle)
            self._notify_anim_changed()
        self._reschedule_special_idle()

    def _notify_anim_changed(self) -> None:
        if self._on_animation_changed:
            self._on_animation_changed()

    def is_sleeping(self) -> bool:
        """是否在睡觉（用于 state.tick 判断是否跳过衰减）。"""
        return self._sleeping

    def set_idle(self) -> None:
        """切到主待机（从 idle_default 中随机选择一个）。"""
        self._sleeping = False
        if not self.atlas.idle_default:
            return
        # 从主待机变体中随机选择一个（Default 目录可能有多个动画）
        new_idle = random.choice(self.atlas.idle_default)
        if not new_idle:
            return
        cur = self.player.current_animation()
        # 如果已经是同一个动画，不需要切换
        if cur is new_idle:
            return
        self.player.play(new_idle)
        self._current_idle = new_idle
        self._last_group_getter = lambda: new_idle
        self._last_idle_set_at = time.time()
        self._notify_anim_changed()

    def set_lock_first_idle(self, on: bool) -> None:
        """兼容接口。"""

    def set_sleep(self) -> None:
        """睡觉：先播「入睡」动画（sleep），播完循环播放「睡着」动画（sleep_2）。"""
        self._sleeping = True
        self.atlas.ensure_loaded('sleep')
        self.atlas.ensure_loaded('sleep_2')
        a = self.atlas.any(self.atlas.sleep)
        if a:
            self.player.play_one_shot(a, on_finished=self._on_sleep_transition)
            self._notify_anim_changed()
        elif self.atlas.any(self.atlas.sleep_2):
            self.player.play(self.atlas.any(self.atlas.sleep_2))
            self._notify_anim_changed()

    def _on_sleep_transition(self) -> None:
        """入睡动画播完 → 循环播放睡着动画。"""
        self.atlas.ensure_loaded('sleep_2')
        a = self.atlas.any(self.atlas.sleep_2)
        if a:
            self.player.play(a)
            self._notify_anim_changed()

    def set_wake(self) -> None:
        """醒来：播放「醒来」动画（sleep_3），播完回到待机。"""
        self.atlas.ensure_loaded('sleep_3')
        a = self.atlas.any(self.atlas.sleep_3)
        if a:
            self._sleeping = False
            self.player.play_one_shot(a, on_finished=self._on_wake_finished)
            self._notify_anim_changed()
        else:
            self.set_idle()

    def _on_wake_finished(self) -> None:
        """醒来动画播完 → 回到待机。"""
        self.set_idle()

    def play_eat(self) -> None:
        """吃饭：播放一次吃饭动画，播完回到待机。"""
        self.atlas.ensure_loaded('eat_rice')
        a = self.atlas.any(self.atlas.eat_rice)
        if a:
            self.player.play_one_shot(a, on_finished=self.set_idle)
            self._notify_anim_changed()
        else:
            self.set_idle()

    def set_walk(self, direction: str) -> None:
        """切到走路动画：direction in {'left','right'}."""
        grp_name = 'walk_left' if direction == 'left' else 'walk_right'
        self.atlas.ensure_loaded(grp_name)
        grp = self.atlas.walk_left if direction == 'left' else self.atlas.walk_right
        a = self.atlas.any(grp)
        if a:
            self.player.play(a)
            self._last_group_getter = lambda g=grp: self.atlas.any(g)
            self._notify_anim_changed()

    def start_drag(self) -> None:
        """开始/持续拖动时：循环播放拖动动画 1（Drag_1）。"""
        self.atlas.ensure_loaded('drag_1')
        if not self.atlas.drag_1:
            return
        a = self.atlas.any(self.atlas.drag_1)
        if a:
            self._drag_chain_active = True
            # LOOP 模式循环播放 Drag_1（拖拽期间一直显示）
            self.player.play(a)
            self._notify_anim_changed()

    def end_drag(self) -> None:
        """松开鼠标：播放一次拖动动画 2（Drag_2），播完回到默认动画。"""
        self._drag_chain_active = False
        self.atlas.ensure_loaded('drag_2')
        if self.atlas.drag_2:
            b = self.atlas.any(self.atlas.drag_2)
            if b:
                # ONCE 模式：Drag_2 只播一次，播完回到默认待机
                from app.animation.animations import Animation as AnimCls
                once_anim = AnimCls(b.name, b.frames, AnimCls.ONCE)
                self.player.play(once_anim, on_finished=self._on_drag_2_finished)
                self._notify_anim_changed()
                return
        # 没有 Drag_2 时直接回默认待机
        self.set_idle()

    def _on_drag_2_finished(self) -> None:
        """Drag_2 播完回到默认待机。"""
        self.set_idle()

    def set_emotion(self, name: str) -> None:
        """切到情绪动画（播放一次后回到 idle）。"""
        self.atlas.ensure_loaded(f'emotion_{name}')
        grp = getattr(self.atlas, f'emotion_{name}', [])
        a = self.atlas.any(grp)
        if a:
            once_anim = Animation(a.name, a.frames, Animation.ONCE)
            self.player.play(once_anim)
            self._notify_anim_changed()

    def set_thinking(self) -> None:
        """思考中：随机播放 think 或 think_2 动画（循环）。"""
        self.atlas.ensure_loaded('thinking')
        self.atlas.ensure_loaded('thinking_2')
        pool: list[Animation] = []
        if self.atlas.thinking:
            pool.extend(self.atlas.thinking)
        if self.atlas.thinking_2:
            pool.extend(self.atlas.thinking_2)
        if pool:
            a = random.choice(pool)
            self.player.play(a)
            self._notify_anim_changed()

    def play_file(self) -> None:
        """吃文件：播放一次 file 动画，播完回到待机。"""
        self.atlas.ensure_loaded('file')
        a = self.atlas.any(self.atlas.file)
        if a:
            self.player.play_one_shot(a, on_finished=self.set_idle)
            self._notify_anim_changed()
        else:
            self.set_idle()

    def play_stretch(self) -> None:
        """伸懒腰：播放一次 stretch 动画，播完回到待机。"""
        self.atlas.ensure_loaded('stretch')
        a = self.atlas.any(self.atlas.stretch)
        if a:
            self.player.play_one_shot(a, on_finished=self.set_idle)
            self._notify_anim_changed()
        else:
            self.set_idle()

    def play_jump(self) -> None:
        """起跳：播放一次 jump 动画，播完回到待机。"""
        self.atlas.ensure_loaded('jump')
        a = self.atlas.any(self.atlas.jump)
        if a:
            self.player.play_one_shot(a, on_finished=self.set_idle)
            self._notify_anim_changed()
        else:
            self.set_idle()

    def play_swim(self) -> None:
        """游泳：播放一次 swimming 动画，播完回到待机。"""
        self.atlas.ensure_loaded('playing_water')
        a = self.atlas.any(self.atlas.playing_water)
        if a:
            once_anim = Animation(a.name, a.frames, Animation.ONCE)
            self.player.play_one_shot(once_anim, on_finished=self.set_idle)
            self._notify_anim_changed()
        else:
            self.set_idle()

    def play_reaction(self, where: str) -> None:
        """播一次性反应动画（摸头/摸身体）。播完回到之前状态。"""
        if where == 'head':
            self.atlas.ensure_loaded('touch_head')
            grp = self.atlas.touch_head
        elif where == 'body':
            self.atlas.ensure_loaded('touch_body')
            grp = self.atlas.touch_body
        else:
            grp = []
        a = self.atlas.any(grp)
        if a:
            self.player.play_one_shot(a)
            self._notify_anim_changed()
        elif self._last_group_getter:
            self.player.back_to_idle()
            self._notify_anim_changed()

    def play_spin(self) -> None:
        """播放转圈圈动画（一次性），播完回到主待机。"""
        self.atlas.ensure_loaded('spin')
        a = self.atlas.any(self.atlas.spin)
        if a:
            once_anim = Animation(a.name, a.frames, Animation.ONCE)
            self.player.play(once_anim, on_finished=self.set_idle)
            self._notify_anim_changed()
        else:
            self.set_idle()

    def set_startup(self) -> None:
        a = self.atlas.any(self.atlas.startup)
        if a:
            self.player.play(a)
            self._notify_anim_changed()

    def set_shutdown(self) -> None:
        a = self.atlas.any(self.atlas.shutdown)
        if a:
            self.player.play(a)
            self._notify_anim_changed()

    def play_emotion_reaction(self, emotion: str) -> None:
        """播一次性情绪反应 + 回到 idle。"""
        self.atlas.ensure_loaded(f'emotion_{emotion}')
        grp = getattr(self.atlas, f'emotion_{emotion}', [])
        a = self.atlas.any(grp)
        if a:
            once_anim = Animation(a.name, a.frames, Animation.ONCE)
            self.player.play(once_anim)
            self._notify_anim_changed()
        else:
            self.player.back_to_idle()
            self._notify_anim_changed()
