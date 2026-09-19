"""SpriteRenderer：现有 sprite 渲染的 PetRenderer 适配实现。

内部封装：
    - SpriteAtlas（加载所有动画组）
    - AnimationPlayer（QTimer-based 播放循环）
    - 一个 QLabel（显示当前帧）

对外暴露 PetRenderer 接口。

性能优化：一次性 prescale 所有帧到目标尺寸，切换动画不卡顿。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

from app.animation.animations import Animation, AnimationPlayer, Frame
from app.animation.pet_renderer import PetRenderer
from app.animation.sprite_atlas import SpriteAtlas, PetAnimator
from app.core.qt_compat import QLabel, QPixmap, Qt

log = logging.getLogger(__name__)


def _scale_pixmap_keep_alpha(pix: QPixmap, target_size) -> QPixmap:
    """缩放 QPixmap 保持 alpha + 平滑。"""
    if pix is None or pix.isNull():
        return pix
    return pix.scaled(
        target_size, Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


class SpriteRenderer(PetRenderer):
    """PetRenderer 的 sprite 实现。

    创建时加载 sprite 资源到内存（prescale），后续切动画不再读盘。
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

        # 播放器
        self.player = AnimationPlayer()
        # 注意：AnimationPlayer 没用 Qt signal；帧更新由 PetWindow._start_frame_timer() 驱动

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

        # 高层动作（内部用 PetAnimator 实现 PetRenderer 接口）
        self.animator = PetAnimator(
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

    # ----- PetRenderer 接口 -----
    def set_idle(self) -> None:
        self.animator.set_idle()

    def set_emotion(self, name: str) -> None:
        self.animator.set_emotion(name)

    def set_emotion_reaction(self, emotion: str) -> None:
        # PetAnimator 内部会播一次性 emotion 动画后回 idle
        self.animator.play_emotion_reaction(emotion)

    # ----- 状态查询（覆盖基类默认值，转发给 PetAnimator） -----
    def is_sleeping(self) -> bool:
        """是否在睡觉（用于 state.tick 跳过衰减）。"""
        return self.animator.is_sleeping()

    def set_sleep(self) -> None:
        self.animator.set_sleep()

    def set_wake(self) -> None:
        self.animator.set_wake()

    def set_thinking(self) -> None:
        self.animator.set_thinking()

    def set_walk(self, direction: str) -> None:
        self.animator.set_walk(direction)

    def start_drag(self) -> None:
        self.animator.start_drag()

    def end_drag(self) -> None:
        self.animator.end_drag()

    def play_reaction(self, where: str) -> None:
        self.animator.play_reaction(where)

    def play_animation(self, anim_name: str) -> None:
        self.animator.play_animation(anim_name)

    def play_eat(self) -> None:
        self.animator.play_eat()

    def play_file(self) -> None:
        self.animator.play_file()

    def play_spin(self) -> None:
        self.animator.play_spin()

    def play_stretch(self) -> None:
        self.animator.play_stretch()

    def play_jump(self) -> None:
        self.animator.play_jump()

    def play_swim(self) -> None:
        self.animator.play_swim()

    def set_parameter(self, name: str, value: float, duration_ms: int = 0) -> None:
        # sprite 不支持参数控制
        pass

    def set_expression(self, name: str) -> None:
        # sprite 不支持 expression
        pass

    def get_widget(self):
        return self.label

    def shutdown(self) -> None:
        self.animator.shutdown()
        # 注意：AnimationPlayer 无状态（timer 在 PetWindow）
        if self.label:
            self.label.deleteLater()


__all__ = ["SpriteRenderer"]