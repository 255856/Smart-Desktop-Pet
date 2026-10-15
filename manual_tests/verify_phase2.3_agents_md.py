"""手动验证 Phase 2.3 AGENTS.md 自动加载。

用法:
    cd E:\study\desktop-pet
    $env:PYTHONPATH="E:\study\desktop-pet"
    python manual_tests\verify_phase2.3_agents_md.py

输出:
    - 在 cwd 下临时写一个 AGENTS.md
    - 构造 LangChainAgent,触发 _ensure_agent
    - 打印 system_prompt 拼接后的内容(截掉反幻觉规则太长部分)
    - 主人可以肉眼看到项目指令确实被加进去了

不消耗 token。
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain.agents import create_agent as _real_create_agent

from app.brain.langchain_agent import LangChainAgent, LangChainAgentConfig
from app.brain.llm_client import LLMConfig
from app.engine.tools import ToolRegistry


def main():
    print("=== Phase 2.3 AGENTS.md 自动加载手动验证 ===\n")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        # 写一个 AGENTS.md,内容里塞一个标志性词,方便搜
        agents_md = td / "AGENTS.md"
        agents_md.write_text(
            "项目铁律:\n"
            "1. 永远不要在外面的代码里写 `pass` 语句\n"
            "2. 函数必须有 docstring\n",
            encoding="utf-8",
        )
        print(f"[setup] cwd = {td}")
        print(f"[setup] AGENTS.md = {agents_md}")
        print(f"[setup] 内容:\n---\n{agents_md.read_text(encoding='utf-8')}\n---\n")

        agent = LangChainAgent(
            llm_cfg=LLMConfig(),
            registry=ToolRegistry(),
            cfg=LangChainAgentConfig(),
            persona="我是桌宠",
            cwd=str(td),
        )

        # 抓 create_agent 的 system_prompt
        captured = {}
        original_create = _real_create_agent

        def patched_create_agent(**kwargs):
            captured["system_prompt"] = kwargs.get("system_prompt", "")
            # 返回原值,但不真跑(避免实际请求 LLM)
            return object()

        import unittest.mock
        with unittest.mock.patch(
            "app.brain.langchain_agent.create_agent", side_effect=patched_create_agent,
        ):
            agent._agent = None
            asyncio.run(agent._ensure_agent())

        sp = captured.get("system_prompt", "")
        print(f"\n[result] system_prompt 长度 = {len(sp)} chars")
        print(f"[result] 包含 '我是桌宠': {'我是桌宠' in sp}")
        print(f"[result] 包含 '项目铁律': {'项目铁律' in sp}")
        print(f"[result] 包含 '永远不要在外面': {'永远不要在外面' in sp}")
        # 注意:反幻觉基线是 dynamic_prompt 中间件在 astream 时注入的,
        # create_agent 阶段拿到的 system_prompt 还不含它。这是设计 —— 不算 bug。
        print(f"[result] 包含 '防幻觉基线'(中间件运行时注入,create 阶段没有): {'防幻觉基线' in sp}")
        print()

        # 打印截断的拼接顺序
        markers = ["我是桌宠", "项目铁律"]
        positions = [(m, sp.find(m)) for m in markers]
        print("[result] 拼接顺序(应:persona < project_docs):")
        for m, p in positions:
            print(f"  pos({p:>4})  {m}")

        assert "我是桌宠" in sp, "persona 没进 system_prompt"
        assert "项目铁律" in sp, "AGENTS.md 没进 system_prompt"

        # 顺序断言
        p_persona = sp.find("我是桌宠")
        p_docs = sp.find("项目铁律")
        assert p_persona < p_docs, (
            f"顺序错:persona={p_persona}, docs={p_docs}"
        )

        print("\n=== 全部通过 ===")


if __name__ == "__main__":
    main()