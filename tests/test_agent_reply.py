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
    # 未闭合的 <think> 起始标签 + 中文段落：sanitize 会剥标签、清空白、最终保留中文
    got = _sanitize_reply("早上好~</think>" + NL + NL + "主人早上好")
    assert "早上好" in got and "主人" in got and "<think>" not in got, got
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

class MultiToolClient:
    """三轮脚本：round1 调 web_search，round2 调 open_website，round3 给最终答案。"""

    def __init__(self):
        self.calls = 0

    async def chat_stream_events(self, messages, tools=None, cancel_check=None,
                                 force_tool_use=False):
        self.calls += 1
        if self.calls == 1:
            yield "finish", {"content": "", "tool_calls": [{
                "id": "a1", "name": "web_search",
                "arguments": '{"query":"十月新番"}'}]}
        elif self.calls == 2:
            yield "finish", {"content": "", "tool_calls": [{
                "id": "b1", "name": "open_website",
                "arguments": '{"url":"https://b.com"}'}]}
        else:
            yield "text", "主人，我搜好啦，十月新番有《无职转生》，B站也帮你打开了~"
            yield "finish", {"content": "", "tool_calls": []}


class MultiRegistry:
    def names(self):
        return ["web_search", "open_website"]

    def to_openai(self):
        return []

    def execute(self, name, args):
        return "找到 5 条结果" if name == "web_search" else "已在浏览器打开 https://b.com"


def test_multitool_then_final_not_force_retried():
    """调过工具后再给最终文字，必须直接放行，绝不 force_retry（核心修复）。"""
    metas, texts, tools_seen = [], [], []
    agent = AgentLoop(MultiToolClient(), MultiRegistry())

    async def run():
        async for ev, *rest in agent.run([
            {"role": "system", "content": "你是桌宠"},
            {"role": "user", "content": "帮我搜一下十月新番，并打开B站"},
        ]):
            if ev == "text":
                texts.append(rest[0])
            elif ev == "meta":
                metas.append(rest[0])
            elif ev == "tool":
                tools_seen.append(rest[0])

    asyncio.run(run())
    assert tools_seen == ["web_search", "open_website"], tools_seen
    # 调过工具后再总结，不能再被强令继续调工具
    assert not any(m.get("event") == "force_retry" for m in metas), metas
    # 恰好三轮：工具A、工具B、最终答案，不多不少
    assert agent.client.calls == 3, agent.client.calls
    assert "".join(texts) == "主人，我搜好啦，十月新番有《无职转生》，B站也帮你打开了~"


class EmptySummaryClient:
    """round1 调 open_app 成功；round2 既不调工具也不给文字（空总结）。"""

    def __init__(self):
        self.calls = 0

    async def chat_stream_events(self, messages, tools=None, cancel_check=None,
                                 force_tool_use=False):
        self.calls += 1
        if self.calls == 1:
            yield "finish", {"content": "", "tool_calls": [{
                "id": "c1", "name": "open_app",
                "arguments": '{"app_name":"QQ"}'}]}
        else:
            yield "finish", {"content": "", "tool_calls": []}


class OpenAppRegistry:
    def names(self):
        return ["open_app"]

    def to_openai(self):
        return []

    def execute(self, name, args):
        return "已打开 QQ"


def test_empty_summary_after_tool_gets_ack():
    """工具成功但模型没给文字：兜底角色化确认，不回显含 URL/原始结果的 tool 输出。"""
    texts = []
    agent = AgentLoop(EmptySummaryClient(), OpenAppRegistry())

    async def run():
        async for ev, *rest in agent.run([
            {"role": "system", "content": "你是桌宠"},
            {"role": "user", "content": "打开QQ"},
        ]):
            if ev == "text":
                texts.append(rest[0])

    asyncio.run(run())
    joined = "".join(texts)
    assert "QQ" in joined and "打开" in joined, joined
    assert "已打开 QQ" not in joined, "不应直接回显原始工具结果"

from app.brain.agent import _format_verify_hint, _final_contradicts_tools


def test_verify_hint_reports_tool_state():
    """结果核对单：列出每个工具的成败状态并要求核对。"""
    hint = _format_verify_hint([
        ("open_app", "{}", "失败：未找到应用 QQ"),
        ("web_search", "{}", "找到 5 条结果"),
    ])
    assert "open_app" in hint and "失败" in hint and "未找到应用 QQ" in hint
    assert "web_search" in hint and "成功" in hint
    assert "核对" in hint
    # 取消态不重复原因
    assert "（已取消）" in _format_verify_hint([("add_reminder", "{}", "用户取消了此操作。")])


class FailThenClaimClient:
    """round1 调 open_app（实际失败）；round2 报喜；round3 纠正后如实告知。"""

    def __init__(self):
        self.calls = 0

    async def chat_stream_events(self, messages, tools=None, cancel_check=None,
                                 force_tool_use=False):
        self.calls += 1
        if self.calls == 1:
            yield "finish", {"content": "", "tool_calls": [{
                "id": "c1", "name": "open_app",
                "arguments": '{"app_name":"星穹铁道"}'}]}
        elif self.calls == 2:
            yield "text", "好的主人，已经帮你打开星穹铁道啦~"
            yield "finish", {"content": "", "tool_calls": []}
        else:
            yield "text", "唔……星穹铁道好像没安装，我这边没能打开，主人帮我看看路径对不对呀？"
            yield "finish", {"content": "", "tool_calls": []}


