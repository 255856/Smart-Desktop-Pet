#!/usr/bin/env python3
"""工具清单对账（Stage 0.3 建立，Stage 3.5 扩覆盖面）：live registry vs 5 个文件里的 8 处名字来源。

对账的 5 个文件
--------------
1. docs/tools-reference.md                          面向人的工具手册
2. .agents/skills/desktop-pet-tool-usage/SKILL.md   喂给模型的 skill
3. app/engine/tools/_risk.py                        每个工具的风险等级
4. app/brain/agent.py                               agent 侧的工具名引用
5. app/core/companion_config.yaml                   执行期白名单（tool_allowlist 段）

一个文件里可能有**多处**名字清单（agent.py 就有 4 处），每一处单独对账，
报告按「来源」分组 —— 每条差异都写明来自哪个文件的哪一处，
改名 / 漏收 / 多写都躲不过。

执行期白名单单独对账（Stage 3.5）
--------------------------------
yaml 的 tool_allowlist 段（exact 精确名 + prefixes 前缀桶）决定工具在**执行期**
能不能跑，所以它跟注册表必须**双向**对得上：

  · 注册表里有、白名单没覆盖 → 执行期会被拒（部署 bug，**阻断**）；
  · 白名单里有、注册表没有   → 死策略条目（只报告，不阻断）。

有意为之的差异仍然只走 ALLOWLIST（带理由），不另开第二个口子。

地面真值（ground truth）
-----------------------
live = app/engine.tools.build_default_tools() 注册的内置工具
     + 运行时由 MCP 桥接进来的工具（每个启用的 MCP server 一组）
         mcp_<yaml 里的 alias>_<server 的工具名>
       alias 来自 mcp_servers.yaml 的 name（app/mcp/protocol.py 的 start_all
       用它当 key），工具名来自对应 server 模块的 TOOLS 常量。
       shipped 配置 = aw（app/mcp/activitywatch_server.py）4 个
                    + fs（app/mcp/filesystem_server.py）3 个 = 7 个。

用法
----
    python scripts/check_tool_docs.py            人看的分组报告
    python scripts/check_tool_docs.py --json     机器读（JSON）
    python scripts/check_tool_docs.py --strict-docs
        额外把「注册表里有、文档里没收录」也当成错误（默认只报告）

退出码
------
0  没有无法解释的差异（白名单里的有意差异不算）
1  有无法解释的差异

白名单
------
见下面 ALLOWLIST：每条带一行理由。没有理由的差异就是 bug，不许进白名单。

红线：脚本**不许**把 Qt 拉进来（对账是纯文本活儿，CI 上也只跑它）。
main() 每次都会检查 sys.modules 里有没有 Qt 绑定 —— 有就当错误报出来。
"""
from __future__ import annotations

import argparse
import importlib
import json
import logging
import re
import sys
from pathlib import Path
from typing import NamedTuple, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

log = logging.getLogger(__name__)

DOC_PATH = ROOT / "docs" / "tools-reference.md"
SKILL_PATH = ROOT / ".agents" / "skills" / "desktop-pet-tool-usage" / "SKILL.md"
AW_SERVER_PATH = ROOT / "app" / "mcp" / "activitywatch_server.py"
MCP_SERVERS_PATH = ROOT / "mcp_servers.yaml"
CONFIG_PATH = ROOT / "app" / "core" / "companion_config.yaml"

# 执行期白名单那条的来源 key（错误文案要专门写「执行期会被拒」）
ALLOWLIST_SOURCE_KEY = "app/core/companion_config.yaml tool_allowlist"

# Qt 绑定：脚本一旦把它们拉进来就说明哪里写错了（对账不该碰 GUI）
QT_ROOTS = ("pyqt5", "pyqt6", "pyside2", "pyside6")

