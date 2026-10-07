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


# 主人在聊天窗里看到的网络异常兜底文案。
#
# 之前的实现直接把 httpx 异常 text（`RemoteProtocolError('Server disconnected...')`）
# 渲染给主人，stack trace 露出来 = 主人看到内部细节 = 体验崩溃 + 完全没意义。
#
# 现在 catch 端把网络异常统一改写成这一句，LLMError 的 message 里带它，
# 上层 chat_window / dashboard 直接显示。
USER_FRIENDLY_NETWORK_ERROR = "主人连接超时啦，稍等再试试~"


def friendly_network_error(exc: BaseException | None = None) -> LLMError:
    """网络异常 → LLMError，message 是给主人看的（不带 stack / 库名）。

    Args:
        exc: 原始异常，仅用于 trace/log（不暴露给 UI）。

    Returns:
        LLMError，message = USER_FRIENDLY_NETWORK_ERROR。
    """
    return LLMError(USER_FRIENDLY_NETWORK_ERROR)


__all__ = ["LLMError", "USER_FRIENDLY_NETWORK_ERROR", "friendly_network_error"]