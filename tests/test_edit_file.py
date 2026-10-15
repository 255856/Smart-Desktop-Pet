"""edit_file + multi_edit 工具测试。

共用沙箱:测试时把 HOME 临时指到 tmp_path,确保不污染用户文件。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.engine.tools._file import edit_file, multi_edit


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """临时家目录 + 把 _file._HOME 也指过来(否则沙箱会拒)。"""
    monkeypatch.setattr("app.engine.tools._file._HOME", tmp_path)
    (tmp_path / "test.txt").write_text("hello world\nfoo bar\nhello world\n", encoding="utf-8")
    return tmp_path


class TestEditFile:
    def test_replace_unique_match(self, sandbox):
        p = sandbox / "test.txt"
        result = edit_file(str(p), old_string="foo bar", new_string="baz qux")
        assert "已修改" in result
        assert "1 处" in result
        assert p.read_text(encoding="utf-8") == "hello world\nbaz qux\nhello world\n"

    def test_replace_all_replaces_all_occurrences(self, sandbox):
        p = sandbox / "test.txt"
        result = edit_file(str(p), old_string="hello world",
                           new_string="hi", replace_all=True)
        assert "2 处" in result
        assert p.read_text().count("hi") == 2

    def test_rejects_non_unique_without_replace_all(self, sandbox):
        p = sandbox / "test.txt"
        result = edit_file(str(p), old_string="hello world", new_string="hi")
        assert "为防误伤" in result
        # 文件未改
        assert p.read_text() == "hello world\nfoo bar\nhello world\n"

    def test_rejects_empty_old_string(self, sandbox):
        result = edit_file(str(sandbox / "test.txt"), old_string="", new_string="x")
        assert "old_string 不能为空" in result

    def test_rejects_missing_old_string(self, sandbox):
        result = edit_file(str(sandbox / "test.txt"),
                           old_string="not there", new_string="x")
        assert "未找到" in result

    def test_rejects_noop_replace(self, sandbox):
        result = edit_file(str(sandbox / "test.txt"),
                           old_string="foo bar", new_string="foo bar")
        assert "替换前后内容相同" in result

    def test_rejects_nonexistent_file(self, sandbox):
        result = edit_file(str(sandbox / "no.txt"),
                           old_string="x", new_string="y")
        assert "不存在" in result

    def test_rejects_path_outside_sandbox(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.engine.tools._file._HOME", tmp_path)
        outside = Path("C:/Windows/System32/drivers/etc/hosts").resolve()
        result = edit_file(str(outside), old_string="x", new_string="y")
        assert "安全策略拒绝" in result

    def test_dry_run_does_not_write(self, sandbox):
        p = sandbox / "test.txt"
        original = p.read_text(encoding="utf-8")
        result = edit_file(str(p), old_string="foo bar",
                           new_string="baz qux", dry_run=True)
        assert "[dry_run]" in result
        # 文件未动
        assert p.read_text(encoding="utf-8") == original


class TestMultiEdit:
    def test_sequential_replacements(self, sandbox):
        p = sandbox / "test.txt"
        result = multi_edit(str(p), [
            {"old_string": "hello world", "new_string": "hi", "replace_all": True},
            {"old_string": "foo bar", "new_string": "FOO BAR"},
        ])
        assert "2 处替换" in result
        assert p.read_text() == "hi\nFOO BAR\nhi\n"

    def test_per_edit_summary_lists_every_step(self, sandbox):
        """2026-10-15:每条 edit 必须显式出现在 per-edit 摘要里(防止模型总结时漏)。"""
        p = sandbox / "test.txt"
        result = multi_edit(str(p), [
            {"old_string": "hello world", "new_string": "hi", "replace_all": True},
            {"old_string": "foo bar", "new_string": "FOO BAR"},
        ])
        # 每条 edit 都必须出现在返回里 —— 编辑 #0 + 编辑 #1
        assert "[编辑 #0]" in result
        assert "[编辑 #1]" in result
        # 实际替换的内容也必须在摘要里
        assert "'hello world' → 'hi'" in result
        assert "'foo bar' → 'FOO BAR'" in result
        # diff 分隔符
        assert "[--- diff ---]" in result
        # diff 真的在最后
        diff_pos = result.find("[--- diff ---]")
        assert diff_pos > 0
        assert "---" in result[diff_pos:]   # difflib 的 --- a/ --- b/ 头

    def test_rollback_on_any_failure(self, sandbox):
        """第一个 edit 的 old_string 匹配多次(没有 replace_all)→ 失败 → 文件未变。"""
        p = sandbox / "test.txt"
        original = p.read_text(encoding="utf-8")
        # hello world 出现 2 次,但没传 replace_all → 第一个 edit 直接报错,
        # 等同于「回滚」(实际根本没写)
        result = multi_edit(str(p), [
            {"old_string": "hello world", "new_string": "hi"},
            {"old_string": "not there", "new_string": "x"},
        ])
        assert "匹配 2 次" in result
        assert p.read_text(encoding="utf-8") == original, "文件必须未变"

    def test_empty_edits(self, sandbox):
        result = multi_edit(str(sandbox / "test.txt"), [])
        assert "edits 不能为空" in result

    def test_dry_run_no_write(self, sandbox):
        p = sandbox / "test.txt"
        original = p.read_text(encoding="utf-8")
        result = multi_edit(str(p), [
            {"old_string": "hello world", "new_string": "hi", "replace_all": True},
        ], dry_run=True)
        assert "[dry_run]" in result
        assert p.read_text(encoding="utf-8") == original