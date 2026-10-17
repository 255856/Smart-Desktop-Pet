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
                continue  # 已有意移出，见下方回归测试
            assert k in DANGEROUS_TOOLS, (
                f"危险工具 {k!r} 应在 DANGEROUS_TOOLS 里")

    def test_open_actions_are_not_dangerous(self):
        """回归：open_app / open_website 不得再要求确认。

        这两个是桌宠最高频、零破坏性的操作，列为危险工具会导致每次都要过弹窗。
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
    """_confirm_dangerous_tool 不再因 name not in DANGEROUS_TOOLS 而静默 return True。

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


class TestConfirmCrossThreadDispatch:
    """_confirm_dangerous_tool 必须用 QMetaObject.invokeMethod + Qt.QueuedConnection (避免跨线程 QTimer.singleShot 静默丢)。"""

    def test_confirm_uses_invokemethod_not_single_shot(self):
        """_confirm_dangerous_tool 里不能再用 QTimer.singleShot(0, lambda ...) 跨线程投递。

        但允许 singleShot(0, self._xxx) 这种主线程兜底调用(例如 _close_confirm_box)。
        """
        import ast
        from pathlib import Path
        src = Path("app/ui/chat_window/__init__.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        target = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_confirm_dangerous_tool":
                target = node
                break
        assert target is not None
        # 找 singleShot(0, <lambda>) 或 singleShot(<int>, <lambda>) 调用
        # 模式:second arg 是 Lambda,意味着 cross-thread 投递
        for sub in ast.walk(target):
            if (isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "singleShot"
                    and len(sub.args) >= 2
                    and isinstance(sub.args[1], ast.Lambda)):
                pytest.fail(
                    "_confirm_dangerous_tool 不能再用 QTimer.singleShot(N, lambda ...) —— "
                    "child thread 调用会被 PyQt5 静默丢,弹窗永远不出现。"
                    "改用 QMetaObject.invokeMethod + Qt.QueuedConnection。"
                )

    def test_confirm_uses_invokemethod_invokeMethod_call(self):
        """反向守卫:必须存在 QMetaObject.invokeMethod 调用,目标方法是
        _invoke_confirm_dispatch_slot。"""
        import ast
        from pathlib import Path
        src = Path("app/ui/chat_window/__init__.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        # 找 _confirm_dangerous_tool 函数
        target = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_confirm_dangerous_tool":
                target = node
                break
        assert target is not None
        # 必须有 QMetaObject.invokeMethod 调用
        found_invoke = False
        for sub in ast.walk(target):
            if (isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "invokeMethod"):
                found_invoke = True
                # 第二个位置参数应该是字符串 literal "_invoke_confirm_dispatch_slot"
                if len(sub.args) >= 2 and isinstance(sub.args[1], ast.Constant):
                    assert sub.args[1].value == "_invoke_confirm_dispatch_slot", (
                        f"invokeMethod 目标方法必须是 '_invoke_confirm_dispatch_slot',"
                        f"实际 {sub.args[1].value!r}"
                    )
        assert found_invoke, (
            "_confirm_dangerous_tool 必须用 QMetaObject.invokeMethod 跨线程投递弹窗任务"
        )

    def test_dispatch_slot_decorated_with_pyqtSlot(self):
        """_invoke_confirm_dispatch_slot 必须用 @pyqtSlot 装饰,否则 QMetaObject.invokeMethod
        找不到该方法(QMetaObject::invokeMethod: No such method)。"""
        import ast
        from pathlib import Path
        src = Path("app/ui/chat_window/__init__.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        target = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_invoke_confirm_dispatch_slot":
                target = node
                break
        assert target is not None, "_invoke_confirm_dispatch_slot 必须存在"
        # 检查装饰器
        deco_names = []
        for deco in target.decorator_list:
            if isinstance(deco, ast.Name):
                deco_names.append(deco.id)
            elif isinstance(deco, ast.Attribute):
                deco_names.append(deco.attr)
            elif isinstance(deco, ast.Call) and isinstance(deco.func, ast.Name):
                deco_names.append(deco.func.id)
            elif isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute):
                deco_names.append(deco.func.attr)
        assert any("pyqtSlot" in n for n in deco_names), (
            f"_invoke_confirm_dispatch_slot 必须有 @pyqtSlot 装饰,"
            f"否则 QMetaObject.invokeMethod 找不到它。"
            f"实际装饰器: {deco_names}"
        )