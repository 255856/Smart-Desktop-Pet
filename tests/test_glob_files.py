"""glob_files 工具的测试 —— 覆盖 glob 模式、include_dirs、跨平台分隔符、
越权路径、默认目录、排序行为等。

约定：单测用 monkeypatch 把 _HOME 指向 tmp_path。
路径相关断言用 normalize() 替换分隔符,保证跨平台一致。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from app.engine.tools import _filesearch


def _norm(s: str) -> str:
    """把输出里的路径分隔符标准化成正斜杠,跨平台断言稳定。"""
    return s.replace("\\", "/")


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(_filesearch, "_HOME", tmp_path)

    # 造一棵层次分明的树：
    #   tmp/src/a.py     tmp/src/b.txt
    #   tmp/src/sub/c.py tmp/src/sub/d.md
    #   tmp/other/e.py   tmp/other/f.json
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x=1\n", encoding="utf-8")
    (tmp_path / "src" / "b.txt").write_text("hello\n", encoding="utf-8")
    (tmp_path / "src" / "sub").mkdir()
    (tmp_path / "src" / "sub" / "c.py").write_text("x=2\n", encoding="utf-8")
    (tmp_path / "src" / "sub" / "d.md").write_text("# hi\n", encoding="utf-8")
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "e.py").write_text("x=3\n", encoding="utf-8")
    (tmp_path / "other" / "f.json").write_text("{}\n", encoding="utf-8")
    return tmp_path


def _build_reg_with_filesearch():
    from app.engine.tools._core import ToolRegistry
    reg = ToolRegistry()
    _filesearch.register(reg)
    return reg


def test_basic_glob_extension(sandbox):
    """**/*.py 应该命中 src/a.py、src/sub/c.py、other/e.py。"""
    reg = _build_reg_with_filesearch()
    fn = reg.get("glob_files").fn
    out = fn(pattern="**/*.py", directory=str(sandbox))
    out_n = _norm(out)
    assert "src/a.py" in out_n
    assert "src/sub/c.py" in out_n
    assert "other/e.py" in out_n
    assert "src/b.txt" not in out_n


def test_glob_single_star_extension(sandbox):
    """src/*.py（单层 *）只命中 src/a.py，不命中 src/sub/c.py。"""
    reg = _build_reg_with_filesearch()
    fn = reg.get("glob_files").fn
    out = fn(pattern="src/*.py", directory=str(sandbox))
    out_n = _norm(out)
    assert "src/a.py" in out_n
    assert "src/sub/c.py" not in out_n


def test_glob_excludes_dirs_by_default(sandbox):
    """默认 include_dirs=False：结果只含文件,不含 'src/sub' 这个目录条目。"""
    reg = _build_reg_with_filesearch()
    fn = reg.get("glob_files").fn
    out = fn(pattern="**/sub", directory=str(sandbox))
    # 默认仅文件：目录自身不被列出来。
    # 我们的 fixture 没有 "sub" 这种文件名,所以结果应当为空。
    assert "未找到匹配" in out


def test_glob_includes_dirs_when_asked(sandbox):
    """include_dirs=True 时,目录也参与匹配。"""
    reg = _build_reg_with_filesearch()
    fn = reg.get("glob_files").fn
    out = fn(pattern="**/sub", directory=str(sandbox), include_dirs=True)
    out_n = _norm(out)
    assert "src/sub" in out_n


def test_max_results_caps_output(sandbox):
    """max_results 截断命中。"""
    reg = _build_reg_with_filesearch()
    fn = reg.get("glob_files").fn
    out = fn(pattern="**/*.py", directory=str(sandbox), max_results=2)
    # 数包含 '  (' 的行 —— 每条匹配是这种格式
    hits = [ln for ln in out.splitlines() if "  (" in ln]
    assert len(hits) == 2


def test_sorted_by_mtime_descending(sandbox):
    """结果按 mtime 倒序：最后一次 touch 的文件排在最前。"""
    # 按这个顺序 touch: a.py → c.py → e.py
    # 所以 e.py 应该是最新的,排第一
    (sandbox / "src" / "a.py").write_text("v1", encoding="utf-8")
    time.sleep(0.05)
    (sandbox / "src" / "sub" / "c.py").write_text("v2", encoding="utf-8")
    time.sleep(0.05)
    (sandbox / "other" / "e.py").write_text("v3", encoding="utf-8")
    reg = _build_reg_with_filesearch()
    fn = reg.get("glob_files").fn
    out = fn(pattern="**/*.py", directory=str(sandbox))
    out_n = _norm(out)
    pos_e = out_n.find("other/e.py")
    pos_c = out_n.find("src/sub/c.py")
    pos_a = out_n.find("src/a.py")
    assert pos_e >= 0 and pos_c >= 0 and pos_a >= 0, f"missing files in:\n{out}"
    assert pos_e < pos_c < pos_a, (
        f"expected order e< c < a (mtime desc), got positions e={pos_e} "
        f"c={pos_c} a={pos_a}:\n{out}")


def test_forward_slash_pattern_works(sandbox):
    """正斜杠 pattern 必须 work —— Windows 下工具内部转 OS 分隔符。"""
    reg = _build_reg_with_filesearch()
    fn = reg.get("glob_files").fn
    out = fn(pattern="src/**/*.py", directory=str(sandbox))
    out_n = _norm(out)
    assert "src/a.py" in out_n
    assert "src/sub/c.py" in out_n


def test_empty_pattern(sandbox):
    reg = _build_reg_with_filesearch()
    fn = reg.get("glob_files").fn
    out = fn(pattern="", directory=str(sandbox))
    assert "pattern 不能为空" in out


def test_sandbox_rejects_outside_home(tmp_path, monkeypatch):
    monkeypatch.setattr(_filesearch, "_HOME", tmp_path)
    reg = _build_reg_with_filesearch()
    fn = reg.get("glob_files").fn
    out = fn(pattern="*.py", directory="C:\\Windows\\System32")
    assert "用户主目录下" in out or "拒绝" in out


def test_nonexistent_directory(sandbox):
    reg = _build_reg_with_filesearch()
    fn = reg.get("glob_files").fn
    out = fn(pattern="*.py", directory=str(sandbox / "does_not_exist"))
    assert "目录不存在" in out


def test_no_match_returns_friendly_message(sandbox):
    reg = _build_reg_with_filesearch()
    fn = reg.get("glob_files").fn
    out = fn(pattern="*.docx", directory=str(sandbox))
    assert "未找到匹配" in out


def test_default_directory_uses_desktop(tmp_path, monkeypatch):
    monkeypatch.setattr(_filesearch, "_HOME", tmp_path)
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    (desktop / "thing.py").write_text("x=1", encoding="utf-8")
    reg = _build_reg_with_filesearch()
    fn = reg.get("glob_files").fn
    out = fn(pattern="*.py")
    out_n = _norm(out)
    assert "thing.py" in out_n


def test_tool_schema_is_valid():
    from app.engine.tools._core import ToolRegistry
    reg = ToolRegistry()
    _filesearch.register(reg)
    tool_obj = reg.get("glob_files")
    assert tool_obj.name == "glob_files"
    assert "pattern" in tool_obj.parameters["required"]
    assert "max_results" in tool_obj.parameters["properties"]
    assert tool_obj.parameters["properties"]["max_results"]["maximum"] == 2000


def test_all_three_tools_registered_together():
    """register() 应该一次性把 search_files / grep_files / glob_files 都注册了。"""
    from app.engine.tools._core import ToolRegistry
    reg = ToolRegistry()
    _filesearch.register(reg)
    assert {"search_files", "grep_files", "glob_files"} <= set(reg.names())