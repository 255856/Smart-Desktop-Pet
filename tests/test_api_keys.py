"""回归测试：占位符识别必须集中在一处。

为什么需要这一组：
    2026-10-05 审查发现 "PUT-YOUR-API-KEY-HERE" / "" / "PUT-YOUR-MINIMAX-API-KEY-HERE"
    被 config.py / llm_client.py / proactive.py / chat_window.py 各自硬编码。
    个人改一处漏改另一处 → 启动报"未配置 key" 但 LLM 调用却没被挡住，或反过来。
    本测试锁住：
        - 公共函数 is_placeholder_key 行为正确
        - 关键使用点真的调它（不是改回硬编码字符串后还能过）
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from app.core.api_keys import PLACEHOLDER_KEYS, is_placeholder_key

ROOT = Path(__file__).resolve().parent.parent


# =============================================================================
# 1) 函数本身
# =============================================================================

@pytest.mark.parametrize("value", [
    "",
    "PUT-YOUR-API-KEY-HERE",
    "PUT-YOUR-MINIMAX-API-KEY-HERE",
])
def test_placeholder_strings_are_detected(value):
    """每个已知占位符都要被判成「没填」。"""
    assert is_placeholder_key(value) is True, value


@pytest.mark.parametrize("value", [
    "sk-1234567890abcdef",
    "sk-real-key",
    "PUT-YOUR-API-KEY-HERE-extra-suffix",   # 含占位符子串的真 key 不该误判
    "my-ollama-key",
])
def test_real_keys_are_not_placeholders(value):
    assert is_placeholder_key(value) is False, value


def test_placeholder_keys_is_frozenset():
    """必须是不可变集合，运行时谁也加不进去偷换语义。"""
    assert isinstance(PLACEHOLDER_KEYS, frozenset)


# =============================================================================
# 2) 关键使用点真的调了 is_placeholder_key（不是改回硬编码）
# =============================================================================

@pytest.mark.parametrize("file_rel,needles", [
    # 每一处的判定语句必须出现 is_placeholder_key(
    ("app/core/config.py", ["is_placeholder_key("]),
    ("app/brain/llm_client.py", ["is_placeholder_key("]),
    ("app/brain/proactive.py", ["is_placeholder_key("]),
    ("app/ui/chat_window/__init__.py", ["is_placeholder_key("]),
])
def test_critical_callers_use_helper(file_rel, needles):
    """每个 API key 判定点必须走公共函数。

    守卫：某天有人把这 4 个文件任何一个改回 `== "PUT-YOUR-API-KEY-HERE"`，
    本测试立刻挂掉并指出哪一行漏改。
    """
    src = (ROOT / file_rel).read_text(encoding="utf-8")
    for n in needles:
        assert n in src, f"{file_rel} 必须调用 {n!r}，却找不到"


@pytest.mark.parametrize("file_rel", [
    "app/core/config.py",
    "app/brain/proactive.py",
    "app/ui/chat_window/__init__.py",
])
def test_no_inline_placeholder_literal_in_critical_files(file_rel):
    """禁止「判 API key 是否占位符」的位置再次内联硬编码字面值。

    注释 / docstring 里出现是允许的（排除注释行后必须 0 命中）。
    """
    src = (ROOT / file_rel).read_text(encoding="utf-8")
    # 去掉注释行（# / ''' / """），再搜字面值
    lines = []
    for line in src.split("\n"):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        lines.append(line)
    no_comments = "\n".join(lines)
    # 干掉 docstring 块
    no_docstring = re.sub(r'\"\"\".*?\"\"\"', "", no_comments, flags=re.S)
    no_docstring = re.sub(r"'''.*?'''", "", no_docstring, flags=re.S)

    # 只看形如 == "PUT-YOUR-... 或 in {"PUT-... 这种"正在被当作判定字面值"的位置
    bad = re.findall(r'(?:==\s*[\'"]|in\s*\{)[\'"]?PUT-YOUR-', no_docstring)
    assert not bad, f"{file_rel} 还在内联硬编码 placeholder：{bad!r}"


# =============================================================================
# 3) 真实调用路径（穿透测试 —— 验证完整链路）
# =============================================================================

def test_config_has_api_key_true_for_real_key():
    """真实链：Config 加载 → has_api_key()。"""
    from app.core.config import Config
    cfg = Config(llm={"api_key": "sk-real-1234567890"})
    assert cfg.has_api_key() is True


@pytest.mark.parametrize("bad_key", [
    "",
    "PUT-YOUR-API-KEY-HERE",
    "PUT-YOUR-MINIMAX-API-KEY-HERE",
])
def test_config_has_api_key_false_for_placeholders(bad_key):
    """穿透：Config 加载占位符 → has_api_key() 必为 False。"""
    from app.core.config import Config
    cfg = Config(llm={"api_key": bad_key})
    assert cfg.has_api_key() is False, bad_key