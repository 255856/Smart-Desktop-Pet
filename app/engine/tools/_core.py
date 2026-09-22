"""Tool / ToolRegistry 核心类型。"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger(__name__)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict            # JSON Schema
    fn: Callable[..., str]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return list(self._tools)

    def to_openai(self) -> list[dict]:
        """转成 OpenAI tools 字段格式。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in self._tools.values()
        ]

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def execute(self, name: str, arguments: str | dict) -> str:
        """执行一个工具调用，永远返回字符串（异常也转成错误文本给模型）。"""
        tool = self._tools.get(name)
        if tool is None:
            return f"错误：未知工具 {name}"
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments or {})
        except json.JSONDecodeError as e:
            return f"错误：参数不是合法 JSON（{e}）"
        try:
            return str(tool.fn(**args))
        except TypeError as e:
            return f"错误：参数不匹配（{e}）"
        except Exception as e:  # noqa: BLE001
            log.exception("工具 %s 执行失败", name)
            return f"错误：{e}"


__all__ = ["Tool", "ToolRegistry"]