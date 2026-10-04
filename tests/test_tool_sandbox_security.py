"""工具层安全边界回归测试：路径沙箱 + shell 元字符注入。

背景：这些是真实漏洞的回归锁——
1. 沙箱曾用 ``str(p).startswith(str(home))`` 做前缀比较，导致
   ``C:\\Users\\alice-backup\\x.txt`` 通过 ``C:\\Users\\alice`` 的检查。
   （_file / _runner / _filesearch 三处都有这个 bug）
2. open_app 兜底分支曾用 ``subprocess.Popen([name], shell=True)``，
   而守卫只挡非 ASCII，``&`` / ``"`` 等元字符可形成命令注入。
3. search_files 曾无遍历上限，在大目录树上会跑好几分钟，
   而工具是同步执行，会阻塞整轮 Agent 对话。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.engine.tools import _file as file_tools
from app.engine.tools import _runner as runner_tools
from app.engine.tools import _system as system_tools
from app.engine.tools._core import is_under_any


def _args(**kw) -> str:
    """Windows 路径含反斜杠，必须用 json.dumps 序列化，不能手拼字符串。"""
    return json.dumps(kw, ensure_ascii=False)


# --------------------------------------------------------------------------
# 1) is_under_any：路径语义比较
# --------------------------------------------------------------------------

def test_is_under_any_accepts_true_descendant(tmp_path):
    root = tmp_path / "alice"
    inside = root / "docs" / "note.txt"
    inside.parent.mkdir(parents=True)
    inside.write_text("x", encoding="utf-8")
    assert is_under_any(inside, [root]) is True


def test_is_under_any_rejects_sibling_prefix(tmp_path):
    """核心回归：同前缀兄弟目录必须判为不安全。"""
    base = tmp_path
    root = base / "alice"
    sibling = base / "alice-backup"
    root.mkdir()
    sibling.mkdir()
    victim = sibling / "secret.txt"
    victim.write_text("TOP SECRET", encoding="utf-8")

    assert is_under_any(victim, [root]) is False, \
        "C:\\Users\\alice-backup 不属于 C:\\Users\\alice，前缀比较是漏洞"


def test_is_under_any_rejects_numeric_suffix_sibling(tmp_path):
    base = tmp_path
    root = base / "alice"
    root.mkdir()
    (base / "alice2").mkdir()
    assert is_under_any(base / "alice2" / "x", [root]) is False


def test_is_under_any_rejects_parent_traversal(tmp_path):
    root = tmp_path / "alice"
    root.mkdir()
    escape = root / ".." / "outside.txt"
    escape.write_text("x", encoding="utf-8")
    assert is_under_any(escape, [root]) is False


def test_is_under_any_root_itself_is_inside(tmp_path):
    root = tmp_path / "alice"
    root.mkdir()
    assert is_under_any(root, [root]) is True


def test_is_under_any_empty_roots_rejects(tmp_path):
    assert is_under_any(tmp_path / "x", []) is False


def test_is_under_any_multiple_roots(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert is_under_any(b / "f.txt", [a, b]) is True
    assert is_under_any(tmp_path / "c" / "f.txt", [a, b]) is False


# --------------------------------------------------------------------------
# 2) _file._is_safe_path
# --------------------------------------------------------------------------

def test_file_safe_path_blocks_sibling_prefix(monkeypatch, tmp_path):
    root = tmp_path / "alice"
    sibling = tmp_path / "alice-backup"
    root.mkdir()
    sibling.mkdir()
    monkeypatch.setattr(file_tools, "_HOME", root)

    assert file_tools._is_safe_path(sibling / "secret.txt") is False
    assert file_tools._is_safe_path(root / "ok.txt") is True


def test_file_read_tool_rejects_outside_home(monkeypatch, tmp_path):
    """read_text_file 走真实注册工具，确认越界读被拦。"""
    from app.engine.tools import build_default_tools

    class _State:
        def get(self, *a, **k):
            return None

    outside = tmp_path / "alice-backup" / "secret.txt"
    outside.parent.mkdir(parents=True)
    outside.write_text("TOP SECRET", encoding="utf-8")

    monkeypatch.setattr(file_tools, "_HOME", tmp_path / "alice")
    reg = build_default_tools(state=_State(), reminders=None, memory=None)
    out = reg.execute("read_text_file", _args(path=str(outside)))
    assert "TOP SECRET" not in out
    assert "安全" in out or "沙箱" in out or "拒绝" in out or "主目录" in out


# --------------------------------------------------------------------------
# 3) _runner._is_under_sandbox
# --------------------------------------------------------------------------

def test_runner_sandbox_blocks_sibling_prefix(monkeypatch, tmp_path):
    root = tmp_path / "alice"
    sibling = tmp_path / "alice-backup"
    root.mkdir()
    sibling.mkdir()
    monkeypatch.setattr(runner_tools, "_HOME", root)

    evil = sibling / "evil.py"
    evil.write_text("print('pwned')", encoding="utf-8")
    good = root / "ok.py"
    good.write_text("print('ok')", encoding="utf-8")

    assert runner_tools._is_under_sandbox(evil) is False
    assert runner_tools._is_under_sandbox(good) is True


def test_run_script_rejects_sandbox_escape(monkeypatch, tmp_path):
    """真实工具入口：沙箱外脚本必须被拒绝，且不得执行。"""
    from app.engine.tools import build_default_tools

    class _State:
        def get(self, *a, **k):
            return None

    sibling = tmp_path / "alice-backup"
    sibling.mkdir()
    marker = tmp_path / "executed.txt"
    evil = sibling / "evil.py"
    evil.write_text(
        "from pathlib import Path\nPath(r'%s').write_text('pwned')\n" % marker,
        encoding="utf-8",
    )

    monkeypatch.setattr(runner_tools, "_HOME", tmp_path / "alice")
    reg = build_default_tools(state=_State(), reminders=None, memory=None)
    out = reg.execute("run_script", _args(script_path=str(evil)))

    assert "沙箱" in out or "安全策略" in out
    assert not marker.exists(), "沙箱外脚本被执行了！"


# --------------------------------------------------------------------------
# 4) open_app shell 元字符注入
# --------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    "notepad&calc",
    'notepad"&calc&"',
    "notepad|calc",
    "notepad`calc`",
    "notepad>out.txt",
    "notepad^&calc",
    "$(calc)",
    "notepad;calc",
])
def test_shell_metachars_are_detected(payload):
    assert system_tools._SHELL_METACHARS & set(payload), \
        f"{payload!r} 应被识别为含 shell 元字符"


@pytest.mark.parametrize("safe_name", [
    "notepad", "mspaint", "explorer", "chrome", "Code", "obsidian64",
    "C:\\Program Files\\App\\app.exe",
])
def test_shell_metachars_not_flagged_for_legit_names(safe_name):
    """正常应用名/路径不能被误伤，否则是功能回归。"""
    # 路径里的反斜杠、冒号、空格、点都是合法的
    assert not (system_tools._SHELL_METACHARS & set(safe_name)), \
        f"{safe_name!r} 被误判为含元字符，会导致正常应用打不开"


def test_open_app_rejects_injection_payload(monkeypatch):
    """open_app 遇到注入 payload 必须直接拒绝，不得调用 Popen。"""
    from app.engine.tools import build_default_tools

    called = {"n": 0}
    real_popen = system_tools.subprocess.Popen

    def spy(*a, **k):
        called["n"] += 1
        return real_popen(*a, **k)

    class _State:
        def get(self, *a, **k):
            return None

    monkeypatch.setattr(system_tools.subprocess, "Popen", spy)
    reg = build_default_tools(state=_State(), reminders=None, memory=None)
    out = reg.execute("open_app", _args(app_name="notepad&calc"))

    assert "非法字符" in out, f"未拒绝注入 payload，实际返回：{out[:200]}"
    assert called["n"] == 0, "含元字符的 app_name 仍然触发了进程创建"


def test_open_app_popen_never_uses_shell(monkeypatch):
    """兜底 PATH 搜索分支不得使用 shell=True。"""
    from app.engine.tools import build_default_tools

    seen = {}
    real_popen = system_tools.subprocess.Popen

    def spy(*a, **k):
        seen.update(k)
        seen["args"] = a
        # 造一个假的成功结果，避免真的启动程序
        class _P:
            returncode = 0
        return _P()

    class _State:
        def get(self, *a, **k):
            return None

    monkeypatch.setattr(system_tools.subprocess, "Popen", spy)
    monkeypatch.setattr(
        "app.core.app_registry.get_registry",
        lambda: type("R", (), {"resolve": lambda self, n: None})(),
    )
    reg = build_default_tools(state=_State(), reminders=None, memory=None)
    reg.execute("open_app", _args(app_name="zzz_no_such_app_zzz"))

    if seen:                      # 走到了兜底分支
        assert seen.get("shell") in (None, False), \
            "open_app 兜底分支仍在用 shell=True，存在命令注入"


# --------------------------------------------------------------------------
# 5) _filesearch 第三处沙箱绕过 + 遍历上限（防阻塞 Agent）
# --------------------------------------------------------------------------

def test_filesearch_sandbox_blocks_sibling_prefix(monkeypatch, tmp_path):
    """_filesearch 之前也是 startswith 前缀比较（第三处同款漏洞）。"""
    from app.engine.tools import _filesearch as fs_tools

    root = tmp_path / "alice"
    sibling = tmp_path / "alice-backup"
    root.mkdir()
    sibling.mkdir()
    monkeypatch.setattr(fs_tools, "_HOME", root)

    assert fs_tools._is_safe_path(sibling) is False
    assert fs_tools._is_safe_path(root) is True


def test_search_files_rejects_outside_home(monkeypatch, tmp_path):
    from app.engine.tools import _filesearch as fs_tools
    from app.engine.tools import build_default_tools

    class _State:
        def get(self, *a, **k):
            return None

    outside = tmp_path / "alice-backup"
    outside.mkdir()
    monkeypatch.setattr(fs_tools, "_HOME", tmp_path / "alice")
    reg = build_default_tools(state=_State(), reminders=None, memory=None)
    out = reg.execute("search_files", _args(keyword="x", directory=str(outside)))
    assert "主目录" in out or "拒绝" in out, out


def test_search_files_has_scan_budget(monkeypatch, tmp_path):
    """必须有时间/数量预算，否则大目录树上会阻塞整轮对话。"""
    from app.engine.tools import _filesearch as fs_tools
    import time as _time

    assert fs_tools._MAX_VISIT > 0
    assert fs_tools._TIME_BUDGET_S > 0
    assert fs_tools._TIME_BUDGET_S <= 30, "时间预算过大仍会卡住用户"

    # 造一棵宽而浅的大树，把预算压到很小以验证会提前收手
    big = tmp_path / "alice" / "big"
    big.mkdir(parents=True)
    for i in range(400):
        (big / f"f{i}.txt").write_text("x", encoding="utf-8")

    monkeypatch.setattr(fs_tools, "_HOME", tmp_path / "alice")
    monkeypatch.setattr(fs_tools, "_MAX_VISIT", 50)
    monkeypatch.setattr(fs_tools, "_TIME_BUDGET_S", 8.0)

    from app.engine.tools import build_default_tools

    class _State:
        def get(self, *a, **k):
            return None

    reg = build_default_tools(state=_State(), reminders=None, memory=None)
    t0 = _time.monotonic()
    out = reg.execute("search_files", _args(keyword="不存在zzz", directory=str(big)))
    dt = _time.monotonic() - t0

    assert dt < 5.0, f"search_files 耗时 {dt:.1f}s，仍会阻塞对话"
    assert "上限" in out or "不完整" in out, f"应如实说明结果被截断：{out[:150]}"