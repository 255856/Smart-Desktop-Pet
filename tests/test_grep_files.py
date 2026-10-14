"""grep_files 工具的测试 —— 覆盖基本匹配、空 pattern、非法正则、include 过滤、
max_results 上限、case_insensitive、越权路径、大文件跳过、truncated 提示等。

约定：单测用 monkeypatch 把 _HOME 指向 tmp_path，模拟沙箱。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.engine.tools import _filesearch


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """把 _HOME 重定向到 tmp_path，构造一个可控的文件树。"""
    monkeypatch.setattr(_filesearch, "_HOME", tmp_path)

    # 故意造一棵小树：
    #   tmp/a.py        含 "TODO fix login"
    #   tmp/b.py        含 "  todo "  (大小写不同)
    #   tmp/notes.txt   含 "todo buy milk"
    #   tmp/big.py      超大（2 MiB+），应跳过
    #   tmp/bin/.git/config  （看起来是代码目录，可能被 rglob 撞上）
    (tmp_path / "a.py").write_text("def hello():\n    pass\n# TODO fix login\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("x = 1\n# todo low priority\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("todo buy milk\ndone.\n", encoding="utf-8")
    big = tmp_path / "big.py"
    big.write_text("x = 1\n" + "padding " * 400000 + "\n", encoding="utf-8")  # ~2.8 MiB
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / ".git").mkdir(parents=True)
    (tmp_path / "bin" / ".git" / "config").write_text("[core]\nTODO inside git\n", encoding="utf-8")

    return tmp_path


def test_basic_match(sandbox):
    """最简单的命中：pattern 命中 a.py 一行。"""
    from app.engine.tools._core import ToolRegistry
    reg = ToolRegistry()
    _filesearch.register(reg)
    tool_obj = reg.get("grep_files")
    assert tool_obj is not None, "grep_files 没注册到 ToolRegistry"
    fn = tool_obj.fn
    out = fn(pattern="TODO fix login", directory=str(sandbox))
    assert "a.py" in out
    assert "TODO fix login" in out
    assert "notes.txt" not in out


def test_case_sensitive_default(sandbox):
    """默认 case sensitive：lowercase "todo" 不命中 "TODO"。"""
    from app.engine.tools._core import ToolRegistry
    reg = ToolRegistry()
    _filesearch.register(reg)
    fn = reg.get("grep_files").fn
    out = fn(pattern="todo", directory=str(sandbox))
    # "todo" lowercase 应当只命中 b.py 和 notes.txt，不命中 a.py
    assert "b.py" in out
    assert "notes.txt" in out
    assert "a.py" not in out


def test_case_insensitive(sandbox):
    """case_insensitive=True 时，lowercase "todo" 也命中 "TODO"。"""
    from app.engine.tools._core import ToolRegistry
    reg = ToolRegistry()
    _filesearch.register(reg)
    fn = reg.get("grep_files").fn
    out = fn(pattern="todo", directory=str(sandbox), case_insensitive=True)
    assert "a.py" in out
    assert "b.py" in out
    assert "notes.txt" in out


def test_include_filter(sandbox):
    """include 限定 *.py —— notes.txt 的 todo 不该出现。"""
    from app.engine.tools._core import ToolRegistry
    reg = ToolRegistry()
    _filesearch.register(reg)
    fn = reg.get("grep_files").fn
    out = fn(pattern="todo", directory=str(sandbox), include="*.py",
             case_insensitive=True)
    assert "a.py" in out
    assert "b.py" in out
    assert "notes.txt" not in out


def test_max_results_cap(sandbox):
    """max_results 限制命中行数。"""
    from app.engine.tools._core import ToolRegistry
    reg = ToolRegistry()
    _filesearch.register(reg)
    fn = reg.get("grep_files").fn
    # pattern 是空 pattern 会 rejected，先用一个能多次命中的：
    # 我们多塞几行让它容易超 limit
    big = sandbox / "many.py"
    lines = "\n".join([f"line {i} todo" for i in range(20)])
    big.write_text(lines, encoding="utf-8")
    out = fn(pattern="todo", directory=str(sandbox), include="*.py",
             case_insensitive=True, max_results=5)
    # 5 处限制，应该正好 5 个 file:line:xxx 行
    hits = [ln for ln in out.splitlines() if ".py:" in ln and "todo" in ln.lower()]
    assert len(hits) == 5
    assert "max_results" in out or "上限" in out


def test_empty_pattern(sandbox):
    from app.engine.tools._core import ToolRegistry
    reg = ToolRegistry()
    _filesearch.register(reg)
    fn = reg.get("grep_files").fn
    out = fn(pattern="", directory=str(sandbox))
    assert "pattern 不能为空" in out


def test_invalid_regex(sandbox):
    """非法正则要给出可读错误，而不是抛异常。"""
    from app.engine.tools._core import ToolRegistry
    reg = ToolRegistry()
    _filesearch.register(reg)
    fn = reg.get("grep_files").fn
    out = fn(pattern="(unclosed", directory=str(sandbox))
    assert "正则表达式非法" in out


def test_sandbox_rejects_outside_home(tmp_path, monkeypatch):
    """directory 越出家目录必须被拒绝，不能读系统目录。"""
    # _HOME 指向 tmp_path
    monkeypatch.setattr(_filesearch, "_HOME", tmp_path)
    from app.engine.tools._core import ToolRegistry
    reg = ToolRegistry()
    _filesearch.register(reg)
    fn = reg.get("grep_files").fn
    # Windows 系统目录，跨越 _HOME
    out = fn(pattern="anything", directory="C:\\Windows\\System32")
    assert "用户主目录下" in out or "拒绝" in out


def test_big_file_skipped(sandbox):
    """>2MiB 的文件不参与匹配（不然一次 grep 几分钟都跑不完）。"""
    from app.engine.tools._core import ToolRegistry
    reg = ToolRegistry()
    _filesearch.register(reg)
    fn = reg.get("grep_files").fn
    # 把一个超大 token 写进 big.py：但因为 big.py 被跳过，所以不会命中
    big = sandbox / "big.py"
    big.write_text("UNIQUE_BIG_FILE_TOKEN x = 1\n" + "padding " * 400000, encoding="utf-8")
    out = fn(pattern="UNIQUE_BIG_FILE_TOKEN", directory=str(sandbox))
    assert "big.py" not in out


def test_default_directory_uses_desktop(tmp_path, monkeypatch):
    """directory 不传时，落到 ~/Desktop。"""
    monkeypatch.setattr(_filesearch, "_HOME", tmp_path)
    fake_desktop = tmp_path / "Desktop"
    fake_desktop.mkdir()
    (fake_desktop / "x.py").write_text("NEEDLE_TOKEN\n", encoding="utf-8")
    from app.engine.tools._core import ToolRegistry
    reg = ToolRegistry()
    _filesearch.register(reg)
    fn = reg.get("grep_files").fn
    out = fn(pattern="NEEDLE_TOKEN")
    assert "x.py" in out


def test_no_match_returns_friendly_message(sandbox):
    """没命中时给可读提示，不要抛 stack trace。"""
    from app.engine.tools._core import ToolRegistry
    reg = ToolRegistry()
    _filesearch.register(reg)
    fn = reg.get("grep_files").fn
    out = fn(pattern="DEFINITELY_NO_SUCH_STRING_ZZZ", directory=str(sandbox))
    assert "未找到匹配" in out


def test_unicode_pattern(sandbox):
    """中文 pattern 也能命中（re 默认 unicode-safe）。"""
    from app.engine.tools._core import ToolRegistry
    reg = ToolRegistry()
    _filesearch.register(reg)
    fn = reg.get("grep_files").fn
    (sandbox / "cn.txt").write_text("记一笔：今天有雷阵雨\n", encoding="utf-8")
    out = fn(pattern="雷阵雨", directory=str(sandbox))
    assert "cn.txt" in out


def test_search_files_still_works(sandbox):
    """清理重复时不能把 search_files 弄坏。"""
    from app.engine.tools._core import ToolRegistry
    reg = ToolRegistry()
    _filesearch.register(reg)
    fn = reg.get("search_files").fn
    out = fn(keyword="notes", directory=str(sandbox))
    assert "notes.txt" in out


def test_tool_schema_is_valid():
    """grep_files 的 JSON schema 必须能被 ToolRegistry 当 Tool 暴露出去。"""
    from app.engine.tools._core import ToolRegistry
    reg = ToolRegistry()
    _filesearch.register(reg)
    tool_obj = reg.get("grep_files")
    assert tool_obj.name == "grep_files"
    assert "pattern" in tool_obj.parameters["required"]
    assert "directory" in tool_obj.parameters["properties"]
    assert tool_obj.parameters["properties"]["max_results"]["maximum"] == 1000