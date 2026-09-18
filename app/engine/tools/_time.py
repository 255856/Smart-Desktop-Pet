"""时间 + 状态相关工具：get_current_time / get_pet_status / parse_absolute_time。"""
from __future__ import annotations

import datetime
import json
from typing import Optional

from ._core import Tool, ToolRegistry


def fmt_time() -> str:
    now = datetime.datetime.now()
    wd = "一二三四五六日"[now.weekday()]
    return now.strftime(f"%Y-%m-%d %H:%M:%S 星期{wd}")


def parse_absolute_time(at_time: str) -> Optional[float]:
    """解析绝对时间字符串，返回 unix timestamp。

    支持格式：
        - 'HH:MM'（今天，若已过则推到明天）
        - 'YYYY-MM-DD HH:MM'（指定日期时间）
        - '明天 HH:MM' / '后天 HH:MM'
        - '早上 HH:MM' / '晚上 HH:MM' / '下午 HH:MM' 等
    """
    import re
    from datetime import timedelta

    now = datetime.datetime.now()
    at_time = at_time.strip()

    # 格式1: 'HH:MM'（今天）
    m = re.match(r'^(\d{1,2}):(\d{2})$', at_time)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        target = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target.timestamp()

    # 格式2: 'YYYY-MM-DD HH:MM'
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})$', at_time)
    if m:
        try:
            target = datetime.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                                       int(m.group(4)), int(m.group(5)))
            return target.timestamp()
        except ValueError:
            return None

    # 格式3: '明天 HH:MM' / '后天 HH:MM'
    m = re.match(r'^(明天|后天)\s*(\d{1,2}):(\d{2})$', at_time)
    if m:
        days = 1 if m.group(1) == "明天" else 2
        h, mi = int(m.group(2)), int(m.group(3))
        target = (now + timedelta(days=days)).replace(hour=h, minute=mi, second=0, microsecond=0)
        return target.timestamp()

    # 格式4: '早上 HH:MM' 等
    m = re.match(r'^(早上|上午|中午|下午|晚上|凌晨)\s*(\d{1,2}):(\d{2})$', at_time)
    if m:
        h, mi = int(m.group(2)), int(m.group(3))
        period = m.group(1)
        if period in ("晚上", "凌晨") and h < 12:
            h += 12
        target = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target.timestamp()

    return None


def register(reg: ToolRegistry, *, state) -> None:
    """注册 get_current_time + get_pet_status。"""

    def get_current_time() -> str:
        return json.dumps({"now": fmt_time()}, ensure_ascii=False)

    def get_pet_status() -> str:
        return json.dumps({
            "summary": state.stats_summary(),
            "strength": round(state.strength, 1),
            "food": round(state.strength_food, 1),
            "drink": round(state.strength_drink, 1),
            "feeling": round(state.feeling, 1),
            "health": round(state.health, 1),
            "money": round(state.money, 1),
            "exp": round(state.exp, 1),
            "level": state.level,
            "likability": round(state.likability, 1),
        }, ensure_ascii=False)

    reg.register(Tool(
        name="get_current_time",
        description="获取当前的日期、时间和星期。",
        parameters={"type": "object", "properties": {}},
        fn=get_current_time,
    ))
    reg.register(Tool(
        name="get_pet_status",
        description="查看桌宠自己的状态：体力/饱食/口渴/心情/健康/金币/等级/好感度。",
        parameters={"type": "object", "properties": {}},
        fn=get_pet_status,
    ))


__all__ = ["fmt_time", "parse_absolute_time", "register"]