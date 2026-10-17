"""提醒系统：把 LLM 输出的「XX 分钟后提醒我做 YY」类指令落盘定时触发。

持久化从 JSON 迁到 SQLite（启动加速 + 写盘原子性 + 为后续查询/同步铺路）。
迁移策略与 chat_store 一致 —— 首次 load 时 JSON → DB，然后删 JSON。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.core.qt_compat import QObject, QTimer, Signal

log = logging.getLogger(__name__)


@dataclass
class ReminderItem:
    id: str
    text: str
    fire_at: float          # unix timestamp
    created_at: float = field(default_factory=time.time)
    done: bool = False


# 简单从中文里解析「N 秒/分钟/小时后做 X」
# 顺序敏感：更具体的模式放前面（"一个半小时后" 必须在 "X个半小时后" 之前，
# 否则 "一个半小时" 会被当成 "1 个半小时" = 30 min）
_PATTERNS = [
    (re.compile(r"(\d+)\s*秒后"), 1),
    (re.compile(r"(\d+)\s*分钟后"), 60),
    (re.compile(r"(\d+)\s*小时后"), 3600),
    (re.compile(r"一个半小时后"), 5400),
    (re.compile(r"(\d+)\s*个半小时后"), 1800),
    (re.compile(r"([一二三四五六七八九十]+)\s*个半小时后"), 1800),
]


def _cn_digit_to_int(s: str) -> int:
    """把一位中文数字（一二三四五六七八九十）转成阿拉伯数字；超出一位返回 None。"""
    table = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    if len(s) == 1:
        return table.get(s)
    # 多位（十一、二十三等）暂不展开
    return None


def parse_quick_reminder(text: str) -> Optional[tuple[int, str]]:
    """识别「30 分钟后提醒我喝水」这类便捷指令,返回 (delay_seconds, content)。"""
    if "提醒" not in text and "提醒我" not in text:
        return None
    delay = None
    for pat, factor in _PATTERNS:
        m = pat.search(text)
        if m:
            if m.lastindex:
                g = m.group(1)
                # 中文数字模式：用 _cn_digit_to_int 解析；解析失败回退 factor
                if g and any('\u4e00' <= ch <= '\u9fff' for ch in g):
                    n = _cn_digit_to_int(g)
                    delay = (n * factor) if n is not None else factor
                else:
                    delay = int(g) * factor
            else:  # 无捕获组（中文数字），直接用 factor
                delay = factor
            break
    if delay is None:
        return None
    # 抽出提醒内容（保留时间模式，方便后续清洗时连同时间一起剥掉）
    content = text
    for kw in ["帮我", "请", "麻烦", "记得"]:
        content = content.replace(kw, "")
    content = re.sub(r"^(设置)?(一个)?提醒(我)?[:：]?\s*", "", content)
    # 时间模式：阿拉伯数字 + 单位 / 中文数字 + 单位 / "一个半小时"
    content = re.sub(r"(\d+|[一二三四五六七八九十]+|一个半)\s*(秒|分钟|小时|个半小时)后", "", content)
    content = content.replace("提醒我", "").replace("提醒", "")
    content = content.strip(" ，,。.!?！？")
    if not content:
        content = "该做事啦"
    return delay, content


_SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id          TEXT PRIMARY KEY,
    text        TEXT NOT NULL,
    fire_at     REAL NOT NULL,
    created_at  REAL NOT NULL,
    done        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_reminders_fire_at ON reminders(fire_at);
"""


