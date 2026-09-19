"""AgentLoop 回复清洗回归：工具轮独白不上屏、<think> 剥离、system 铁律注入。"""
import asyncio
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, r"E:\study\desktop-pet")
sys.path.insert(0, r"E:\study\desktop-pet\.local-packages")

from app.brain.agent import AgentLoop, _sanitize_reply

NL = chr(10)


def test_sanitize_strips_think_block():
    assert _sanitize_reply("<think>内部推理</think>你好呀主人") == "你好呀主人"
    got = _sanitize_reply("早上好~</think>" + NL + NL + "主人早上好")
    assert got.replace(NL, "") == "早上好~主人早上好"
    assert _sanitize_reply("  <think>x" + NL + "y</think>  ") == ""


class FakeClient:
    """两轮脚本：round0 工具独白+调工具；round1 含 think 标签的最终回复。"""

    def __init__(self):
        self.calls = 0

    async def chat_stream_events(self, messages, tools=None, cancel_check=None,
                                 force_tool_use=False):
        self.calls += 1
        if messages and messages[0].get("role") == "system":
            assert "回复风格铁律" in str(messages[0].get("content")), "铁律未注入"
        if self.calls == 1:
            yield "text", "这是一个URL类型的请求，我应该使用open_website工具来打开它。"
            yield "finish", {"content": "", "tool_calls": [{
                "id": "c1", "name": "open_website",
                "arguments": '{"url":"https://x.com"}'}]}
        else:
            yield "text", "<think>思考中</think>早上好呀主人！新的一天开始啦~"
            yield "finish", {"content": "", "tool_calls": []}


class FakeRegistry:
    def names(self):
        return ["open_website"]

    def to_openai(self):
        return []

    def execute(self, name, args):
        return "已在浏览器打开 https://x.com"


def test_tool_round_narration_never_reaches_ui():
    texts = []
    agent = AgentLoop(FakeClient(), FakeRegistry())

    async def run():
        async for ev, *rest in agent.run([
            {"role": "system", "content": "你是桌宠"},
            {"role": "user", "content": "打开抖音"},
        ]):
            if ev == "text":
                texts.append(rest[0])

    asyncio.run(run())
    joined = "".join(texts)
    # 只发最终轮的一段、无工具独白、无 think 残留
    assert len(texts) == 1, f"最终回复应一次性发出（got {len(texts)} 段）"
    assert "open_website" not in joined and "工具" not in joined, joined
    assert "<think>" not in joined, joined
    assert joined == "早上好呀主人！新的一天开始啦~"
