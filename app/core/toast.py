"""Windows 原生系统通知（任务栏右下角的 toast）。

这是「直接在电脑上设置闹钟」能落地的部分：它弹的是**系统级通知**，
进 Windows 通知中心，桌宠窗口隐藏 / 最小化 / 被别的窗口盖住都照样看得到。

依赖 `winsdk`（`pip install winsdk`）。装不上就返回 False，由调用方退回
桌宠自己的置顶弹窗——**绝不假装通知已经发出**。

API 说明（winsdk 1.0.0b10 实测）：
    doc  = XmlDocument(); doc.load_xml(xml)
    toast = ToastNotification(doc)                       # 构造时传入
    ToastNotificationManager.create_toast_notifier(AUMID).show(toast)
注意 `toast.set_xml(doc)` 在这个版本会抛 `TypeError: Invalid parameter count`，
所以只能走构造函数。
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# 用 PowerShell 的 AUMID：它一定注册过，省去给桌宠建带
# System.AppUserModel.ID 的开始菜单快捷方式这一堆事。
_APP_ID = "Microsoft.Windows.PowerShell"
_TITLE = "桌宠小汐"

# 探测结果缓存：import 一次就够了，别每次到点都去 importlib 折腾
_probe: bool | None = None


def _load():
    """返回 (dom, ToastNotification, ToastNotificationManager) 或 None。"""
    global _probe
    if _probe is False:
        return None
    try:
        import winsdk.windows.data.xml.dom as dom  # type: ignore
        from winsdk.windows.ui.notifications import (  # type: ignore
            ToastNotification, ToastNotificationManager,
        )
        _probe = True
        return dom, ToastNotification, ToastNotificationManager
    except ImportError:
        _probe = False
        log.debug("winsdk 未安装，跳过系统通知")
        return None
    except Exception:  # noqa: BLE001
        _probe = False
        log.debug("winsdk 导入异常", exc_info=True)
        return None


def is_available() -> bool:
    """本机能否发系统通知。"""
    return _load() is not None


def missing_hint() -> str:
    """不可用时给主人看的说明。"""
    if is_available():
        return ""
    return "（系统通知不可用：未安装 winsdk，已退回桌宠自己的置顶弹窗）"


def _esc(s: str) -> str:
    """XML 转义——通知文案是纯文本，但也不该把 XML 结构搞坏。"""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def show(title: str, message: str, *, alarm: bool = True) -> bool:
    """弹一条系统通知。返回是否成功**投递给了系统外壳**。

    注意：返回 True 只代表 Windows 接收了这条通知；专注助手（Focus Assist）
    开着、或通知被设置成「仅横幅」时，主人在屏幕上仍可能只看到一闪。
    所以调用方不要拿它当「主人一定看到了」的凭据。
    """
    if not (message or "").strip():
        message = "（无内容）"
    parts = _load()
    if parts is None:
        return False
    dom, ToastNotification, ToastNotificationManager = parts

    audio = ('<audio src="ms-winsoundevent:Notification.Looping.Alarm" loop="true"/>'
             if alarm else
             '<audio src="ms-winsoundevent:Notification.SMS"/>')
    scenario = ' scenario="alarm"' if alarm else ""
    xml = f"""<toast launch="action"{scenario} duration="long">
  <visual>
    <binding template="ToastGeneric">
      <text>{_esc(title or _TITLE)}</text>
      <text>{_esc(message)}</text>
    </binding>
  </visual>
  {audio}
</toast>"""
    try:
        doc = dom.XmlDocument()
        doc.load_xml(xml)
        toast = ToastNotification(doc)
        notifier = ToastNotificationManager.create_toast_notifier(_APP_ID)
        notifier.show(toast)
        log.info("已发系统通知：%s / %s", title, str(message)[:60])
        return True
    except Exception:  # noqa: BLE001
        log.warning("系统通知发送失败", exc_info=True)
        return False


__all__ = ["show", "is_available", "missing_hint"]
