"""存档系统：把 PetState 持久化到磁盘 + 自动定时保存。

路径 + 写盘时机走 companion_config.persistence yaml：
  - file: 默认 data/pet_state.json
  - write_on_update: 立即写（每次 state 改动触发 mark_dirty）
  - write_min_interval_seconds: 防抖间隔
  - autosave_seconds: QTimer 周期兜底
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional

from app.core.qt_compat import QObject, QTimer, Signal
from app.engine.state import PetState

log = logging.getLogger(__name__)

# 连续快速写的判定窗口（秒）：窗口内的写盘跳过 fsync + .bak 备份
_RAPID_WRITE_WINDOW_S = 1.0


def default_save_path() -> Path:
    """`~/.desktop-pet/save.json`，跨平台。"""
    return Path.home() / ".desktop-pet" / "save.json"


def _resolve_save_path(yaml_path: str) -> Path:
    """解析 yaml persistence.file 路径。

    优先相对 cwd（项目根），缺省 fallback 到 ~/.desktop-pet/save.json。
    """
    p = Path(yaml_path)
    if p.is_absolute():
        return p
    cwd_candidate = Path.cwd() / p
    if cwd_candidate.parent.is_dir():
        return cwd_candidate
    return default_save_path()


class SaveStore(QObject):
    """存档管理器。

    路径/写盘时机走 yaml companion_config.persistence：
      - persistence.file（默认 data/pet_state.json）
      - persistence.write_on_update（默认 True，改 state 立即写）
      - persistence.write_min_interval_seconds（默认 5，防抖）
      - autosave_seconds（默认 60，QTimer 兜底）
    """

    saved = Signal()    # 每次落盘发一次

    def __init__(self, path: Optional[Path] = None,
                 autosave_seconds: int = 60,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        if path is None:
            # 默认走 yaml persistence.file
            try:
                from app.core.companion_config import companion_config
                path = _resolve_save_path(companion_config.persistence.file)
            except Exception:  # noqa: BLE001
                path = default_save_path()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.autosave_seconds = int(autosave_seconds)

        # write_on_update + 防抖
        try:
            from app.core.companion_config import companion_config
            self.write_on_update = bool(companion_config.persistence.write_on_update)
            self.write_min_interval = int(companion_config.persistence.write_min_interval_seconds)
        except Exception:  # noqa: BLE001
            self.write_on_update = True
            self.write_min_interval = 5

        self._state: PetState = PetState()
        self._timer = QTimer(self)
        self._timer.setInterval(self.autosave_seconds * 1000)
        self._timer.timeout.connect(self._on_tick)
        self._dirty = False
        self._last_write_ts: float = 0.0    # 防抖用

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
            log.info("存档读取成功：Lv.%d exp=%d money=%.1f mode=%s path=%s",
                     self._state.level, self._state.exp,
                     self._state.money, self._state.mode.value, self.path)
            return self._state
        except Exception as e:  # noqa: BLE001
            log.warning("存档损坏：%s，使用默认", e)
            self._state = PetState()
            return self._state

    def write_sync(self, state: PetState) -> None:
        """原子写：先写临时文件 + fsync，再原子替换。

        避免中途崩溃导致空文件。同时把上一份 copy 到 .bak。

        连续快速写（开发者模式拖滑块时每个像素都写一次）会跳过 fsync 和
        .bak 备份——fsync 是这里最贵的一步，而拖动过程中的中间值丢了也
        无所谓（内容本身仍然是原子替换的，不会写坏文件）。
        """
        payload = state.to_dict()
        text = json.dumps(payload, ensure_ascii=False, indent=2)

        # 上一次写盘不到 1 秒 → 认为是「连续快速写」，走轻量路径
        rapid = bool(
            self._last_write_ts
            and (time.time() - self._last_write_ts) < _RAPID_WRITE_WINDOW_S
        )

        # 备份旧文件（如果存在）—— 连续快速写时跳过：备份的是上一份，
        # 连续写时它每次都被覆盖，留着「本次连续写之前的那份」更有用。
        if not rapid and self.path.is_file():
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
                if not rapid:
                    try:
                        os.fsync(f.fileno())
                    except (OSError, ValueError, KeyError, TypeError) as e:
                        log.debug("ignored: %s", e)
            os.replace(tmp, self.path)
            self._dirty = False
            self._last_write_ts = time.time()    # 防抖
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
        """外部在改 state 后调用：标记 dirty，如果 write_on_update 立即写。

        防抖：距离上次写 < write_min_interval 就不写，累计多次再写一次。
        """
        self._dirty = True
        if not self.write_on_update:
            return
        # 防抖：距离上次写 < min_interval → 等 timer 兜底
        now = time.time()
        if self._last_write_ts and (now - self._last_write_ts) < self.write_min_interval:
            log.debug("mark_dirty: 防抖中, 距离上次写 %.1fs (< %ds), 跳过",
                      now - self._last_write_ts, self.write_min_interval)
            return
        if self._state is not None:
            try:
                self.write_sync(self._state)
            except Exception as e:  # noqa: BLE001
                log.warning("mark_dirty 立即写盘失败: %s", e)

    def _on_tick(self) -> None:
        if self._dirty and self._state is not None:
            try:
                self.write_sync(self._state)
            except Exception as e:  # noqa: BLE001
                log.warning("自动存档失败：%s", e)
