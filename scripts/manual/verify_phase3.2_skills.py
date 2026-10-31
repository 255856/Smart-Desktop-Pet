"""手动验证 Phase 3.2 Skills 系统。

用法:
    cd E:\study\desktop-pet
    $env:PYTHONPATH="E:\study\desktop-pet"
    python scripts\manual\verify_phase3.2_skills.py

输出:
    - 创建临时 skills_dir,放两个 skill 文件
    - 构造 LangChainAgent 注入 skills_dir + auto_load_skills
    - 抓 create_agent 的 system_prompt,打印出 skills 段
    - 主人可以肉眼看到 skill 内容被拼到 system_prompt

不消耗 token。
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

# 脚本在 scripts/manual/ 下 → 往上两层才是项目根（app 包所在）
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from app.brain.langchain_agent import LangChainAgent, LangChainAgentConfig
from app.brain.llm_client import LLMConfig
from app.engine.tools import ToolRegistry


def main():
    print("=== Phase 3.2 Skills 系统 手动验证 ===\n")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        # 写两个 skill
        (td / "code-review.md").write_text(
            "做 code review 时:\n"
            "- 先看测试是否覆盖\n"
            "- 再看错误处理\n"
            "- 最后看命名\n",
            encoding="utf-8",
        )
        (td / "test-writing.md").write_text(
            "写测试时:\n"
            "- 一个测试只测一个东西\n"
            "- 边界条件一定要有\n",
            encoding="utf-8",
        )
        (td / "ignored-skill.md").write_text(
            "不该被加载(不在 auto_load_skills 里)\n",
            encoding="utf-8",
        )
        print(f"[setup] skills_dir = {td}")
        for p in sorted(td.glob("*.md")):
            print(f"  - {p.name}: {p.read_text(encoding='utf-8')[:60]}...")

        agent = LangChainAgent(
            llm_cfg=LLMConfig(),
            registry=ToolRegistry(),
            cfg=LangChainAgentConfig(),
            persona="我是桌宠",
            skills_dir=str(td),
            auto_load_skills=["code-review", "test-writing"],
        )

        # 抓 create_agent 的 system_prompt
        captured = {}
        import unittest.mock
        with unittest.mock.patch(
            "app.brain.langchain_agent.create_agent",
            side_effect=lambda **kw: captured.update(kw) or object(),
        ):
            agent._agent = None
            asyncio.run(agent._ensure_agent())

        sp = captured.get("system_prompt", "")
        print(f"\n[result] system_prompt 长度 = {len(sp)} chars")

        for marker in [
            "我是桌宠",
            "[Skill: code-review]",
            "做 code review 时",
            "[Skill: test-writing]",
            "写测试时",
            "ignored-skill",   # 不该出现
            "Skills 结束",
        ]:
            in_sp = marker in sp
            print(f"  - 含 {marker!r}: {in_sp}")

        assert "[Skill: code-review]" in sp
        assert "做 code review 时" in sp
        assert "[Skill: test-writing]" in sp
        assert "写测试时" in sp
        assert "ignored-skill" not in sp, "auto_load_skills 没指定的不该被加载"
        assert "Skills 结束" in sp

        print("\n[result] 拼接顺序(应:persona < skills;中间件规则运行时注入,create 阶段不在)")
        markers = ["我是桌宠", "[Skill: code-review]"]
        for m in markers:
            print(f"  - pos({sp.find(m):>4})  {m}")

        assert sp.find("我是桌宠") < sp.find("[Skill: code-review]"), "顺序错了"
        print("  → 顺序 OK")

        print("\n=== 全部通过 ===")


if __name__ == "__main__":
    main()