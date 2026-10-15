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
        """真正需要确认的工具必须都在 DANGEROUS_TOOLS 里。"""
        from app.brain.agent import DANGEROUS_TOOLS
        for k in _DANGEROUS_LABELS:
            if k in ("open_app", "open_website"):
                continue  # 2026-10-04 起有意移出，见下方回归测试
            assert k in DANGEROUS_TOOLS, (
                f"危险工具 {k!r} 应在 DANGEROUS_TOOLS 里")

    def test_open_actions_are_not_dangerous(self):
        """回归：open_app / open_website 不得再要求确认。

        这两个是桌宠最高频、零破坏性的操作，列为危险工具会导致每次都要过弹窗；
        弹窗若不置前还会 60s 超时被拒（2026-10-04 日志两次「打开星穹铁道」失败）。
        """
        from app.brain.agent import DANGEROUS_TOOLS
        assert "open_app" not in DANGEROUS_TOOLS
        assert "open_website" not in DANGEROUS_TOOLS
        # 真正有破坏性的必须仍在
        for k in ("lock_screen", "kill_process", "run_script", "shutdown_computer"):
            assert k in DANGEROUS_TOOLS, f"{k} 不该被移出危险工具表"


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


# -------------------- Bug 7 旁路回归 --------------------

class TestConfirmBypassRegression:
    """2026-10-15:验证 _confirm_dangerous_tool 不再因 name not in DANGEROUS_TOOLS
    而静默 return True。

    之前 Bug 7 的修复只覆盖了 RiskyToolConfirm._fire_blocked 的 callback 路径,
    但 _confirm_dangerous_tool 内部有一道「name not in DANGEROUS_TOOLS → 直接
    return True」的旁路。这跟 RiskyToolConfirm 用的 _RISKY_TOOLS 列表不一致
    (例如 send_email 在 _RISKY_TOOLS 但不在 DANGEROUS_TOOLS),导致中间件拦
    下来 → callback 返回 True → 完全不弹窗。

    修复:删除旁路。无论 RiskyToolConfirm 拦住时 tool 名字在不在 DANGEROUS_TOOLS,
    都进弹窗路径。
    """

    def test_legacy_bypass_removed_from_source(self):
        """用 AST 解析源码,确认 _confirm_dangerous_tool 函数体内不再有
        `if name not in DANGEROUS_TOOLS: return True` 这种旁路分支。
        注释里提到也是注释 —— 只看实际代码。
        """
        import ast
        from pathlib import Path
        src = Path("app/ui/chat_window/__init__.py").read_text(encoding="utf-8")
        tree = ast.parse(src)

        def in_confirm_body(node):
            """只看 _confirm_dangerous_tool 函数体里的代码,排除已删除分支。"""
            return (isinstance(node, ast.FunctionDef)
                    and node.name == "_confirm_dangerous_tool")

        for node in ast.walk(tree):
            if not in_confirm_body(node):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.If):
                    # 检查 `if name not in DANGEROUS_TOOLS` 这种比较表达式
                    test = sub.test
                    if (isinstance(test, ast.Compare)
                            and isinstance(test.left, ast.Name)
                            and test.left.id == "name"):
                        # 比较右操作数包含 DANGEROUS_TOOLS
                        for comp in test.comparators:
                            if (isinstance(comp, ast.Name)
                                    and comp.id == "DANGEROUS_TOOLS"):
                                pytest.fail(
                                    "_confirm_dangerous_tool 不该再有 "
                                    "`if name not in DANGEROUS_TOOLS` 这种"
                                    "旁路分支 —— 已被 Bug 7 回归修复"
                                )

    def test_risky_tool_not_in_dangerous_tools_diff_is_nonempty(self):
        """存在至少一个『在 _RISKY_TOOLS 但不在 DANGEROUS_TOOLS』的工具。
        这是 Bug 7 旁路能触发的前提条件;一旦两边一致了,这个测试就 obsolete。
        """
        from app.brain.agent import DANGEROUS_TOOLS
        from app.brain.hallucination_guard import _RISKY_TOOLS

        diff = _RISKY_TOOLS - DANGEROUS_TOOLS
        assert diff, (
            "要求 _RISKY_TOOLS 跟 DANGEROUS_TOOLS 不一致 —— "
            "send_email 在 _RISKY_TOOLS 但不在 DANGEROUS_TOOLS 之类。"
            "如果列表已统一,这个测试可以删,但仍建议保留 audit 防回潮。"
        )