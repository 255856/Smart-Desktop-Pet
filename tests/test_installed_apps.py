"""installed_apps 模块 + open_app 候选推荐 的回归测试。

Bug 复盖：
- 安装应用扫描能正确列出常见 Windows 应用
- 相似度匹配能找到相似的中文 / 英文应用名
- open_app 找不到时返回候选列表（之前是直接报错或硬开 notepad）
- list_installed_apps 工具可用
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".local-packages"))
sys.path.insert(0, str(ROOT))

from app.core.installed_apps import (
    InstalledAppsCache,
    _normalize_for_search,
    _scan_installed_apps,
)
from app.core.app_registry import AppRegistry, get_registry


# ============================================================
#  归一化
# ============================================================

class TestNormalize:
    def test_strips_lnk_suffix(self):
        """去掉 .lnk 后缀（注意：不去大小写）。"""
        assert _normalize_for_search("Visual Studio Code.lnk") == "Visual Studio Code"

    def test_strips_url_suffix(self):
        assert _normalize_for_search("Some Site.url") == "Some Site"

    def test_strips_exe_suffix(self):
        assert _normalize_for_search("chrome.exe") == "chrome"

    def test_replaces_punctuation_with_space(self):
        """点、横线、下划线都换成空格（VSCode vs VS Code 互相匹配）。"""
        # 「VSCode」里没有 -_. 等符号，原样保留（大小写也不变）
        assert _normalize_for_search("VSCode") == "VSCode"
        # 「VS.Code」里 . 被替换成空格
        assert _normalize_for_search("VS.Code") == "VS Code"
        assert _normalize_for_search("VS_Code") == "VS Code"
        assert _normalize_for_search("VS-Code") == "VS Code"

    def test_chinese_passthrough(self):
        """中文标点不变。"""
        assert _normalize_for_search("微信") == "微信"
        assert _normalize_for_search("企业微信") == "企业微信"

    def test_empty(self):
        assert _normalize_for_search("") == ""


# ============================================================
#  扫描（用临时 fake registry 数据，避免依赖真实系统）
# ============================================================

class TestScanInstalledApps:
    def test_scan_returns_list(self):
        """扫描至少返回 list（可能为空如果系统太干净）。"""
        apps = _scan_installed_apps()
        assert isinstance(apps, list)

    def test_each_app_has_required_fields(self):
        """每个 InstalledApp 至少有 name/path/source。"""
        apps = _scan_installed_apps()
        for app in apps[:20]:
            assert app.name
            assert app.path
            assert app.source in {
                "app_paths", "start_menu", "desktop", "uninstall", "program_files"
            }
            # path 必须存在（扫描时检查过）
            assert Path(app.path).is_file(), \
                f"App path doesn't exist: {app.path}"


# ============================================================
#  Cache + find_similar（注入 mock 数据，不依赖系统）
# ============================================================

class TestCacheAndSimilarity:
    def _make_cache(self, apps_data: list[dict]) -> InstalledAppsCache:
        """造一个 cache 并注入 fake 数据。"""
        from app.core.installed_apps import InstalledApp
        cache = InstalledAppsCache()
        # 注入 mock apps
        cache._result.apps = [
            InstalledApp(
                name=d["name"],
                path=d.get("path", "C:/fake/" + d["name"] + ".exe"),
                source=d.get("source", "app_paths"),
            )
            for d in apps_data
        ]
        cache._result.scanned_at = __import__("time").time()
        return cache

    def test_exact_name_match(self):
        """完全相同的名字：100% 匹配。"""
        cache = self._make_cache([
            {"name": "QQ"},
            {"name": "微信"},
        ])
        candidates = cache.find_similar("QQ", limit=5, min_ratio=0.5)
        assert candidates
        assert candidates[0][0].name == "QQ"
        assert candidates[0][1] >= 0.99

    def test_partial_match(self):
        """部分匹配（typo / 不完整名字）。"""
        cache = self._make_cache([
            {"name": "Visual Studio Code"},
            {"name": "Visual Studio Installer"},
        ])
        candidates = cache.find_similar("vscode", limit=5, min_ratio=0.5)
        # 「vscode」对「visual studio code」算 typo，应该匹配
        names = [c[0].name for c in candidates]
        assert "Visual Studio Code" in names

    def test_chinese_query(self):
        """中文查询能匹配中文应用名。"""
        cache = self._make_cache([
            {"name": "微信"},
            {"name": "企业微信"},
            {"name": "QQ"},
        ])
        candidates = cache.find_similar("微信", limit=5, min_ratio=0.5)
        assert candidates
        # 第一个候选应该是「微信」（完全匹配）
        assert candidates[0][0].name == "微信"

    def test_no_match_returns_empty(self):
        """完全无关的查询返回空（min_ratio=0.5）。"""
        cache = self._make_cache([
            {"name": "QQ"},
        ])
        candidates = cache.find_similar("xyznonexistent123", limit=5, min_ratio=0.5)
        assert candidates == []

    def test_min_ratio_filter(self):
        """min_ratio 阈值正确过滤低分候选。"""
        cache = self._make_cache([
            {"name": "QQ"},
        ])
        # 阈值 0.99：QQ 与 QQ 完全匹配 → 通过
        c1 = cache.find_similar("QQ", limit=5, min_ratio=0.99)
        assert len(c1) == 1
        # 阈值 0.99：「QQ123」与「QQ」相似度 0.5 → 拒绝
        c2 = cache.find_similar("QQ123", limit=5, min_ratio=0.99)
        assert c2 == []

    def test_limit_caps_results(self):
        """limit 限制返回数量。"""
        cache = self._make_cache([{"name": f"App{i}"} for i in range(20)])
        # 用空 query + score boost 让所有都通过（不可能，除非用 "abc"）
        candidates = cache.find_similar("App", limit=3, min_ratio=0.3)
        assert len(candidates) <= 3


# ============================================================
#  open_app 找不到时返回候选
# ============================================================

class TestOpenAppSuggestions:
    def test_open_app_with_unknown_name_returns_suggestions(self):
        """open_app('暴雪娱乐') 返回未找到（不是硬开 notepad，也不是闪 cmd）。

        用中文名（非 ASCII），确保不走 shell 兜底。
        """
        from app.engine.tools._core import ToolRegistry
        from app.engine.tools._system import register
        import unittest.mock as mock

        reg = ToolRegistry()
        register(reg)

        # Mock find_similar_apps 返回空（让 _collect_suggestions 拿不到候选）
        with mock.patch(
            "app.core.installed_apps.find_similar_apps", return_value=[],
        ):
            result = reg.get("open_app").fn("暴雪娱乐公司客户端")

        # 应该报错而不是启动
        assert "未找到" in result
        # 不应该调用 os.startfile
        # 不应该调用 subprocess.Popen（中文名不能走 shell 兜底）
        assert "已尝试启动" not in result

    def test_open_app_with_typo_finds_installed_match(self):
        """open_app('不存在的应用名xyz') 找到已装应用扫描的候选。"""
        from app.engine.tools._core import ToolRegistry
        from app.engine.tools._system import register
        from app.core.installed_apps import InstalledApp, find_similar_apps
        import unittest.mock as mock

        reg = ToolRegistry()
        register(reg)

        # 用 notepad.exe 这个真实存在路径来绕过 is_file 检查
        real_path = "C:/Windows/notepad.exe"
        mock_apps = [
            (InstalledApp(
                name="Notepad",
                path=real_path,
                source="app_paths",
            ), 0.95),
        ]

        # 不 mock registry.resolve —— 真实调用，让 _resolve_from_installed_apps 跑
        # 只 mock find_similar_apps（避免真的去扫描系统）
        # 用一个绝对不在内置映射里、也不在 _BUILTIN_MAP 里模糊命中的名字
        with mock.patch(
            "app.core.installed_apps.find_similar_apps", return_value=mock_apps,
        ), mock.patch(
            "app.engine.tools._system.os.startfile",
        ) as mock_startfile:
            result = reg.get("open_app").fn("zzzzunique_random_app_zzz")

        # 应该启动了 Notepad（路径是 notepad.exe，open_app 返回用户输入名）
        assert "已启动" in result
        # path 应包含 "notepad"（因为 mock 返回的路径是 notepad.exe）
        assert "notepad" in result.lower()
        mock_startfile.assert_called_once()
        # mock_startfile 应被调用的参数是 mock 里的 notepad 路径
        call_arg = mock_startfile.call_args[0][0]
        assert "notepad" in call_arg.lower()


# ============================================================
#  list_installed_apps 工具
# ============================================================

class TestListInstalledAppsTool:
    def test_tool_registered(self):
        from app.engine.tools._core import ToolRegistry
        from app.engine.tools._system import register
        reg = ToolRegistry()
        register(reg)
        tool = reg.get("list_installed_apps")
        assert tool is not None

    def test_tool_description_mentions_key_use_cases(self):
        """工具描述应该说明用法（避免 LLM 不知道怎么用）。"""
        from app.engine.tools._core import ToolRegistry
        from app.engine.tools._system import register
        reg = ToolRegistry()
        register(reg)
        tool = reg.get("list_installed_apps")
        desc = tool.description.lower()
        # 至少提到 query 参数 + 用途
        assert "query" in desc
        assert "list" in desc or "列出" in desc

    def test_tool_with_query_returns_similar_apps(self):
        """query 模式下按相似度返回。"""
        from app.engine.tools._core import ToolRegistry
        from app.engine.tools._system import register
        from app.core.installed_apps import InstalledApp
        import unittest.mock as mock

        reg = ToolRegistry()
        register(reg)

        # mock cache with known apps
        cache = InstalledAppsCache()
        cache._result.apps = [
            InstalledApp(name="Visual Studio Code", path="C:/fake/Code.exe", source="app_paths"),
            InstalledApp(name="VSCode Insiders", path="C:/fake/Insiders.exe", source="app_paths"),
        ]
        cache._result.scanned_at = __import__("time").time()

        with mock.patch(
            "app.core.installed_apps.get_installed_apps_cache", return_value=cache,
        ):
            result = reg.get("list_installed_apps").fn("vscode")

        # 应该列出两个 vscode 候选
        assert "Visual Studio Code" in result
        assert "VSCode Insiders" in result
        # 应该有相似度百分比
        assert "%" in result

    def test_tool_with_empty_query_lists_all(self):
        """无 query 时列出所有（至少格式正确）。"""
        from app.engine.tools._core import ToolRegistry
        from app.engine.tools._system import register
        from app.core.installed_apps import InstalledApp
        import unittest.mock as mock

        reg = ToolRegistry()
        register(reg)

        cache = InstalledAppsCache()
        cache._result.apps = [
            InstalledApp(name="App1", path="C:/fake/App1.exe", source="app_paths"),
            InstalledApp(name="App2", path="C:/fake/App2.exe", source="app_paths"),
        ]
        cache._result.scanned_at = __import__("time").time()

        with mock.patch(
            "app.core.installed_apps.get_installed_apps_cache", return_value=cache,
        ):
            result = reg.get("list_installed_apps").fn("")

        assert "App1" in result
        assert "App2" in result
        assert "共" in result


# ============================================================
#  AppRegistry.find_similar_in_registry
# ============================================================

class TestRegistryFindSimilar:
    def test_returns_top_n_by_similarity(self):
        """top-N 候选按相似度降序。"""
        reg = AppRegistry()
        # 「chrome」内置在 _BUILTIN_MAP
        candidates = reg.find_similar_in_registry("chorme", limit=3)
        assert candidates
        # 第一个最相似
        assert candidates[0][0] == "chrome"
        # 按 score 降序
        scores = [s for _, s in candidates]
        assert scores == sorted(scores, reverse=True)

    def test_limit_caps_results(self):
        reg = AppRegistry()
        candidates = reg.find_similar_in_registry("c", limit=3)
        assert len(candidates) <= 3

    def test_no_match_returns_empty(self):
        reg = AppRegistry()
        candidates = reg.find_similar_in_registry(
            "asdfqwertycompletelynonexistent", limit=5,
        )
        # 应该没有候选
        assert candidates == []


# ============================================================
#  AppRegistry.resolve 集成 installed_apps 兜底
# ============================================================

class TestRegistryResolveFallback:
    def test_falls_back_to_installed_apps(self):
        """内置没匹配但已装应用有 → resolve 返回已装路径（用真实路径）。"""
        from app.core.installed_apps import InstalledApp, find_similar_apps
        import unittest.mock as mock

        reg = AppRegistry()
        # 用真实存在的 notepad.exe 路径（保证 _resolve_from_installed_apps 的 is_file 检查通过）
        with mock.patch(
            "app.core.installed_apps.find_similar_apps",
            return_value=[
                (InstalledApp(
                    name="Notepad",
                    path="C:/Windows/notepad.exe",
                    source="app_paths",
                ), 0.9),
            ],
        ):
            # "mytest" 不在内置映射里 → 降级到已装应用扫描
            result = reg.resolve("mytest_unique_name_xyz")

        assert result == "C:/Windows/notepad.exe"

    def test_no_match_anywhere_returns_none(self):
        """内置 + 已装都没有 → None（不硬开 notepad）。"""
        import unittest.mock as mock

        reg = AppRegistry()
        with mock.patch(
            "app.core.installed_apps.find_similar_apps", return_value=[],
        ):
            result = reg.resolve("totallynonexistentapp")

        assert result is None
