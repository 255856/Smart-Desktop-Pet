"""Stage 3（工具可信）回归锁：白名单 / 结构化信封 / 统一裁剪 / 墙钟预算 / 分组。

对应 PLAN.md「Stage 3 — 工具可信」的 3.1 / 3.2 / 3.3 / 3.4。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.brain.agent import AgentLoop  # noqa: E402
from app.brain.prompt_layer import build_tool_list  # noqa: E402
from app.engine.tools._core import (  # noqa: E402
    DEFAULT_ALLOWLIST_PREFIXES, TOOL_RESULT_HEAD_CHARS, TOOL_RESULT_MAX_CHARS,
    TOOL_RESULT_TAIL_CHARS, Tool, ToolRegistry, ToolResult,
    fit_tool_payload, load_tool_allowlist, trim_tool_result,
)


# =============================================================================
# 3.1 执行期白名单
# =============================================================================

def _default_registry() -> ToolRegistry:
    """真·默认工具集（61 个内置工具）。"""
    from app.core.companion_config import companion_config as cc
    from app.engine.tools import build_default_tools

    class _State:
        def get(self, *a, **k):
            return None

    return build_default_tools(state=_State(), reminders=None, memory=None,
                               cfg=cc)


def test_allowlist_policy_comes_from_yaml_with_two_levels():
    """白名单在 companion_config.yaml 的 tool_allowlist 段：精确名 + 前缀。"""
    policy = load_tool_allowlist(force=True)
    assert policy is not None, "yaml 里必须有 tool_allowlist 段"
    assert policy.from_yaml is True
    assert len(policy.exact) == 67, policy.exact
    for p in DEFAULT_ALLOWLIST_PREFIXES:
        assert p in policy.prefixes, policy.prefixes


def test_all_existing_builtin_tools_are_still_allowed():
    """存量 61 个内置工具全部仍可调用（白名单不能误伤）。"""
    reg = _default_registry()
    reg.seal_baseline()
    denied = [n for n in reg.names() if not reg._allowlist_allows(n)[0]]
    assert denied == [], f"存量工具被白名单误伤：{denied}"


def test_mcp_namespace_prefix_is_allowed():
    """MCP 工具按命名空间前缀放行（mcp_aw_ / mcp_fs_）。"""
    reg = _default_registry()
    reg.seal_baseline()
    for name in ("mcp_aw_aw_status", "mcp_fs_fs_read_file"):
        ok, _ = reg._allowlist_allows(name)
        assert ok is True, name


def test_new_tool_is_denied_at_execution_time():
    """新增（未登记）工具在**执行期**被拒 —— 模型硬点名也拦得住。"""
    reg = _default_registry()
    reg.seal_baseline()
    reg.register(Tool(name="brand_new_tool", description="新工具",
                      parameters={"type": "object", "properties": {}},
                      fn=lambda: "我被执行了"))
    out = reg.execute("brand_new_tool", "{}")
    assert out["status"] == "denied", out
    assert out["error_code"] == "not_allowed", out
    assert "未登记" in out["data"], out


def test_denied_tool_is_not_offered_to_the_model():
    """白名单外的工具连「下发」都不该有（两个后端都走 tools_for_level）。"""
    reg = _default_registry()
    reg.seal_baseline()
    reg.register(Tool(name="brand_new_tool", description="新工具",
                      parameters={"type": "object", "properties": {}},
                      fn=lambda: "ok"))
    offered = {t["function"]["name"] for t in reg.to_openai_for_level(10)}
    assert "brand_new_tool" not in offered
    assert "open_app" in offered


def test_unknown_tool_is_not_found():
    """未知工具 → error_code='not_found'（稳定的机器可读码）。"""
    reg = ToolRegistry()
    out = reg.execute("no_such_tool", "{}")
    assert out["status"] == "error", out
    assert out["error_code"] == "not_found", out


# =============================================================================
# 3.2 结构化结果信封
# =============================================================================

def test_envelope_shape_is_stable():
    """信封字段固定：status / data / truncated / error_code。"""
    reg = ToolRegistry()
    reg.register(Tool(name="ok_tool", description="d",
                      parameters={"type": "object", "properties": {}},
                      fn=lambda: "一切正常"))
    out = reg.execute("ok_tool", "{}")
    assert set(out.keys()) == {"status", "data", "truncated", "error_code"}
    assert out["status"] == "ok"
    assert out["data"] == "一切正常"
    assert out["truncated"] is False
    assert out["error_code"] == ""


def test_tool_exception_becomes_tool_error():
    """工具抛异常 → error_code='tool_error'，不把异常原文泄漏给模型。"""
    def _boom():
        raise RuntimeError("内部细节")

    reg = ToolRegistry()
    reg.register(Tool(name="boom", description="d",
                      parameters={"type": "object", "properties": {}},
                      fn=_boom))
    out = reg.execute("boom", "{}")
    assert out["status"] == "error", out
    assert out["error_code"] == "tool_error", out


def test_keyword_tables_are_gone():
    """_TOOL_FAIL_MARKERS / _HONEST_FAIL_WORDS 两张词表整体删除。"""
    src = (ROOT / "app" / "brain" / "agent.py").read_text(encoding="utf-8")
    assert "_TOOL_FAIL_MARKERS" not in src
    assert "_HONEST_FAIL_WORDS" not in src


# =============================================================================
# 3.3 统一裁剪 + 墙钟预算
# =============================================================================

def test_uniform_trim_keeps_head_and_tail():
    """超阈值时保头保尾，中间有明确省略标记，并标 truncated。"""
    raw = ("HEAD" * 10) + ("M" * 80000) + ("TAIL" * 10)
    out, truncated = trim_tool_result(raw)
    assert truncated is True
    assert len(out) <= TOOL_RESULT_MAX_CHARS
    assert out.startswith("HEAD")
    assert out.endswith("TAIL")
    assert "省略" in out


def test_execute_trims_big_result():
    """execute() 的后置统一裁剪：超 50000 字符 → truncated=True。"""
    reg = ToolRegistry()
    reg.register(Tool(name="big", description="d",
                      parameters={"type": "object", "properties": {}},
                      fn=lambda: "x" * 60000))
    out = reg.execute("big", "{}")
    assert out["truncated"] is True, out
    assert len(out["data"]) <= TOOL_RESULT_MAX_CHARS


def test_fit_payload_keeps_head_and_tail():
    """整轮注入预算：保头保尾，中间省略。"""
    contents = [("A" * 4000) + str(i) for i in range(20)]
    fitted, changed = fit_tool_payload(contents)
    assert changed is True
    total = sum(len(c) for c in fitted)
    assert total <= TOOL_RESULT_MAX_CHARS, total
    assert "省略" in "".join(fitted)
    assert fitted[0].startswith("A")
    assert fitted[-1].endswith("19")


class _ManyToolsClient:
    """第一轮点名 N 个工具，第二轮给最终答复。"""

    def __init__(self, n: int = 20):
        self.n = n
        self.calls = 0

    async def chat_stream_events(self, messages, tools=None,
                                 cancel_check=None, force_tool_use=False):
        self.calls += 1
        if self.calls == 1:
            yield ("finish", {"content": "", "reasoning": "", "tool_calls": [
                {"id": f"c{i}", "name": f"tool_{i}", "arguments": "{}"}
                for i in range(self.n)]})
        else:
            yield ("text", "好的主人，都查完啦~")
            yield ("finish", {"content": "好的主人，都查完啦~",
                              "tool_calls": [], "reasoning": ""})


def test_twenty_big_results_stay_under_budget():
    """一轮 20 个大结果 → 注入上下文 <= 50000，头尾都在、中间有省略标记。"""
    n = 20
    reg = ToolRegistry()
    for i in range(n):
        reg.register(Tool(
            name=f"tool_{i}", description="d",
            parameters={"type": "object", "properties": {}},
            fn=(lambda i: (lambda: f"[head{i}]" + ("y" * 6000) + f"[tail{i}]"))(i)))
    agent = AgentLoop(client=_ManyToolsClient(n), registry=reg, max_turns=3)
    messages: list[dict] = [{"role": "user", "content": "帮我查一下"}]

    async def drive():
        async for _ev, *_rest in agent.run(messages):
            pass

    asyncio.run(drive())
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_msgs) == n
    total = sum(len(m["content"]) for m in tool_msgs)
    assert total <= TOOL_RESULT_MAX_CHARS, total
    joined = "".join(m["content"] for m in tool_msgs)
    assert "[head0]" in joined, "头部内容必须保留"
    assert f"[tail{n - 1}]" in joined, "尾部内容必须保留"
    assert "省略" in joined, "中间必须有明确省略标记"


def test_single_tool_timeout_is_reported():
    """单工具超时 → error_code='timeout'（不静默挂死）。"""
    import app.brain.agent as agent_mod

    old = agent_mod.TOOL_CALL_TIMEOUT_SECONDS
    agent_mod.TOOL_CALL_TIMEOUT_SECONDS = 0.2
    try:
        reg = ToolRegistry()
        reg.register(Tool(name="slow", description="d",
                          parameters={"type": "object", "properties": {}},
                          fn=lambda: time.sleep(3) or "太慢了"))
        agent = AgentLoop(client=None, registry=reg)
        envs: list = []

        async def drive():
            async for ev, *rest in agent._execute_tools_parallel(
                    [{"id": "c1", "name": "slow", "arguments": "{}"}],
                    [], out_envelopes=envs):
                pass

        asyncio.run(drive())
        assert envs, "应产出工具结果"
        env = envs[0][2]
        assert env["error_code"] == "timeout", env
        assert env["status"] == "error", env
    finally:
        agent_mod.TOOL_CALL_TIMEOUT_SECONDS = old


def test_tool_phase_budget_stops_further_tools():
    """整轮工具阶段预算用尽 → 停止执行新工具，并可观测（meta 事件 + 错误码）。"""
    import app.brain.agent as agent_mod

    old = agent_mod.TOOL_PHASE_BUDGET_SECONDS
    agent_mod.TOOL_PHASE_BUDGET_SECONDS = 0.3
    try:
        reg = ToolRegistry()
        for i in range(3):
            reg.register(Tool(
                name=f"slow_{i}", description="d",
                parameters={"type": "object", "properties": {}},
                fn=lambda: time.sleep(2) or "慢"))
        agent = AgentLoop(client=None, registry=reg)
        envs: list = []
        metas: list = []

        async def drive():
            async for ev, *rest in agent._execute_tools_parallel(
                    [{"id": f"c{i}", "name": f"slow_{i}", "arguments": "{}"}
                     for i in range(3)],
                    [], out_envelopes=envs):
                if ev == "meta":
                    metas.append(rest[0])

        asyncio.run(drive())
        assert metas, "预算用尽必须可观测（meta 事件）"
        assert metas[0].get("event") == "tool_budget_exhausted", metas
        codes = {e[2]["error_code"] for e in envs}
        assert "budget_exhausted" in codes, envs
        assert all(e[2]["truncated"] is True for e in envs), envs
    finally:
        agent_mod.TOOL_PHASE_BUDGET_SECONDS = old


# =============================================================================
# 3.4 工具分组
# =============================================================================

def test_registry_groups_tools_by_unlock_level():
    """工具按能力分组（与 tool_unlock 的解锁层级对齐）。"""
    reg = _default_registry()
    groups = reg.grouped_names()
    assert len(groups) >= 5, groups
    assert sum(len(v) for v in groups.values()) == len(reg.names())
    assert "基础能力" in groups, groups


def test_tool_has_group_field():
    """Tool 数据类带 group 字段。"""
    t = Tool(name="x", description="d", parameters={}, fn=lambda: "ok")
    assert t.group == ""
    reg = ToolRegistry()
    reg.register(t)
    reg.ensure_groups()
    assert reg.get("x").group


def test_prompt_tool_list_is_grouped_not_flat():
    """【可用工具列表】按组展示，不再平铺工具名。"""
    reg = _default_registry()
    groups = reg.grouped_names()
    out = build_tool_list(groups)
    assert "【可用工具列表】" in out
    for name in groups:
        assert name in out, name
    # 平铺的工具名一个都不该出现
    for tool_name in reg.names():
        assert tool_name not in out, tool_name
    # 分组提示要告诉模型「按场景点名工具」
    assert "JSON schema" in out
    assert "哪一组" in out


def test_prompt_size_shrinks_after_grouping():
    """分组后提示词明显变小（原来平铺 61 个名字）。"""
    reg = _default_registry()
    flat = ("【可用工具列表】\n"
            f"你**真正可调用**的工具名（共 {len(reg.names())} 个）："
            + "、".join(reg.names()) + "。\n"
            "**何时调哪个工具**的规则见上面的 SKILL 文档。")
    grouped = build_tool_list(reg.grouped_names())
    # 平铺 61 个名字 → 10 行分组：明显变小（具体数字见交付报告）
    assert len(grouped) < len(flat) * 0.7, (len(grouped), len(flat))

# =============================================================================
# 3.5 标准后端（LangChain）的白名单可达性 + UI 侧状态判定
# =============================================================================

#: 故意从执行期白名单里剔掉的工具（真工具，不是假名字）
EXCLUDED_TOOL = "kill_process"
#: 子 agent 那条路上要用的**非危险**工具（危险工具本来就不进子 agent 的工具集，
#: 用它们测不出白名单有没有生效）
EXCLUDED_SAFE_TOOL = "ocr_image"


def _policy_without(monkeypatch, drop_exact=(), drop_prefixes=()):
    """把某些工具从执行期白名单里剔掉（模拟主人改了 yaml 的 tool_allowlist）。"""
    from app.engine.tools import _core
    orig = _core.load_tool_allowlist

    def fake(*a, **k):
        p = orig(force=True)
        return _core.AllowlistPolicy(
            exact=frozenset(p.exact) - set(drop_exact),
            prefixes=tuple(p for p in p.prefixes if p not in drop_prefixes),
            from_yaml=True,
        )

    monkeypatch.setattr(_core, "load_tool_allowlist", fake)


def test_excluded_tool_is_not_offered_to_the_standard_backend(monkeypatch):
    """标准后端：白名单外的工具**不下发**给模型（tools_for_level 就滤掉了）。"""
    _policy_without(monkeypatch, drop_exact={EXCLUDED_TOOL})
    from app.brain.langchain_agent import convert_tools_for_level

    reg = _default_registry()
    reg.seal_baseline()
    names = {t.name for t in convert_tools_for_level(reg, 10)}
    assert EXCLUDED_TOOL not in names
    assert "open_app" in names


def test_handcrafted_tool_call_cannot_reach_excluded_tool(monkeypatch):
    """模型（哪怕被伪造 / 被诱导）硬点名白名单外的工具，也执行不到。

    端到端：真 create_agent + 假模型发 tool_call → LangChain 的 ToolNode
    根本没收到这个工具（下发时就被滤掉了）→ 报错，不执行工具本体。
    """
    _policy_without(monkeypatch, drop_exact={EXCLUDED_TOOL})
    from app.brain.langchain_agent import convert_tools_for_level
    from langchain.agents import create_agent
    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    called: list = []
    reg = _default_registry()
    reg.seal_baseline()
    base_fn = reg.get(EXCLUDED_TOOL).fn

    def _spy(*a, **k):
        called.append(EXCLUDED_TOOL)
        return base_fn(*a, **k)

    reg.get(EXCLUDED_TOOL).fn = _spy

    class _FakeModel(BaseChatModel):
        n: int = 0

        @property
        def _llm_type(self) -> str:
            return "fake"

        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            self.n += 1
            if self.n == 1:
                msg = AIMessage(content="", tool_calls=[{
                    "id": "c1", "name": EXCLUDED_TOOL,
                    "args": {"name": "notepad"}, "type": "tool_call"}])
            else:
                msg = AIMessage(content="FINAL")
            return ChatResult(generations=[ChatGeneration(message=msg)])

    lc_tools = convert_tools_for_level(reg, 10)
    assert EXCLUDED_TOOL not in {t.name for t in lc_tools}
    agent = create_agent(model=_FakeModel(), tools=lc_tools, system_prompt="x")
    out = agent.invoke({"messages": [{"role": "user", "content": "go"}]})
    tool_msgs = [m for m in out["messages"] if type(m).__name__ == "ToolMessage"]
    assert tool_msgs, out["messages"]
    assert "not a valid tool" in tool_msgs[0].content, tool_msgs[0].content
    assert called == [], "白名单外的工具被执行了！"


def test_subagent_registry_honors_execution_allowlist(monkeypatch):
    """Task 派出去的子 agent 也受执行期白名单约束（Stage 3.5 补上的洞）。

    没 seal 的注册表走「快照兜底」分支 —— 白名单对它不生效：
    主 agent 被挡掉的工具，子 agent 照样能调。
    """
    _policy_without(monkeypatch, drop_exact={EXCLUDED_SAFE_TOOL})
    from app.brain.langchain_agent import LangChainAgent, convert_tools_for_level

    reg = _default_registry()
    reg.seal_baseline()

    class _FakeSelf:
        registry = reg

    sub_reg = LangChainAgent._build_subagent_registry(_FakeSelf())
    assert sub_reg._sealed is True, "子注册表没 seal → 白名单对它不生效"
    assert sub_reg._allowlist_allows(EXCLUDED_SAFE_TOOL)[0] is False
    names = {t.name for t in convert_tools_for_level(sub_reg, 10)}
    assert EXCLUDED_SAFE_TOOL not in names
    assert "web_search" in names, "子 agent 的工具集不能被清空"


def test_aw_prefetch_respects_execution_allowlist(monkeypatch):
    """AW 预取走集成层（不经过注册表）→ 必须自己查一次白名单。"""
    from app.brain.langchain_agent import LangChainAgent

    reg = _default_registry()
    reg.seal_baseline()

    class _FakeSelf:
        registry = reg

    fake = _FakeSelf()
    name = "mcp_aw_aw_get_recent_activities"
    assert LangChainAgent._aw_tool_allowed(fake, name) is True
    _policy_without(monkeypatch, drop_prefixes=("mcp_aw_",))
    assert LangChainAgent._aw_tool_allowed(fake, name) is False


def test_ui_tool_status_uses_the_shared_classifier():
    """UI 侧不再自己猜成败：走脑层 _tool_result_state（工具层判定表的出口）。"""
    from app.ui.chat_window.utils import _tool_status

    assert _tool_status("操作成功") == "ok"
    assert _tool_status("错误：文件不存在") == "fail"
    # 成功、正文里提到「错误：」→ 不该判失败（旧表是子串匹配，会误判）
    assert _tool_status("正文里提到「错误：xxx」，但工具本身成功") == "ok"
    # 成功、正文里提到「取消」→ 不该判取消（旧表是裸「取消」子串，会误判）
    assert _tool_status("已取消静音") == "ok"
    # 真的被主人取消了 → cancel
    assert _tool_status("用户取消了此操作。") == "cancel"
    # 桌宠拒绝 → rejected
    assert _tool_status('{"status": "rejected", "rejected_by_companion": true}') == "rejected"


def test_ui_fail_markers_come_from_the_tool_layer():
    """UI 侧不再自己抄一份失败前缀表 —— 直接转发工具层那份。"""
    from app.engine.tools._core import _ERROR_PREFIXES
    from app.ui.chat_window.utils import _TOOL_FAIL_MARKERS

    assert tuple(_TOOL_FAIL_MARKERS) == tuple(_ERROR_PREFIXES)

