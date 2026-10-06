"""chat_window 子模块：聊天消息 dataclass。

独立成模块的理由：
    - Message 是 UI 层最常被 import 的类型（test_chat_rendering、
      test_hallucination、test_memory_panel 都引用）
    - 与 ChatWindow 实例状态完全无关，单独放最自然
"""
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


__all__ = ["Message"]