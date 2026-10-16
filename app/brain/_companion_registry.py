"""桌宠 PetState 注册表 (v22-3 策略层)。

strategy 层需要根据桌宠当前 PetState 调整 system_prompt 注入 (心情好
走探索风格, 心情低走保守风格)。但 LangChain 中间件 + dynamic_prompt
不知道 PetState 实例, 只知道 request.config。

本模块维护 ``thread_id → PetState`` 的映射:
  - LangChainAgent.set_companion_state(state) 调 set_state_for_thread 注册
  - grounding_dynamic_prompt(dynamic_prompt) 在生成 system_prompt 时按
    request.config["configurable"]["thread_id"] 查出来用
  - clear_state_for_thread 在 agent 析构 / 主线程结束时调用

线程安全: 锁保护所有读 / 写。桌宠单进程 QApplication 下其实不需要锁,
但 desk pet 跨 background 衰减 timer + UI thread 都可能读写, 还是上锁稳。
"""
from __future__ import annotations

import threading
from typing import Any, Optional

_LOCK = threading.Lock()
_REGISTRY: dict[str, Any] = {}


def set_state_for_thread(thread_id: str, state: Optional[Any]) -> None:
    """v22-3: LangChainAgent 启动一个 thread 时调这里注入当前桌宠 PetState。

    state=None 等价于 clear_state_for_thread(thread_id).
    """
    if not thread_id:
        return
    with _LOCK:
        if state is None:
            _REGISTRY.pop(thread_id, None)
        else:
            _REGISTRY[thread_id] = state


def get_state_for_thread(thread_id: str) -> Optional[Any]:
    """grounding_dynamic_prompt 按 thread_id 查 PetState 实例 (无注入返 None)。"""
    if not thread_id:
        return None
    with _LOCK:
        return _REGISTRY.get(thread_id)


def clear_state_for_thread(thread_id: str) -> None:
    """thread 结束时清理 (一般跟 LangChainAgent 生命周期同步)。"""
    if not thread_id:
        return
    with _LOCK:
        _REGISTRY.pop(thread_id, None)


def clear_all() -> None:
    """测试 / 重启场景用, 清掉全部注册。"""
    with _LOCK:
        _REGISTRY.clear()


__all__ = ["set_state_for_thread", "get_state_for_thread",
           "clear_state_for_thread", "clear_all"]