# 运行时由 MCP 桥接进来的工具：mcp_<yaml 里的 alias>_<server 的工具名>。
#
# 命名链路（app/mcp/protocol.py）：start_all() 用**配置里的 alias**当 key
# （self._clients[name] = client，name 来自 mcp_servers.yaml 的 name），
# all_tools() 把这个 alias 当 server，bridge_to() 拼
# local_name = f"mcp_{mcp_tool.server}_{mcp_tool.name}"。
# 所以 alias 写错 / server 没启用，暴露出来的工具名就跟文档对不上。
#
# mcp_servers.yaml 是 .gitignore 的本机文件，CI 上没有 -> 用下面这张默认表兜底。
# 默认表必须跟 shipped 配置一致：aw 4 个 + fs 3 个 = 7 个。
DEFAULT_MCP_SERVERS: dict = {
    "aw": "app.mcp.activitywatch_server",
    "fs": "app.mcp.filesystem_server",
}

# 有意的差异：这些名字在**某一份**参考资料里出现，注册表里故意没有 / 故意换个名。
# 按来源分开写：同一个名字在别的来源里出现仍然算差异（比如文档里写
# take_screenshot 就是错的 —— 真实名字是 take_screenshot_tool）。
# 每条必须写清理由 —— 没理由的差异就是 bug，不许进白名单。
ALLOWLIST: dict[str, dict[str, str]] = {
    "docs/tools-reference.md": {},
    ".agents/skills/desktop-pet-tool-usage/SKILL.md": {},
    "app/engine/tools/_risk.py TOOL_RISK_LEVEL": {
        "send_email": "从未实现：_risk.py / hallucination_guard.py / settings_window.py "
                      "里的历史引用，工具本体不存在（改这些文件不在本阶段范围）",
        "take_screenshot": "别名：真实注册名是 take_screenshot_tool，_risk.py 里两个名字"
                           "都留着（表里带注释说明）",
        "shutdown_computer": "预留未实现(docs/tools-reference.md 写明本版本不做这个工具函数): "
                             "风险表先给它 IRREVERSIBLE, 真实现时等级不用再改",
        "delete_file": "工具本体不存在(仓库里只在 tests/stress_run.py 的危险名清单里出现): "
                       "风险表按 RISK_RATIONALE[4]「文件物理删除」占 IRREVERSIBLE 档",
    },
    "app/brain/agent.py DANGEROUS_TOOLS": {
        "shutdown_computer": "预留未实现：agent.py 的 DANGEROUS_TOOLS 里留着占位，"
                             "对应工具函数故意没写（docs/tools-reference.md 有说明）",
    },
    "app/brain/agent.py _INTENT_TOOL_NAMES": {
        "mcp_aw_aw_browser_tabs": "代码侧旧名：真实工具是 mcp_aw_aw_get_browser_tabs"
                                  "（app/mcp/activitywatch_server.py:245）。本阶段不改代码，"
                                  "先让这处漂移在 CI 里可见（后续阶段修）",
        "take_screenshot": "代码侧旧名：真实工具是 take_screenshot_tool"
                           "（同上，_risk.py 里两个名字都留着）",
    },
    # _TOOL_PRETTY 只是「工具名 -> 中文标签」的兜底映射（agent.py 空总结时用）。
    # 以前这里混着一批早就没有对应工具的旧名（take_screenshot / calculator /
    # get_system_info / memory_search / web_deep_search / set_alarm / set_timer /
    # screen_capture / add_todo），当时用白名单收着。
    # Stage 3.5：那些名字**已经改掉了**（take_screenshot_tool / calculate /
    # system_info / recall_memory …），差异不再存在 → 白名单清空。
    # 往后这里再冒出「只有资料里有」的名字，就是真漂移，必须报出来。
    "app/brain/agent.py _TOOL_PRETTY": {},
    "app/brain/agent.py _INTENT_BLOCKED_TOOLS": {},
    # 执行期白名单这条来源：有意为之的差异登记处。
    # 例：某个工具**故意**不进执行期白名单（运行期就该被拒）→ 在这里登记
    # 并写清理由，对账就不会把它当成部署 bug 报出来。
    "app/core/companion_config.yaml tool_allowlist": {},
}

# 幽灵工具名 -> 已知还在引用它们的位置（只报告，不阻断）
PHANTOM_REFS: dict[str, tuple[str, ...]] = {
    "send_email": ("app/engine/tools/_risk.py", "app/brain/hallucination_guard.py",
                   "app/ui/settings_window.py", "app/engine/tools/_subagent.py"),
    "shutdown_computer": ("app/brain/agent.py", "app/brain/hallucination_guard.py",
                          "app/ui/chat_window/utils.py"),
}


