"""回归测试：网络异常给主人看的友好文案。

2026-10-06 现场
- 用户问「帮我打开星穹铁道」
- LLM 中途断连 → chat_window 显示原始栈
  「RemoteProtocolError('Server disconnected without sending a response.')」
- 主人看到内部异常 = 体验崩溃

修复：
    - worker 的 except 分支识别 httpx 网络异常族 → 转 USER_FRIENDLY_NETWORK_ERROR
    - 其他异常保留 repr（LLMError 的 message 已友好；`{e!r}` 是最后兜底）
"""
from __future__ import annotations

import pytest

from app.ui.chat_window.worker import _failure_message, USER_FRIENDLY_NETWORK_ERROR


class TestNetworkErrorFriendlyText:
    """httpx 网络异常族 → USER_FRIENDLY_NETWORK_ERROR（主人看到的文案）。"""

    @pytest.mark.parametrize("exc_cls,args", [
        ("RemoteProtocolError", ("Server disconnected without sending a response.",)),
        ("ConnectError", ("Connection refused",)),
        ("ConnectTimeout", ("Connect timeout",)),
        ("ReadTimeout", ("Read timed out",)),
        ("WriteTimeout", ("Write timed out",)),
        ("PoolTimeout", ("Pool timeout",)),
    ])
    def test_httpx_network_exceptions_become_friendly(self, exc_cls, args):
        """所有 httpx 网络异常族 → 友好文案（不暴露库名 / 栈）。"""
        import httpx
        cls = getattr(httpx, exc_cls)
        exc = cls(*args)
        msg = _failure_message(exc)
        assert msg == USER_FRIENDLY_NETWORK_ERROR
        # 不应包含库名 / 异常类名
        assert "httpx" not in msg
        assert "Error" not in msg
        assert "RemoteProtocol" not in msg

    def test_plain_connection_error_also_friendly(self):
        """Python 内置 ConnectionError 也按网络异常处理（httpx 内部会转一层）。"""
        exc = ConnectionError("Connection refused")
        assert _failure_message(exc) == USER_FRIENDLY_NETWORK_ERROR


class TestOtherExceptionsKeepRepr:
    """非网络异常 → repr（保留信息给 Trace；UI 仍按本来的可读性显示）。"""

    def test_value_error_keeps_repr(self):
        exc = ValueError("参数不对")
        msg = _failure_message(exc)
        assert "参数不对" in msg
        assert msg == repr(exc)

    def test_runtime_error_keeps_repr(self):
        exc = RuntimeError("oops")
        assert _failure_message(exc) == repr(exc)

    def test_llm_error_keeps_message(self):
        """LLMError 自带友好 message，保留而不是替换。"""
        from app.core.errors import LLMError
        exc = LLMError("HTTP 401: unauthorized")
        # repr 包含类名（不是 message），所以这里 _failure_message 走 repr 分支
        # 对 LLMError 也按 repr 处理 —— UI 仍能拿到原文
        msg = _failure_message(exc)
        assert "401" in msg or "unauthorized" in msg


class TestUserFriendlyTextStable:
    """文案锁死（防止后续误改）。"""

    def test_message_is_chinese_natural(self):
        assert "主人" in USER_FRIENDLY_NETWORK_ERROR
        assert "稍等" in USER_FRIENDLY_NETWORK_ERROR
        # 不应有英文 / 技术词
        for bad in ["Error", "exception", "timeout", "disconnect"]:
            assert bad.lower() not in USER_FRIENDLY_NETWORK_ERROR.lower(), (
                f"USER_FRIENDLY_NETWORK_ERROR 含技术词 {bad!r}: "
                f"{USER_FRIENDLY_NETWORK_ERROR!r}"
            )