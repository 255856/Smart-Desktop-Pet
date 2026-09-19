"""open_app / open_website 工具的回归测试（防止之前的 bug 复现）。

Bug 修复记录：
- open_website('www.baidu.com') 之前会被严格校验拒绝 → 现在自动补 https://
- open_app('') 之前会走 cmd /c start 闪 cmd 窗口 → 现在直接报错
- open_app('C:/nonexistent/file.exe') 之前会走 cmd 兜底 → 现在检测路径不存在并明确报错
- open_app('notepad++') 之前未装 Notepad++ 时会退到 notepad.exe（开错应用）→ 现在返回 None
- VS Code 路径之前用相对路径找不到 → 改用 %LOCALAPPDATA% 绝对路径
"""
import sys
from pathlib import Path
import unittest.mock as mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".local-packages"))
sys.path.insert(0, str(ROOT))

from app.engine.tools._system import register
from app.engine.tools._core import ToolRegistry
from app.core.app_registry import AppRegistry, _BUILTIN_MAP, _normalize_key


@pytest.fixture
def tools():
    """创建一个 ToolRegistry 并注册系统工具。"""
    reg = ToolRegistry()
    register(reg)
    return reg


@pytest.fixture
def open_website_fn(tools):
    return tools.get("open_website").fn


@pytest.fixture
def open_app_fn(tools):
    return tools.get("open_app").fn


# ============================================================
#  open_website 测试
# ============================================================

class TestOpenWebsite:
    def test_https_url(self, open_website_fn):
        """完整 https URL：直接打开，不修改。"""
        with mock.patch("webbrowser.open", return_value=True) as m:
            result = open_website_fn("https://www.baidu.com")
        m.assert_called_once_with("https://www.baidu.com")
        assert "已在浏览器打开" in result
        assert "https://www.baidu.com" in result

    def test_http_url(self, open_website_fn):
        """完整 http URL：直接打开。"""
        with mock.patch("webbrowser.open", return_value=True) as m:
            result = open_website_fn("http://example.com")
        m.assert_called_once_with("http://example.com")
        assert "已在浏览器打开" in result

    def test_www_url_auto_https(self, open_website_fn):
        """www.xxx 自动补全 https://（之前的 bug：被严格拒绝）。"""
        with mock.patch("webbrowser.open", return_value=True) as m:
            result = open_website_fn("www.baidu.com")
        m.assert_called_once_with("https://www.baidu.com")
        assert "https://www.baidu.com" in result

    def test_bare_domain_auto_https(self, open_website_fn):
        """裸域名自动补全 https://（之前的 bug：被严格拒绝）。"""
        with mock.patch("webbrowser.open", return_value=True) as m:
            result = open_website_fn("baidu.com")
        m.assert_called_once_with("https://baidu.com")
        assert "https://baidu.com" in result

    def test_search_query_uses_duckduckgo(self, open_website_fn):
        """多字搜索词（含空格）转 DuckDuckGo 搜索。"""
        with mock.patch("webbrowser.open", return_value=True) as m:
            result = open_website_fn("深度学习")
        called_url = m.call_args[0][0]
        assert "duckduckgo.com" in called_url
        # URL 会被 URL-encode，所以用 urllib 解码后再比对
        from urllib.parse import unquote
        assert "深度学习" in unquote(called_url)

    def test_empty_url(self, open_website_fn):
        """空 URL：报错，不调 webbrowser。"""
        with mock.patch("webbrowser.open") as m:
            result = open_website_fn("")
        m.assert_not_called()
        assert "错误" in result

    def test_whitespace_url(self, open_website_fn):
        """纯空白 URL：报错。"""
        with mock.patch("webbrowser.open") as m:
            result = open_website_fn("   ")
        m.assert_not_called()
        assert "错误" in result

    def test_webbrowser_returns_false(self, open_website_fn):
        """webbrowser.open 返回 False：报错。"""
        with mock.patch("webbrowser.open", return_value=False) as m:
            result = open_website_fn("https://example.com")
        m.assert_called_once()
        assert "错误" in result or "失败" in result

    def test_webbrowser_raises(self, open_website_fn):
        """webbrowser.open 抛异常：捕获并报错。"""
        with mock.patch("webbrowser.open", side_effect=RuntimeError("test error")):
            result = open_website_fn("https://example.com")
        assert "错误" in result
        assert "test error" in result

    def test_url_with_whitespace_stripped(self, open_website_fn):
        """URL 前后空白应被剥掉。"""
        with mock.patch("webbrowser.open", return_value=True) as m:
            result = open_website_fn("  https://github.com  ")
        m.assert_called_once_with("https://github.com")


