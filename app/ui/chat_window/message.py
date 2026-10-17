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
    tools: list[tuple[str, str, str]] = field(default_factory=list)  # (工具名, args_json, 结果)
    thoughts: list[str] = field(default_factory=list)  # 思考文本（按时间累积）
    execs: list[dict] = field(default_factory=list)  # 命令执行：{cmd, output, exit_code}
    # per-message skill：主人写 /skills/<name> 时关联到当条 user message
    skill: Optional[str] = None


__all__ = ["Message"]