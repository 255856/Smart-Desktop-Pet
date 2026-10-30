# -*- coding: utf-8 -*-
"""「降级了」这件事要**说给主人听**（不是写一行日志）。

日志要主人自己去翻，而这里的症状是「桌宠好像变笨了」——主人只会觉得莫名其妙，
不会去查日志。所以走桌宠冒泡 / Windows 系统通知这条已有的通道。

原来住在已经删掉的向量索引模块里，那模块下线后搬出来，
现在只有记忆 sidecar 这一个调用方（app/brain/brain_controller.py）。
"""
from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger(__name__)

_NOTICE_LOCK = threading.Lock()
#: 已经提醒过主人的内容（键 → 文案）。面板 / 测试可以看它。
_OWNER_NOTICES: dict = {}

#: 花费告警的去重表（键 → {"message": 文案, "at": 单调时钟}）。
#: **与降级告警分开**：降级是「同一个原因永远只说一次」；花费告警是
#: 「同一个会话在一段时间内只说一次」—— 超预算是「这轮偏贵」，主人有权被
#: 反复提醒，但也不该被同一个会话刷屏。
_BUDGET_NOTICES: dict = {}


def _owner_notice_sink(title: str, message: str) -> None:
    """默认的提醒出口：**桌宠冒泡说给主人听** + Windows 系统通知。

    - 桌宠的「说一声」通道（app/engine/tools/_timer.py 的 say hook）：
      主程序会注入一个把文字冒泡给主人的函数（app/engine/tools/__init__.py
      里注入的 _bubble）。跨线程调用是安全的（Qt 信号 emit，Qt 会排队到
      接收者线程）—— 跟倒计时那条路一样。
    - 没有这个通道时（测试 / 没起 UI）→ 退到 Windows 系统通知
      （app/core/toast.py，需要 winsdk）。

    单独抽成函数是为了让测试能替换它（不真的弹窗）。
    """
    said = False
    try:
        from app.engine.tools import _timer
        hook = getattr(_timer, "_global_say_hook", None)
        if callable(hook):
            hook(message)
            said = True
    except Exception as e:  # noqa: BLE001 - 没有气泡通道就退到系统通知
        log.info("桌宠气泡通道不可用（退到系统通知）：%s", e)
    if said:
        return
    try:
        from app.core.toast import show
        show(title, message, alarm=False)
    except Exception as e:  # noqa: BLE001 - 提醒失败不许影响任何东西
        log.info("系统通知发不出去（不影响功能）：%s", e)


def notify_owner_degraded(key: str, title: str, message: str) -> bool:
    """告诉主人某件能力降级了（**同一个原因只提醒一次**）。

    返回是否真的提醒了（False = 这个原因已经提醒过了）。
    """
    with _NOTICE_LOCK:
        if key in _OWNER_NOTICES:
            return False
        _OWNER_NOTICES[key] = message

    def _run() -> None:
        try:
            _owner_notice_sink(title, message)
        except Exception as e:  # noqa: BLE001 - 提醒失败不许影响任何东西
            log.info("系统通知发不出去（不影响功能）：%s", e)

    # 放后台线程：**提醒永远不许拖慢聊天路径**
    threading.Thread(target=_run, daemon=True,
                     name="OwnerNotice").start()
    return True


def notify_owner_budget_exceeded(key: str, title: str, message: str,
                                 min_interval_seconds: float = 0.0) -> bool:
    """告诉主人「这一轮的输入超预算了」（**不是失败**）。

    走跟降级告警**同一条出口**（桌宠气泡 → Windows 系统通知），但去重表分开，
    且支持「间隔到了可以再说」：
      - 同一个 key 在 min_interval_seconds 内只提醒一次（0 = 不限间隔）；
      - 间隔到了会再提醒一次 —— 因为「超预算」不是一次性事件。

    返回是否真的提醒了（False = 刚提醒过，没打扰主人）。
    """
    try:
        interval = float(min_interval_seconds or 0)
    except (TypeError, ValueError):
        interval = 0.0
    now = time.monotonic()
    with _NOTICE_LOCK:
        prev = _BUDGET_NOTICES.get(key)
        if prev is not None and interval > 0:
            try:
                elapsed = now - float(prev.get("at") or 0.0)
            except (TypeError, ValueError):
                elapsed = interval
            if elapsed < interval:
                return False
        _BUDGET_NOTICES[key] = {"message": message, "at": now}

    def _run() -> None:
        try:
            _owner_notice_sink(title, message)
        except Exception as e:  # noqa: BLE001 - 提醒失败不许影响任何东西
            log.info("预算告警发不出去（不影响功能）：%s", e)

    # 放后台线程：**提醒永远不许拖慢聊天路径**
    threading.Thread(target=_run, daemon=True,
                     name="OwnerNoticeBudget").start()
    return True


def reset_budget_notice(key: str = "") -> None:
    """清掉花费告警的去重记录（key 为空 = 全清；测试用）。"""
    with _NOTICE_LOCK:
        if key:
            _BUDGET_NOTICES.pop(key, None)
        else:
            _BUDGET_NOTICES.clear()


__all__ = ["notify_owner_degraded", "notify_owner_budget_exceeded",
           "reset_budget_notice", "_owner_notice_sink", "_OWNER_NOTICES",
           "_BUDGET_NOTICES"]
