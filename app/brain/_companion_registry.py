"""桌宠 PetState 注册表：维护 thread_id → PetState 的映射，供 dynamic_prompt 注入使用。

LangChainAgent 启动时调 set_state_for_thread 注册，grounding_dynamic_prompt
按 thread_id 查出来用，agent 结束调 clear_state_for_thread 清理。
所有读写加锁（背景衰减 timer 与 UI thread 都会读写）。
"""
from __future__ import annotations

import threading
from typing import Any, Optional

_LOCK = threading.Lock()
_REGISTRY: dict[str, Any] = {}


def set_state_for_thread(thread_id: str, state: Optional[Any]) -> None:
    """LangChainAgent 启动 thread 时调这里注入桌宠 PetState。

    state=None 等价于 clear_state_for_thread(thread_id)。
    """
    if not thread_id:
        return
    with _LOCK:
        if state is None:
            _REGISTRY.pop(thread_id, None)
        else:
            _REGISTRY[thread_id] = state


def get_state_for_thread(thread_id: str) -> Optional[Any]:
    """按 thread_id 查 PetState 实例（无注入返 None）。"""
    if not thread_id:
        return None
    with _LOCK:
        return _REGISTRY.get(thread_id)


def clear_state_for_thread(thread_id: str) -> None:
    """thread 结束时清理（一般跟 LangChainAgent 生命周期同步）。"""
    if not thread_id:
        return
    with _LOCK:
        _REGISTRY.pop(thread_id, None)


def clear_all() -> None:
    """测试 / 重启场景用，清掉全部注册。"""
    with _LOCK:
        _REGISTRY.clear()


__all__ = ["set_state_for_thread", "get_state_for_thread",
           "clear_state_for_thread", "clear_all"]
