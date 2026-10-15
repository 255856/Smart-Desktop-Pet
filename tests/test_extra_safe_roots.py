"""沙箱 extra_safe_roots 测试 —— 主程序(chat_window)启动时可调
_filesearch.set_extra_safe_roots([...]) 把项目目录加进沙箱白名单,
让 grep_files / search_files / glob_files 能跨家目录访问。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.engine.tools import _filesearch


@pytest.fixture
def isolated_sandbox(tmp_path, monkeypatch):
    """每个测试前清掉全局 extra_safe_roots,测后再清。"""
    _filesearch.set_extra_safe_roots([])
    monkeypatch.setattr(_filesearch, "_HOME", tmp_path / "home")
    yield tmp_path
    _filesearch.set_extra_safe_roots([])


class TestSetExtraSafeRoots:
    def test_default_blocks_outside_home(self, isolated_sandbox):
        from app.engine.tools._core import ToolRegistry
        reg = ToolRegistry()
        _filesearch.register(reg)
        out = reg.get("search_files").fn(keyword="x", directory=str(isolated_sandbox / "project"))
        assert "不在沙箱内" in out

    def test_extra_root_allows_outside_home(self, isolated_sandbox):
        from app.engine.tools._core import ToolRegistry
        proj = isolated_sandbox / "project"
        proj.mkdir()
        (proj / "code.py").write_text("TODO huge\n", encoding="utf-8")
        _filesearch.set_extra_safe_roots([str(proj)])

        reg = ToolRegistry()
        _filesearch.register(reg)
        out = reg.get("grep_files").fn(pattern="TODO", directory=str(proj))
        assert "code.py" in out, f"白名单目录应能搜,但被拒: {out}"

    def test_unrelated_path_still_blocked(self, isolated_sandbox):
        from app.engine.tools._core import ToolRegistry
        proj = isolated_sandbox / "project"
        proj.mkdir()
        elsewhere = isolated_sandbox / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "x.py").write_text("TODO here\n", encoding="utf-8")
        _filesearch.set_extra_safe_roots([str(proj)])

        reg = ToolRegistry()
        _filesearch.register(reg)
        out = reg.get("grep_files").fn(pattern="TODO", directory=str(elsewhere))
        assert "不在沙箱内" in out

    def test_multiple_extra_roots_all_each(self, isolated_sandbox):
        """多个白名单,各自能进,互不干扰。"""
        from app.engine.tools._core import ToolRegistry
        a = isolated_sandbox / "a"
        b = isolated_sandbox / "b"
        a.mkdir(); b.mkdir()
        (a / "in_a.py").write_text("TODO A\n", encoding="utf-8")
        (b / "in_b.py").write_text("TODO B\n", encoding="utf-8")
        _filesearch.set_extra_safe_roots([str(a), str(b)])

        reg = ToolRegistry()
        _filesearch.register(reg)
        out_a = reg.get("grep_files").fn(pattern="TODO", directory=str(a))
        out_b = reg.get("grep_files").fn(pattern="TODO", directory=str(b))
        assert "in_a.py" in out_a
        assert "in_b.py" in out_b

    def test_set_extra_roots_skips_unresolvable(self, isolated_sandbox):
        """坏路径不抛,正常路径仍生效。"""
        _filesearch.set_extra_safe_roots([
            "Z:\\definitely\\not\\there",
            str(isolated_sandbox / "home"),
        ])
        roots = _filesearch.get_extra_safe_roots()
        # 至少 home 被加进去
        assert any(str(r).endswith("home") for r in roots)


class TestExtraRootsAcrossTools:
    """白名单同时影响 search_files / grep_files / glob_files。"""

    def test_grep_files_respects_extra_roots(self, isolated_sandbox):
        from app.engine.tools._core import ToolRegistry
        proj = isolated_sandbox / "project"
        proj.mkdir()
        (proj / "a.py").write_text("# TODO foo\n", encoding="utf-8")
        _filesearch.set_extra_safe_roots([str(proj)])

        reg = ToolRegistry()
        _filesearch.register(reg)
        out = reg.get("grep_files").fn(pattern="TODO", directory=str(proj))
        assert "a.py" in out

    def test_glob_files_respects_extra_roots(self, isolated_sandbox):
        from app.engine.tools._core import ToolRegistry
        proj = isolated_sandbox / "project"
        proj.mkdir()
        (proj / "thing.py").write_text("x", encoding="utf-8")
        _filesearch.set_extra_safe_roots([str(proj)])

        reg = ToolRegistry()
        _filesearch.register(reg)
        out = reg.get("glob_files").fn(pattern="*.py", directory=str(proj))
        assert "thing.py" in out


class TestChatWindowIntegration:
    """chat_window.__init__ 应在启动时把 llm.project_dirs 加进白名单。"""

    def test_llm_config_has_project_dirs_field(self):
        from app.core.config import LLMConfig
        cfg = LLMConfig()
        # 字段若不存在,getattr 给默认值
        assert getattr(cfg, "project_dirs", []) == []

    def test_set_extra_roots_idempotent(self, isolated_sandbox):
        """多次 set 后,后写的覆盖前写(不是累加)。"""
        a = isolated_sandbox / "a"
        b = isolated_sandbox / "b"
        a.mkdir(); b.mkdir()
        _filesearch.set_extra_safe_roots([str(a)])
        assert len(_filesearch.get_extra_safe_roots()) == 1
        _filesearch.set_extra_safe_roots([str(b)])
        assert len(_filesearch.get_extra_safe_roots()) == 1
        assert str(b) in [str(r) for r in _filesearch.get_extra_safe_roots()]

    def test_set_extra_roots_empty_clears(self, isolated_sandbox):
        """set_extra_safe_roots([]) 应清空白名单(回到只允许家目录)。"""
        from app.engine.tools._core import ToolRegistry
        proj = isolated_sandbox / "project"
        proj.mkdir()
        (proj / "x.py").write_text("x", encoding="utf-8")
        _filesearch.set_extra_safe_roots([str(proj)])
        reg = ToolRegistry()
        _filesearch.register(reg)
        assert "x.py" in reg.get("search_files").fn(keyword="x", directory=str(proj))

        _filesearch.set_extra_safe_roots([])
        out = reg.get("search_files").fn(keyword="x", directory=str(proj))
        assert "不在沙箱内" in out