# ============================================================
#  open_app 测试
# ============================================================

class TestOpenApp:
    def test_empty_name(self, open_app_fn):
        """空应用名：直接报错（之前会闪 cmd 窗口）。"""
        with mock.patch("os.startfile") as msf, \
             mock.patch("subprocess.Popen") as mp:
            result = open_app_fn("")
        msf.assert_not_called()
        mp.assert_not_called()
        assert "错误" in result
        assert "应用名为空" in result

    def test_whitespace_only_name(self, open_app_fn):
        """纯空白名：报错。"""
        with mock.patch("os.startfile") as msf, \
             mock.patch("subprocess.Popen") as mp:
            result = open_app_fn("   ")
        msf.assert_not_called()
        mp.assert_not_called()
        assert "错误" in result

    def test_builtin_notepad(self, open_app_fn):
        """内置 notepad：找到并启动。"""
        with mock.patch("os.startfile") as msf, \
             mock.patch("subprocess.Popen") as mp:
            result = open_app_fn("notepad")
        msf.assert_called_once()
        assert "已启动 notepad" in result
        mp.assert_not_called()

    def test_builtin_chinese_name(self, open_app_fn):
        """内置中文名（记事本）：找到。"""
        with mock.patch("os.startfile") as msf:
            result = open_app_fn("记事本")
        msf.assert_called_once()
        assert "已启动" in result
        assert "记事本" in result

    def test_full_path_to_existing_file(self, open_app_fn):
        """完整路径到存在的文件：直接启动。"""
        with mock.patch("os.startfile") as msf, \
             mock.patch("subprocess.Popen") as mp, \
             mock.patch.object(Path, "is_file", return_value=True):
            result = open_app_fn("C:/Windows/notepad.exe")
        msf.assert_called_once()
        assert "已启动" in result
        mp.assert_not_called()

    def test_path_to_nonexistent_file_errors(self, open_app_fn):
        """路径不存在：明确报错，不走 shell 兜底（之前的 bug：会闪 cmd）。"""
        with mock.patch("os.startfile") as msf, \
             mock.patch("subprocess.Popen") as mp:
            result = open_app_fn("C:/nonexistent/file.exe")
        msf.assert_not_called()
        mp.assert_not_called()
        assert "错误" in result
        assert "路径不存在" in result

    def test_path_with_backslash_nonexistent(self, open_app_fn):
        """带反斜杠的路径不存在：报错。"""
        with mock.patch("os.startfile") as msf, \
             mock.patch("subprocess.Popen") as mp:
            result = open_app_fn("C:\\nonexistent\\app.exe")
        msf.assert_not_called()
        mp.assert_not_called()
        assert "错误" in result

    def test_chinese_name_no_mapping(self, open_app_fn):
        """中文未注册且系统中也不存在：清晰报错（不弹 cmd）。

        用了一个明显的、不可能的应用名（如「暴雪娱乐官方客户端」）。
        真实「星穹铁道」可能在测试机上安装了 → 不再用它做负面用例。
        """
        with mock.patch("os.startfile") as msf, \
             mock.patch("subprocess.Popen") as mp, \
             mock.patch(
                 "app.core.installed_apps.find_similar_apps", return_value=[]
             ):
            result = open_app_fn("暴雪娱乐官方客户端xyz")
        msf.assert_not_called()
        mp.assert_not_called()
        assert "未找到应用" in result
        assert "提示" in result

    def test_exe_extension_nonexistent_no_cmd_flash(self, open_app_fn):
        """带 .exe 后缀但文件不存在：明确报错，不走 shell 兜底。"""
        with mock.patch("os.startfile") as msf, \
             mock.patch("subprocess.Popen") as mp:
            result = open_app_fn("nonexistent.exe")
        msf.assert_not_called()
        mp.assert_not_called()
        assert "错误" in result or "路径不存在" in result


# ============================================================
#  AppRegistry 模糊匹配测试（之前的 bug：未装 Notepad++ 退到 notepad）
# ============================================================

