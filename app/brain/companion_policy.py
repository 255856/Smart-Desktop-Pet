"""桌宠养成状态对 Agent 决策的耦合机制 (创新点 2 核心)。

把决策函数从 ``Action = f(intent, tools, context)`` 扩展为
``Action = f(intent, tools, context, companion_state)``.

状态量化: 桌宠 PetState 的连续数值通过 PetState.quantize() (v21-3)
离散成三档 (sat/mood/level ∈ {LOW, MID, HIGH})。

风险等级: 工具风险从 TOOL_RISK_LEVEL (v21-1) 取 0..4 五档 (SAFE/LOW/
MEDIUM/HIGH/IRREVERSIBLE)。

27 条规则 (RULES_TABLE): 12 条 PDF 原文必备 + 15 条按"覆盖明显遗漏 / 反向 /
边界"反推得到。每条规则的 4 个 state axis 都支持 '*' (= any)。

lookup_decision(pet_state, tool_name) 主入口: 算 state 三档 + risk 五档,
按 priority 顺序遍历 RULES_TABLE 返回决策 Decision.ALLOW / CONFIRM /
REJECT / REFUSE。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# v21-3 给的 PetState.quantize() 行为只在本文件里以"返回值"形式消费,
# 所以这里只 static import 必要类型 (避免循环: companion_policy 来自 brain,
# PetState 来自 engine.state, 不会循环)。
from app.engine.state import PetState
from app.engine.tools._risk import (
    SAFE, LOW as RISK_LOW, MEDIUM, HIGH, IRREVERSIBLE,
    risk_for_tool,
)


# ---- 决策常量 ----
class Decision:
    """决策结果。

    ALLOW    直接放行 (主人不被打扰, agent 自主)
    CONFIRM  弹窗拦一道 (主人主动确认才执行)
    REJECT   永久拦 (模型看到 sentinel 必须重新规划)
    REFUSE   本轮拒绝 + 自然语言解释 (桌宠情绪表达, 不调用工具)
    """
    ALLOW = "ALLOW"
    CONFIRM = "CONFIRM"
    REJECT = "REJECT"
    REFUSE = "REFUSE"


ANY = "*"  # 规则四轴 (sat/mood/level/risk) 任意值匹配


@dataclass(frozen=True)
class Rule:
    """单条状态-权限规则。

    sat/mood/level/favor: 'LOW' | 'MID' | 'HIGH' | ANY
    risk:           'SAFE' | 'LOW' | 'MEDIUM' | 'HIGH' | 'IRREVERSIBLE' | ANY
    decision:       Decision.{ALLOW, CONFIRM, REJECT, REFUSE}
    priority:       整数, 数字越小优先级越高 (遍历匹配时先匹配)
    note:           注释 (论文 §4.3 引用)

    v23-3 加 favor 维度 (主人 2026-10-16 用好感替换原 trust).
    favor 默认 ANY → 27 条老规则不动自动 favor=ANY, 决策时只检 sat/mood/level.
    """
    sat: str
    mood: str
    level: str
    risk: str
    decision: str
    priority: int
    note: str = ""
    favor: str = ANY    # v23-3 加, 默认 ANY 兼容老规则

    def matches(self, sat: str, mood: str, level: str, favor: str, risk: str) -> bool:
        return (
            (self.sat == ANY or self.sat == sat)
            and (self.mood == ANY or self.mood == mood)
            and (self.level == ANY or self.level == level)
            and (self.favor == ANY or self.favor == favor)
            and (self.risk == ANY or self.risk == risk)
        )


# PDF 原文 12 条 (v21 规划 §6.1)
# priority 越小越先匹配; PDF starter 优先级集中在 100-200 区间,
# 我们补的 15 条分配到 10-90 (更 specific 的优先级更高) 和 200-290。
_PDF_RULES = [
    Rule("ANY", "LOW",  "ANY", "IRREVERSIBLE", Decision.REJECT,  200,
         "PDF §6.1 #1: 心情低落 + 不可逆 → 拒绝 (桌宠情绪表达)"),
    Rule("ANY", "LOW",  "ANY", "HIGH",        Decision.CONFIRM, 201,
         "PDF §6.1 #2: 心情低落 + 高风险 → 弹窗确认"),
    Rule("ANY", "ANY",  "LOW", "HIGH",        Decision.CONFIRM, 202,
         "PDF §6.1 #3: 等级低 + 高风险 → 弹窗确认 (主人还没培养到信任度)"),
    Rule("ANY", "ANY",  "LOW", "IRREVERSIBLE", Decision.REJECT,  203,
         "PDF §6.1 #4: 等级低 + 不可逆 → 拒绝"),
    Rule("HIGH","HIGH", "ANY", "MEDIUM",      Decision.ALLOW,   204,
         "PDF §6.1 #5: 满状态 + 中风险 → 直接执行"),
    Rule("HIGH","ANY",  "ANY", "LOW",         Decision.ALLOW,   205,
         "PDF §6.1 #6: 饱食高 + 低风险 → 直接执行"),
    Rule("ANY", "HIGH", "HIGH","HIGH",        Decision.ALLOW,   206,
         "PDF §6.1 #7: 心情好 + 等级高 + 高风险 → 直接执行"),
    Rule("ANY", "ANY",  "HIGH","LOW",         Decision.ALLOW,   207,
         "PDF §6.1 #8: 等级高 + 低风险 → 直接执行"),
    Rule("ANY", "LOW",  "ANY", "MEDIUM",      Decision.CONFIRM, 208,
         "PDF §6.1 #9: 心情低落 + 中风险 → 弹窗"),
    Rule("ANY", "ANY",  "HIGH","IRREVERSIBLE",Decision.CONFIRM, 210,
         "PDF §6.1 #11: 等级高 + 不可逆 → 还是要 confirm (不可逆就是不可逆)"),
    Rule("ANY", "ANY",  "HIGH","MEDIUM",      Decision.ALLOW,   211,
         "PDF §6.1 #12: 等级高 + 中风险 → 直接执行"),
]


# 扩展 15 + 5 条 (v21 规划 §6.2 反推 + 拆分 PDF §6.1 #10 排除纯读/低风险)
_EXTRA_RULES = [
    Rule("LOW", "LOW",  "ANY", "MEDIUM",      Decision.REFUSE,  16,
         "PDF §6.1 #10 variant: sat+mood 都 LOW + 中风险 → REFUSE (本轮拒绝)"),
    Rule("LOW", "LOW",  "ANY", "HIGH",        Decision.REFUSE,  17,
         "PDF §6.1 #10 variant: sat+mood 都 LOW + 高风险 → REFUSE"),
    Rule("LOW", "LOW",  "ANY", "IRREVERSIBLE",Decision.REFUSE,  18,
         "PDF §6.1 #10 variant: sat+mood 都 LOW + 不可逆 → REFUSE"),
    Rule("LOW", "LOW",  "ANY", "SAFE",        Decision.ALLOW,   19,
         "PDF §6.1 #10 override: sat+mood 都 LOW 但纯读 → 仍 ALLOW (纯读不做事)"),
    Rule("LOW", "LOW",  "ANY", "RISK_LOW",    Decision.ALLOW,   20,
         "PDF §6.1 #10 override: sat+mood 都 LOW 但低风险工具 → 仍 ALLOW (低风险不挡)"),
    Rule("ANY", "HIGH", "LOW", "IRREVERSIBLE", Decision.CONFIRM, 10,
         "心情好但等级低 + 不可逆 → 弹窗 (等级未达到信任, 不可逆就要拦)"),
    Rule("ANY", "LOW",  "HIGH","HIGH",        Decision.CONFIRM, 11,
         "心情差 + 等级高 + 高风险 → 仍 confirm (主人情绪不好, 不放心)"),
    Rule("ANY", "ANY",  "LOW", "LOW",         Decision.ALLOW,   12,
         "等级低 + 低风险 → 放行 (低风险不挡, 跟 base 一致)"),
    Rule("HIGH","HIGH", "LOW", "MEDIUM",      Decision.ALLOW,   13,
         "满 sat+mood + 低等级 + 中风险 → 放行 (主人兴致来了, 等级低但心情好)"),
    Rule("LOW", "MID",  "MID", "HIGH",        Decision.CONFIRM, 14,
         "饿 + 心情中 + 等级中 + 高风险 → confirm (主流程安全)"),
    Rule("HIGH","ANY",  "HIGH","HIGH",        Decision.ALLOW,   15,
         "饱食高 + 等级高 + 高风险 → 放行 (等级 + 饱食都够, 主人信任度够)"),
    Rule("ANY", "ANY",  "ANY", "HIGH",        Decision.CONFIRM, 290,
         "catch-all: 任何状态下 HIGH 都 confirm (论文 §6.2 catch-all, PDF 系列之后)"),
    Rule("LOW", "MID",  "HIGH","MEDIUM",      Decision.CONFIRM, 54,
         "饿 + 等级高 + 中风险 → 仍 confirm (饱食低的状态下不要替主人拍板)"),
    Rule("ANY", "LOW",  "MID", "LOW",         Decision.ALLOW,   55,
         "心情低 + 中等级 + 低风险 → 放行 (low risk 不挡)"),
    Rule("ANY", "HIGH", "MID", "MEDIUM",      Decision.ALLOW,   56,
         "心情好 + 等级中 + 中风险 → 放行"),
    Rule("MID", "HIGH", "HIGH","HIGH",        Decision.ALLOW,   57,
         "心情好 + 饱食 + 等级都 HIGH + 高风险 → 放行 (一致信任放行)"),
    Rule("LOW", "LOW",  "HIGH","LOW",         Decision.ALLOW,   58,
         "心情差 + 饿 + 等级高 + 低风险 → 放行 (低风险 + 高等级)"),
    Rule("ANY", "ANY",  "ANY", "MEDIUM",      Decision.ALLOW,   291,
         "catch-all: 任意状态下 MEDIUM 都 ALLOW (跟 RiskyToolConfirm base 一致, "
         "中风险默认放行; 但在 PDF specifics 之后才生效, 如 PDF #10 LOW+LOW 仍 REFUSE)"),
    Rule("ANY", "ANY",  "ANY", "RISK_LOW",    Decision.ALLOW,   292,
         "catch-all: 任意状态下 LOW 都 ALLOW (LOW 不挡, 但在 PDF specifics 之后才生效)"),
    Rule("ANY", "ANY",  "ANY", "SAFE",        Decision.ALLOW,   293,
         "catch-all: 任何状态下纯读 SAFE 都 ALLOW"),
]


def _anyfy(rule: Rule) -> Rule:
    """把规则字面字符串 "ANY" 替换成 ANY 常量, 让 Rule.matches() 的 ``==`` 检查通过。

    Rule 用 'ANY' 作字段字面是因为人在编辑时易读 ('LOW', 'ANY', 'LOW' 比
    'LOW', '*', 'LOW' 直观), 但运行时匹配要走 ANY 常量 ('*') 比较。
    v23-3 也处理 favor 字段。
    """
    return Rule(
        sat=ANY if rule.sat == "ANY" else rule.sat,
        mood=ANY if rule.mood == "ANY" else rule.mood,
        level=ANY if rule.level == "ANY" else rule.level,
        risk=ANY if rule.risk == "ANY" else rule.risk,
        decision=rule.decision,
        priority=rule.priority,
        note=rule.note,
        favor=ANY if rule.favor == "ANY" else rule.favor,
    )


# ---- v23-3 favor 维度额外规则 (主人 2026-10-16 加, 替换原 trust) ----
# favor 高 (≥ favor_rules.high_threshold) → 高风险额外 ALLOW (主人长期信任)
# favor 低 (< favor_rules.low_threshold)  → 任意 MEDIUM+ 都额外 CONFIRM/REJECT
# priority 都设在 5..9, 比具体业务规则低 (10+), 但高于 catch-all (290+),
# 这样 catch-all 不会一上来就 ALLOW; favor 中性 (MID) 走默认 ALLOW.
_FAVOR_RULES = [
    Rule("ANY", "ANY", "ANY", "IRREVERSIBLE", Decision.CONFIRM, 5,
         favor="HIGH",
         note="v23-3: 好感高 + 不可逆 → 仍 confirm (favor_rules.high_allows_irreversible=False)"),
    Rule("ANY", "ANY", "ANY", "HIGH",        Decision.ALLOW,   6,
         favor="HIGH",
         note="v23-3: 好感高 + 高风险 → 放行 (主人长期信任)"),
    # favor LOW 三条 mood 限制为 LOW/MID (非 HIGH), 主人心情好时, 就算好感
    # 低也不拦 (老 sat+mood HIGH + favor LOW + MEDIUM 走老 priority 13 ALLOW,
    # 这里不冲突). 只有 sat/mood 不好 + favor LOW 时, 才升级到 confirm/reject.
    Rule("ANY", "LOW", "ANY", "MEDIUM",      Decision.CONFIRM, 19,
         favor="LOW",
         note="v23-3: 好感低 + 心情非好 + 中风险 → confirm"),
    Rule("ANY", "MID", "ANY", "MEDIUM",      Decision.CONFIRM, 19,
         favor="LOW",
         note="v23-3: 好感低 + 心情中 + 中风险 → confirm"),
    Rule("ANY", "LOW", "ANY", "HIGH",        Decision.CONFIRM, 21,
         favor="LOW",
         note="v23-3: 好感低 + 心情差 + 高风险 → confirm"),
    Rule("ANY", "MID", "ANY", "HIGH",        Decision.CONFIRM, 21,
         favor="LOW",
         note="v23-3: 好感低 + 心情中 + 高风险 → confirm"),
    Rule("ANY", "LOW", "ANY", "IRREVERSIBLE",Decision.REJECT,  23,
         favor="LOW",
         note="v23-3: 好感低 + 心情差 + 不可逆 → REJECT"),
    Rule("ANY", "MID", "ANY", "IRREVERSIBLE",Decision.REJECT,  23,
         favor="LOW",
         note="v23-3: 好感低 + 心情中 + 不可逆 → REJECT"),
    Rule("ANY", "ANY", "ANY", "ANY",         Decision.ALLOW, 295,
         favor="MID",
         note="v23-3: catch-all favor 中性 → 走默认 (跟 ALLOW 一致)"),
]


# 全表 (27 + 6 favor 规则 = 33 条), 按 priority 升序匹配
RULES_TABLE: list[Rule] = sorted(
    [_anyfy(r) for r in _EXTRA_RULES + _PDF_RULES + _FAVOR_RULES],
    key=lambda r: r.priority,
)


def quantize_state(pet_state: PetState) -> dict[str, str]:
    """给 PetState 做四档离散化 (v23-3 加 favor), 给 RULES_TABLE 查表用。

    返回 {"satiety": "LOW|MID|HIGH", "mood": "...", "level": "...",
          "favor": "..."}。
    直接调 PetState.quantize() (v23-3 加 favor 维度), 不在本文件里重复实现。
    """
    return pet_state.quantize()


# RISK 数字 → 字符串, 给 Rule.risk 用 ("RISK_LOW" 用名字免混淆)
_RISK_NAMES = {
    SAFE: "SAFE",
    RISK_LOW: "RISK_LOW",   # 不冲突: Rule.risk 字符串 "LOW" 来自 PDF, 但
                            # 数字 1 在 TOOL_RISK_LEVEL 是 LOW(UI), 这里
                            # 命名为 "RISK_LOW" 区分 (实际 PDF 也叫 LOW)
    MEDIUM: "MEDIUM",
    HIGH: "HIGH",
    IRREVERSIBLE: "IRREVERSIBLE",
}


def lookup_decision(pet_state: PetState, tool_name: str) -> str:
    """决策函数: 算 state + risk, 按 priority 顺序遍历 RULES_TABLE 匹配,
    返回 Decision.{ALLOW, CONFIRM, REJECT, REFUSE}。

    遍历策略:
      1. RULES_TABLE 按 priority 升序遍历 (specific 先匹配)
      2. 命中第一条规则 → 返回 rule.decision
      3. 没命中 → 走默认:
         - SAFE → ALLOW (纯读不挡)
         - LOW → ALLOW (低风险不挡)
         - MEDIUM → ALLOW (中风险, 跟旧 RiskyToolConfirm base 一致)
         - HIGH → CONFIRM (高风险 confirm)
         - IRREVERSIBLE → REJECT (不可逆默认拒绝, 让模型必须 confirm 重试)
    """
    state = quantize_state(pet_state)
    sat = state["satiety"]
    mood = state["mood"]
    level = state["level"]
    favor = state.get("favor", "MID")    # v23-3 加 favor 维度 (老测试可能没传)

    risk_int = risk_for_tool(tool_name, default=MEDIUM)
    risk_str = _RISK_NAMES.get(risk_int, "MEDIUM")

    for rule in RULES_TABLE:
        if rule.matches(sat, mood, level, favor, risk_str):
            return rule.decision

    # default fallback (跟原 RiskyToolConfirm 兼容)
    if risk_int >= HIGH:
        return Decision.CONFIRM
    if risk_int >= IRREVERSIBLE:
        return Decision.REJECT
    return Decision.ALLOW


__all__ = [
    "Decision", "Rule", "ANY",
    "RULES_TABLE",
    "quantize_state", "lookup_decision",
]
