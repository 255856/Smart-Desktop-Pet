"""chat_window 聊天消息 dataclass。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from app.voice.character import Emotion


@dataclass
class Message:
    role: str
    content: str
    emotion: Optional[Emotion] = None
    ts: float = field(default_factory=time.time)
    # (工具名, args_json, 结果[, 信封状态]) —— 第 4 元是 Stage 3.5 加的
    # 工具信封状态（ok/error/denied/cancel）；历史老记录只有 3 元。
    tools: list[tuple[str, ...]] = field(default_factory=list)
    thoughts: list[str] = field(default_factory=list)  # 思考文本（按时间累积）
    execs: list[dict] = field(default_factory=list)  # 命令执行：{cmd, output, exit_code}
    # per-message skill：主人写 /skills/<name> 时关联到当条 user message
    skill: Optional[str] = None
    # 说话人（桌宠人设名）—— **只给界面用**。
    # 换人设后老消息仍显示当时的名字，不会被改成新人设；
    # 空串 = 用当前人设名兜底（老记录没有这个字段）。
    speaker: str = ""


__all__ = ["Message"]