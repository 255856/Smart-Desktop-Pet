"""存档系统：把 PetState 持久化到磁盘 + 自动定时保存。"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from app.core.qt_compat import QObject, QTimer, Signal
from app.engine.state import PetState

log = logging.getLogger(__name__)


def default_save_path() -> Path:
    """`~/.desktop-pet/save.json`，跨平台。"""
    return Path.home() / ".desktop-pet" / "save.json"


class SaveStore(QObject):
    """存档管理器。"""

    saved = Signal()    # 每次落盘发一次

    def __init__(self, path: Optional[Path] = None,
                 autosave_seconds: int = 60,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.path = Path(path) if path else default_save_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.autosave_seconds = int(autosave_seconds)
        self._state: PetState = PetState()
        self._timer = QTimer(self)
        self._timer.setInterval(self.autosave_seconds * 1000)
        self._timer.timeout.connect(self._on_tick)
        self._dirty = False

    def start(self) -> None:
        if self.autosave_seconds > 0:
            self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        # 退出前最后存一次
        if self._dirty and self._state is not None:
            try:
                self.write_sync(self._state)
            except Exception as e:  # noqa: BLE001
                log.warning("退出前存档失败：%s", e)

    def get_state(self) -> PetState:
        return self._state

    def set_state(self, state: PetState) -> None:
        """主程序用：注入当前 state，让定时器自己掌握 dirty 标记。"""
        self._state = state

    def load(self) -> PetState:
        """读取存档，缺失/损坏时给默认值。"""
        if not self.path.is_file():
            log.info("存档不存在，使用默认状态：%s", self.path)
            self._state = PetState()
            return self._state
        try:
            raw = self.path.read_text("utf-8")
            data = json.loads(raw)
            self._state = PetState.from_dict(data)
            log.info("存档读取成功：Lv.%d exp=%d money=%.1f mode=%s",
                     self._state.level, self._state.exp,
                     self._state.money, self._state.mode.value)
            return self._state
        except Exception as e:  # noqa: BLE001
            log.warning("存档损坏：%s，使用默认", e)
            self._state = PetState()
            return self._state

    def write_sync(self, state: PetState) -> None:
        """原子写：先写临时文件 + fsync，再原子替换。

        避免中途崩溃导致空文件。同时把上一份 copy 到 .bak。
        """
        payload = state.to_dict()
        text = json.dumps(payload, ensure_ascii=False, indent=2)

        # 备份旧文件（如果存在）
        if self.path.is_file():
            try:
                shutil.copy2(self.path, self.path.with_suffix(".bak.json"))
            except Exception as e:  # noqa: BLE001
                log.warning("备份存档失败：%s", e)

        # 用临时文件 + 替换做原子写
        fd, tmp = tempfile.mkstemp(
            prefix=".save-", suffix=".json", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass
            os.replace(tmp, self.path)
            self._dirty = False
            self.saved.emit()
            log.debug("存档写入：%s", self.path)
        except Exception:
            # 清理 tmp
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def mark_dirty(self) -> None:
        """外部在改 state 后调用，标记下次 tick 自动存。"""
        self._dirty = True

    def _on_tick(self) -> None:
        if self._dirty and self._state is not None:
            try:
                self.write_sync(self._state)
            except Exception as e:  # noqa: BLE001
                log.warning("自动存档失败：%s", e)
