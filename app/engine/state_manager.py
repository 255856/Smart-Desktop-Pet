"""状态管理器：封装 PetState + SaveStore + ReminderStore 的管理逻辑。"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from app.core.qt_compat import QObject, Signal
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

        self.save_store = SaveStore(path=default_save_path(), autosave_seconds=60)
        self._last_food_warn = 0.0

        if args.reset_state:
            self.state = PetState()
            self.save_store.set_state(self.state)
        else:
            self.state = self.save_store.load()
            self.save_store.set_state(self.state)

        # 任何 state 改动都触发 mark_dirty → 下一次 autosave 落盘
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

    def start(self) -> None:
        """启动自动存档定时器。"""
        self.save_store.start()

    def on_about_to_quit(self) -> None:
        """退出前最终存档。"""
        log.info("退出前最终存档")
        try:
            self.save_store.write_sync(self.state)
        except Exception as e:  # noqa: BLE001
            log.warning("退出存档失败：%s", e)

    def state_tick_once(self, is_sleeping: bool = False) -> None:
        """每秒推进一次状态（衰减 + 跨夜）。"""
        now = time.time()
        self.state.tick(
            dt_seconds=1.0,
            is_night=is_night(now),
            sleeping=is_sleeping,
        )
        # 指标过低 → 发出 food_low 信号（节流 60s）
        if (self.state.strength_food < self.state.food_low
                and self._last_food_warn + 60 < now):
            self.food_low.emit()
            self._last_food_warn = now

    def on_eat_requested(self) -> None:
        """吃饭：涨饱食度 + 体力 + 心情。"""
        self.state.feed(
            strength=20, strength_food=40, strength_drink=15,
            feeling=10, health=5, exp=3,
        )
        self.state.on_interact(feeling_gain=8, likability_gain=2)

    def _forward_mode_change(self, old, new) -> None:
        """转发 mode 切换信号。"""
        self.mode_changed.emit(old, new)