class _StubState:
    """最小 state stub：注册阶段用不到任何属性。"""

    def get(self, *a, **k):
        return None

    def stats_summary(self) -> str:
        return "stub"


def build_live_registry():
    """live 注册表（内置工具）。"""
    from app.engine.tools import build_default_tools
    return build_default_tools(
        state=_StubState(), reminders=None, memory=None, items=None
    )


def _module_of_server(cfg: dict) -> Optional[str]:
    """从 server 配置里认出它跑的是哪个 app.mcp.* 模块。

    mcp_servers.yaml 里写的是 command: python / args: ["-m", "app.mcp.xxx", ...]，
    模块名就在 args 里 —— 按 args 认，不按 alias 猜（alias 是配置里给人起的名字，
    模块才是代码事实；alias 决定暴露出来的前缀，模块决定工具名）。
    """
    args = cfg.get("args") or []
    for i, a in enumerate(args):
        if a == "-m" and i + 1 < len(args):
            mod = str(args[i + 1])
            if mod.startswith("app.mcp."):
                return mod
        elif isinstance(a, str) and a.startswith("app.mcp."):
            return a
    return None


def mcp_servers() -> dict:
    """alias -> server 模块。

    读 mcp_servers.yaml 里 enabled: true 的 server；文件不在（CI 没这个本机文件）
    或认不出模块时，用 DEFAULT_MCP_SERVERS 兜底。
    """
    servers: dict = {}
    if MCP_SERVERS_PATH.is_file():
        try:
            import yaml
            data = yaml.safe_load(MCP_SERVERS_PATH.read_text("utf-8")) or {}
            for s in data.get("servers", []) or []:
                if not s.get("enabled", True):
                    continue
                alias = str(s.get("name") or "").strip()
                mod = _module_of_server(s)
                if alias and mod:
                    servers[alias] = mod
        except Exception as e:  # noqa: BLE001
            log.warning("读 mcp_servers.yaml 失败，用默认 server 表: %s", e)
    if not servers:
        servers = dict(DEFAULT_MCP_SERVERS)
    return servers


def mcp_inventory() -> dict:
    """alias -> 暴露出来的工具名列表（mcp_<alias>_<tool>）。"""
    out: dict = {}
    for alias, mod in mcp_servers().items():
        try:
            module = importlib.import_module(mod)
        except Exception as e:  # noqa: BLE001
            log.warning("MCP server 模块 %s 导入失败: %s", mod, e)
            continue
        names = []
        for t in getattr(module, "TOOLS", []) or []:
            name = t.get("name")
            if name:
                names.append("mcp_" + alias + "_" + str(name))
        if names:
            out[alias] = sorted(names)
    return out


# --- 文档里工具名的提取 -------------------------------------------------
# tools-reference.md：每个工具一个标题（### `name`）
_HEADING_TOOL_RE = re.compile(r"^#{3,4}\s+`([A-Za-z_][A-Za-z0-9_]*)`", re.M)
# SKILL.md：Quick reference 表格第一列
_TABLE_TOOL_RE = re.compile(r"^\|\s*`([A-Za-z_][A-Za-z0-9_]*)`", re.M)

