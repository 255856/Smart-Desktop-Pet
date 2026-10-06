"""chat_window 子模块：常量与无状态工具函数。

集中所有「不需要 ChatWindow 实例状态」的辅助代码：
    - 危险工具中文标签（弹窗显示）
    - 工具 UI 元数据（中文名 + 参数键）
    - HTML 转义 / 时间格式化
    - 工具结果状态判定

被 chat_window 主类与 test_chat_rendering 共用。
"""
from __future__ import annotations

import json
from typing import Tuple


# 危险工具的中文标签（弹窗里给主人看）
_DANGEROUS_LABELS = {
    "open_app": "打开应用",
    "open_website": "打开网址",
    "lock_screen": "锁屏",
    "kill_process": "杀进程",
    "run_script": "执行脚本",
    "set_wifi": "开关 Wi-Fi",
    "set_bluetooth": "开关蓝牙",
    "shutdown_computer": "关机 / 重启",
}

# 危险工具确认弹窗的等待上限（秒）。超时按「未确认」处理，不算用户拒绝。
_CONFIRM_TIMEOUT_S = 60.0


# 工具名 -> (中文名, 动作目标参数键，按优先级)
_TOOL_UI_META = {
    "web_search": ("联网搜索", ["query", "q", "keyword"]),
    "open_website": ("打开网页", ["url", "website", "query"]),
    "open_app": ("启动应用", ["app_name", "name", "app"]),
    "list_installed_apps": ("查找应用", ["query", "keyword"]),
    "calculate": ("计算", ["expression", "query"]),
    "convert_units": ("单位换算", ["query", "expression"]),
    "add_reminder": ("设置提醒", ["content", "text", "title", "message"]),
    "list_reminders": ("查看提醒", []),
    "delete_reminder": ("删除提醒", ["content", "title", "index"]),
    "remember_fact": ("记住信息", ["content", "fact", "key"]),
    "recall_memory": ("回忆信息", ["query", "key", "topic"]),
    "forget_memory": ("忘记信息", ["key", "query"]),
    "take_screenshot": ("屏幕截图", []),
    "system_info": ("系统信息", []),
    "get_current_time": ("查询时间", ["timezone"]),
    "date_info": ("查询日期", ["query"]),
    "get_pet_status": ("桌宠状态", []),
    "feed_self": ("投喂", ["food"]),
    "play_animation": ("播放动画", ["animation", "name"]),
    "change_pet_emotion": ("切换表情", ["emotion"]),
    "say_to_user": ("发送消息", ["text", "content"]),
    "clipboard_copy": ("复制到剪贴板", ["text", "content"]),
    "send_notification": ("发送通知", ["title", "message"]),
    "list_desktop_files": ("查看桌面文件", []),
    "read_text_file": ("读取文件", ["path", "file"]),
    "open_task_manager": ("打开任务管理器", []),
    "open_control_panel": ("打开控制面板", []),
    "open_windows_settings": ("打开系统设置", []),
    "open_file_explorer": ("打开文件资源管理器", []),
    "open_terminal": ("打开终端", []),
    "open_notepad": ("打开记事本", []),
    "open_calculator": ("打开计算器", []),
}

_CIRCLED_NUMS = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩"]

_TOOL_FAIL_MARKERS = ("失败", "错误", "无法", "未安装", "没有找到", "找不到")


def _unpack_tool(t):
    """tools 元组兼容 2 元 (name, summary) 与 3 元 (name, args, result)。"""
    if len(t) >= 3:
        return t[0], t[1] or "", t[2] or ""
    return t[0], "", (t[1] if len(t) > 1 else "") or ""


def _tool_label(name: str, args: str) -> Tuple[str, str]:
    """返回 (中文名, 动作目标短文本)。"""
    cn, keys = _TOOL_UI_META.get(name, (name, []))
    target = ""
    a = {}
    try:
        a = json.loads(args) if args else {}
    except Exception:  # noqa: BLE001
        a = {}
    if isinstance(a, dict):
        for k in keys:
            v = a.get(k)
            if v:
                target = str(v)
                break
        if not target:
            # 兜底：取第一个短参数
            for v in a.values():
                if isinstance(v, (str, int, float)) and 0 < len(str(v)) <= 30:
                    target = str(v)
                    break
    if target:
        if name == "open_website" and "://" in target:
            target = target.split("://", 1)[1]
        if target.startswith("www."):
            target = target[4:]
        if len(target) > 20:
            target = target[:20] + "…"
    return cn, target


def _tool_status(result: str) -> str:
    """工具结果状态：'cancel' / 'fail' / 'ok'。

    失败标记涵盖：取消 / 失败 / 错误 / 未安装 / 找不到 等。
    """
    r = result or ""
    if "取消" in r:
        return "cancel"
    if any(k in r for k in _TOOL_FAIL_MARKERS):
        return "fail"
    return "ok"


def _html_escape(text: str) -> str:
    """HTML 字符转义（防 XSS + 让浏览器不解析）。"""
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;"))


def _format_time_short(ts: float) -> str:
    """把时间戳格式化成 HH:MM（用于气泡上方 meta）。"""
    import datetime
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%H:%M")
    except Exception:
        return ""


__all__ = [
    "_DANGEROUS_LABELS", "_CONFIRM_TIMEOUT_S",
    "_TOOL_UI_META", "_CIRCLED_NUMS", "_TOOL_FAIL_MARKERS",
    "_unpack_tool", "_tool_label", "_tool_status",
    "_html_escape", "_format_time_short",
]