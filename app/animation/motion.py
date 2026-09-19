"""自动运动系统：桌宠自己会沿屏走、探头、随机溜达。

VPet 风格的 move 系统（PR4 增强）：
    walk.left/right        沿屏幕水平走动
    crawl.left/right       沿屏幕底部爬行
    climb.top.left/right   沿屏幕顶部走（占位 —— 未启用的 sprite 子系统）
    sidehide.left/right    探头到屏幕左/右边缘 → 5–10 秒后爬回
    smartmove.toward       朝鼠标当前位置走两步（被吸引），不紧追

跨屏：所有屏幕 availableGeometry 取并集，桌宠会跨屏走。
"""
from __future__ import annotations

import logging
import math
import random
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from app.core.qt_compat import QCursor, QGuiApplication, QPoint, QRect, QTimer

log = logging.getLogger(__name__)


class MoveMode(Enum):
    IDLE = "idle"             # 静止待机
    WALK_LEFT = "walk_left"
    WALK_RIGHT = "walk_right"
    CRAWL_LEFT = "crawl_left"
    CRAWL_RIGHT = "crawl_right"
    SIDEHIDE_LEFT = "sidehide_left"
    SIDEHIDE_RIGHT = "sidehide_right"
    SMARTMOVE = "smartmove"   # 朝鼠标当前位置走一段


@dataclass
class MoveDecision:
    mode: MoveMode
    duration_ms: int          # 这个动作持续多久
    speed_px_per_sec: float   # 移动速度
    target: QPoint | None     # 目标位置（可选）


