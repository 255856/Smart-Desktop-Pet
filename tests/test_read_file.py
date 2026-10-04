"""read_text_file / read_file 工具的单元测试。

重点覆盖：沙箱 / 大小限制 / 截断 / 多格式分发。
"""
import os
import tempfile
from pathlib import Path

from app.engine.tools import ToolRegistry
from app.engine.tools import _file


class TestFileToolRegistration:
    def test_three_tools_registered(self):
        reg = ToolRegistry()
        _file.register(reg)
        names = set(reg.names())
        assert {"list_desktop_files", "read_text_file", "read_file"} <= names


class TestReadTextFileSafety:
    """沙箱 + 限大小 + 截断。"""

    def setup_method(self):
        self._home = Path.home()
        self._tmp_path = self._home / ".smart_desktop_pet_test_read_file.txt"
        self._tmp_path.write_text("hello\nworld\n", encoding="utf-8")
        self._reg = ToolRegistry()
        _file.register(self._reg)
        self._fn = self._reg.get("read_text_file").fn

    def teardown_method(self):
        self._tmp_path.unlink(missing_ok=True)

    def test_read_inside_home(self):
        result = self._fn(str(self._tmp_path))
        assert "hello" in result
        assert "world" in result

    def test_block_outside_home(self):
        # Windows 系统目录（任何用户主目录外的路径）
        result = self._fn("C:/Windows/System32/drivers/etc/hosts")
        assert "安全策略拒绝" in result
        assert "C:" in result

    def test_reject_nonexistent(self):
        result = self._fn(str(self._home / ".does_not_exist_99999.txt"))
        assert "文件不存在" in result

    def test_reject_too_large(self):
        big = self._home / ".smart_desktop_pet_test_big.txt"
        try:
            big.write_text("x" * (1024 * 1024 + 100), encoding="utf-8")
            result = self._fn(str(big))
            assert "文件太大" in result
        finally:
            big.unlink(missing_ok=True)

    def test_truncate_by_max_chars(self):
        long_file = self._home / ".smart_desktop_pet_test_long.txt"
        try:
            long_file.write_text("a" * 10000, encoding="utf-8")
            result = self._fn(str(long_file), max_chars=500)
            assert len(result) < 700   # 含截断提示
            assert "截断" in result
        finally:
            long_file.unlink(missing_ok=True)


class TestReadFileDispatch:
    """read_file 按扩展名分发。"""

    def setup_method(self):
        self._reg = ToolRegistry()
        _file.register(self._reg)
        self._fn = self._reg.get("read_file").fn

    def test_unknown_extension_rejected(self):
        p = Path.home() / ".smart_desktop_pet_test.xyz"
        try:
            p.write_text("mystery format", encoding="utf-8")
            result = self._fn(path=str(p))
            assert "暂不支持该格式" in result
        finally:
            p.unlink(missing_ok=True)

    def test_block_outside_home(self):
        result = self._fn(path="C:/Windows/System32/drivers/etc/hosts")
        assert "安全策略拒绝" in result

    def test_md_extension(self):
        # 家目录下的临时 md 文件
        p = Path.home() / ".smart_desktop_pet_test.md"
        try:
            p.write_text("# title\n\nbody text\n", encoding="utf-8")
            result = self._fn(path=str(p))
            assert "title" in result
            assert "body text" in result
        finally:
            p.unlink(missing_ok=True)

    def test_txt_extension(self):
        p = Path.home() / ".smart_desktop_pet_test.txt"
        try:
            p.write_text("plain text content\n", encoding="utf-8")
            result = self._fn(path=str(p))
            assert "plain text content" in result
        finally:
            p.unlink(missing_ok=True)

    def test_search_by_question(self):
        p = Path.home() / ".smart_desktop_pet_test_unique_search.md"
        try:
            p.write_text("# unique_search_doc\n\nfound me\n", encoding="utf-8")
            result = self._fn(path="", question="unique_search")
            assert "found me" in result
        finally:
            p.unlink(missing_ok=True)

    def test_search_miss(self):
        result = self._fn(path="", question="zzz_does_not_exist_zzz")
        assert "未找到" in result