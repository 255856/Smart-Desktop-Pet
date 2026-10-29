#!/usr/bin/env python3
"""工具清单对账（Stage 0.3）：live registry vs 5 份参考资料。

对账的 5 份参考
--------------
1. docs/tools-reference.md                        面向人的工具手册
2. .agents/skills/desktop-pet-tool-usage/SKILL.md 喂给模型的 skill
3. app/engine/tools/_risk.py -> TOOL_RISK_LEVEL     每个工具的风险等级
4. app/brain/agent.py -> DANGEROUS_TOOLS            需要二次确认的危险工具
5. app/brain/agent.py -> _INTENT_TOOL_NAMES         意图 -> 工具白名单

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
"""
from __future__ import annotations

import argparse
import importlib
import json
import logging
import re
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

log = logging.getLogger(__name__)

DOC_PATH = ROOT / "docs" / "tools-reference.md"
SKILL_PATH = ROOT / ".agents" / "skills" / "desktop-pet-tool-usage" / "SKILL.md"
AW_SERVER_PATH = ROOT / "app" / "mcp" / "activitywatch_server.py"
MCP_SERVERS_PATH = ROOT / "mcp_servers.yaml"

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

    from app.engine.tools._risk import TOOL_RISK_LEVEL
    from app.brain.agent import DANGEROUS_TOOLS, _INTENT_TOOL_NAMES

    doc_names = set(_HEADING_TOOL_RE.findall(doc_text))
    skill_names = set(_TABLE_TOOL_RE.findall(skill_text))
    intent_names = {n for names in _INTENT_TOOL_NAMES.values() for n in names}

    sources = {
        "docs/tools-reference.md": {
            "diff": _source_diff(doc_names, live, live,
                                 ALLOWLIST["docs/tools-reference.md"]),
            "note": "文档是手工维护的精简手册：注册表里有、文档没收录的只报告",
        },
        ".agents/skills/desktop-pet-tool-usage/SKILL.md": {
            "diff": _source_diff(skill_names, live, live,
                                 ALLOWLIST[".agents/skills/desktop-pet-tool-usage/SKILL.md"]),
            "note": "同上（Quick reference 表格第一列 = 工具清单）",
        },
        "app/engine/tools/_risk.py TOOL_RISK_LEVEL": {
            "diff": _source_diff(set(TOOL_RISK_LEVEL), live, set(builtin),
                                 ALLOWLIST["app/engine/tools/_risk.py TOOL_RISK_LEVEL"]),
            "note": "风险表只管内置工具；MCP 工具运行时桥接，不进静态表",
        },
        "app/brain/agent.py DANGEROUS_TOOLS": {
            "diff": _source_diff(set(DANGEROUS_TOOLS), live, set(),
                                 ALLOWLIST["app/brain/agent.py DANGEROUS_TOOLS"]),
            "note": "只有危险工具在名单里，其余的工具不在名单是正常的",
        },
        "app/brain/agent.py _INTENT_TOOL_NAMES": {
            "diff": _source_diff(intent_names, live, set(),
                                 ALLOWLIST["app/brain/agent.py _INTENT_TOOL_NAMES"]),
            "note": "意图白名单：名字写错 → 该意图永远选不到那个工具",
        },
    }

    errors = []
    for name, s in sources.items():
        for n in s["diff"]["only_in_source"]:
            errors.append(name + " 里写了 " + n + "，但注册表里没有这个工具")
    # 风险表是单源真相：每个内置工具都得有等级（_risk.py 自己的规矩）
    for n in sources["app/engine/tools/_risk.py TOOL_RISK_LEVEL"]["diff"]["only_in_live"]:
        errors.append("工具 " + n + " 在 TOOL_RISK_LEVEL 里没有风险等级")
    # 文档没收录的工具：默认只报告（--strict-docs 才当错误）
    if strict_docs:
        for key in ("docs/tools-reference.md",
                    ".agents/skills/desktop-pet-tool-usage/SKILL.md"):
            for n in sources[key]["diff"]["only_in_live"]:
                errors.append(key + " 里没有收录工具 " + n)

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
        out.append("    （" + s["note"] + "）")
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
        description="工具清单对账：live registry vs 4 份参考资料")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--strict-docs", action="store_true",
                    help="文档没收录的工具也算错误（默认只报告）")
    args = ap.parse_args(argv)

    result = collect(strict_docs=args.strict_docs)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render(result))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
