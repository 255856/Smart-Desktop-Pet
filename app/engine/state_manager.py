"""状态管理器：封装 PetState + SaveStore + ReminderStore 的管理逻辑。

数值持久化走 yaml companion_config.persistence；事件钩子走 yaml event_hooks；
时间驱动衰减走 yaml time_decay。
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from app.core.qt_compat import QObject, QTimer, Signal
from app.engine.reminder import ReminderStore
from app.core.save import SaveStore, default_save_path
from app.engine.state import PetState, is_night

log = logging.getLogger(__name__)


class StateManager(QObject):
    """封装 state + save_store + reminder 的生命周期管理。

    通过信号暴露事件，避免直接持有 UI 对象。
    """

    # 信号：食物过低时发出（由 UI 层接收并弹气泡）
    food_low = Signal()
    # 信号：提醒触发时转发（原始文本）
    reminder_fired = Signal(str)
    # 信号：mode 切换（由 UI 层接收并切动画）
    mode_changed = Signal(object, object)

    def __init__(self, root: Path, cfg, args, parent: QObject | None = None) -> None:
        """
        Args:
            root: 项目根目录
            cfg:  Config 对象
            args: 命令行参数（含 reset_state）
            parent: Qt 父对象
        """
        super().__init__(parent)
        self.root = root
        self.cfg = cfg

        # 路径走 yaml persistence.file，默认 data/pet_state.json
        self.save_store = SaveStore(path=None, autosave_seconds=60)
        self._last_food_warn = 0.0

        if args.reset_state:
            self.state = PetState()
            self.save_store.set_state(self.state)
        else:
            self.state = self.save_store.load()
            self.save_store.set_state(self.state)

        # 任何 state 改动都触发 mark_dirty → write_on_update 立即写（yaml 配）
        self.state.on_change = self.save_store.mark_dirty

        # mode 切换信号转发
        self.state.on_mode_change = self._forward_mode_change

        # data_file 默认是 "reminders.json"（裸文件名）；若不带路径前缀，加 data/
        rem_file = self.cfg.reminder.data_file
        if rem_file.endswith(".json") and "/" not in rem_file and "\\" not in rem_file:
            rem_path = root / "data" / rem_file
        else:
            rem_path = root / rem_file
        self.reminders = ReminderStore(rem_path)
        # 转发 reminder_triggered 信号
        self.reminders.reminder_triggered.connect(self.reminder_fired.emit)
        self._last_tick_ts: float = 0.0   # 上次状态 tick 时间：按真实 dt 推进，支持降频

        # 时间驱动：每小时检查一次衰减 + 互动时间检查
        from app.core.companion_config import companion_config
        self._decay_timer = QTimer(self)
        self._decay_timer.setInterval(
            companion_config.time_decay.check_interval_seconds * 1000
        )
        self._decay_timer.timeout.connect(self._on_decay_tick)
        self._decay_timer.start()
        # v23-6: 记录上次衰减检查时间, 用真实 dt 算衰减量
        self._last_decay_ts: float = 0.0

    def start(self) -> None:
        """启动自动存档定时器 + 时间衰减定时器。"""
        self.save_store.start()
        if not self._decay_timer.isActive():
            self._decay_timer.start()

    def on_about_to_quit(self) -> None:
        """退出前最终存档。"""
        log.info("退出前最终存档")
        try:
            self.save_store.write_sync(self.state)
        except Exception as e:  # noqa: BLE001
            log.warning("退出存档失败：%s", e)
        if self._decay_timer.isActive():
            self._decay_timer.stop()

    def state_tick_once(self, is_sleeping: bool = False) -> None:
        """按真实流逝时间推进状态（衰减 + 跨夜）。

        状态衰减对 dt 完全线性，所以 tick 不必每秒执行；外层定时器降频后，
        这里按上次 tick 的实际间隔传 dt，结果与逐秒一致。
        """
        now = time.time()
        if not self._last_tick_ts:
            dt = 1.0
        else:
            # 间隔上限 60s：避免首次 tick、或长时间休眠后一次性衰减过多
            dt = min(max(now - self._last_tick_ts, 0.0), 60.0)
        self._last_tick_ts = now
        self.state.tick(
            dt_seconds=dt,
            is_night=is_night(now),
            sleeping=is_sleeping,
        )
        # 指标过低 → 发出 food_low 信号（节流 60s）
        if (self.state.strength_food < self.state.food_low
                and self._last_food_warn + 60 < now):
            self.food_low.emit()
            self._last_food_warn = now

    def on_eat_requested(self) -> None:
        """吃饭: 走 yaml event_hooks.user_feed (主人 2026-10-16).

        v23-5 起改成走 yaml, 默认 satiety +15, mood +5. 同时调 on_interact
        记录互动时间戳 (心情衰减计时用).
        """
        # v23-5: 走 yaml 钩子, 不再 hardcode feed 参数
        self.state.apply_feed()
        self.state.apply_interact()    # 记录 _last_interact_ts, 心情 +3 / 好感 +2
        # 食物给 exp 算 v23-5 yaml 钩子没动, 沿用旧 +3 (主人没明说, 暂保留)
        self.state.exp = self.state.exp + 3.0

    def _on_decay_tick(self) -> None:
        """v23-6 时间驱动衰减 (每 10 分钟一次, 按真实 dt 算).

        yaml time_decay:
          - satiety_per_hour = -0.833 (24h -20)
          - mood_no_interact_48h = -5
          - mood_low_satiety_extra_per_24h = -5
        """
        from app.core.companion_config import companion_config
        cfg = companion_config.time_decay
        now = time.time()
        if not self._last_decay_ts:
            self._last_decay_ts = now
            return
        hours = (now - self._last_decay_ts) / 3600.0
        self._last_decay_ts = now
        if hours <= 0:
            return

        before = (self.state.strength_food, self.state.feeling, self.state.likability)
        # 饱食度按小时衰减
        if cfg.satiety_per_hour:
            self.state.strength_food = max(
                0.0,
                self.state.strength_food + cfg.satiety_per_hour * hours
            )
        # 心情: 48h 没互动扣 5
        last_interact = self.state._last_interact_ts or 0.0
        if cfg.mood_no_interact_48h and last_interact:
            hours_no_interact = (now - last_interact) / 3600.0
            if hours_no_interact >= 48:
                # 按比例缩放 (-5 / 24h, 但只在 48h+ 时扣)
                self.state.feeling = max(
                    0.0,
                    self.state.feeling + cfg.mood_no_interact_48h
                )
        # 饱食度<30 时, 心情额外扣 (按 24h -5 折算每小时)
        if cfg.mood_low_satiety_extra_per_24h and self.state.strength_food < 30:
            self.state.feeling = max(
                0.0,
                self.state.feeling + (cfg.mood_low_satiety_extra_per_24h / 24.0) * hours
            )
        after = (self.state.strength_food, self.state.feeling, self.state.likability)
        if before != after:
            log.info("时间驱动衰减: hours=%.2f food %.1f→%.1f mood %.1f→%.1f",
                     hours, before[0], after[0], before[1], after[1])

    def _forward_mode_change(self, old, new) -> None:
        """转发 mode 切换信号。"""
        self.mode_changed.emit(old, new)
