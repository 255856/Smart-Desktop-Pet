"""手动验证 Phase 2.2 Plan mode。

用法:
    cd E:\study\desktop-pet
    $env:PYTHONPATH="E:\study\desktop-pet"
    python scripts\manual\verify_phase2.2_plan_mode.py

输出:
    [OK] plan() 是 async generator
    [OK] plan-mode 提示已附加到 user 消息末尾
    [OK] config metadata 标记 plan_mode='1'
    [OK] multimodal 时图片保留,提示只加到 text
    [OK] plan() 跑空 messages 不崩

不消耗 token,纯本地。
"""
import asyncio
import sys

import os
# 脚本在 scripts/manual/ 下 → 往上两层才是项目根（app 包所在）
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from langchain_core.messages import AIMessageChunk

from app.brain.langchain_agent import LangChainAgent
from app.brain.llm_client import LLMConfig
from app.engine.tools import ToolRegistry


class _FakeGraph:
    """假的 agent graph —— 抓 input_payload / config,再 yield 一个假 chunk。"""

    def __init__(self, capture):
        self.capture = capture

    async def astream(self, input_payload, config=None, stream_mode="messages"):
        self.capture["input"] = input_payload
        self.capture["config"] = config
        yield AIMessageChunk(content="## 计划\n1. 第一步"), {}


def main():
    print("=== Phase 2.2 Plan mode 手动验证 ===\n")

    # 1) plan() 是 async generator
    agent = LangChainAgent(
        llm_cfg=LLMConfig(), registry=ToolRegistry(),
        persona="p", thread_id="t",
    )
    gen = agent.plan([{"role": "user", "content": "整理桌面"}])
    import inspect
    assert inspect.isasyncgen(gen), "plan() 必须返回 async generator"
    gen.aclose()
    print("[OK] plan() 是 async generator")

    # 2) plan-mode 提示附加 + config metadata + multimodal 保留
    captured = {}
    agent._agent = _FakeGraph(captured)

    async def go():
        events = []
        async for ev in agent.plan([
            {"role": "user", "content": [
                {"type": "text", "text": "整理桌面"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
            ]},
        ]):
            events.append(ev)
        return events

    asyncio.run(go())
    last_msg = captured["input"]["messages"][-1]
    assert last_msg.content[0]["text"].endswith(LangChainAgent._PLAN_MODE_PROMPT), \
        "提示没附加到 text 部分"
    assert last_msg.content[1]["type"] == "image_url", "图片被破坏了"
    assert last_msg.content[1]["image_url"]["url"] == "data:image/png;base64,AAA"
    print("[OK] plan-mode 提示附加到 text 部分(图片保留)")
    print("[OK] multimodal 不被破坏")

    cfg = captured["config"]
    assert cfg.get("metadata", {}).get("plan_mode") == "1", "config metadata 缺 plan_mode"
    assert cfg["configurable"]["thread_id"] == "t"
    print("[OK] config metadata 标记 plan_mode='1'")

    # 3) 空 messages 不崩
    captured2 = {}
    agent._agent = _FakeGraph(captured2)
    async def go2():
        async for _ in agent.plan([]):
            pass
    asyncio.run(go2())
    assert len(captured2["input"]["messages"]) == 1
    print("[OK] 空 messages 不崩,生成空 HumanMessage")

    print("\n=== 全部通过 ===")


if __name__ == "__main__":
    main()