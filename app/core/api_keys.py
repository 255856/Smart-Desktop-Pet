"""API Key 占位符识别。

为什么独立成模块：
    config.py / llm_client.py / chat_window.py / proactive.py 各自内联
    判过 "PUT-YOUR-API-KEY-HERE" / "" / "PUT-YOUR-MINIMAX-API-KEY-HERE"。
    漏改任一处都会被某一路径误放行」→ 在启动时被发现是在 UI 刚弹"未配置"
    之后、气泡还报错；这是个人修改代价低的 bug 源。

统一在这里定义 → 改集合只改一次；加 placeholder 也只加一次。
"""
from __future__ import annotations

# 全部占位符 key —— 没填真 key 时 LLMClient 直接抛「未配置」而不是发请求被 401
PLACEHOLDER_KEYS: frozenset[str] = frozenset({
    "",
    "PUT-YOUR-API-KEY-HERE",
    "PUT-YOUR-MINIMAX-API-KEY-HERE",
})


def is_placeholder_key(k: str) -> bool:
    """判断是否为占位符 / 未填值。

    返回 True 的情形：
        - 空字符串（用户没填 / 字段缺失）
        - 任意 PLACEHOLDER_KEYS 集合内的字面值
    """
    return not k or k in PLACEHOLDER_KEYS


__all__ = ["PLACEHOLDER_KEYS", "is_placeholder_key"]