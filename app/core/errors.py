"""桌宠统一异常类型。

为什么独立成模块：
    LLMError 原本定义在 `app/brain/llm_client.py`，被 chat_window / worker /
    dashboard / tests 多处 import。但「LLM 调用错误」是一个跨层概念（UI 层
    catch、agent 层 throw），不属于某个 LLM 实现细节。
    把它收进 `app.core.errors` 后：
      1) UI 层不需要 import 整个 llm_client；
      2) 后续新增的 MCP / 工具错误可一并放这里，统一约定；
      3) `from app.brain.llm_client import LLMError` 仍然兼容（向后兼容 re-export）。
"""
from __future__ import annotations


class LLMError(RuntimeError):
    """大模型调用异常（HTTP 非 2xx / 流中断 / 超时 / 解析失败等）。"""


__all__ = ["LLMError"]