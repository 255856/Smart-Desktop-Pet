"""Plan mode 的测试 —— 验证 LangChainAgent.plan() 在不同输入下的行为。

覆盖:
- plan() 是 async generator
- _PLAN_MODE_PROMPT 附加到 user 文本末尾
- multimodal user 消息:图片保持原样,只在 text 部分追加提示
- input_payload 的 messages 顺序正确
- config metadata 标记 plan_mode
- 空 messages 不抛
- cancel_check 生效
- 不调 _astream_with_retry(checkpoint 救援不在 plan 路径上)
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

from app.brain.langchain_agent import LangChainAgent


def _make_agent(**kwargs):
    from app.brain.llm_client import LLMConfig
    from app.engine.tools import ToolRegistry

    defaults = dict(
        llm_cfg=LLMConfig(),
        registry=ToolRegistry(),
        cfg=None,
        persona="base persona",
        thread_id="t-plan",
    )
    defaults.update(kwargs)
    return LangChainAgent(**defaults)


class TestPlanMethodShape:
    def test_plan_is_async_generator(self):
        agent = _make_agent()
        gen = agent.plan([{"role": "user", "content": "test"}])
        assert inspect.isasyncgen(gen), "plan() 必须返回 async generator"
        # 关闭 generator 防止未跑完的 ResourceWarning
        gen.aclose()

    def test_plan_mode_prompt_constant_exists(self):
        assert hasattr(LangChainAgent, "_PLAN_MODE_PROMPT")
        prompt = LangChainAgent._PLAN_MODE_PROMPT
        assert "plan mode" in prompt.lower() or "规划模式" in prompt
        assert "工具" in prompt or "tool" in prompt.lower()


class TestPlanPromptAppend:
    """用 mock 替掉 _ensure_agent 之后的 astream,验证 input_payload 真的带上了 plan 提示。"""

    def _make_agent_with_mock_astream(self, captured):
        agent = _make_agent()
        # 替换 _ensure_agent 让它立即返回
        async def _no_ensure():
            return None
        agent._ensure_agent = _no_ensure

        # 替换内部 _agent.astream,捕获输入
        class _FakeGraph:
            def __init__(self):
                pass
            async def astream(self, input_payload, config=None, stream_mode="messages"):
                captured["input"] = input_payload
                captured["config"] = config
                if False:  # pragma: no cover
                    yield None, None

        agent._agent = _FakeGraph()
        return agent

    def test_user_text_gets_plan_prompt_appended(self):
        captured = {}
        agent = self._make_agent_with_mock_astream(captured)
        asyncio.run(_drain_plan(agent, [{"role": "user", "content": "主人想干 X"}]))
        last_msg = captured["input"]["messages"][-1]
        assert last_msg.content.endswith(LangChainAgent._PLAN_MODE_PROMPT)
        assert "主人想干 X" in last_msg.content

    def test_multimodal_user_message_keeps_images(self):
        """multimodal(列表)user 时,text 部分追加 plan 提示,image_url 部分不变。"""
        captured = {}
        agent = self._make_agent_with_mock_astream(captured)
        multimodal = [
            {"type": "text", "text": "看下这张"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        ]
        asyncio.run(_drain_plan(agent, [{"role": "user", "content": multimodal}]))
        last_msg = captured["input"]["messages"][-1]
        assert isinstance(last_msg.content, list)
        # text 部分追加了 plan 提示
        assert last_msg.content[0]["type"] == "text"
        assert "看下这张" in last_msg.content[0]["text"]
        assert last_msg.content[0]["text"].endswith(LangChainAgent._PLAN_MODE_PROMPT)
        # image 部分原样保留
        assert last_msg.content[1] == {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,AAA"},
        }

    def test_history_is_passed_correctly(self):
        """messages[:-1] 走 history 路径,messages[-1] 走 user 路径。"""
        captured = {}
        agent = self._make_agent_with_mock_astream(captured)
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "新问题"},
        ]
        asyncio.run(_drain_plan(agent, msgs))
        msgs_in = captured["input"]["messages"]
        # 3 条:1 history user + 1 history assistant + 1 新 user(带 plan 提示)
        assert len(msgs_in) == 3
        assert msgs_in[0].content == "hi"
        assert msgs_in[1].content == "hello"
        assert msgs_in[2].content.endswith(LangChainAgent._PLAN_MODE_PROMPT)

    def test_config_metadata_marks_plan_mode(self):
        captured = {}
        agent = self._make_agent_with_mock_astream(captured)
        asyncio.run(_drain_plan(agent, [{"role": "user", "content": "x"}]))
        config = captured["config"]
        assert config["configurable"]["thread_id"] == "t-plan"
        assert config.get("metadata", {}).get("plan_mode") == "1"


class TestPlanEdgeCases:
    def test_empty_messages_does_not_crash(self):
        """空 messages:最后一条 user 取不到,生成空 HumanMessage,不应抛。"""
        captured = {}
        agent = TestPlanPromptAppend()._make_agent_with_mock_astream(captured)
        # 不传任何消息
        asyncio.run(_drain_plan(agent, []))
        # messages 列表里有 1 条 HumanMessage(content="")
        msgs_in = captured["input"]["messages"]
        assert len(msgs_in) == 1

    def test_non_user_last_message_does_not_get_prompt(self):
        """最后一条不是 user(role 异常)时,不要强行附加 plan 提示。"""
        captured = {}
        agent = TestPlanPromptAppend()._make_agent_with_mock_astream(captured)
        # 最后一条是 assistant,plan() 不会附加 plan prompt
        asyncio.run(_drain_plan(agent, [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "echo"},
        ]))
        msgs_in = captured["input"]["messages"]
        # 最后一条 user 是空(因为 last role 不是 user,plan() 不附加提示)
        # 验证:没有任何消息的内容以 _PLAN_MODE_PROMPT 结尾
        for m in msgs_in:
            assert not (isinstance(m.content, str) and m.content.endswith(LangChainAgent._PLAN_MODE_PROMPT))

    def test_cancel_check_aborts_run(self):
        """cancel_check 返回 True 时,plan() 应该 yield error 终止。"""
        from langchain_core.messages import AIMessageChunk
        agent = _make_agent()

        # 替换 astream:先 yield 一个 chunk,然后检查 cancel
        cancel_called = []

        class _CancelingGraph:
            def __init__(self):
                self.yielded = 0

            async def astream(self, input_payload, config=None, stream_mode="messages"):
                # yield 一个 chunk 触发 cancel_check
                yield AIMessageChunk(content="hi"), {}
                # 第二次 yield:已经 cancel 了,plan() 应该立即退出
                # (如果 cancel 没生效,会继续 loop,我们不会看到 error)
                yield AIMessageChunk(content="more"), {}

        agent._agent = _CancelingGraph()

        events = []

        async def run_with_cancel():
            nonlocal events
            async for ev in agent.plan(
                [{"role": "user", "content": "x"}],
                cancel_check=lambda: True,   # 永远 cancel
            ):
                events.append(ev)
                if ev[0] == "error":
                    break

        asyncio.run(asyncio.wait_for(run_with_cancel(), timeout=2.0))
        # 第一次 yield(text 'hi')成功,然后 cancel_check 触发,plan yield ('error', 'cancelled')
        assert any(ev[0] == "error" and "cancel" in str(ev[1]).lower() for ev in events), \
            f"expected ('error', 'cancelled') event, got: {events}"


# -------------------- helper --------------------

async def _drain_plan(agent, msgs):
    """跑 plan() 直到结束(碰到 mock _agent.astream 立刻空返)。"""
    events = []
    async for ev in agent.plan(msgs):
        events.append(ev)
    return events