class ReminderStore(QObject):
    """按需调度的提醒管理器。

    不再固定 1s 轮询,而是找到最近的未触发提醒,设置单次定时器到该时间点。
    触发后、添加后、移除后都会重新调度。没有提醒时不启动定时器。
    """

    reminder_triggered = Signal(str)   # 提醒文本
    list_changed = Signal()
    # 请求「重新调度定时器」。工具（add_reminder）在 asyncio.to_thread 工作线程跑，
    # QTimer 只允许在属主线程 start()，跨线程直接调是 Qt 明令禁止的 no-op
    # （QObject::startTimer: Timers cannot be started from another thread）——
    # 那个 no-op 不报错、isActive() 还返回 True，于是提醒被存进磁盘、列表里看得见、
    # 工具回报「已设置」但永远不响。用信号把调度请求排队回主线程就安全了。
    reschedule = Signal()

    def __init__(self, data_file: str | Path, parent: Optional[QObject] = None):
        super().__init__(parent)
        # 兼容旧 .json 入参
        if str(data_file).lower().endswith(".json"):
            self.legacy_json = Path(data_file)
            self.data_file = self.legacy_json.with_suffix(".db")
            log.info("ReminderStore: 检测到 .json, 自动迁向 %s", self.data_file)
        else:
            self.data_file = Path(data_file)
            self.legacy_json = self.data_file.with_suffix(".json")
        self._items: list[ReminderItem] = []
        self._lock = threading.Lock()
        # 最近一次落盘是否成功（add_reminder 工具读它决定能不能说「已设置」）
        self.last_save_ok = True
        self._load()
        # 首次启动建空 DB（让用户能看到数据位置）
        if not self.data_file.exists():
            try:
                self.data_file.parent.mkdir(parents=True, exist_ok=True)
                self._connect()    # 触发 CREATE TABLE
            except Exception:  # noqa: BLE001
                pass
        # 单次定时器（按需调度）
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_tick)
        # 跨线程调度请求 → 排队回主线程执行（见 reschedule 信号注释）
        self.reschedule.connect(self._schedule_next_now)
        # 初始调度
        self._schedule_next()

    def _connect(self) -> sqlite3.Connection:
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.data_file))
        conn.executescript(_SCHEMA)
        conn.commit()
        return conn

    def _migrate_from_json(self) -> int:
        if not self.legacy_json.is_file():
            return 0
        try:
            raw = json.loads(self.legacy_json.read_text("utf-8"))
            loaded = [ReminderItem(**r) for r in raw]
        except Exception as e:  # noqa: BLE001
            log.warning("ReminderStore: legacy JSON 读失败(%s),跳过迁移", e)
            return 0
        if not loaded:
            self.legacy_json.unlink(missing_ok=True)
            return 0
        try:
            conn = self._connect()
            try:
                rows = [(it.id, it.text, it.fire_at, it.created_at, 1 if it.done else 0)
                        for it in loaded]
                conn.executemany(
                    "INSERT OR REPLACE INTO reminders(id, text, fire_at, created_at, done) "
                    "VALUES (?, ?, ?, ?, ?)", rows,
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            log.warning("ReminderStore: 写 DB 失败(%s),保留 JSON", e)
            return 0
        try:
            self.legacy_json.unlink(missing_ok=True)
            log.info("ReminderStore: 已迁移 %d 条 reminder, 删除 %s",
                     len(loaded), self.legacy_json)
        except OSError:
            pass
        return len(loaded)

    def _load(self) -> None:
        # 0) JSON 迁 DB
        if not self.data_file.is_file():
            self._migrate_from_json()
            if not self.data_file.is_file():
                return
        try:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT id, text, fire_at, created_at, done FROM reminders"
                )
                loaded = [
                    ReminderItem(
                        id=row[0], text=row[1], fire_at=row[2],
                        created_at=row[3], done=bool(row[4]),
                    )
                    for row in cur.fetchall()
                ]
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            log.warning("加载 reminders 失败:%s", e)
            return
        # 启动清理：已 done 的 + fire_at 早已过期（>1 天前）丢弃。
