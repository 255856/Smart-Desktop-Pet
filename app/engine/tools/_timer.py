"""倒计时：区别于 add_reminder，秒级精度 + 触发时立即 say_to_user / 桌面通知。"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from ._core import Tool, ToolRegistry

log = logging.getLogger(__name__)

# 模块级倒计时调度器：列表存活的倒计时线程
_counters: list[tuple[int, float, str]] = []     # (id, fire_ts, label)
_lock = threading.Lock()
_counter_id = 0


def _add_countdown(seconds: float, label: str) -> int:
    global _counter_id
    with _lock:
        _counter_id += 1
        cid = _counter_id
        fire_ts = time.time() + seconds
        _counters.append((cid, fire_ts, label))
    t = threading.Thread(
        target=_run_countdown, args=(cid, seconds, label),
        daemon=True, name=f"countdown-{cid}",
    )
    t.start()
    return cid


def _run_countdown(cid: int, seconds: float, label: str) -> None:
    time.sleep(seconds)
    # 时间到：从列表移除
    with _lock:
        _counters[:] = [c for c in _counters if c[0] != cid]
    text = label.strip() or "时间到啦~"
    log.info("countdown #%d fired: %r", cid, text)
    # 调全局 hook：say_to_user
    hook = _global_say_hook
    if callable(hook):
        try:
            hook(text)
        except Exception:  # noqa: BLE001
            log.exception("say_to_user hook failed in countdown")
    else:
        # 兜底：调 win10toast
        try:
            import win10toast
            win10toast.ToastNotifier().show_toast(
                "桌宠倒计时", text, duration=5, threaded=True,
            )
        except Exception:
            log.info("countdown #%d fired (no hook): %r", cid, text)


_global_say_hook: Optional[Callable[[str], None]] = None


def set_say_hook(fn: Optional[Callable[[str], None]]) -> None:
    """由主程序注入：到点时让桌宠主动说话（也用于未接 hook 时的兜底）。"""
    global _global_say_hook
    _global_say_hook = fn


def countdown(seconds: float, message: str = "") -> str:
    """启动秒级倒计时（最长 1 小时）。到点让桌宠主动说话 + 桌面通知。"""
    if seconds is None:
        return "错误：缺秒数"
    try:
        s = float(seconds)
    except (TypeError, ValueError):
        return f"错误：seconds 必须是数字（{seconds!r}）"
    if s <= 0:
        return "错误：秒数必须 > 0"
    if s > 3600:
        return "错误：秒数最多 3600（1 小时）。更长的用 add_reminder"
    cid = _add_countdown(s, message)
    if s < 60:
        ago = f"{int(s)} 秒"
    elif s < 3600:
        ago = f"{int(s // 60)} 分{int(s % 60)} 秒"
    else:
        ago = "1 小时"
    return f"已设定倒计时 {ago}（id={cid}），到点主人会收到提醒"


def list_countdowns() -> str:
    """列出当前所有未到点的倒计时。"""
    with _lock:
        active = list(_counters)
    if not active:
        return "当前没有活跃倒计时"
    now = time.time()
    lines = []
    for cid, fire_ts, label in active:
        remain = max(0, int(fire_ts - now))
        lines.append(f"  #{cid}  {remain} 秒后  {label or '(无话)'}")
    return "活跃倒计时：\n" + "\n".join(lines)


def cancel_countdown(counter_id: int) -> str:
    """取消一个活跃倒计时。"""
    try:
        cid = int(counter_id)
    except (TypeError, ValueError):
        return f"错误：counter_id 必须是整数（{counter_id!r}）"
    with _lock:
        before = len(_counters)
        _counters[:] = [c for c in _counters if c[0] != cid]
        removed = before - len(_counters)
    if removed == 0:
        return f"未找到 id={cid} 的倒计时"
    return f"已取消倒计时 #{cid}"


def register(reg: ToolRegistry) -> None:
    """注册倒计时工具。"""
    reg.register(Tool(
        name="countdown",
        description="启动秒级倒计时（最长 1 小时）。到点桌宠主动说话 + Windows 通知。"
                    "比 add_reminder 更适合秒级场景（煮面、休息几秒等）。",
        parameters={"type": "object",
                    "properties": {
                        "seconds": {"type": "number",
                                    "description": "倒计时秒数，0.1~3600"},
                        "message": {"type": "string",
                                   "description": "到点说的话，空则默认「时间到啦~」"},
                    },
                    "required": ["seconds"]},
        fn=countdown,
    ))
    reg.register(Tool(
        name="list_countdowns",
        description="列出当前所有未到点的倒计时。",
        parameters={"type": "object", "properties": {}},
        fn=list_countdowns,
    ))
    reg.register(Tool(
        name="cancel_countdown",
        description="取消一个活跃倒计时（用 list_countdowns 看 id）。",
        parameters={"type": "object",
                    "properties": {"counter_id": {"type": "integer",
                                                  "description": "倒计时 id"}},
                    "required": ["counter_id"]},
        fn=cancel_countdown,
    ))


__all__ = ["register", "set_say_hook"]