# --- 文档里「工具总数」的声明 -------------------------------------------
# 写法很散，每处单独一个正则（都带上下文，避免误伤历史数字）
DOC_COUNT_CLAIMS = [
    (re.compile(r"^#\s.*?(\d+)\s*个工具全集", re.M), "标题里的工具总数"),
    (re.compile(r"^\|\s*\*\*合计\*\*\s*\|\s*\*\*(\d+)\*\*", re.M), "总览表的合计"),
    (re.compile(r"^\-\s*\*\*(\d+)\s*个工具\*\*", re.M), "文末统计"),
]
SKILL_COUNT_CLAIMS = [
    (re.compile(r"the\s+(\d+)\s+available\s+tools", re.I), "frontmatter 描述"),
    (re.compile(r"\*\*(\d+)\s*tools\*\*"), "正文里的工具总数"),
    (re.compile(r"\((\d+)\s*tools\s*:"), "Quick reference 标题"),
]
# 「多少内置 / 多少 MCP / 其中多少 aw、多少 fs」——两份文档里都可能出现，
# 一律按 live 的实际数字校验（数量对不上就是文档过期）。
BUILTIN_CLAIMS = [
    (re.compile(r"(?<![\d.])(\d+)\s*(?:个\s*)?builtin", re.I), "内置工具数"),
    (re.compile(r"(?<![\d.])(\d+)\s*(?:个\s*)?内置"), "内置工具数"),
]
MCP_CLAIMS = [
    (re.compile(r"(?<![\d.])(\d+)\s*(?:个\s*)?MCP(?![A-Za-z])"), "MCP 工具数"),
]
AW_CLAIMS = [
    (re.compile(r"(?<![\d.])(\d+)\s*(?:个\s*)?ActivityWatch"), "ActivityWatch 工具数"),
]
FS_CLAIMS = [
    (re.compile(r"(?<![\d.])(\d+)\s*(?:个\s*)?filesystem", re.I), "filesystem 工具数"),
    (re.compile(r"(?<![\d.])(\d+)\s*(?:个\s*)?文件系统"), "filesystem 工具数"),
]


# --- 各来源的名字提取 ----------------------------------------------------
# 每个来源一个 reader 函数：collect() 只通过 SOURCE_SPECS 里的 read 取名字，
# 所以测试可以替换 read 来注入假名字（不用改仓库里的任何文件）。


def _read_doc_names() -> set:
    """docs/tools-reference.md：每个工具一个标题（### `name`）。"""
    return set(_HEADING_TOOL_RE.findall(DOC_PATH.read_text("utf-8")))


def _read_skill_names() -> set:
    """SKILL.md：Quick reference 表格第一列。"""
    return set(_TABLE_TOOL_RE.findall(SKILL_PATH.read_text("utf-8")))


def _read_risk_names() -> set:
    """_risk.py 的 TOOL_RISK_LEVEL：每个内置工具的风险等级。"""
    from app.engine.tools._risk import TOOL_RISK_LEVEL
    return set(TOOL_RISK_LEVEL)


def _read_dangerous_names() -> set:
    """agent.py 的 DANGEROUS_TOOLS：需要二次确认的危险工具。"""
    from app.brain.agent import DANGEROUS_TOOLS
    return set(DANGEROUS_TOOLS)


def _read_intent_names() -> set:
    """agent.py 的 _INTENT_TOOL_NAMES：意图 -> 工具白名单。"""
    from app.brain.agent import _INTENT_TOOL_NAMES
    return {n for names in _INTENT_TOOL_NAMES.values() for n in names}


def _read_pretty_names() -> set:
    """agent.py 的 _TOOL_PRETTY：工具名 -> 中文标签（空总结时的角色化兜底）。"""
    from app.brain.agent import _TOOL_PRETTY
    return set(_TOOL_PRETTY)


def _read_blocked_names() -> set:
    """agent.py 的 _INTENT_BLOCKED_TOOLS：意图下硬拒的工具名。"""
    from app.brain.agent import _INTENT_BLOCKED_TOOLS
    return {n for d in _INTENT_BLOCKED_TOOLS.values() for n in d}


class AllowlistView(NamedTuple):
    """执行期白名单（yaml 的 tool_allowlist 段）：精确名 + 前缀桶。"""

    exact: frozenset = frozenset()
    prefixes: tuple = ()


def _read_allowlist_policy() -> Optional[AllowlistView]:
    """执行期白名单：app/core/companion_config.yaml 的 tool_allowlist 段。

    走工具层自己的读取口（app/engine/tools/_core.load_tool_allowlist）：
    它把默认 yaml 和用户覆盖 yaml 合并 —— 跟**运行期真正生效**的策略同一份，
    不会出现「脚本说放行、运行期拒绝」这种对不上的情况。
    返回 None = yaml 里没有 tool_allowlist 段（运行期退回基线快照 = 全放行，
    这时没有「该登记却没登记」的问题可查）。
    """
    from app.engine.tools._core import load_tool_allowlist
    policy = load_tool_allowlist(force=True)
    if policy is None:
        return None
    return AllowlistView(exact=frozenset(policy.exact),
                         prefixes=tuple(policy.prefixes))