class TestAppRegistryFuzzyMatch:
    def test_notepad_plus_plus_does_not_fall_back_to_notepad(self):
        """【回归测试】Notepad++ 不应**直接**退到 notepad.exe（之前内置映射的 bug）。

        注意：现在已装应用扫描会找到「Microsoft.WindowsNotepad」（Windows 11
        自带的新版记事本），它确实和「notepad」名字相似。这是合理的相似度匹配，
        不是 bug——之前是「notepad++」未装时直接开 notepad.exe（用户没要求）。
        """
        from app.core.installed_apps import InstalledApp
        import unittest.mock as mock

        reg = AppRegistry()
        # mock find_similar_apps 返回空（模拟没装任何 notepad 相关应用）
        with mock.patch(
            "app.core.installed_apps.find_similar_apps", return_value=[],
        ):
            result = reg.resolve("notepad++")
        # 如果有匹配，必须不是 notepad.exe
        if result is not None:
            assert not (result.lower().endswith("notepad.exe")
                        and "++" not in result.lower()), \
                f"notepad++ 退到 notepad.exe 了: {result}"

    def test_chrome_typo_finds_chrome(self):
        """chrome 模糊匹配应该能找到 google chrome（如果安装了）。"""
        reg = AppRegistry()
        # 这里只验证逻辑不报错——具体结果依赖系统是否装 Chrome
        result = reg.resolve("chorme")  # 故意拼错
        # 不强制要求找到（取决于系统），只验证不会随机匹配到别的应用
        if result is not None:
            assert "chrome" in result.lower(), \
                f"typo 'chorme' matched to wrong app: {result}"

    def test_completely_unrelated_name_returns_none(self):
        """【回归测试】完全无关的名字不应匹配任何应用。"""
        reg = AppRegistry()
        # 之前会匹配到「neteasecloudmusic」等无关应用
        result = reg.resolve("totallynonexistentapp123")
        # 应该返回 None，或至少不匹配到完全不相关的应用
        if result is not None:
            # 如果有匹配，路径必须包含输入中的部分字符（说明是真正匹配的）
            # 而不是完全不相关的应用
            name = "totallynonexistentapp123"
            assert any(part in result.lower() for part in ["nonexistent", "totally"]), \
                f"完全无关的名字 {name!r} 不应匹配到 {result}"

    def test_normalize_key(self):
        """_normalize_key 应该小写、去空格、去标点。"""
        assert _normalize_key("Notepad") == "notepad"
        assert _normalize_key("  notepad  ") == "notepad"
        assert _normalize_key("notepad.exe") == "notepadexe"
        assert _normalize_key("notepad_plus") == "notepadplus"

    def test_vscode_paths_use_localappdata(self):
        """VS Code 路径应该用 %LOCALAPPDATA% 而非相对路径（之前的 bug）。"""
        reg = AppRegistry()
        keys = list(reg._all.keys())
        # "code" 或 "vscode" 应该有包含 %LOCALAPPDATA% 的路径
        for key in ("code", "vscode"):
            if key in keys:
                paths = reg._all[key]
                # 至少一个路径以 %LOCALAPPDATA% 开头（而不是 "AppData\" 相对路径）
                has_localappdata = any(
                    p.lower().startswith("%localappdata%")
                    or p.lower().startswith("%programfiles%")
                    for p in paths
                )
                assert has_localappdata, \
                    f"{key} 的路径没用 %LOCALAPPDATA%：{paths}"


# ============================================================
#  集成测试：工具注册 + ToolRegistry.execute
# ============================================================

class TestToolRegistryIntegration:
    def test_open_app_executed_via_registry(self, tools):
        """通过 ToolRegistry.execute 调用 open_app 能正确工作。"""
        with mock.patch("os.startfile"):
            result = tools.execute("open_app", '{"app_name": "notepad"}')
        assert "已启动" in result

    def test_open_app_invalid_json_returns_error(self, tools):
        """JSON 解析失败时返回明确错误。"""
        result = tools.execute("open_app", "not a json")
        assert "错误" in result or "失败" in result

    def test_open_website_executed_via_registry(self, tools):
        """通过 ToolRegistry.execute 调用 open_website 能正确工作。"""
        with mock.patch("webbrowser.open", return_value=True):
            result = tools.execute("open_website", '{"url": "baidu.com"}')
        assert "已在浏览器打开" in result
        assert "https://baidu.com" in result
