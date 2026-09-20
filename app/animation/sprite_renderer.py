"""Sprite 渲染器：高层"我想做什么"语义 + QLabel 显示 + PetRenderer 接口实现。

模块布局：
    - `PetAnimator`：继承 `PetRenderer`，实现所有高层动作（set_idle / set_emotion /
      set_sleep / play_eat / ...）+ 一次性 idle 定时器 + 拖动链 + 一次性情绪反应等。
    - `SpriteRenderer`：继承 `PetAnimator`，负责资源加载（`SpriteAtlas.prescale`）
      与显示控件（`QLabel`），并把 PetAnimator 的初始化参数从外面传进来。
    - `SpriteAtlas`：资源加载器，独立于本模块，但 PetAnimator 在动画组访问前会调用其
      `ensure_loaded()`。当前 `SpriteAtlas` 仍放在 `app.animation.sprite_atlas`，保留
      `from app.animation.sprite_atlas import PetAnimator` 旧 import 路径的兼容。

性能优化：构造时一次性 prescale 所有帧到目标尺寸，切换动画不卡顿。
"""
from __future__ import annotations

import logging
import random
import time
from pathlib import Path
from typing import Callable, Optional

from app.animation.animations import Animation, AnimationPlayer, Frame
from app.animation.pet_renderer import PetRenderer
from app.animation.sprite_atlas import SpriteAtlas
from app.core.qt_compat import QLabel, QPixmap, Qt, QTimer

log = logging.getLogger(__name__)


def _scale_pixmap_keep_alpha(pix: QPixmap, target_size) -> QPixmap:
    """缩放 QPixmap 保持 alpha + 平滑。"""
    if pix is None or pix.isNull():
        return pix
    return pix.scaled(
        target_size, Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


class PetAnimator(PetRenderer):
    """Sprite 渲染器：高层语义 + 一次性特殊待机定时器 + 拖动动画链。

    实现 PetRenderer 接口（sprite 版本）。Live2DRenderer 是另一份实现。
    """

    # 特殊待机触发间隔：60 秒（每分钟随机触发一次）
    SPECIAL_IDLE_INTERVAL_S = 60

    def __init__(self, atlas: SpriteAtlas, player: AnimationPlayer,
                 display_widget: Optional["QLabel"] = None,
                 on_animation_changed: Optional[Callable] = None):
        # PetRenderer.__init__ 不需要，但保持 isinstance(pet_renderer.PetRenderer) 可用
        # 通过 metaclass 自动；这里不需要显式 super().__init__()（ABCMeta 不要求）
        self.atlas = atlas
        self.player = player
        self.display_widget = display_widget  # QLabel（PetWindow 传入）
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

    # ---------- PetRenderer 接口实现 ----------

    def play_animation(self, anim_name: str) -> None:
        """通用一次性动作（按名字分发到对应方法）。"""
        m = {
            'jump': self.play_jump,
            'stretch': self.play_stretch,
            'spin': self.play_spin,
            'swim': self.play_swim,
            'eat': self.play_eat,
            'file': self.play_file,
            'jump': self.play_jump,
        }.get(anim_name.lower())
        if m:
            m()
        else:
            log.warning("play_animation: 未知动作 '%s'", anim_name)

    def set_parameter(self, name: str, value: float, duration_ms: int = 0) -> None:
        # sprite 不支持参数控制
        pass

    def set_expression(self, name: str) -> None:
        # sprite 不支持 expression
        pass

    def shutdown(self) -> None:
        """清理资源。"""
        if self._special_idle_timer is not None:
            self._special_idle_timer.stop()
            self._special_idle_timer = None
        # 注意：AnimationPlayer 是无状态的（QTimer 在 PetWindow 里），不需要 stop


class SpriteRenderer(PetAnimator):
    """PetRenderer 的 sprite 实现 + 资源加载 + 显示控件。

    创建时加载 sprite 资源到内存（prescale），后续切动画不再读盘。
    内部继承 PetAnimator，因此 `pet.set_idle() / play_eat() / ...` 直接可用，无需转发。
    """

    def __init__(self, sprite_dir: Path, fallback_image: Optional[Path],
                 window_size, scale: float = 1.0):
        self.sprite_dir = Path(sprite_dir)
        self.fallback_image = Path(fallback_image) if fallback_image else None
        self.window_size = window_size
        self.scale = scale

        # 资源
        self.atlas = SpriteAtlas(self.sprite_dir, fallback_image=self.fallback_image)
        self.atlas.prescale(window_size, _scale_pixmap_keep_alpha)

        # 播放器（AnimationPlayer 无 Qt signal；帧更新由 PetWindow._start_frame_timer() 驱动）
        self.player = AnimationPlayer()

        # 显示控件
        self.label = QLabel()
        self.label.setFixedSize(window_size.width(), window_size.height())
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setStyleSheet("background-color: transparent;")

        # 立刻显示 fallback 图（避免第一帧加载期间显示空白）
        if self.fallback_image and self.fallback_image.is_file():
            from app.core.qt_compat import QPixmap
            pix = QPixmap(str(self.fallback_image))
            if not pix.isNull():
                pix = pix.scaled(
                    self.window_size,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                self.label.setPixmap(pix)

        # PetAnimator 初始化（设置 atlas/player/label/_on_animation_changed）
        super().__init__(
            atlas=self.atlas,
            player=self.player,
            display_widget=self.label,
            on_animation_changed=self._on_anim_changed,
        )

    def _on_frame_changed(self, frame: Frame) -> None:
        if frame.pixmap and not frame.pixmap.isNull():
            self.label.setPixmap(frame.pixmap)

    def _on_anim_changed(self) -> None:
        # PetAnimator 触发；具体更新在 _on_frame_changed 里
        pass

    def get_widget(self):
        # 覆盖 PetAnimator.get_widget（PetAnimator 返回 display_widget；这里返回 self.label 同对象）
        return self.label

    def reload_atlas(self, fallback_image: Optional[Path] = None) -> None:
        """重新加载 sprite 资源（用于「帧时长 retune」后刷新内存中的 atlas）。

        保留同一个 SpriteRenderer / AnimationPlayer / QLabel 实例，只换内部 atlas 引用；
        避免 retune 期间出现「新旧两个 atlas 同时存在、player 引用旧 atlas」的同步问题。
        """
        fb = Path(fallback_image) if fallback_image else self.fallback_image
        new_atlas = SpriteAtlas(self.sprite_dir, fallback_image=fb)
        new_atlas.prescale(self.window_size, _scale_pixmap_keep_alpha)
        self.atlas = new_atlas
        # 同步当前动画：sprite retune 后帧时长变了，强制回到主待机让新时长生效
        self.set_idle()

    def shutdown(self) -> None:
        super().shutdown()
        # 注意：AnimationPlayer 无状态（timer 在 PetWindow）
        if self.label:
            self.label.deleteLater()


__all__ = ["SpriteRenderer", "PetAnimator"]