# 一份「来源」= 一个文件里的**一处**名字清单。
#   file     ：来源文件（报告里写清楚差异来自哪个文件）
#   read     ：取名字的函数（上面那些 _read_*）
#   scope    ：这份来源「应该覆盖」的那部分 live 名字
#              "live" = 全部（内置 + MCP）；"builtin" = 只有内置；"none" = 不覆盖
#   required ：来源里漏了 live 的工具时算不算错误
#              "always" = 总是算（风险表：每个内置工具都得有等级）
#              "strict" = 只有 --strict-docs 才算（两份文档是手工维护的精简手册）
#              "never"  = 不算（agent 侧的名单本来就是「挑一部分」）
SOURCE_SPECS: tuple[dict, ...] = (
    {
        "key": "docs/tools-reference.md",
        "file": "docs/tools-reference.md",
        "read": _read_doc_names,
        "scope": "live",
        "required": "strict",
        "note": "文档是手工维护的精简手册：注册表里有、文档没收录的只报告",
    },
    {
        "key": ".agents/skills/desktop-pet-tool-usage/SKILL.md",
        "file": ".agents/skills/desktop-pet-tool-usage/SKILL.md",
        "read": _read_skill_names,
        "scope": "live",
        "required": "strict",
        "note": "同上（Quick reference 表格第一列 = 工具清单）",
    },
    {
        "key": "app/engine/tools/_risk.py TOOL_RISK_LEVEL",
        "file": "app/engine/tools/_risk.py",
        "read": _read_risk_names,
        "scope": "builtin",
        "required": "always",
        "note": "风险表只管内置工具；MCP 工具运行时桥接，不进静态表",
    },
    {
        "key": "app/brain/agent.py DANGEROUS_TOOLS",
        "file": "app/brain/agent.py",
        "read": _read_dangerous_names,
        "scope": "none",
        "required": "never",
        "note": "只有危险工具在名单里，其余的工具不在名单是正常的",
    },
    {
        "key": "app/brain/agent.py _INTENT_TOOL_NAMES",
        "file": "app/brain/agent.py",
        "read": _read_intent_names,
        "scope": "none",
        "required": "never",
        "note": "意图白名单：名字写错 → 该意图永远选不到那个工具",
    },
    {
        "key": "app/brain/agent.py _TOOL_PRETTY",
        "file": "app/brain/agent.py",
        "read": _read_pretty_names,
        "scope": "none",
        "required": "never",
        "note": "工具名 -> 中文标签：名字对不上只是兜底文案退化，"
                "但它说明工具改过名而 agent 侧没跟",
    },
    {
        "key": "app/brain/agent.py _INTENT_BLOCKED_TOOLS",
        "file": "app/brain/agent.py",
        "read": _read_blocked_names,
        "scope": "none",
        "required": "never",
        "note": "意图下硬拒的工具：名字写错 → 硬拒失效（工具照常下发）",
    },
    {
        "key": ALLOWLIST_SOURCE_KEY,
        "file": "app/core/companion_config.yaml",
        "read": _read_allowlist_policy,
        "scope": "live",
        "required": "always",
        "diff": "allowlist",
        "only_in_source_blocking": False,
        "note": "执行期白名单（yaml 的 tool_allowlist 段）：exact 精确名 + "
                "prefixes 前缀桶。注册表里有、白名单没覆盖 = 执行期会被拒"
                "（部署 bug，阻断）；白名单里有、注册表没有 = 死策略条目"
                "（只报告，不阻断）",
    },
)

# 风险表那条的来源 key（错误文案要专门写「没有风险等级」）
RISK_SOURCE_KEY = "app/engine/tools/_risk.py TOOL_RISK_LEVEL"
# 两份文档的来源 key（--strict-docs 时才把「文档没收录」当错误）
DOC_SOURCE_KEYS = (
    "docs/tools-reference.md",
    ".agents/skills/desktop-pet-tool-usage/SKILL.md",
)


