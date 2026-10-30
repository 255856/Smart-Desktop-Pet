"""chat_window 常量与无状态工具函数。"""
from __future__ import annotations

import json
import re
from typing import Tuple

# 工具结果的成败判定**不在本模块自维护词表**（Stage 3.5）：
#   · 成败 / 取消 → 脑层的 _tool_result_state（app/brain/agent.py）——
#     它读的是工具层那张唯一的判定表（classify_legacy_result + 用户取消标记），
#     跟工具信封的 status 同一个出处，不会出现「脑层说失败、界面显示成功」。
#   · 本模块只多认一种：桌宠拒绝（companion 策略的产物，工具层不认）。
# 权威顺序：**信封 status 优先**（Stage 3.5 修复）——
#   工具事件现在把信封的 status 一起送到界面
#   （app/brain/agent.py 的 yield "tool", name, args, _result_text(env), status），
#   界面直接用它渲染成败标记，不再从结果文本猜成败：
#   失败文本没有「错误：」前缀会被显示成「完成」（谎报成功），
#   成功文本里碰巧出现「错误：」会被显示成「失败」（谎报失败）。
#   只有拿不到 status 的调用方（老路径 / 只有文本的折叠预览）才回退到文本判定。
from app.brain.agent import _tool_result_state
from app.engine.tools._core import _ERROR_PREFIXES


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
    # 语音 MCP（Stage 6）：桌宠自己决定要不要发语音/唱歌
    "mcp_voice_speak": ("念一段话", ["text"]),
    "mcp_voice_sing": ("唱几句", ["lyrics"]),
    "mcp_voice_status": ("查本机音色", []),
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

#: 语音类工具（桌宠自己决定要不要发语音 / 唱歌）。
#: 语音是「听得见的动作」，界面上必须留下痕迹：叫什么、成了没成、
#: 念了多久、音频落在哪 —— 全部来自工具自己的结果文本。
_VOICE_TOOLS = ("mcp_voice_speak", "mcp_voice_sing", "mcp_voice_status")

#: 工具步骤状态 → 行 / 标记的 CSS 类名。
#: 颜色**不在这里写死**：统一交给 CHAT_MD_CSS（亮 / 暗两套主题同步），
#: 否则暗色主题下这几种标记会跟背景糊在一起。
_TOOL_STEP_CLASS = {
    "ok": "ok",
    "fail": "failed",
    "cancel": "cancelled",
    "rejected": "rejected",
}

# 工具层的失败前缀契约 —— UI 侧只是**转发**这份表，不自己抄一份
# （app/ui/chat_window/__init__.py 还在导入这个名字）。
# 判定成败不再用这张表：走脑层 _tool_result_state（工具层判定表的唯一出口）。
_TOOL_FAIL_MARKERS = _ERROR_PREFIXES

# companion REJECT/REFUSE 标记（让桌宠拒绝可视化）
_COMPANION_REJECT_MARKERS = (
    "rejected_by_companion",
    '"status": "rejected"',
    "桌宠当前状态拒绝执行",
)


def _unpack_tool(t):
    """tools 元组兼容 2 元 (name, summary)、3 元 (name, args, result)
    与 4 元 (name, args, result, status)。

    第 4 元是**信封状态**（ok / error / denied / cancel，Stage 3.5）：
    老记录 / 历史回放没有它 → 空串 → 调用方回退到文本判定。
    """
    if len(t) >= 4:
        # 只认**认识的状态字符串**：老记录的第 4 元可能是别的东西
        # （历史上有 (name, args, result, 100) 这种形态），认不出来就当没有，
        # 走文本兜底 —— 不能把不认识的东西当成成功。
        raw = t[3]
        st = raw.strip().lower() if isinstance(raw, str) else ""
        return t[0], t[1] or "", t[2] or "", (st if st in _STATUS_FROM_ENVELOPE else "")
    if len(t) >= 3:
        return t[0], t[1] or "", t[2] or "", ""
    return t[0], "", (t[1] if len(t) > 1 else "") or "", ""


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


#: 信封状态 → 界面状态。denied（策略拒绝：白名单 / 等级闸）算**失败**，
#: 不是「取消」—— 主人没点取消，是策略不让做，两者必须分开显示。
_STATUS_FROM_ENVELOPE = {
    "ok": "ok",
    "error": "fail",
    "denied": "fail",
    "fail": "fail",
    "cancel": "cancel",
    "rejected": "rejected",
}