class FailRegistry:
    def names(self):
        return ["open_app"]

    def to_openai(self):
        return []

    def execute(self, name, args):
        return "失败：未找到应用 星穹铁道"


def test_failure_but_claiming_success_is_corrected():
    """工具失败却报喜：应触发一次 verify_retry，最终采用如实失败的答复。"""
    texts, metas = [], []
    agent = AgentLoop(FailThenClaimClient(), FailRegistry())

    async def run():
        async for ev, *rest in agent.run([
            {"role": "system", "content": "你是桌宠"},
            {"role": "user", "content": "打开星穹铁道"},
        ]):
            if ev == "text":
                texts.append(rest[0])
            elif ev == "meta":
                metas.append(rest[0])

    asyncio.run(run())
    joined = "".join(texts)
    assert any(m.get("event") == "verify_retry" for m in metas), metas
    assert "没能打开" in joined and "没安装" in joined, joined
    assert "已经帮你打开" not in joined, "报喜答复不应保留"
    assert agent.client.calls == 3, agent.client.calls


class FailThenHonestClient:
    """round1 调 open_app 失败；round2 如实告知失败（不应被纠正）。"""

    def __init__(self):
        self.calls = 0

    async def chat_stream_events(self, messages, tools=None, cancel_check=None,
                                 force_tool_use=False):
        self.calls += 1
        if self.calls == 1:
            yield "finish", {"content": "", "tool_calls": [{
                "id": "c1", "name": "open_app",
                "arguments": '{"app_name":"星穹铁道"}'}]}
        else:
            yield "text", "唔，星穹铁道好像没安装，我这边打不开呢。"
            yield "finish", {"content": "", "tool_calls": []}


def test_failure_reported_honestly_no_correction():
    """模型已如实说明失败：不纠正、不增加额外往返。"""
    texts, metas = [], []
    agent = AgentLoop(FailThenHonestClient(), FailRegistry())

    async def run():
        async for ev, *rest in agent.run([
            {"role": "system", "content": "你是桌宠"},
            {"role": "user", "content": "打开星穹铁道"},
        ]):
            if ev == "text":
                texts.append(rest[0])
            elif ev == "meta":
                metas.append(rest[0])

    asyncio.run(run())
    joined = "".join(texts)
    assert not any(m.get("event") == "verify_retry" for m in metas), metas
    assert "没安装" in joined and "打不开" in joined, joined
    assert agent.client.calls == 2, agent.client.calls


def test_contradiction_detector():
    """矛盾检测器：失败报喜 True，如实失败/成功正常 False。"""
    tools = [("open_app", "{}", "失败：未找到应用")]
    assert _final_contradicts_tools("好的主人，已经帮你打开啦~", tools) is True
    assert _final_contradicts_tools("唔，没安装，我这边打不开呢", tools) is False
    assert _final_contradicts_tools("已经帮你打开啦", [("open_app", "{}", "已打开")]) is False

def test_claim_success_fallback_when_no_turns_left():
    """max_turns=2 无剩余轮次重答时：报喜也被诚实失败文案兜底替换。"""
    texts, metas = [], []
    agent = AgentLoop(FailThenClaimClient(), FailRegistry(), max_turns=2)

    async def run():
        async for ev, *rest in agent.run([
            {"role": "system", "content": "你是桌宠"},
            {"role": "user", "content": "打开星穹铁道"},
        ]):
            if ev == "text":
                texts.append(rest[0])
            elif ev == "meta":
                metas.append(rest[0])

    asyncio.run(run())
    joined = "".join(texts)
    assert not any(m.get("event") == "verify_retry" for m in metas), metas
    assert "没成功" in joined, joined
    assert "已经帮你打开" not in joined, joined


class SearchFailEmptyClient:
    """round1 web_search 失败；round2 空文字（无 tool_calls）。"""

    def __init__(self):
        self.calls = 0

    async def chat_stream_events(self, messages, tools=None, cancel_check=None,
                                 force_tool_use=False):
        self.calls += 1
        if self.calls == 1:
            yield "finish", {"content": "", "tool_calls": [{
                "id": "c1", "name": "web_search",
                "arguments": '{"query":"十月新番"}'}]}
        else:
            yield "finish", {"content": "", "tool_calls": []}


class SearchFailRegistry:
    def names(self):
        return ["web_search"]

    def to_openai(self):
        return []

    def execute(self, name, args):
        return "搜索失败（网络错误）。"


def test_search_failure_empty_summary_is_honest():
    """搜索失败 + 模型空总结：兜底说『没搜到』，不能报喜『结果来啦』。"""
    texts = []
    agent = AgentLoop(SearchFailEmptyClient(), SearchFailRegistry())

    async def run():
        async for ev, *rest in agent.run([
            {"role": "system", "content": "你是桌宠"},
            {"role": "user", "content": "搜一下十月新番"},
        ]):
            if ev == "text":
                texts.append(rest[0])

    asyncio.run(run())
    joined = "".join(texts)
    assert "没搜到" in joined, joined
    assert "结果来啦" not in joined, joined