# 否则桌宠关几天再开，磁盘里堆的过期提醒一启动全 fire，主人会被一串闹钟吵醒。
# 留 1 天 grace period：刚过期的还可能有用（比如关掉几小时又开）。
        cutoff = time.time() - 86400
        before = len(loaded)
        loaded = [it for it in loaded if not it.done and it.fire_at > cutoff]
        dropped = before - len(loaded)
        if dropped:
            log.info("ReminderStore: 启动清理 %d 个过期 reminders", dropped)
            # 写回 DB
            try:
                conn = self._connect()
                try:
                    conn.execute("DELETE FROM reminders WHERE done = 1 OR fire_at <= ?",
                                 (cutoff,))
                    conn.commit()
                finally:
                    conn.close()
            except Exception as e:  # noqa: BLE001
                log.warning("ReminderStore: 清理过期 reminder 写盘失败:%s", e)
        self._items = loaded
        # 重复提醒太多时给主人提个醒：>5 条历史同时 fire 就像闹钟炸弹
        if len(self._items) >= 5:
            log.warning("ReminderStore: 当前有 %d 个未触发的提醒,"
                        "右键菜单 → 提醒列表 可以批量清理", len(self._items))

    def _save(self) -> bool:
        """落盘。返回是否成功（工具层据此决定能不能说「已设置」）。"""
        try:
            conn = self._connect()
            try:
                # 全量替换：简单可靠；reminder 数量小（<100），性能不是瓶颈
                conn.execute("DELETE FROM reminders")
                conn.executemany(
                    "INSERT INTO reminders(id, text, fire_at, created_at, done) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [(it.id, it.text, it.fire_at, it.created_at,
                      1 if it.done else 0) for it in self._items],
                )
                conn.commit()
                return True
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            log.warning("保存 reminders 失败:%s", e)
            return False

    def add(self, delay_seconds: int, text: str) -> ReminderItem:
        rid = f"r{int(time.time() * 1000)}"
        item = ReminderItem(id=rid, text=text, fire_at=time.time() + delay_seconds)
        with self._lock:
            self._items.append(item)
            # 工具层读它决定能不能对主人说「已设置提醒」——写盘失败时
            # 提醒只活在内存里，进程一退就没了。
            self.last_save_ok = self._save()
        self.list_changed.emit()
        self._schedule_next()
        return item

    def remove(self, rid: str) -> bool:
        with self._lock:
            before = len(self._items)
            self._items = [i for i in self._items if i.id != rid]
            removed = len(self._items) != before
            if removed:
                self._save()
        if removed:
            self.list_changed.emit()
            self._schedule_next()
        return removed

    def list(self) -> list[ReminderItem]:
        with self._lock:
            return list(self._items)

    def parse_and_add(self, user_text: str) -> Optional[ReminderItem]:
        parsed = parse_quick_reminder(user_text)
        if not parsed:
            return None
        delay, content = parsed
        return self.add(delay, content)

    def _schedule_next(self) -> None:
        """请求重新调度。可从任意线程调用。

        内部 emit 一个信号，由 Qt 排队到定时器属主线程真正执行。
        直接调 QTimer.start() 只在主线程有效——见 reschedule 信号注释。
        """
        self.reschedule.emit()

    def _schedule_next_now(self) -> None:
        """真正设置定时器。只允许在定时器属主线程（主线程）执行。"""
        self._timer.stop()
        now = time.time()
        next_fire = None
        with self._lock:
            for it in self._items:
                if not it.done and it.fire_at > now:
                    if next_fire is None or it.fire_at < next_fire:
                        next_fire = it.fire_at

        if next_fire is None:
            log.debug("ReminderStore: 没有待触发提醒,定时器停止")
            return

        delay_ms = max(0, int((next_fire - now) * 1000))
        log.info("ReminderStore: 下次提醒在 %.1f 秒后", delay_ms / 1000)
        self._timer.start(delay_ms)

    def _on_tick(self) -> None:
        """定时器触发：检查哪些提醒到期，触发后重新调度。"""
        now = time.time()
        fired: list[ReminderItem] = []
        with self._lock:
            for it in self._items:
                if not it.done and it.fire_at <= now:
                    it.done = True
                    fired.append(it)
            if fired:
                self._save()
        for it in fired:
            self.reminder_triggered.emit(it.text)
            log.info("提醒触发:%s", it.text)
        # 重新调度下一次
        self._schedule_next()


__all__ = ["ReminderStore", "ReminderItem", "parse_quick_reminder"]