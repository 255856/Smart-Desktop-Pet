# -*- coding: utf-8 -*-
"""browser 网关：playwright 的工具不原生下发，只经网关按需调用。"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from app.core.qt_compat import QApplication  # noqa: E402


def _reg_with_fake_browser():
    """造一个注册表：里面塞两个假的 playwright 工具。"""
    from app.engine.tools import Tool
    from app.engine.tools._browser_gateway import register
    from app.engine.tools._core import ToolRegistry

    reg = ToolRegistry()
    reg.register(Tool(
        name='mcp_playwright_browser_navigate',
        description='open a url',
        parameters={'type': 'object', 'properties': {'url': {'type': 'string'}}},
        fn=lambda **kw: 'navigated:' + str(kw.get('url')),
        gateway_only=True,
    ))
    reg.register(Tool(
        name='mcp_playwright_browser_run_code_unsafe',
        description='run js',
        parameters={'type': 'object', 'properties': {}},
        fn=lambda **kw: 'should never run',
        gateway_only=True,
    ))
    register(reg)
    return reg


def test_gateway_only_tools_are_not_exposed_to_the_model(qapp):
    """原生工具表里**不能**有 playwright 的工具（省 9.5k token）。"""
    reg = _reg_with_fake_browser()
    names = reg.names(10)
    assert 'mcp_playwright_browser_navigate' not in names, names
    # 但它仍然在注册表里（网关要能分发到）
    assert 'mcp_playwright_browser_navigate' in reg._tools


def test_gateway_dispatches_by_action(qapp):
    """browser(action=...) → 真的调到那个工具。"""
    reg = _reg_with_fake_browser()
    gw = reg._tools['browser']
    out = gw.fn(action='browser_navigate', args={'url': 'https://example.com'})
    assert 'navigated:https://example.com' in str(out), out


def test_gateway_rejects_unknown_action(qapp):
    reg = _reg_with_fake_browser()
    gw = reg._tools['browser']
    out = str(gw.fn(action='nope', args={}))
    assert '没有 nope 这个动作' in out, out
    assert 'browser_navigate' in out, out   # 告诉模型能用哪些


def test_gateway_denies_unsafe_action(qapp):
    """页面里跑任意 JS 的那个动作**直接拒绝**（不给用）。"""
    reg = _reg_with_fake_browser()
    gw = reg._tools['browser']
    out = str(gw.fn(action='browser_run_code_unsafe', args={}))
    assert '不给用' in out, out
    assert 'should never run' not in out


def test_gateway_accepts_prefixed_action(qapp):
    """action 写成 mcp_playwright_browser_navigate 也认。"""
    reg = _reg_with_fake_browser()
    gw = reg._tools['browser']
    out = gw.fn(action='mcp_playwright_browser_navigate',
                args={'url': 'https://x.test'})
    assert 'navigated:https://x.test' in str(out), out
