"""工具清单对账脚本的回归测试（Stage 0.3）。

钉死三件事：
1. live 工具数 = 68（61 个内置 + 7 个 MCP：aw 4 + fs 3）；
2. 两份文档都声明 68（且拆成 61 内置 + 7 MCP = 4 ActivityWatch + 3 filesystem）；
3. 没有 mcp_servers.yaml 时（CI 场景，本机文件被 gitignore）默认表也给出 7 个 MCP 工具。

数量一变（加/删内置工具、改 MCP server 列表、文档忘了同步），这里就红。
不需要 API key / 网络 / GUI。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = ROOT / "scripts" / "check_tool_docs.py"

EXPECTED_TOTAL = 68
EXPECTED_BUILTIN = 61
EXPECTED_MCP = 7
EXPECTED_AW = 4
EXPECTED_FS = 3


def _load_script():
    """按文件路径加载 scripts/check_tool_docs.py（scripts 不是 python 包）。"""
    spec = importlib.util.spec_from_file_location("check_tool_docs", SCRIPT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def script():
    return _load_script()


def test_live_count_is_68(script):
    """live = 61 内置 + 7 MCP（aw 4 + fs 3）= 68。"""
    result = script.collect()
    assert len(result["live_builtin"]) == EXPECTED_BUILTIN
    assert len(result["live_mcp"]) == EXPECTED_MCP
    assert result["live_count"] == EXPECTED_TOTAL
    split = {alias: len(names) for alias, names in result["mcp_by_server"].items()}
    assert split == {"aw": EXPECTED_AW, "fs": EXPECTED_FS}


def test_no_unexplained_differences(script):
    """对账脚本自己报不出差异（否则 CI 那一步会红）。"""
    result = script.collect()
    assert result["errors"] == [], result["errors"]


def test_docs_declare_68(script):
    """两份文档都声明 68（= 61 内置 + 7 MCP = 4 aw + 3 fs）。"""
    for path, total_patterns in (
        (script.DOC_PATH, script.DOC_COUNT_CLAIMS),
        (script.SKILL_PATH, script.SKILL_COUNT_CLAIMS),
    ):
        text = path.read_text("utf-8")
        claims = script._count_claims(text, total_patterns)
        assert claims, path.name + " 里没有工具总数声明"
        for label, value, line in claims:
            assert value == EXPECTED_TOTAL, (
                path.name + " 第 " + str(line) + " 行（" + label + "）写的是 "
                + str(value) + "，应为 " + str(EXPECTED_TOTAL))
        for label, value, line in script._count_claims(text, script.BUILTIN_CLAIMS):
            assert value == EXPECTED_BUILTIN, (
                path.name + " 第 " + str(line) + " 行（" + label + "）写的是 "
                + str(value) + "，应为 " + str(EXPECTED_BUILTIN))
        for label, value, line in script._count_claims(text, script.MCP_CLAIMS):
            assert value == EXPECTED_MCP, (
                path.name + " 第 " + str(line) + " 行（" + label + "）写的是 "
                + str(value) + "，应为 " + str(EXPECTED_MCP))


def test_docs_cover_every_live_tool(script):
    """两份文档都把 live 的工具写全了（没有「注册表里有、文档没收录」）。"""
    result = script.collect()
    for key in ("docs/tools-reference.md",
                ".agents/skills/desktop-pet-tool-usage/SKILL.md"):
        diff = result["sources"][key]["diff"]
        assert diff["only_in_live"] == [], key + " 漏了工具：" + str(diff["only_in_live"])
        assert diff["only_in_source"] == [], key + " 写了不存在的工具：" + str(diff["only_in_source"])


def test_default_mcp_table_without_yaml(script, tmp_path, monkeypatch):
    """没有 mcp_servers.yaml（CI）时，默认表也得给出 aw 4 + fs 3 = 7 个。"""
    monkeypatch.setattr(script, "MCP_SERVERS_PATH", tmp_path / "no-such-file.yaml")
    inventory = script.mcp_inventory()
    split = {alias: len(names) for alias, names in inventory.items()}
    assert split == {"aw": EXPECTED_AW, "fs": EXPECTED_FS}
    assert sum(len(names) for names in inventory.values()) == EXPECTED_MCP


def test_stale_intent_name_is_reported(script):
    """agent.py 意图白名单里的旧名（mcp_aw_aw_browser_tabs）要被报出来。

    本阶段不改代码（那是后续阶段的事），但漂移必须留在 CI 报告里，
    所以这里断言它被 allowlist 收着、且理由写明「后续修」。
    """
    allow = script.ALLOWLIST["app/brain/agent.py _INTENT_TOOL_NAMES"]
    assert "mcp_aw_aw_browser_tabs" in allow
    assert allow["mcp_aw_aw_browser_tabs"]
