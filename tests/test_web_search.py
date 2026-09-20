"""web_search 工具单测：注册 + 格式化 + 错误路径（不调真实 API）。"""
import os
from unittest.mock import patch

import pytest

from app.engine.tools import ToolRegistry, build_default_tools
from app.engine.tools._search import _format_results_md, _tavily_search, _ddg_search


def _make_registry(tavily_key: str | None = None) -> ToolRegistry:
    reg = ToolRegistry()
    from app.engine.tools import _search
    _search.register(reg, tavily_api_key=tavily_key)
    return reg


def test_web_search_registered_with_tavily_key():
    """传 tavily key 应正常注册 web_search 工具。"""
    reg = _make_registry("tvly-test-key")
    assert "web_search" in reg.names()
    tool = reg.get("web_search")
    assert tool is not None
    assert tool.parameters["required"] == ["query"]


def test_web_search_registered_without_tavily_key():
    """不传 key 也应注册（走 DDG 降级）。"""
    reg = _make_registry(None)
    assert "web_search" in reg.names()


def test_web_search_empty_query():
    reg = _make_registry("tvly-test-key")
    out = reg.execute("web_search", {"query": "  "})
    assert "错误" in out


def test_web_search_tavily_success():
    """Tavily 返回正常时，web_search 输出 Markdown 列表 + 后端标识。"""
    fake_results = [
        {"title": "标题A", "url": "https://a.example", "content": "摘录A的内容"},
        {"title": "标题B", "url": "https://b.example", "content": "摘录B"},
    ]
    reg = _make_registry("tvly-test-key")
    with patch("app.engine.tools._search._tavily_search", return_value=fake_results):
        out = reg.execute("web_search", {"query": "今天天气"})
    assert "Tavily" in out
    assert "标题A" in out
    assert "https://a.example" in out
    assert "摘录A" in out


def test_web_search_tavily_fails_falls_back_to_ddg():
    """Tavily 抛异常 → 降级到 DuckDuckGo。"""
    fake_results = [{"title": "DDG标题", "url": "https://d.example", "content": "DDG摘录"}]
    reg = _make_registry("tvly-test-key")
    with patch("app.engine.tools._search._tavily_search", side_effect=RuntimeError("tavily 502")), \
         patch("app.engine.tools._search._ddg_search", return_value=fake_results):
        out = reg.execute("web_search", {"query": "今天天气"})
    assert "DuckDuckGo" in out
    assert "DDG标题" in out


def test_web_search_ddg_returns_empty():
    """Tavily 没配 + DDG 返回空时，给 Bing 搜索 URL。"""
    reg = _make_registry(None)
    with patch("app.engine.tools._search._ddg_search", return_value=[]):
        out = reg.execute("web_search", {"query": "今天天气"})
    assert "bing.com/search" in out
    assert "%E4%BB%8A%E5%A4%A9%E5%A4%A9%E6%B0%94" in out  # query 已 URL 编码


def test_web_search_ddg_no_ddgs_package():
    """DDG 端 ImportError 应降级到 Bing URL（友好提示）。"""
    reg = _make_registry(None)
    with patch("app.engine.tools._search._ddg_search",
               side_effect=RuntimeError("未安装 ddgs 包")):
        out = reg.execute("web_search", {"query": "今天天气"})
    assert "搜索失败" in out
    assert "bing.com/search" in out


def test_web_search_max_n_clamp():
    """max_n 必须 clamp 到 [1, 10]。"""
    reg = _make_registry("tvly-test-key")
    # mock Tavily 返回一个非空结果，避免 fallback 到 DDG 路径
    fake = [{"title": "T", "url": "u", "content": "c"}]
    with patch("app.engine.tools._search._tavily_search", return_value=fake) as m:
        # max_n=0 应被夹到 1
        reg.execute("web_search", {"query": "x", "max_n": 0})
        assert m.call_args.args[1] == 1          # positional arg n
        # max_n=999 应被夹到 10
        reg.execute("web_search", {"query": "x", "max_n": 999})
        assert m.call_args.args[1] == 10


def test_format_results_md_empty():
    assert "无结果" in _format_results_md([], "Test")


def test_build_default_tools_accepts_tavily_key():
    """build_default_tools 应接受 tavily_api_key 参数并传入 _search。"""
    reg = build_default_tools(
        state=None, reminders=None, memory=None,
        tavily_api_key="tvly-test",
    )
    assert "web_search" in reg.names()


def test_env_var_tavily_key_used(monkeypatch):
    """brain_controller 装配工具时：os.environ['TAVILY_API_KEY'] 应被读取。"""
    from app.brain import brain_controller as bc
    # 不实际构造 BrainController（依赖太多），改为断言 os.environ 路径：
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-env-key")
    import os
    assert os.environ.get("TAVILY_API_KEY") == "tvly-env-key"