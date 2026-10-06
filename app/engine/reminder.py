"""提醒系统：把 LLM 输出的「XX 分钟后提醒我做 YY」类指令落盘定时触发。"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass, field
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
# 否则 "一个半小时" 会被当成 "1 个半小时" = 30 min）。
_PATTERNS = [
    (re.compile(r"(\d+)\s*秒后"), 1),
    (re.compile(r"(\d+)\s*分钟后"), 60),
    (re.compile(r"(\d+)\s*小时后"), 3600),
    # 「一个半小时后」= 1.5 小时 = 90 分钟（口语里 "一个半" 就是 "1.5"）
    (re.compile(r"一个半小时后"), 5400),
    # 「X 个半小时后」= X × 30 分钟（如 "5个半小时后" = 2.5 小时 = 9000 秒）
    (re.compile(r"(\d+)\s*个半小时后"), 1800),
    # 中文数字 X 个半小时（如 "五个半小时后"）→ X × 30 分钟
    (re.compile(r"([一二三四五六七八九十]+)\s*个半小时后"), 1800),
]


def _cn_digit_to_int(s: str) -> int:
    """把一位中文数字（一二三四五六七八九十）转成阿拉伯数字；超出一位返回 None。"""
    table = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
    if len(s) == 1:
        return table.get(s)
    # 多位（十一、二十三 等）暂不展开——用户场景下常见的是单个 X（5个半小时）
    return None


def parse_quick_reminder(text: str) -> Optional[tuple[int, str]]:
    """识别「30 分钟后提醒我喝水」这类便捷指令，返回 (delay_seconds, content)。"""
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


class ReminderStore(QObject):
    """按需调度的提醒管理器。

    不再固定 1s 轮询，而是找到最近的未触发提醒，设置单次定时器到该时间点。
    触发后、添加后、移除后都会重新调度。没有提醒时不启动定时器。
    """

    reminder_triggered = Signal(str)   # 提醒文本
    list_changed = Signal()
    # 请求「重新调度定时器」。工具（add_reminder）在 asyncio.to_thread 的
    # 工作线程里跑，QTimer 只允许在其属主线程（主线程）start()，
    # 跨线程直接调用是 Qt 明令禁止的 no-op：
    #   QObject::startTimer: Timers cannot be started from another thread
    # 那个 no-op 不会报错、isActive() 还会返回 True，于是提醒被存进磁盘、
    # 列表里看得见、工具也回报「已设置提醒」，但**永远不会响**（实测）。
    # 用信号把调度请求排队回主线程，跨线程调用就安全了。
    reschedule = Signal()

    def __init__(self, data_file: str | Path, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.data_file = Path(data_file)
        self._items: list[ReminderItem] = []
        self._lock = threading.Lock()
        # 最近一次落盘是否成功。add_reminder 工具读它来决定能不能说「已设置」。
        self.last_save_ok = True
        self._load()
        # 首次启动建空文件（让用户能看到数据位置；不破坏现有行为）
        if not self.data_file.exists():
            try:
                self.data_file.parent.mkdir(parents=True, exist_ok=True)
                self.data_file.write_text("[]", encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass
        # 单次定时器（按需调度，不再固定 1s 轮询）
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._on_tick)
        # 跨线程调度请求 → 排队回主线程执行（见 reschedule 信号注释）
        self.reschedule.connect(self._schedule_next_now)
        # 初始调度
        self._schedule_next()

    def _load(self) -> None:
        if not self.data_file.is_file():
            return
        try:
            raw = json.loads(self.data_file.read_text("utf-8"))
            self._items = [ReminderItem(**r) for r in raw]
        except Exception as e:  # noqa: BLE001
            log.warning("加载 reminders 失败：%s", e)

    def _save(self) -> bool:
        """落盘。返回是否成功（原来只 log.warning 吞掉，工具层无从得知）。"""
        try:
            self.data_file.parent.mkdir(parents=True, exist_ok=True)
            self.data_file.write_text(
                json.dumps([asdict(i) for i in self._items], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("保存 reminders 失败：%s", e)
            return False

    def add(self, delay_seconds: int, text: str) -> ReminderItem:
        rid = f"r{int(time.time() * 1000)}"
        item = ReminderItem(id=rid, text=text, fire_at=time.time() + delay_seconds)
        with self._lock:
            self._items.append(item)
            # 工具层读它来决定能不能对主人说「已设置提醒」——
            # 写盘失败时提醒只活在内存里，进程一退就没了。
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
        """请求重新调度。**可从任意线程调用**。

        内部 emit 一个信号，由 Qt 排队到定时器属主线程真正执行。
        直接调 QTimer.start() 只在主线程有效——见 reschedule 信号注释。
        """
        self.reschedule.emit()

    def _schedule_next_now(self) -> None:
        """真正设置定时器。**只允许在定时器属主线程（主线程）执行。**"""
        self._timer.stop()
        now = time.time()
        next_fire = None
        with self._lock:
            for it in self._items:
                if not it.done and it.fire_at > now:
                    if next_fire is None or it.fire_at < next_fire:
                        next_fire = it.fire_at

        if next_fire is None:
            log.debug("ReminderStore: 没有待触发提醒，定时器停止")
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
            log.info("提醒触发：%s", it.text)
        # 重新调度下一次
        self._schedule_next()
