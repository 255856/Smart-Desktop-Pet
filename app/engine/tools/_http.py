"""工具层共用的 HTTP 客户端（连接池复用）。

以前每个工具调用都是 `with httpx.Client(...) as c:` —— 每次调用都新建连接、
付一次 TCP + TLS 握手，连接池完全用不上（keep-alive 形同虚设）。
天气工具一次调用要建**两个** client（地理编码 + 预报），等于两次握手。

这里按 timeout 缓存复用 client：同一进程内相同超时的调用共用一条连接池，
后续调用直接复用已建立的连接。

httpx.Client 本身是线程安全的，工具又可能从多个线程并发调用，所以用锁保护
「创建/取用」这一步即可，请求本身不需要加锁。
"""
from __future__ import annotations

import logging
import threading

import httpx

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
_CLIENTS: dict[tuple, httpx.Client] = {}

# 连接池参数：工具调用是「少量、偶发」的，不需要很大的池
_LIMITS = httpx.Limits(max_keepalive_connections=4, max_connections=8)


def get_client(timeout: float, **kwargs) -> httpx.Client:
    """拿一个按 (timeout, 选项) 缓存的共享 httpx.Client（keep-alive 生效）。

    注意：返回的是**共享** client，调用方**不要** close 它，
    也不要把它当上下文管理器用（`with client:` 会关掉共享连接池）。
    """
    key = (float(timeout), tuple(sorted(kwargs.items())))
    with _LOCK:
        c = _CLIENTS.get(key)
        # is_closed 用 getattr 兜底：测试里 httpx.Client 会被替换成假对象，
        # 未必有这个属性
        if c is None or getattr(c, "is_closed", False):
            c = httpx.Client(timeout=timeout, limits=_LIMITS, **kwargs)
            _CLIENTS[key] = c
        return c


def close_all() -> None:
    """关闭所有共享 client（进程退出 / 测试清理时调用）。"""
    with _LOCK:
        for c in _CLIENTS.values():
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        _CLIENTS.clear()


__all__ = ["get_client", "close_all"]
