"""危险工具确认弹窗 (_confirm_dangerous_tool) 单元测试。"""
import pytest

from app.ui.chat_window import _DANGEROUS_LABELS


class TestDangerousLabels:
    def test_all_keys_translated(self):
        for k in ("open_app", "open_website", "lock_screen", "kill_process",
                  "run_script", "set_wifi", "set_bluetooth", "shutdown_computer"):
            assert k in _DANGEROUS_LABELS
            assert _DANGEROUS_LABELS[k]    # 非空

    def test_label_is_chinese(self):
        # 应是中文标签，不是 raw name
        assert _DANGEROUS_LABELS["lock_screen"] == "锁屏"
        assert _DANGEROUS_LABELS["kill_process"] == "杀进程"
        assert _DANGEROUS_LABELS["set_wifi"] == "开关 Wi-Fi"


class TestConfirmDangerousTool:
    """验证 _confirm_dangerous_tool 的集成点（不真弹窗，避免 offscreen 卡死）。"""

    def test_dangerous_tool_set_passed_to_agent(self):
        """AgentLoop 的 DANGEROUS_TOOLS 集合包含 _DANGEROUS_LABELS 的所有 key。"""
        from app.brain.agent import DANGEROUS_TOOLS
        for k in _DANGEROUS_LABELS:
            assert k in DANGEROUS_TOOLS, (
                f"危险工具 {k!r} 应在 DANGEROUS_TOOLS 里")


class TestDangerousToolsIntegration:
    """确认 AgentLoop 真的把 confirm_tool 用上了。"""

    def test_agent_loop_signature_accepts_confirm_tool(self):
        from app.brain.agent import AgentLoop
        import inspect
        sig = inspect.signature(AgentLoop.__init__)
        assert "confirm_tool" in sig.parameters

    def test_agent_loop_skips_when_no_confirm(self):
        """无 confirm_tool 时危险工具直接执行（不卡死）。"""
        from app.brain.agent import AgentLoop
        from app.brain.llm_client import LLMClient
        from app.core.config import LLMConfig
        from app.engine.tools import ToolRegistry, Tool

        reg = ToolRegistry()
        reg.register(Tool(name="lock_screen",
                          description="dummy", parameters={},
                          fn=lambda: "locked"))

        client = LLMClient(LLMConfig(), "test")
        # 不传 confirm_tool
        agent = AgentLoop(client=client, registry=reg, confirm_tool=None)
        assert agent.confirm_tool is None

    def test_agent_confirm_calls_callback_for_safe_tool(self):
        """非危险工具：confirm_tool 不应被调，直接放行。"""
        import asyncio
        from app.brain.agent import AgentLoop
        from app.brain.llm_client import LLMClient
        from app.core.config import LLMConfig
        from app.engine.tools import ToolRegistry, Tool

        reg = ToolRegistry()
        reg.register(Tool(name="get_current_time",
                          description="safe", parameters={},
                          fn=lambda: "12:00"))

        called = []
        def fake_confirm(name, args):
            called.append(name)
            return True

        client = LLMClient(LLMConfig(), "test")
        agent = AgentLoop(client=client, registry=reg, confirm_tool=fake_confirm)
        # 非危险工具 confirm_tool 不应被调
        result = asyncio.run(agent._confirm_or_skip("get_current_time", "{}"))
        assert result is None    # None = 继续执行
        assert called == []

    def test_agent_confirm_blocks_when_user_says_no(self):
        """危险工具 + 拒绝 → 返回错误字符串（不执行）。"""
        import asyncio
        from app.brain.agent import AgentLoop
        from app.brain.llm_client import LLMClient
        from app.core.config import LLMConfig
        from app.engine.tools import ToolRegistry, Tool

        reg = ToolRegistry()
        reg.register(Tool(name="lock_screen",
                          description="danger", parameters={},
                          fn=lambda: "should not run"))

        def deny(name, args):
            return False

        client = LLMClient(LLMConfig(), "test")
        agent = AgentLoop(client=client, registry=reg, confirm_tool=deny)
        result = asyncio.run(agent._confirm_or_skip("lock_screen", "{}"))
        assert result == "用户取消了此操作。"