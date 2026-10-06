"""提醒工具：add_reminder / list_reminders / delete_reminder。"""
from __future__ import annotations

import datetime
import time

from ._core import Tool, ToolRegistry
from ._time import parse_absolute_time


def _arm_backup(item, when_desc: str) -> str:
    """给提醒注册一个 Windows 计划任务兜底，并把结果**如实**告诉模型。

    桌宠自己的定时器在进程里：崩了、关��、或那条「QTimer 在工作线程 start
    无效」的老 bug 复发，闹钟就哑了。注册一次性计划任务后由 Windows 内核负责
    到点拉起提示，和桌宠进程无关。

    注册不上（剩不到 2 分钟 / 非 Windows / schtasks 失败）时返回空串——
    工具文案不会谎称装了兜底。
    """
    try:
        from app.core import win_alarm
        if not win_alarm.is_available():
            return ""
        name = win_alarm.arm(item.fire_at, item.text, key=item.id)
        if name:
            return f"。已加系统级兜底：即使桌宠关掉，到点 Windows 也会响"
        # 剩太近等注册不上——提醒本身仍然有效，不算失败，只是没有兜底
        return f"。注：{when_desc} 离现在太近，没能加系统级兜底（需要至少 2 分钟）"
    except Exception:  # noqa: BLE001
        return ""


def register(reg: ToolRegistry, *, reminders) -> None:
    """注册 reminder 三件套。"""

    def add_reminder(text: str, delay_minutes: float = None, at_time: str = None) -> str:
        """设置提醒。

        二选一：
        - delay_minutes: 多少分钟后提醒
        - at_time: 绝对时间，格式如 '08:00'（今天）、'2026-09-12 08:00'（指定日期）、'明天 08:00'
        """
        if not (text or "").strip():
            return "错误：提醒内容不能为空"
        # 两个参数同时给了要说清用哪个，别让 at_time 静默胜出
        if at_time and delay_minutes:
            return ("错误：at_time 和 delay_minutes 只能二选一，"
                    "两个都给了。请只保留其中一个再调用。")
        if at_time:
            fire_ts = parse_absolute_time(at_time)
            if fire_ts is None:
                return f"错误：无法解析时间「{at_time}」，支持格式：'08:00'、'2026-09-12 08:00'、'明天 08:00'"
            if fire_ts <= time.time():
                return f"错误：时间「{at_time}」已经过去"
            item = reminders.add(int(fire_ts - time.time()), text.strip())
            ok = getattr(reminders, "last_save_ok", True)
            if not ok:
                return (f"错误：提醒已排入内存但**没能保存到磁盘**"
                        f"（{at_time} 触发），桌宠重启后会丢失。")
            return f"已设置提醒「{item.text}」，将在 {at_time} 触发（id={item.id}）" + \
                _arm_backup(item, f"将在 {at_time} 触发")
        # 原来 `elif delay_minutes and delay_minutes > 0` 让 0 / 负数走到
        # 「请指定 delay_minutes」——参数其实给了，报错却说没给，模型无法自我修正。
        if delay_minutes is None or delay_minutes == "":
            return "错误：请指定 delay_minutes（多少分钟后）或 at_time（绝对时间）"
        try:
            mins = float(delay_minutes)
        except (TypeError, ValueError):
            return f"错误：delay_minutes 必须是数字，收到 {delay_minutes!r}"
        if mins <= 0:
            return f"错误：delay_minutes 必须是正数，收到 {mins:g}"
        item = reminders.add(int(mins * 60), text.strip())
        if not getattr(reminders, "last_save_ok", True):
            return (f"错误：提醒已排入内存但**没能保存到磁盘**"
                    f"（{mins:g} 分钟后触发），桌宠重启后会丢失。")
        return (f"已设置提醒「{item.text}」，{mins:g} 分钟后触发"
                f"（id={item.id}）"
                + _arm_backup(item, f"{mins:g} 分钟后触发"))

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
        if not reminders.remove(reminder_id):
            return f"错误：找不到 id={reminder_id} 的提醒，什么都没删掉。"
        # 同步撤掉系统级兜底，否则桌宠删了提醒、Windows 那边还会照响
        try:
            from app.core import win_alarm
            win_alarm.disarm_key(reminder_id)
        except Exception:  # noqa: BLE001
            pass
        return "已删除。"

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