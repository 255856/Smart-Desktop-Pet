"""工具清单对账脚本的回归测试（Stage 0.3 建立 / Stage 3.5 扩覆盖面）。

钉死几件事：

1. live 工具数 = 68（61 个内置 + 7 个 MCP：aw 4 + fs 3）；
2. 两份文档都声明 68（且拆成 61 内置 + 7 MCP = 4 ActivityWatch + 3 filesystem）；
3. 没有 mcp_servers.yaml 时（CI 场景，本机文件被 gitignore）默认表也给出 7 个 MCP 工具；
4. **五个文件里的 8 处名字来源都被对账**（文档 / SKILL.md / _risk.py / agent.py /
   companion_config.yaml 的 tool_allowlist 段），注入一个假名字进去必须被报出来，
   且报告要写明差异来自哪个来源；
5. **执行期白名单跟注册表双向对账**（Stage 3.5）：注册表里有、白名单没覆盖 = 阻断；
   白名单里有、注册表没有 = 死策略条目（只报告）；
6. **脚本不许把 Qt 拉进来**（对账是纯文本活儿）。

数量一变（加/删内置工具、改 MCP server 列表、文档忘了同步），这里就红。
不需要 API key / 网络 / GUI。
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = ROOT / "scripts" / "check_tool_docs.py"

EXPECTED_TOTAL = 68
EXPECTED_BUILTIN = 61
EXPECTED_MCP = 7
EXPECTED_AW = 4
EXPECTED_FS = 3

QT_ROOTS = ("pyqt5", "pyqt6", "pyside2", "pyside6")

# 五个被对账的文件（每个文件里可能有几处名字清单）
EXPECTED_SOURCE_FILES = {
    "docs/tools-reference.md",
    ".agents/skills/desktop-pet-tool-usage/SKILL.md",
    "app/engine/tools/_risk.py",
    "app/brain/agent.py",
    "app/core/companion_config.yaml",
}

# 每个文件挑一处名字来源做「注入假名字」的试验
SOURCE_FILE_CASES = [
    ("docs/tools-reference.md", "docs/tools-reference.md"),
    (".agents/skills/desktop-pet-tool-usage/SKILL.md",
     ".agents/skills/desktop-pet-tool-usage/SKILL.md"),
    ("app/engine/tools/_risk.py TOOL_RISK_LEVEL", "app/engine/tools/_risk.py"),
    ("app/brain/agent.py _INTENT_TOOL_NAMES", "app/brain/agent.py"),
]

BOGUS_NAME = "definitely_not_a_real_tool"

#: 注入到注册表里的「没登记进执行期白名单」的工具（Stage 3.5）
UNCOVERED_TOOL = "unregistered_but_in_registry"
#: 注入到白名单里的「注册表里根本没有」的死条目（Stage 3.5）
DEAD_ALLOWLIST_ENTRY = "ghost_tool_in_allowlist"
DEAD_ALLOWLIST_PREFIX = "mcp_ghost_"


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


def _replace_reader(script, monkeypatch, key, new_read):
    """把某个来源的取名字函数换掉（测试注入假名字用）。"""
    specs = []
    for spec in script.SOURCE_SPECS:
        if spec["key"] == key:
            spec = {**spec, "read": new_read}
        specs.append(spec)
    monkeypatch.setattr(script, "SOURCE_SPECS", tuple(specs))


def _inject_name(script, monkeypatch, key, bogus):
    """往某个来源里塞一个注册表里不存在的名字。"""
    orig = next(s["read"] for s in script.SOURCE_SPECS if s["key"] == key)

    def read():
        return set(orig()) | {bogus}

    _replace_reader(script, monkeypatch, key, read)


def _drop_name(script, monkeypatch, key, gone):
    """把某个来源里的某个名字去掉（模拟「漏收 / 改名后没跟上」）。"""
    orig = next(s["read"] for s in script.SOURCE_SPECS if s["key"] == key)

    def read():
        return {n for n in orig() if n != gone}

    _replace_reader(script, monkeypatch, key, read)


# --- 数量 / 文档声明 ----------------------------------------------------


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


# --- 覆盖面：四个文件里的 7 处名字来源 ------------------------------------


def test_five_source_files_are_all_reconciled(script):
    """五个文件（文档 / SKILL.md / _risk.py / agent.py / companion_config.yaml）
    都得在对账范围里。"""
    files = {spec["file"] for spec in script.SOURCE_SPECS}
    assert files == EXPECTED_SOURCE_FILES


def test_agent_side_name_sources_are_all_covered(script):
    """agent.py 里的 4 处工具名引用都要单独对账。"""
    agent_sources = [s["key"] for s in script.SOURCE_SPECS
                     if s["file"] == "app/brain/agent.py"]
    assert agent_sources == [
        "app/brain/agent.py DANGEROUS_TOOLS",
        "app/brain/agent.py _INTENT_TOOL_NAMES",
        "app/brain/agent.py _TOOL_PRETTY",
        "app/brain/agent.py _INTENT_BLOCKED_TOOLS",
    ]


def test_every_source_has_allowlist_entry(script):
    """每个来源都要在 ALLOWLIST 里有一项（白名单是「有意差异」的登记处）。"""
    assert {s["key"] for s in script.SOURCE_SPECS} <= set(script.ALLOWLIST)


def test_allowlist_entries_all_have_a_reason(script):
    """白名单每条都要写理由 —— 没理由的差异就是 bug。"""
    for src, entries in script.ALLOWLIST.items():
        for name, reason in entries.items():
            assert reason.strip(), src + " / " + name + " 没写理由"


# --- 注入差异必须被报出来（A1） -------------------------------------------


@pytest.mark.parametrize("key,rel_file", SOURCE_FILE_CASES)
def test_injected_name_is_reported_with_its_source(script, monkeypatch, key, rel_file):
    """往任意一个来源里塞一个不存在的工具名 -> 必须报错，且写明来自哪个来源。"""
    _inject_name(script, monkeypatch, key, BOGUS_NAME)
    result = script.collect()
    assert result["errors"], "注入的假名字没被报出来"
    assert any(BOGUS_NAME in e and key in e for e in result["errors"]), result["errors"]
    diffs = [d for d in result["differences"] if d["name"] == BOGUS_NAME]
    assert diffs, "differences 里没有这条差异"
    assert diffs[0]["source"] == key
    assert diffs[0]["file"] == rel_file
    assert diffs[0]["blocking"] is True


def test_injected_name_in_temp_doc_copy_fails_the_script(script, tmp_path,
                                                         monkeypatch, capsys):
    """把文档拷到临时目录、塞进一个假工具名 -> 脚本退出码 1 且报出这个名字。"""
    fake = tmp_path / "tools-reference.md"
    fake.write_text(
        script.DOC_PATH.read_text("utf-8")
        + "\n### `" + BOGUS_NAME + "`\n\n假的工具\n",
        "utf-8",
    )
    monkeypatch.setattr(script, "DOC_PATH", fake)
    assert script.main([]) == 1
    out = capsys.readouterr().out
    assert BOGUS_NAME in out
    assert "docs/tools-reference.md" in out


def test_json_mode_reports_the_source_of_a_difference(script, monkeypatch, capsys):
    """--json 的输出里也能看出差异来自哪个来源。"""
    _inject_name(script, monkeypatch,
                 ".agents/skills/desktop-pet-tool-usage/SKILL.md", BOGUS_NAME)
    assert script.main(["--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    hits = [d for d in payload["differences"] if d["name"] == BOGUS_NAME]
    assert hits and hits[0]["source"].endswith("SKILL.md")
    assert hits[0]["file"] == ".agents/skills/desktop-pet-tool-usage/SKILL.md"


def test_render_names_the_source_of_each_difference(script, monkeypatch):
    """人看的报告也要写明每条差异来自哪个来源。"""
    _inject_name(script, monkeypatch, "app/brain/agent.py _TOOL_PRETTY", BOGUS_NAME)
    out = script.render(script.collect())
    assert BOGUS_NAME in out
    assert "app/brain/agent.py _TOOL_PRETTY" in out


def test_tool_without_risk_level_is_reported(script, monkeypatch):
    """每个内置工具都得在 TOOL_RISK_LEVEL 里有等级（_risk.py 自己的规矩）。"""
    _drop_name(script, monkeypatch, script.RISK_SOURCE_KEY, "web_search")
    result = script.collect()
    assert any("web_search" in e and "TOOL_RISK_LEVEL" in e
               for e in result["errors"]), result["errors"]


def test_strict_docs_flags_missing_doc_entry(script, monkeypatch):
    """默认只报告「文档没收录」；--strict-docs 才当错误。"""
    _drop_name(script, monkeypatch, "docs/tools-reference.md", "web_search")

    loose = script.collect()
    assert not any("web_search" in e for e in loose["errors"]), loose["errors"]
    diff = [d for d in loose["differences"]
            if d["name"] == "web_search" and d["source"] == "docs/tools-reference.md"]
    assert diff and diff[0]["kind"] == "only_in_live"
    assert diff[0]["blocking"] is False

    strict = script.collect(strict_docs=True)
    assert any("web_search" in e for e in strict["errors"]), strict["errors"]


# --- 执行期白名单 ↔ 注册表：双向对账（Stage 3.5） ---------------------------


def test_stale_pretty_allowlist_entries_are_gone(script):
    """Stage 3.5：_TOOL_PRETTY 那 9 条旧名白名单已清空。

    那些名字（take_screenshot / calculator / get_system_info / memory_search /
    web_deep_search / set_alarm / set_timer / screen_capture / add_todo）
    都已经改掉了 —— 白名单留着就是「过期条目」，会让对账报告说谎。
    """
    assert script.ALLOWLIST["app/brain/agent.py _TOOL_PRETTY"] == {}


def test_allowlist_covers_every_live_tool(script):
    """执行期白名单必须覆盖注册表里的每个工具。

    没覆盖 = 运行期 execute() 会拒它（部署 bug）→ 对账必须报出来。
    """
    result = script.collect()
    diff = result["sources"][script.ALLOWLIST_SOURCE_KEY]["diff"]
    assert diff["only_in_live"] == [], diff
    assert diff["only_in_source"] == [], diff
    assert diff["dead_prefixes"] == [], diff


def test_uncovered_registry_tool_is_blocking(script, monkeypatch, capsys):
    """注册表里有、白名单没覆盖的工具 → 阻断（脚本退出码 1 + 明确文案）。"""
    from app.engine.tools._core import Tool

    reg = script.build_live_registry()
    reg.register(Tool(name=UNCOVERED_TOOL, description="新工具",
                      parameters={"type": "object", "properties": {}},
                      fn=lambda: "ok"))
    monkeypatch.setattr(script, "build_live_registry", lambda: reg)

    result = script.collect()
    hits = [d for d in result["differences"]
            if d["source"] == script.ALLOWLIST_SOURCE_KEY
            and d["name"] == UNCOVERED_TOOL]
    assert hits, result["differences"]
    assert hits[0]["blocking"] is True, hits
    assert any(UNCOVERED_TOOL in e and "tool_allowlist" in e
               for e in result["errors"]), result["errors"]

    assert script.main([]) == 1
    out = capsys.readouterr().out
    assert UNCOVERED_TOOL in out
    assert "tool_allowlist" in out


def test_dead_allowlist_entry_is_reported(script, monkeypatch):
    """白名单里匹配不到任何注册工具的条目 = 死策略 → 报出来（不阻断）。"""
    orig = script._read_allowlist_policy

    def read():
        p = orig()
        return script.AllowlistView(exact=p.exact | {DEAD_ALLOWLIST_ENTRY},
                                    prefixes=p.prefixes)

    _replace_reader(script, monkeypatch, script.ALLOWLIST_SOURCE_KEY, read)
    result = script.collect()
    hits = [d for d in result["differences"]
            if d["source"] == script.ALLOWLIST_SOURCE_KEY
            and d["name"] == DEAD_ALLOWLIST_ENTRY]
    assert hits, result["differences"]
    assert hits[0]["blocking"] is False, hits
    assert DEAD_ALLOWLIST_ENTRY in script.render(result)


def test_dead_allowlist_prefix_is_reported(script, monkeypatch):
    """白名单里没命中任何工具的前缀桶 = 死前缀 → 报出来（不阻断）。"""
    orig = script._read_allowlist_policy

    def read():
        p = orig()
        return script.AllowlistView(exact=p.exact,
                                    prefixes=tuple(p.prefixes) + (DEAD_ALLOWLIST_PREFIX,))

    _replace_reader(script, monkeypatch, script.ALLOWLIST_SOURCE_KEY, read)
    result = script.collect()
    hits = [d for d in result["differences"]
            if d["source"] == script.ALLOWLIST_SOURCE_KEY
            and d["name"] == DEAD_ALLOWLIST_PREFIX]
    assert hits, result["differences"]
    assert hits[0]["kind"] == "dead_prefix", hits
    assert hits[0]["blocking"] is False, hits
    assert DEAD_ALLOWLIST_PREFIX in script.render(result)


def test_allowlist_source_is_part_of_the_report(script):
    """白名单这条来源要出现在分组报告里（和其它来源一个格式）。"""
    out = script.render(script.collect())
    assert script.ALLOWLIST_SOURCE_KEY in out
    assert "app/core/companion_config.yaml" in out


# --- 红线：脚本不许把 Qt 拉进来 -------------------------------------------


def test_collector_does_not_import_qt_in_a_fresh_process():
    """在一个干净的子进程里跑 collect()，sys.modules 里不许出现 Qt 绑定。"""
    code = (
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location('ctd', r'" + str(SCRIPT_PATH) + "')\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "mod.collect()\n"
        "print([m for m in sys.modules "
        "if m.split('.')[0].lower() in ('pyqt5', 'pyqt6', 'pyside2', 'pyside6')])\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, cwd=str(ROOT))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]", proc.stdout


def test_collect_does_not_pull_qt_into_this_process(script):
    """在本进程里跑 collect() 也不许新拉进 Qt 绑定（子进程之外的第二道保险）。"""
    before = set(sys.modules)
    script.collect()
    new_qt = sorted(m for m in set(sys.modules) - before
                    if m.split(".")[0].lower() in QT_ROOTS)
    assert new_qt == []
