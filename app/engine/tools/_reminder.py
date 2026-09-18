"""提醒工具：add_reminder / list_reminders / delete_reminder。"""
from __future__ import annotations

import datetime
import time

from ._core import Tool, ToolRegistry
from ._time import parse_absolute_time


def register(reg: ToolRegistry, *, reminders) -> None:
    """注册 reminder 三件套。"""

    def add_reminder(text: str, delay_minutes: float = None, at_time: str = None) -> str:
        """设置提醒。

        二选一：
        - delay_minutes: 多少分钟后提醒
        - at_time: 绝对时间，格式如 '08:00'（今天）、'2026-09-12 08:00'（指定日期）、'明天 08:00'
        """
        if at_time:
            fire_ts = parse_absolute_time(at_time)
            if fire_ts is None:
                return f"错误：无法解析时间「{at_time}」，支持格式：'08:00'、'2026-09-12 08:00'、'明天 08:00'"
            if fire_ts <= time.time():
                return f"错误：时间「{at_time}」已经过去"
            item = reminders.add(int(fire_ts - time.time()), text.strip())
            return f"已设置提醒「{item.text}」，将在 {at_time} 触发"
        elif delay_minutes and delay_minutes > 0:
            item = reminders.add(int(delay_minutes * 60), text.strip())
            return f"已设置提醒「{item.text}」，{delay_minutes:g} 分钟后触发（id={item.id}）"
        else:
            return "错误：请指定 delay_minutes 或 at_time"

    def list_reminders() -> str:
        items = [i for i in reminders.list() if not i.done]
        if not items:
            return "当前没有待触发的提醒。"
        now = datetime.datetime.now()
        rows = []
        for i in items:
            left_min = max(0, (i.fire_at - now.timestamp()) / 60)
            rows.append(f"id={i.id} 「{i.text}」 还有 {left_min:.0f} 分钟")
        return "待触发提醒：\n" + "\n".join(rows)

    def delete_reminder(reminder_id: str) -> str:
        return "已删除。" if reminders.remove(reminder_id) else f"找不到 id={reminder_id} 的提醒。"

    reg.register(Tool(
        name="add_reminder",
        description="设置一个定时提醒。可以用 delay_minutes（多少分钟后）或 at_time（绝对时间）二选一。",
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "提醒内容"},
                "delay_minutes": {"type": "number", "description": "多少分钟后提醒"},
                "at_time": {"type": "string", "description": "绝对时间，支持 '08:00'、'2026-09-12 08:00'、'明天 08:00' 等格式"},
            },
            "required": ["text"],
        },
        fn=add_reminder,
    ))
    reg.register(Tool(
        name="list_reminders",
        description="列出所有待触发的提醒。",
        parameters={"type": "object", "properties": {}},
        fn=list_reminders,
    ))
    reg.register(Tool(
        name="delete_reminder",
        description="删除一个提醒。id 从 list_reminders 获得。",
        parameters={
            "type": "object",
            "properties": {"reminder_id": {"type": "string"}},
            "required": ["reminder_id"],
        },
        fn=delete_reminder,
    ))


__all__ = ["register"]