def _source_diff(names: set, live: set, scope: set, allowed: dict) -> dict:
    """一份参考资料 vs live 注册表的三分组。

    scope = 这份参考资料「应该覆盖」的那部分 live 名字：
      - 文档：全部（内置 + MCP）；
      - 风险表：只有内置工具（MCP 工具运行时才桥接进来，不进静态表）。
    """
    only_in_source = sorted(n for n in names - live if n not in allowed)
    allowlisted = sorted(n for n in names - live if n in allowed)
    return {
        "both": sorted(names & live),
        "only_in_source": only_in_source,
        "allowlisted": allowlisted,
        "only_in_live": sorted(scope - names),
        "dead_prefixes": [],
    }


def _allowlist_diff(policy, live: set, scope: set,
                    allowed: dict) -> dict:
    """执行期白名单 vs live 注册表（两级：exact 精确名 + prefixes 前缀桶）。

    返回跟 _source_diff **同形状**的 dict（报告格式统一）：
      both           = 被白名单覆盖的 live 工具（exact 命中或前缀命中）
      only_in_source = 白名单里匹配不到任何注册工具的**死条目**（exact 里的死名字）
      allowlisted    = 有意为之的差异（登记在 ALLOWLIST 里的，带理由）
      only_in_live   = 注册表里有、白名单没覆盖的工具（执行期会被拒 → 部署 bug）
      dead_prefixes  = 没命中任何注册工具的**前缀桶**

    policy = None（yaml 里没有 tool_allowlist 段）时不做对账：运行期退回
    基线快照 = 全放行，没有「该登记却没登记」的问题。
    """
    if policy is None:
        return {"both": [], "only_in_source": [], "allowlisted": [],
                "only_in_live": [], "dead_prefixes": []}
    exact = set(policy.exact)
    prefixes = [p for p in policy.prefixes if p]
    allowed_set = set(allowed)
    covered = sorted(n for n in scope
                     if n in exact or any(n.startswith(p) for p in prefixes))
    uncovered = scope - set(covered)
    dead_exact = set(n for n in exact if n not in live)
    dead_prefixes = set(p for p in prefixes
                        if not any(n.startswith(p) for n in live))
    intentional = ((uncovered & allowed_set)
                   | (dead_exact & allowed_set)
                   | (dead_prefixes & allowed_set))
    return {
        "both": covered,
        "only_in_source": sorted(dead_exact - allowed_set),
        "allowlisted": sorted(intentional),
        "only_in_live": sorted(uncovered - allowed_set),
        "dead_prefixes": sorted(dead_prefixes - allowed_set),
    }


def _count_claims(text: str, patterns) -> list:
    """文档里的数字声明 -> [(出处, 数字, 行号)]"""
    out = []
    for rx, label in patterns:
        for m in rx.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            out.append((label, int(m.group(1)), line))
    return out


def _phantom_report() -> list:
    """幽灵工具名在源码里的已知引用位置（只报告，不阻断）。"""
    out = []
    for name, files in PHANTOM_REFS.items():
        hits = []
        for rel in files:
            p = ROOT / rel
            if not p.is_file():
                continue
            body = p.read_text("utf-8", errors="replace").splitlines()
            for i, line in enumerate(body, 1):
                if name in line:
                    hits.append(rel + ":" + str(i))
        out.append({"name": name, "refs": hits})
    return out


def qt_modules() -> list:
    """sys.modules 里已经被拉进来的 Qt 绑定（脚本不该碰它们）。"""
    return sorted(
        m for m in sys.modules
        if m.split(".")[0].lower() in QT_ROOTS
    )


def _scope_names(spec: dict, live: set, builtin: set) -> set:
    scope = spec.get("scope", "none")
    if scope == "live":
        return set(live)
    if scope == "builtin":
        return set(builtin)
    return set()


