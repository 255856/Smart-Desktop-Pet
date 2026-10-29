# -*- coding: utf-8 -*-
"""工具风险等级表覆盖率回归测试。

钉死三件事:

1. **覆盖率**: live 注册表里的每个内置工具都在 TOOL_RISK_LEVEL 里有等级。
   漏一个 -> risk_for_tool 回落默认 MEDIUM (2) -> 风险表不再是单源真相,
   新工具会**悄悄**改变 confirm 行为 (要么多弹窗, 要么漏拦)。
2. **危险工具不漏 / 不降级**: agent.py 的 DANGEROUS_TOOLS (ReAct 后端自己认的
   危险名单) 里的工具, 在风险表里必须 >= HIGH (3), 一个都不能低于 HIGH。
3. **物理不可逆的两个名字**: shutdown_computer / delete_file 必须是
   IRREVERSIBLE (4), 并且必须出现在 hallucination_guard._RISKY_TOOLS 里
   (这两个是**预留名**, 工具函数还没实现, 但等级先定死, 模型真调这两个
   名字时一律走 confirm 流程)。

表里有、live 注册表里没有的名字**只允许**是下面这张表里逐条写明理由的
「有意保留」条目 (别名 / 预留名); MCP 工具 (mcp_* 前缀) 运行时才桥接进来,
按 _risk.py 自己的规矩**不进**静态风险表。
"""
from __future__ import annotations

from app.brain import hallucination_guard
from app.brain.agent import DANGEROUS_TOOLS
from app.engine.tools import _risk
from app.engine.tools._risk import HIGH, IRREVERSIBLE, LOW, MEDIUM, SAFE


# 表里有、live 注册表里没有的名字 —— 每一个都必须**有意登记**并写明理由,
# 否则就是幽灵工具漂移 (思路跟 scripts/check_tool_docs.py 的 ALLOWLIST 一致)。
INTENTIONALLY_NOT_REGISTERED: dict[str, str] = {
    "send_email": "工具本体从未实现; 风险表 / hallucination_guard / settings_window 仍在引用",
    "take_screenshot": "别名: 真实注册名是 take_screenshot_tool, 表里两个名字都留着",
    "shutdown_computer": "预留未实现 (docs/tools-reference.md 写明本版本不做); 等级先定死",
    "delete_file": "工具本体不存在; 风险表按 RISK_RATIONALE[4]「文件物理删除」占档",
}


class _StubState:
    """最小 state stub: 注册阶段用不到任何属性 (同 scripts/check_tool_docs.py)。"""

    def get(self, *a, **k):
        return None

    def stats_summary(self):
        return "stub"


def _live_builtin_names() -> list[str]:
    """live 内置工具名 —— 从**真实注册表**取, 不猜。"""
    from app.engine.tools import build_default_tools

    reg = build_default_tools(state=_StubState(), reminders=None,
                              memory=None, items=None)
    return sorted(reg._tools)


def test_every_live_builtin_tool_has_a_risk_level():
    """覆盖率: 每个内置工具都有等级 (A1)。"""
    live = set(_live_builtin_names())
    missing = sorted(n for n in live if n not in _risk.TOOL_RISK_LEVEL)
    assert not missing, (
        "这些内置工具在 TOOL_RISK_LEVEL 里没有风险等级: " + str(missing)
    )


def test_table_only_holds_builtin_tools_or_documented_exceptions():
    """表里不许混进 MCP 工具; 表外残留的名字必须逐条登记过。"""
    live = set(_live_builtin_names())
    extra = sorted(n for n in _risk.TOOL_RISK_LEVEL if n not in live)
    mcp_in_table = [n for n in extra if n.startswith("mcp_")]
    assert not mcp_in_table, (
        "MCP 工具运行时才桥接, 不进静态风险表: " + str(mcp_in_table)
    )
    undocumented = [n for n in extra if n not in INTENTIONALLY_NOT_REGISTERED]
    assert not undocumented, (
        "风险表里有 live 注册表没有、且没登记理由的名字: " + str(undocumented)
    )


def test_every_table_level_is_valid():
    """等级只能是 0-4 五档。"""
    for name, level in _risk.TOOL_RISK_LEVEL.items():
        assert isinstance(level, int), f"{name} 的等级不是 int: {level!r}"
        assert level in (SAFE, LOW, MEDIUM, HIGH, IRREVERSIBLE), (
            f"{name} 的等级非法: {level!r}"
        )


def test_dangerous_tools_are_at_least_high():
    """DANGEROUS_TOOLS 里的工具不得低于 HIGH (3), 也不许没等级。"""
    table = _risk.TOOL_RISK_LEVEL
    missing = sorted(n for n in DANGEROUS_TOOLS if n not in table)
    assert not missing, "危险工具在风险表里没有等级: " + str(missing)
    too_low = sorted(n for n in DANGEROUS_TOOLS if table[n] < HIGH)
    assert not too_low, "危险工具的等级低于 HIGH: " + str(too_low)


def test_shutdown_computer_is_irreversible():
    """shutdown_computer: 关机 / 重启 = 物理不可逆 -> IRREVERSIBLE (4)。"""
    assert _risk.TOOL_RISK_LEVEL.get("shutdown_computer") == IRREVERSIBLE, (
        "shutdown_computer 应 IRREVERSIBLE, 实际: "
        + repr(_risk.TOOL_RISK_LEVEL.get("shutdown_computer"))
    )


def test_delete_file_is_irreversible():
    """delete_file: 文件物理删除 -> IRREVERSIBLE (4)。"""
    assert _risk.TOOL_RISK_LEVEL.get("delete_file") == IRREVERSIBLE, (
        "delete_file 应 IRREVERSIBLE, 实际: "
        + repr(_risk.TOOL_RISK_LEVEL.get("delete_file"))
    )


def test_irreversible_names_are_in_risky_tools():
    """_RISKY_TOOLS 从风险表派生 -> 这两个名字必须在里面 (A3)。"""
    risky = hallucination_guard._RISKY_TOOLS
    assert "shutdown_computer" in risky, (
        "shutdown_computer 必须在 _RISKY_TOOLS 里, 否则 confirm 协议拦不住它"
    )
    assert "delete_file" in risky, (
        "delete_file 必须在 _RISKY_TOOLS 里, 否则 confirm 协议拦不住它"
    )
