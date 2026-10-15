"""手动验证 Phase 3.1 Task 工具(Subagent)。

用法:
    cd E:\study\desktop-pet
    $env:PYTHONPATH="E:\study\desktop-pet"
    python manual_tests\verify_phase3.1_task_tool.py

输出:
    - Task 工具在默认 registry 里
    - 没 factory 调 Task 返友好错误
    - 装上 mock factory 后调 Task 拿到 mock 结果
    - _build_subagent_registry 过滤掉 Task 和高风险工具

不消耗 token,纯本地。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.brain.langchain_agent import LangChainAgent, LangChainAgentConfig
from app.brain.llm_client import LLMConfig
from app.brain.hallucination_guard import _RISKY_TOOLS
from app.engine.tools import ToolRegistry, Tool, build_default_tools


def main():
    print("=== Phase 3.1 Subagent Task 工具 手动验证 ===\n")

    print("[step 1] build_default_tools 注册的 registry 里有 Task")
    from unittest.mock import MagicMock
    reg = build_default_tools(state=MagicMock(), reminders=MagicMock(), memory=MagicMock())
    assert "Task" in reg.names(), f"Task 没注册! 有:{sorted(reg.names())[:10]}..."
    print(f"  - Task 已注册(总工具数 {len(reg.names())})")

    print("\n[step 2] 没 factory 时调 Task → 友好错误")
    from app.engine.tools import _subagent
    _subagent.set_subagent_factory(None)
    task_fn = reg.get("Task").fn
    result = task_fn(task="hello")
    print(f"  - 输出: {result[:80]}")
    assert "错误" in result

    print("\n[step 3] 装上 mock factory 后调 Task 拿到 mock 结果")
    captured = []
    def mock_factory(task_str, kwargs):
        captured.append((task_str, kwargs))
        return f"mock-result-for-{task_str}"
    _subagent.set_subagent_factory(mock_factory)
    result = task_fn(task="查 1+1")
    print(f"  - 输出: {result}")
    print(f"  - captured: {captured}")
    assert result == "mock-result-for-查 1+1"
    assert captured == [("查 1+1", {"agent_type": "general"})]

    print("\n[step 4] empty task 拒绝")
    result = task_fn(task="")
    print(f"  - 输出: {result}")
    assert "不能为空" in result

    print("\n[step 5] factory 抛异常 → 错误字符串,不 crash")
    def bad_factory(t, k):
        raise RuntimeError("boom")
    _subagent.set_subagent_factory(bad_factory)
    result = task_fn(task="x")
    print(f"  - 输出: {result}")
    assert "错误" in result
    assert not result.startswith("Traceback")

    print("\n[step 6] _build_subagent_registry 过滤 Task 和高风险工具")
    reg2 = ToolRegistry()
    reg2.register(Tool(name="normal_tool", description="n", parameters={}, fn=lambda: "ok"))
    reg2.register(Tool(name="lock_screen", description="n", parameters={}, fn=lambda: "x"))
    reg2.register(Tool(name="Task", description="n", parameters={}, fn=lambda: "x"))

    agent = LangChainAgent(
        llm_cfg=LLMConfig(), registry=reg2,
        cfg=LangChainAgentConfig(), persona="p", thread_id="t",
    )
    sub_reg = agent._build_subagent_registry()
    sub_names = sub_reg.names()
    print(f"  - 子 agent 工具: {sub_names}")
    assert "normal_tool" in sub_names
    assert "Task" not in sub_names, "Task 必须被过滤(防递归)"
    assert "lock_screen" not in sub_names
    for r in _RISKY_TOOLS:
        assert r not in sub_names, f"{r} 不该出现在子 agent 工具集"
    print(f"  → Task 和 {_RISKY_TOOLS} 全部被过滤")

    print("\n=== 全部通过 ===")
    print("\n[note] 主程序没接 UI 路径 —— 这个工具已经在默认 registry 里,")
    print("       LLM 看到后会主动调 Task 派子任务。要想让主人在聊天框里")
    print("       直接触发,告诉桌宠:'用 Task 工具帮我查一下 X',")
    print("       桌宠会调 Task(task='...'),由 mock 的 factory(实际跑时是")
    print("       LangChainAgent 装的真 factory)返回结果。")
    _subagent.set_subagent_factory(None)  # 清理


if __name__ == "__main__":
    main()