def collect(strict_docs: bool = False) -> dict:
    """跑一遍对账，返回结构化结果。"""
    registry = build_live_registry()
    builtin = sorted(registry._tools)
    mcp_by_server = mcp_inventory()
    mcp = sorted(n for names in mcp_by_server.values() for n in names)
    aw_by_server = mcp_by_server.get("aw", [])
    fs_by_server = mcp_by_server.get("fs", [])
    live = set(builtin) | set(mcp)

    doc_text = DOC_PATH.read_text("utf-8")
    skill_text = SKILL_PATH.read_text("utf-8")

    sources: dict = {}
    differences: list = []
    for spec in SOURCE_SPECS:
        key = spec["key"]
        names = spec["read"]()
        scope = _scope_names(spec, live, set(builtin))
        allowed = ALLOWLIST.get(key, {})
        # 白名单那条的差异算法不一样（两级匹配：精确名 + 前缀桶）
        if spec.get("diff") == "allowlist":
            diff = _allowlist_diff(names, live, scope, allowed)
        else:
            diff = _source_diff(set(names), live, scope, allowed)
        sources[key] = {
            "diff": diff,
            "note": spec["note"],
            "file": spec.get("file", key),
        }
        required = spec.get("required", "never")
        # 「只有资料里有」默认算错误；白名单那条是死策略条目 —— 只报告不阻断
        src_blocking = spec.get("only_in_source_blocking", True)
        for n in diff["only_in_source"]:
            differences.append({
                "source": key, "file": spec.get("file", key),
                "kind": "only_in_source", "kind_label": "只有资料里有",
                "name": n, "blocking": src_blocking,
            })
        for n in diff.get("dead_prefixes", []):
            differences.append({
                "source": key, "file": spec.get("file", key),
                "kind": "dead_prefix", "kind_label": "死的前缀条目",
                "name": n, "blocking": False,
            })
        for n in diff["allowlisted"]:
            differences.append({
                "source": key, "file": spec.get("file", key),
                "kind": "allowlisted", "kind_label": "白名单里的",
                "name": n, "blocking": False,
            })
        for n in diff["only_in_live"]:
            blocking = (required == "always"
                        or (required == "strict" and strict_docs))
            differences.append({
                "source": key, "file": spec.get("file", key),
                "kind": "only_in_live", "kind_label": "只有注册表里有",
                "name": n, "blocking": blocking,
            })

    errors = []
    for d in differences:
        if not d["blocking"]:
            continue
        if d["kind"] == "only_in_source":
            errors.append(d["source"] + " 里写了 " + d["name"]
                          + "，但注册表里没有这个工具")
        elif d["kind"] == "only_in_live":
            if d["source"] == RISK_SOURCE_KEY:
                # 风险表是单源真相：每个内置工具都得有等级（_risk.py 自己的规矩）
                errors.append("工具 " + d["name"] + " 在 TOOL_RISK_LEVEL 里没有风险等级")
            elif d["source"] == ALLOWLIST_SOURCE_KEY:
                # 执行期白名单也是单源真相：注册表里有、策略里没有 = 运行期会被拒
                errors.append("工具 " + d["name"] + " 不在执行期白名单里"
                              "（app/core/companion_config.yaml 的 tool_allowlist 段）"
                              "→ 运行期执行会被拒绝（部署 bug）")
            else:
                errors.append(d["source"] + " 里没有收录工具 " + d["name"])

    # 文档里的「工具总数」声明
    counts = []
    for label, value, line in _count_claims(doc_text, DOC_COUNT_CLAIMS):
        counts.append({"doc": "docs/tools-reference.md", "where": label,
                       "value": value, "line": line, "expect": len(live)})
    for label, value, line in _count_claims(skill_text, SKILL_COUNT_CLAIMS):
        counts.append({"doc": "SKILL.md", "where": label, "value": value,
                       "line": line, "expect": len(live)})
    for doc_name, text in (("docs/tools-reference.md", doc_text),
                           ("SKILL.md", skill_text)):
        for label, value, line in _count_claims(text, BUILTIN_CLAIMS):
            counts.append({"doc": doc_name, "where": label, "value": value,
                           "line": line, "expect": len(builtin)})
        for label, value, line in _count_claims(text, MCP_CLAIMS):
            counts.append({"doc": doc_name, "where": label, "value": value,
                           "line": line, "expect": len(mcp)})
        for label, value, line in _count_claims(text, AW_CLAIMS):
            counts.append({"doc": doc_name, "where": label, "value": value,
                           "line": line, "expect": len(aw_by_server)})
        for label, value, line in _count_claims(text, FS_CLAIMS):
            counts.append({"doc": doc_name, "where": label, "value": value,
                           "line": line, "expect": len(fs_by_server)})
    for c in counts:
        if c["value"] != c["expect"]:
            errors.append(c["doc"] + " 第 " + str(c["line"]) + " 行（" + c["where"]
                          + "）写的是 " + str(c["value"]) + "，live 是 " + str(c["expect"]))

    return {
        "live_builtin": builtin,
        "live_mcp": mcp,
        "mcp_by_server": mcp_by_server,
        "live_count": len(live),
        "sources": sources,
        "differences": differences,
        "counts": counts,
        "phantoms": _phantom_report(),
        "allowlist": ALLOWLIST,
        "errors": errors,
    }