def _tool_status(result: str, status: str = None) -> str:
    """工具结果状态：'cancel' / 'rejected' / 'fail' / 'ok'。

    status 给了就用它 —— 那是**工具信封里的权威答案**（Stage 3.5）：
      · 'ok'      → 成功（哪怕结果文本里出现「错误：」也不改判）
      · 'error'   → 失败
      · 'denied'  → 失败（策略拒绝 ≠ 主人取消）
      · 'cancel'  → 主人主动取消（信封 denied + error_code=user_cancelled）
    没给 status（老调用方 / 只有文本的折叠预览）才回退到文本判定：
    成败与取消交给脑层的 _tool_result_state（工具层判定表的唯一出口）；
    这里额外认「桌宠拒绝」（companion 策略的产物，工具层不认）。
    """
    r = result or ""
    st = str(status or "").strip().lower()
    if st:
        mapped = _STATUS_FROM_ENVELOPE.get(st)
        if mapped:
            if mapped == "fail" and any(k in r for k in _COMPANION_REJECT_MARKERS):
                return "rejected"
            return mapped
        # 认不出的状态 → 落到文本兜底（别把不认识的状态当成成功）
    if any(k in r for k in _COMPANION_REJECT_MARKERS):
        return "rejected"
    return _tool_result_state(r)


def _tool_step_class(status: str) -> str:
    """工具状态 → 步骤行 / 标记的 CSS 类名（ok / failed / cancelled / rejected）。

    颜色不在这儿写死 —— 统一交给 CHAT_MD_CSS 的两套主题，
    否则暗色主题下这几种标记会跟背景糊在一起。
    """
    return _TOOL_STEP_CLASS.get(status, "ok")


def _tool_fail_reason(result: str, limit: int = 60) -> str:
    """失败 / 取消 / 拒绝的**原因**（第一行，去掉失败前缀，截断）。

    工具卡片原来只写一个「[失败]」，失败原因一个字都看不到 ——
    主人不知道是权限不够还是程序崩了。这里把工具自己给的原文
    提出来（只取第一行，截断到 limit 字），原样显示，不做改写。
    """
    text = (result or "").strip()
    if not text:
        return ""
    for prefix in ("失败：", "失败:", "错误：", "错误:"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    line = text.splitlines()[0].strip() if text else ""
    if not line:
        return ""
    if len(line) > limit:
        line = line[:limit] + "…"
    return line


#: 语音结果文本里的「（24.1 KB，3.4 秒）」和「音频在 <路径>」
_VOICE_SIZE_RE = re.compile(r"([\d.]+)\s*KB")
_VOICE_DUR_RE = re.compile(r"([\d.]+)\s*秒")
_VOICE_FILE_RE = re.compile(r"音频在\s*([^。）)]+)")


def _voice_result_note(name: str, result: str) -> str:
    """语音工具的结果摘要：时长 / 体积 / 音频文件（都来自工具自己的文本）。

    语音是「听得见」的动作，界面上必须留痕：不然主人只看到一句
    「念一段话 ✓」，既不知道念了多久，也不知道音频落在哪。
    解析不出来就返回空串 —— 宁可少说，不能编。
    """
    if name not in _VOICE_TOOLS:
        return ""
    text = (result or "").strip()
    if not text:
        return ""
    parts = []
    dur = _VOICE_DUR_RE.search(text)
    if dur:
        parts.append("%s 秒" % dur.group(1))
    size = _VOICE_SIZE_RE.search(text)
    if size:
        parts.append("%s KB" % size.group(1))
    f = _VOICE_FILE_RE.search(text)
    if f:
        parts.append(f.group(1))
    return " · ".join(parts)


def _unpack_status_mark(result: str, status: str = None) -> str:
    """返回工具结果对应的彩色 mark html,用于折叠态预览行."""
    status = _tool_status(result, status)
    cls = "tool-mark tool-%s" % _tool_step_class(status)
    if status == "cancel":
        return '<span class="%s">⊘ 取消</span>' % cls
    if status == "rejected":
        # 桌宠拒绝（companion REJECT/REFUSE）可视化
        return '<span class="%s">桌宠拒绝</span>' % cls
    if status == "fail":
        return '<span class="%s">✕ 失败</span>' % cls
    return '<span class="%s">✓</span>' % cls


def _html_escape(text: str) -> str:
    """HTML 字符转义（防 XSS + 让浏览器不解析）。"""
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;"))


def _img_open_key(url: str) -> str:
    """给图片 src 算稳定 KEY（SHA1 前 10 位）。"""
    import hashlib
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]


def _format_time_short(ts: float) -> str:
    """把时间戳格式化成 HH:MM（用于气泡上方 meta）。"""
    import datetime
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%H:%M")
    except Exception:
        return ""


__all__ = [
    "_DANGEROUS_LABELS", "_CONFIRM_TIMEOUT_S",
    "_TOOL_UI_META", "_CIRCLED_NUMS",
    "_VOICE_TOOLS", "_TOOL_STEP_CLASS",
    "_unpack_tool", "_tool_label", "_tool_status", "_unpack_status_mark",
    "_tool_step_class", "_tool_fail_reason", "_voice_result_note",
    "_html_escape", "_format_time_short", "_img_open_key",
]