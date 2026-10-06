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
# cid → threading.Event。cancel_countdown 必须靠它真正叫停等待线程：
# 原实现只把条目从 _counters 里删掉，_run_countdown 的 time.sleep 照跑不误，
# 到点照样弹提醒——工具回报「已取消」，行为与承诺相反。
_cancel_events: dict[int, threading.Event] = {}
# cid → 线程。cancel_countdown 要等线程真正退出才算「取消完成」，
# 否则会留下一堆停在 Event.wait 的守护线程：正常运行时无害，但进程退出时
# 它们还在阻塞态，解释器拆模块全局变量就可能把进程带崩
# （实测 Windows fatal access violation）。
_countdown_threads: dict[int, threading.Thread] = {}


def _add_countdown(seconds: float, label: str) -> int:
    global _counter_id
    with _lock:
        _counter_id += 1
        cid = _counter_id
        fire_ts = time.time() + seconds
        _counters.append((cid, fire_ts, label))
        _cancel_events[cid] = threading.Event()
        ev = _cancel_events[cid]
    t = threading.Thread(
        target=_run_countdown, args=(cid, seconds, label, ev),
        daemon=True, name=f"countdown-{cid}",
    )
    with _lock:
        _countdown_threads[cid] = t
    t.start()
    return cid


def _run_countdown(cid: int, seconds: float, label: str,
                   cancel_ev: threading.Event) -> None:
    try:
        _run_countdown_inner(cid, seconds, label, cancel_ev)
    finally:
        with _lock:
            _countdown_threads.pop(cid, None)


def _run_countdown_inner(cid: int, seconds: float, label: str,
                         cancel_ev: threading.Event) -> None:
    # 可被 cancel_countdown 立刻唤醒，不必干等到点
    if cancel_ev.wait(timeout=seconds):
        log.info("countdown #%d cancelled before firing", cid)
        with _lock:
            _counters[:] = [c for c in _counters if c[0] != cid]
            _cancel_events.pop(cid, None)
        return
    # 时间到：从列表移除
    with _lock:
        _counters[:] = [c for c in _counters if c[0] != cid]
        _cancel_events.pop(cid, None)
    text = label.strip() or "时间到啦~"
    log.info("countdown #%d fired: %r", cid, text)
    # 桌宠这边先响了就把系统级兜底撤掉，否则到点会响两次
    try:
        from app.core import win_alarm
        win_alarm.disarm_key(f"cd{cid}")
    except Exception:  # noqa: BLE001
        pass
    # 优先走闹钟通道（置顶窗 + 响铃 + 播报 + 系统通知）；
    # 没有就退到普通气泡；再没有才考虑 win10toast。
    alarm = _global_alarm_hook
    if callable(alarm):
        try:
            alarm(text)
            return
        except Exception:  # noqa: BLE001
            log.exception("alarm hook failed in countdown")
    hook = _global_say_hook
    if callable(hook):
        try:
            hook(text)
        except Exception:  # noqa: BLE001
            log.exception("say_to_user hook failed in countdown")
    else:
        # 兜底：调 win10toast（requirements 里默认没装，装了才用）
        try:
            import win10toast
            win10toast.ToastNotifier().show_toast(
                "桌宠倒计时", text, duration=5, threaded=True,
            )
        except Exception:
            # 这里**不能**回头去 emit UI 信号：倒计时跑在后台线程，而 QObject
            # 可能已析构（测试里实测会直接把 pytest 进程打成 access violation）。
            # 诚实告知放在 countdown() 调用时同步做完，见 _has_notify_channel()。
            log.warning("countdown #%d fired but no notification channel: %r",
                        cid, text)


def _has_notify_channel() -> bool:
    """到点时有没有办法惊动主人。

    三条路任一可用即算有：闹钟 hook（置顶窗+响铃）、say hook（冒泡）、
    win10toast（系统通知）。主程序会注入前两个，所以正常情况下恒为 True。
    """
    if callable(_global_alarm_hook) or callable(_global_say_hook):
        return True
    try:
        import win10toast  # noqa: F401
        return True
    except ImportError:
        return False


