"""API Key 占位符识别。

config.py / llm_client.py / chat_window.py / proactive.py 各自内联判过
"PUT-YOUR-API-KEY-HERE" / "" / "PUT-YOUR-MINIMAX-API-KEY-HERE"，
漏改任一处都会被某一路径误放行。统一在这里定义 → 改集合只改一次。
"""
from __future__ import annotations

# 全部占位符 key：没填真 key 时 LLMClient 直接抛「未配置」而不是发请求被 401
PLACEHOLDER_KEYS: frozenset[str] = frozenset({
    "",
    "PUT-YOUR-API-KEY-HERE",
    "PUT-YOUR-MINIMAX-API-KEY-HERE",
})


def is_placeholder_key(k: str) -> bool:
    """判断是否为占位符 / 未填值。"""
    return not k or k in PLACEHOLDER_KEYS


__all__ = ["PLACEHOLDER_KEYS", "is_placeholder_key"]