def render(result: dict) -> str:
    """人看的分组报告。"""
    out = []
    split = "，".join(alias + " " + str(len(names))
                      for alias, names in sorted(result["mcp_by_server"].items()))
    out.append("工具清单对账（live = " + str(len(result["live_builtin"]))
               + " 内置 + " + str(len(result["live_mcp"])) + " MCP（"
               + split + "）= " + str(result["live_count"]) + "）")
    out.append("")
    for name, s in result["sources"].items():
        d = s["diff"]
        out.append("[" + name + "]")
        out.append("    两边都有       : " + str(len(d["both"])) + " 个")
        out.append("    只有资料里有   : " + str(len(d["only_in_source"]))
                   + " 个" + ("  " + ", ".join(d["only_in_source"])
                              if d["only_in_source"] else ""))
        out.append("    白名单里的     : " + str(len(d["allowlisted"]))
                   + " 个" + ("  " + ", ".join(d["allowlisted"])
                              if d["allowlisted"] else ""))
        out.append("    只有注册表里有 : " + str(len(d["only_in_live"]))
                   + " 个" + ("  " + ", ".join(d["only_in_live"])
                              if d["only_in_live"] else ""))
        if d.get("dead_prefixes"):
            out.append("    死的前缀条目   : " + str(len(d["dead_prefixes"]))
                       + " 个" + ("  " + ", ".join(d["dead_prefixes"])))
        out.append("    （" + s["note"] + "）")
    out.append("")
    out.append("差异明细（每条都标明来自哪个来源）")
    if not result["differences"]:
        out.append("    （无）")
    for d in result["differences"]:
        flag = "[阻断]" if d["blocking"] else "[白名单]"
        out.append("    " + flag + " " + d["source"] + " / " + d["kind_label"]
                   + " / " + d["name"])
    out.append("")
    out.append("文档里的工具总数声明")
    for c in result["counts"]:
        flag = "OK" if c["value"] == c["expect"] else "不一致"
        out.append("    " + c["doc"] + " 第 " + str(c["line"]) + " 行（"
                   + c["where"] + "）= " + str(c["value"])
                   + "（live " + str(c["expect"]) + "）" + flag)
    out.append("")
    out.append("已知的幽灵工具引用（有意保留，不阻断）")
    for p in result["phantoms"]:
        out.append("    " + p["name"] + ": " + (", ".join(p["refs"]) or "（没找到引用）"))
    out.append("")
    out.append("白名单（有意的差异，按来源）")
    for src_name, entries in result["allowlist"].items():
        if not entries:
            continue
        for name, reason in entries.items():
            out.append("    " + src_name + " / " + name + " — " + reason)
    out.append("")
    if result["errors"]:
        out.append("结论：" + str(len(result["errors"])) + " 处无法解释的差异")
        for e in result["errors"]:
            out.append("    - " + e)
    else:
        out.append("结论：没有无法解释的差异")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="工具清单对账：live registry vs 5 个文件里的 8 处名字来源")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--strict-docs", action="store_true",
                    help="文档没收录的工具也算错误（默认只报告）")
    args = ap.parse_args(argv)

    result = collect(strict_docs=args.strict_docs)
    # 红线：对账是纯文本活儿，脚本不许把 Qt 拉进来
    qt = qt_modules()
    result["qt_imported"] = qt
    if qt:
        result["errors"].append("对账脚本把 Qt 拉进来了：" + ", ".join(qt))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render(result))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