_global_say_hook: Optional[Callable[[str], None]] = None
_global_alarm_hook: Optional[Callable[[str], None]] = None


def set_say_hook(fn: Optional[Callable[[str], None]]) -> None:
    """由主程序注入：到点时让桌宠主动说话（普通气泡）。"""
    global _global_say_hook
    _global_say_hook = fn


def set_alarm_hook(fn: Optional[Callable[[str], None]]) -> None:
    """由主程序注入：到点走闹钟通道（置顶窗 + 响铃 + 播报 + 系统通知）。

    优先于 set_say_hook —— 只冒气泡主人常常注意不到。
    """
    global _global_alarm_hook
    _global_alarm_hook = fn


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
    # 兜底：即使桌宠进程没了，Windows 到点也会响
    with _lock:
        fire_ts = next((f for c, f, _ in _counters if c == cid),
                       time.time() + s)
    backup = _arm_backup(cid, fire_ts, (message or "").strip() or "时间到啦~")
    if not _has_notify_channel():
        # 倒计时本身是建好了的，但到点**没有任何办法通知主人**
        # （set_say_hook 没注入、win10toast 也没装）。必须在建的那一刻就说清，
        # 否则模型会拿着「到点主人会收到提醒」去骗主人，到点却什么都不会发生。
        # 这里同步判断（工具自己的线程），不要放到后台线程里去 emit UI 信号。
        return (f"已设定倒计时 {ago}（id={cid}），但**当前没有接入任何提醒通道**"
                f"（桌宠主动说话未接线，且未安装 win10toast），到点可能不会有任何通知。"
                f"请如实告诉主人这一点，不要说「到点会提醒你」。"
                + (f"{backup}。" if backup else ""))
    return f"已设定倒计时 {ago}（id={cid}），到点主人会收到提醒{backup}"


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
        # 关键：同时 set 掉 Event，让 _run_countdown 立刻醒来并退出。
        # 少了这一步，工具回报「已取消」但到点仍会提醒。
        ev = _cancel_events.get(cid)
        if ev is not None:
            ev.set()
            _cancel_events.pop(cid, None)
        th = _countdown_threads.get(cid)
    # 等线程真正退出再返回：否则会留下一堆停在 Event.wait 的守护线程，
    # 进程退出时解释器拆全局变量可能把进程带崩（实测 access violation）。
    # 也让「已取消」名副其实——返回时这件事确实不会再发生。
    if th is not None and th.is_alive() and th is not threading.current_thread():
        th.join(timeout=2.0)
    if removed == 0:
        return f"错误：未找到 id={cid} 的倒计时"
    # 撤掉系统级兜底，否则主人取消了、Windows 那边还会照响
    try:
        from app.core import win_alarm
        win_alarm.disarm_key(f"cd{cid}")
    except Exception:  # noqa: BLE001
        pass
    return f"已取消倒计时 #{cid}（到点不会再提醒）"


def _arm_backup(cid: int, fire_ts: float, text: str) -> str:
    """给倒计时注册一个 Windows 计划任务兜底，返回如实说明（不谎称装上了）。"""
    try:
        from app.core import win_alarm
        if not win_alarm.is_available():
            return ""
        if win_alarm.arm(fire_ts, text, key=f"cd{cid}",
                         title="⏰ 桌宠倒计时"):
            return "（已加系统级兜底，桌宠关掉也会响）"
        return "（离现在太近，没能加系统级兜底）"
    except Exception:  # noqa: BLE001
        return ""


def cancel_all() -> int:
    """取消所有活跃倒计时，返回取消个数。

    给测试夹具和进程退出用：确保不留任何停在 Event.wait 的线程。
    """
    with _lock:
        ids = [c[0] for c in _counters]
    n = 0
    for cid in ids:
        if cancel_countdown(cid).startswith("已取消"):
            n += 1
    return n


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


__all__ = ["register", "set_say_hook", "set_alarm_hook", "cancel_all"]