class MotionController:
    """周期性决定下一个动作 + 执行中更新位置。

    PR4 增强：
        - screen_geometry_getter 期望返回**所有屏幕**可用区域的并集（不再假设单屏）。
        - SideHide 模式有 settle_time，到点后**主动爬回来**而不是永远停在屏边。
        - smartmove：每 ~10 s 抽样鼠标位置，若桌宠与鼠标距离 > 阈值，朝鼠标走一段。
          这是 VPet 同款「smartmove#True」行为。

    get_window:           () -> QWidget
    screen_geometry_getter: () -> QRect | None
    animator:             PetAnimator
    cursor_pos_getter:    () -> QPoint (可选，默认走 QCursor.pos())
    """

    # 概率分布（PR-mute-motion：让桌宠大多时间 IDLE，偶发小幅动作）
    # 用户已在桌宠附近时（is_user_interacting）→ 100 % IDLE，再加上「IDLE
    # 期内不重新抽签」的 guard，使"动一下→长时间停"成为主节奏。
    WEIGHTS = [
        (MoveMode.IDLE,             18),   # ~90 % 待机
        (MoveMode.WALK_LEFT,        0.8),
        (MoveMode.WALK_RIGHT,       0.8),
        (MoveMode.SMARTMOVE,        0.4),
    ]
    # 用户关注时的静止时长（毫秒）
    USER_NEAR_IDLE_MIN_MS = 20_000
    USER_NEAR_IDLE_MAX_MS = 45_000
    # IDLE 时长（无用户时）：长时间保持
    IDLE_MIN_MS = 30_000
    IDLE_MAX_MS = 75_000
    # 动作时长（走一段走久一点，而不是频繁切小动作）
    MOVE_MIN_MS = 8_000
    MOVE_MAX_MS = 18_000
    # 决策 timer 间隔：上一版 6-15 s 太频繁，桌宠一直在重抽
    DECIDE_MIN_MS = 25_000
    DECIDE_MAX_MS = 45_000

    # PR-settings-window: 运行时调参接口
    def apply_settings(self, *,
                       idle_seconds: Optional[int] = None,
                       walk_seconds: Optional[int] = None,
                       decide_seconds: Optional[int] = None) -> None:
        """通过 SettingsWindow 实时改变 IDLE / WALK / 决策 节拍。"""
        if idle_seconds is not None:
            self.idle_min_ms = idle_seconds * 1000
            self.idle_max_ms = int(self.idle_min_ms * 1.6)
        if walk_seconds is not None:
            self.move_min_ms = walk_seconds * 1000
            self.move_max_ms = int(self.move_min_ms * 1.8)
        if decide_seconds is not None:
            self.decide_min_ms = decide_seconds * 1000
            self.decide_max_ms = int(self.decide_min_ms * 1.6)
        # 重置 decide timer
        self._reschedule_decide_timer()

    # SideHide 在屏边停留多久后才决定回
    SIDEHIDE_SETTLE_MS = (5000, 10000)
    # smartmove 触发的最小距离（像素）
    SMARTMOVE_MIN_DIST = 120
    # smartmove 走多远
    SMARTMOVE_STEP_DIST = (60, 160)

    def __init__(self, get_window, screen_geometry_getter, animator,
                 cursor_pos_getter: Optional[Callable[[], QPoint]] = None,
                 is_user_interacting: Optional[Callable[[], bool]] = None):
        self.get_window = get_window
        self.screen_geometry_getter = screen_geometry_getter
        self.animator = animator
        self.cursor_pos_getter = cursor_pos_getter or self._default_cursor_pos
        # 用户是否在关注桌宠（鼠标位于桌面 / enter / drag 时为 True）。
        # PR-mute-motion：进入此状态后强制 IDLE 15-30 s。
        self.is_user_interacting = is_user_interacting or (lambda: False)
        # PR-settings-window: 实例级别的节拍常量（每个 pet 可独立调）
        self.user_near_idle_min_ms = self.USER_NEAR_IDLE_MIN_MS
        self.user_near_idle_max_ms = self.USER_NEAR_IDLE_MAX_MS
        self.idle_min_ms = self.IDLE_MIN_MS
        self.idle_max_ms = self.IDLE_MAX_MS
        self.move_min_ms = self.MOVE_MIN_MS
        self.move_max_ms = self.MOVE_MAX_MS
        self.decide_min_ms = self.DECIDE_MIN_MS
        self.decide_max_ms = self.DECIDE_MAX_MS
        self.current: MoveDecision = MoveDecision(MoveMode.IDLE, 5000, 0, None)
        self._decision_at = time.time()

        # 决定新动作的定时器（PR-mute-motion：间隔拉大到 25-45 s 区间随机）
        self._decide_timer = QTimer()
        self._decide_timer.setInterval(random.randint(self.decide_min_ms,
                                                      self.decide_max_ms))
        self._decide_timer.timeout.connect(self._decide_next)
        self._decide_timer.start()
        # 每经过一张动作后顺手重置定时器区间
        self._decide_timer.timeout.connect(self._reschedule_decide_timer)

        # 平滑移动定时器（20 fps = 50ms，性能优化）
        self._move_timer = QTimer()
        self._move_timer.setInterval(50)
        self._move_timer.timeout.connect(self._step_move)

        self._started = False

    @staticmethod
    def _default_cursor_pos() -> QPoint:
        try:
            return QCursor.pos()
        except Exception:
            return QPoint(0, 0)

    def _reschedule_decide_timer(self) -> None:
        """让"决策 timer"间隔抖动到 25-45 s，避免 cadence 太规律像机器人。"""
        if not self._decide_timer.isActive():
            return
        new_ms = random.randint(self.decide_min_ms, self.decide_max_ms)
        self._decide_timer.setInterval(new_ms)
        self._decide_timer.start()  # restart with new interval

    def start(self) -> None:
        if not self._started:
            self._started = True
            self._move_timer.start()
            self._decide_next()

    def stop(self) -> None:
        self._move_timer.stop()
        self._decide_timer.stop()

    def _decide_next(self) -> None:
        """随机选下一个动作。"""
        win = self.get_window()
        if not win:
            return
        scr = self.screen_geometry_getter()
        if not scr or scr.width() <= 0:
            return

        # PR-mute-motion guard：如果当前还是 IDLE 且还没到 duration 末尾，
        # 就不要重新抽签 —— 保留"长时间不动"的节奏（用户反馈"动作太频繁"）。
        elapsed_ms = (time.time() - self._decision_at) * 1000
        if (self.current.mode == MoveMode.IDLE
                and elapsed_ms < self.current.duration_ms):
            return

        # PR-mute-motion：用户正在关注桌宠（鼠标进入 / 拖动中）→ 强 IDLE 20-45 s
        if self.is_user_interacting():
            self.current = MoveDecision(
                MoveMode.IDLE,
                random.randint(self.user_near_idle_min_ms,
                               self.user_near_idle_max_ms),
                0, None,
            )
            self.animator.set_idle()
            self._decision_at = time.time()
            return

        # 概率分布
        weights = self.WEIGHTS
        total = sum(w for _, w in weights)
        r = random.uniform(0, total)
        cum = 0
        chosen = MoveMode.IDLE
        for mode, w in weights:
            cum += w
            if r <= cum:
                chosen = mode
                break

        if chosen == MoveMode.IDLE:
            dur = random.randint(self.idle_min_ms, self.idle_max_ms)
            self.current = MoveDecision(MoveMode.IDLE, dur, 0, None)
            self._decision_at = time.time()
            self._reschedule_decide_timer()
            # 不再强制调用 set_idle()，避免打断用户手动选择的动画
            # 动画切换由用户交互或初始状态控制
            return

        if chosen == MoveMode.SMARTMOVE:
            # 朝鼠标走一段：取距离，过近就直接 IDLE
            w = win.width(); h = win.height()
            cur = win.pos()
            mouse = self.cursor_pos_getter()
            dx = mouse.x() - (cur.x() + w // 2)
            dy = mouse.y() - (cur.y() + h // 2)
            dist = math.hypot(dx, dy)
            if dist < self.SMARTMOVE_MIN_DIST:
                self.current = MoveDecision(MoveMode.IDLE, 2000, 0, None)
                self._decision_at = time.time()
                # 不再强制调用 set_idle()，避免打断用户手动选择的动画
                return
            step = random.randint(*self.SMARTMOVE_STEP_DIST)
            ratio = step / dist
            tx = int(cur.x() + dx * ratio)
            ty = int(cur.y() + dy * ratio)
            # 钳到屏幕内
            tx = max(scr.left(), min(scr.right() - w, tx))
            ty = max(scr.top(),  min(scr.bottom() - h, ty))
            self.current = MoveDecision(
                mode=chosen,
                duration_ms=random.randint(self.move_min_ms, self.move_max_ms),
                speed_px_per_sec=float(random.choice([14, 18]) * 8),
                target=QPoint(tx, ty),
            )
            self._decision_at = time.time()
            # smartmove 没有专属 sprite，复用 walk 方向
            self.animator.set_walk('right' if dx >= 0 else 'left')
            return

        # 速度与目标位置
        speed = random.choice([8, 12, 18])
        w = win.width()
        h = win.height()
        x, y = win.x(), win.y()

        if chosen in (MoveMode.WALK_LEFT, MoveMode.CRAWL_LEFT):
            target_x = max(scr.left(), x - random.randint(80, 200))
            target_y = scr.bottom() - h if chosen == MoveMode.CRAWL_LEFT else y
            if chosen == MoveMode.CRAWL_LEFT:
                self.animator.set_crawl('left')
            else:
                self.animator.set_walk('left')
        elif chosen in (MoveMode.WALK_RIGHT, MoveMode.CRAWL_RIGHT):
            target_x = min(scr.right() - w, x + random.randint(80, 200))
            target_y = scr.bottom() - h if chosen == MoveMode.CRAWL_RIGHT else y
            if chosen == MoveMode.CRAWL_RIGHT:
                self.animator.set_crawl('right')
            else:
                self.animator.set_walk('right')
        elif chosen == MoveMode.SIDEHIDE_LEFT:
            self.animator.set_edge_hide('left')
            target_x = scr.left() - w // 2
            target_y = y
            speed = 6
        elif chosen == MoveMode.SIDEHIDE_RIGHT:
            self.animator.set_edge_hide('right')
            target_x = scr.right() - w // 2
            target_y = y
            speed = 6
        else:
            return

        # SideHide 类型有更长的「停留 settle」时间，到点后会决定「回 vs 留」
        if chosen in (MoveMode.SIDEHIDE_LEFT, MoveMode.SIDEHIDE_RIGHT):
            dur = random.randint(*self.SIDEHIDE_SETTLE_MS)
        else:
            dur = random.randint(self.move_min_ms, self.move_max_ms)

        self.current = MoveDecision(
            mode=chosen,
            duration_ms=dur,
            speed_px_per_sec=float(speed * 8),
            target=QPoint(target_x, target_y),
        )
        self._decision_at = time.time()
        self._reschedule_decide_timer()
        # 一次性触发动画后回 idle（如果 walk/crawl 动画缺）
        # 注意：Live2D 渲染器没有 atlas/player，这里用 getattr 防护，
        # 无 sprite 资源时跳过（set_edge_hide 已在渲染器侧退化为待机）。
        if chosen in (MoveMode.SIDEHIDE_LEFT, MoveMode.SIDEHIDE_RIGHT):
            atlas = getattr(self.animator, "atlas", None)
            player = getattr(self.animator, "player", None)
            if atlas is not None and player is not None:
                hide_anim = atlas.sidehide_left \
                    if chosen == MoveMode.SIDEHIDE_LEFT \
                    else atlas.sidehide_right
                picked = atlas.any(hide_anim)
                if picked is not None:
                    player.play(picked)
                    notify = getattr(self.animator, "_notify_anim_changed", None)
                    if callable(notify):
                        notify()

    def _step_move(self) -> None:
        if self.current.mode == MoveMode.IDLE or not self.current.target:
            return
        # 检查到期
        elapsed = (time.time() - self._decision_at) * 1000
        if elapsed >= self.current.duration_ms:
            self._decide_next()
            return

        win = self.get_window()
        if not win:
            return
        cur = win.pos()
        tgt = self.current.target
        dx = tgt.x() - cur.x()
        dy = tgt.y() - cur.y()
        dist = (dx * dx + dy * dy) ** 0.5
        if dist < 2:
            # 到达目标
            if self.current.mode in (MoveMode.SIDEHIDE_LEFT, MoveMode.SIDEHIDE_RIGHT):
                # SideHide 到达后**不立即退出**—— 等 _decide_next 在 settle 时间到后再触发
                # 这里只是把 target 标 None，避免继续推进
                self.current.target = None
            else:
                # 移动结束，回到 idle 动画
                self.animator.set_idle()
                self._decide_next()
            return
        # 推进
        step = max(1.0, self.current.speed_px_per_sec * 0.033)
        nx = cur.x() + (dx / dist) * step
        ny = cur.y() + (dy / dist) * step
        win.move(int(nx), int(ny))

    # -------------------- 跨屏合并 getter --------------------
    @staticmethod
    def merged_screen_geometry() -> Optional[QRect]:
        """所有屏幕的 availableGeometry 的并集（PR4）。

        没有 QApplication 或 QGuiApplication 时返回 None（让上层退化为单屏）。
        """
        try:
            screens = QGuiApplication.screens()
        except Exception:
            return None
        if not screens:
            return None
        rect = screens[0].availableGeometry()
        for s in screens[1:]:
            rect = rect.united(s.availableGeometry())
        return rect