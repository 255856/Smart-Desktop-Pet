"""手动验证 Phase 3.3 User-wide memory 注入。

用法:
    cd E:\study\desktop-pet
    $env:PYTHONPATH="E:\study\desktop-pet"
    python manual_tests\verify_phase3.3_memory.py

输出:
    - 用真的 MemoryStore(在临时目录落盘)
    - add() 几条记忆
    - 构造 LangChainAgent 注入 memory_store
    - 抓 system_prompt,打印出 memory_block
    - 验证记忆内容被拼到 system_prompt,顺序在中间件规则前

不消耗 token。
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.brain.langchain_agent import LangChainAgent, LangChainAgentConfig
from app.brain.llm_client import LLMConfig
from app.brain.memory import MemoryStore
from app.engine.tools import ToolRegistry


def main():
    print("=== Phase 3.3 User-wide memory 手动验证 ===\n")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        store = MemoryStore(td / "mem.db")

        print("[setup] 加 5 条记忆(模拟主人跨聊天积累的)")
        store.add("主人喜欢喝冰美式,不加糖", category="preference", importance=0.9)
        store.add("主人家里有一只叫团子的橘猫", category="person", importance=0.85)
        store.add("主人在准备编程马拉松下周六", category="event", importance=0.7)
        store.add("主人会用 Python 写后端", category="skill", importance=0.6)
        store.add("主人不喜欢香菜", category="preference", importance=0.95)
        for it in store.all():
            print(f"  - [{it.category}★{it.importance:.2f}] {it.content}")

        agent = LangChainAgent(
            llm_cfg=LLMConfig(),
            registry=ToolRegistry(),
            cfg=LangChainAgentConfig(),
            persona="我是桌宠",
            memory_store=store,
        )

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

        # memory.system_block() 应该含「你对主人的长期记忆」
        for marker in [
            "我是桌宠",
            "冰美式",
            "团子",
            "马拉松",
            "香菜",
            "长期记忆",
            "防幻觉基线",
        ]:
            in_sp = marker in sp
            print(f"  - 含 {marker!r}: {in_sp}")
            if marker not in ("我是桌宠", "防幻觉基线"):
                assert in_sp, f"{marker!r} 没被注入"

        print("\n[result] 拼接顺序:persona < memory;中间件规则运行时注入,create 阶段不在")
        pos_persona = sp.find("我是桌宠")
        pos_mem = sp.find("长期记忆")
        print(f"  pos({pos_persona:>4})  我是桌宠")
        print(f"  pos({pos_mem:>4})  长期记忆")
        assert pos_persona < pos_mem, "顺序错了"

        print("\n=== 全部通过 ===")
        print("\n[note] 在 chat_window 里实际行为:主人说『记住我喜欢冰美式』,")
        print("       桌宠会调 remember_fact 把记忆持久化。下次开新对话,")
        print("       memory.system_block() 会自动把这条 + 其他高 importance")
        print("       的记忆一起注入 system_prompt,所以主人问『我最喜欢喝』")
        print("       时,桌宠会直接答出『冰美式,不加糖』。")
        print("       当前 chat_window 还没把 memory_store 传给 LangChainAgent,")
        print("       所以聊天框里还看不到效果,需要先接线。")


if __name__ == "__main__":
    main()