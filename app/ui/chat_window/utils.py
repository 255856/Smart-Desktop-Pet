"""chat_window 常量与无状态工具函数。"""
from __future__ import annotations

import json
import re
from pathlib import Path
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

#: 语音工具 → 语音产物种类（speak / sing）。
#: mcp_voice_status 只是查音色、没有产物，不在这里面。
_VOICE_KIND = {
    "mcp_voice_speak": "speak",
    "mcp_voice_sing": "sing",
}

#: 项目根（app/ui/chat_window/utils.py → 上三级）。
#: 语音产物目录的相对路径按它解析 —— 跟 MCP 语音 server 的 PROJECT_ROOT 一致。
_PROJECT_ROOT = Path(__file__).resolve().parents[3]

#: 语音产物旁路文件的后缀（生产端见 app/mcp/voice_server.py::VOICE_META_SUFFIX）。
#: 这里是消费端：拿不到生产端的约定就退化成纯文本轨迹（界面不编路径）。
_VOICE_META_SUFFIX = ".voice.json"

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


def _tool_display(name: str, args: str) -> Tuple[str, str, str]:
    """(英文工具名, 中文标签, 动作目标)。

    英文工具名是**主**：主人看的是「刚才到底调了哪个工具」，
    英文名（web_search / mcp_voice_sing / open_app）像日志一样专业、
    也便于排查。中文标签只当**副**的小提示留着；工具没有中文标签
    （陌生工具）时就只显示英文名，不重复一遍。
    """
    cn, target = _tool_label(name, args)
    en = str(name or "")
    cn = str(cn or "")
    if not en or cn == en:
        cn = ""
    return en, cn, target


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
    """语音工具的结果摘要：时长 / 体积（都来自工具自己的文本）。

    语音是「听得见」的动作，界面上必须留痕：不然主人只看到一句
    「念一段话 完成」，既不知道念了多久，也不知道音频落在哪。

    这里只留**时长 / 体积**：音频落在哪由**语音条**（voice bubble）负责，
    同一件事不在两处重复刷屏（工具块要安静，语音条才好看）。
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
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# 语音条：把「这次语音的产物」解析成结构化信息
# ---------------------------------------------------------------------------
#
# 语音是**一条消息**，不是工具块里的一行字 —— 主人要一眼看到
# 「念了多久、音频在哪、能不能再听一遍」。所以界面要拿到
# 路径 / 时长 / 文件名这些结构化信息。
#
# 这些信息**不进工具结果文本**（那是要给模型看的人话，模型看见机器标记
# 就会被带偏），而是走旁路：生产端（app/mcp/voice_server.py）在语音产物
# 落盘时顺手写一份 <产物名>.voice.json，界面这边读它。
#
# 解析入口**只有这一个**（resolve_voice_artifact）：解析不出来就返回空
# dict，界面退化成普通工具行 —— 不编路径、不给死按钮。


def _voice_meta_settings() -> Tuple[Path, str]:
    """(语音产物目录, 旁路文件后缀) —— 跟生产端同一份约定。

    生产端见 app/mcp/voice_server.py（voice_out_dir / VOICE_META_SUFFIX）。
    这里是消费端的**唯一**入口：拿不到就退化成纯文本轨迹。
    """
    try:
        from app.mcp.voice_server import VOICE_META_SUFFIX, voice_out_dir
        return voice_out_dir(), VOICE_META_SUFFIX
    except Exception:  # noqa: BLE001
        pass
    # 兜底：同一份配置（voice.output_dir）+ 同一个项目根
    try:
        from app.voice.voice import load_voice_settings
        cfg = load_voice_settings() or {}
    except Exception:  # noqa: BLE001
        cfg = {}
    rel = str(cfg.get("output_dir") or "data/voice_out")
    p = Path(rel).expanduser()
    out_dir = p if p.is_absolute() else (_PROJECT_ROOT / p)
    return out_dir, _VOICE_META_SUFFIX


def _voice_meta_files() -> list:
    """语音产物的旁路文件（按写入时间倒序 —— 新的在前）。"""
    out_dir, suffix = _voice_meta_settings()
    try:
        if not out_dir.is_dir():
            return []
        files = [p for p in out_dir.glob("*" + suffix) if p.is_file()]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return files
    except Exception:  # noqa: BLE001
        return []


def _voice_spoken_text(args: str) -> str:
    """这次语音念 / 唱了什么（从工具参数里取，截断）。"""
    a = {}
    try:
        a = json.loads(args) if args else {}
    except Exception:  # noqa: BLE001
        a = {}
    if not isinstance(a, dict):
        return ""
    for key in ("text", "lyrics"):
        v = a.get(key)
        if isinstance(v, str) and v.strip():
            t = v.strip()
            return t if len(t) <= 60 else t[:60] + "…"
    return ""


def _voice_path_from_text(result: str) -> str:
    """结果文本里的音频路径（「音频在 xxx」）—— 空串 = 文本里没有。"""
    f = _VOICE_FILE_RE.search(result or "")
    if not f:
        return ""
    raw = f.group(1).strip()
    if not raw:
        return ""
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = _PROJECT_ROOT / p
    return str(p)


def _pick_voice_meta(kind: str, dur: float, size_kb: float,
                     at: float = 0.0) -> dict:
    """从旁路文件里挑出**这一次**语音的产物。

    挑法（确定性，不猜）：
      1. 只看同种类的旁路文件；
      2. 时长 / 体积对得上的优先（文本里的数是四舍五入过的，容忍一点误差）；
      3. 同样对得上时取**离 at（消息时间）最近**的那一个 ——
         历史回放时也能挑对当时那一条。
    """
    best = None
    best_key = None
    for p in _voice_meta_files():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(data, dict) or data.get("kind") != kind:
            continue
        if not data.get("path"):
            continue
        d = float(data.get("duration") or 0.0)
        s = float(data.get("size") or 0) / 1024.0
        if dur > 0 and abs(d - dur) <= 0.25:
            same = 0
        elif size_kb > 0 and abs(s - size_kb) <= 0.35:
            same = 0
        else:
            same = 1
        try:
            gap = abs(p.stat().st_mtime - at) if at > 0 else 0.0
        except OSError:
            gap = 0.0
        key = (same, gap)
        if best_key is None or key < best_key:
            best_key = key
            best = data
    return best or {}


#: 语音产物解析结果的缓存。
#: 流式刷新时同一个气泡会被反复渲染（每来一个字重渲一次），不缓存就是
#: 每个字都扫一遍产物目录。缓存里**不含**「文件还在不在」——
#: 那个每次渲染都重新看一眼（文件被删了，界面要如实说）。
_VOICE_ARTIFACT_CACHE: dict = {}
_VOICE_ARTIFACT_CACHE_MAX = 64


def _resolve_voice_meta(name: str, args: str, result: str,
                        at: float = 0.0) -> dict:
    """语音产物信息（不含「文件还在不在」）。解析不出来返回空 dict。"""
    kind = _VOICE_KIND.get(name)
    if not kind:
        return {}
    text = (result or "").strip()
    if not text:
        return {}
    dur_m = _VOICE_DUR_RE.search(text)
    size_m = _VOICE_SIZE_RE.search(text)
    dur = float(dur_m.group(1)) if dur_m else 0.0
    size_kb = float(size_m.group(1)) if size_m else 0.0
    spoken = _voice_spoken_text(args)
    # ① 结果文本里直接给了路径（唱歌一定会给）—— 用它
    raw = _voice_path_from_text(text)
    if raw:
        p = Path(raw)
        if p.is_file():
            return {"kind": kind, "path": str(p), "file_name": p.name,
                    "duration": dur, "size": int(size_kb * 1024),
                    "text": spoken}
    # ② 文本没给路径（念得成功、但没放出来）→ 找旁路文件
    meta = _pick_voice_meta(kind, dur, size_kb, at)
    if meta:
        path = Path(str(meta.get("path") or ""))
        if str(path):
            return {
                "kind": kind,
                "path": str(path),
                "file_name": str(meta.get("file_name") or path.name),
                "duration": float(meta.get("duration") or dur),
                "size": int(meta.get("size") or 0),
                "text": spoken or str(meta.get("text") or ""),
            }
    # ③ 文本给了路径、但文件已经不在 —— 如实说没有，不给死按钮
    if raw:
        p = Path(raw)
        return {"kind": kind, "path": str(p), "file_name": p.name,
                "duration": dur, "size": int(size_kb * 1024),
                "text": spoken}
    return {}


def resolve_voice_artifact(name: str, args: str, result: str,
                           at: float = 0.0) -> dict:
    """语音工具 → 语音产物信息（路径 / 时长 / 体积 / 文件名 / 在不在）。

    这是**唯一**的语音产物解析入口（界面各处都走它）：
      · 路径 / 时长来自工具自己的结果文本 + 生产端的旁路文件；
      · 「文件还在不在」每次都重新看一眼 —— 文件被删了要说没有，
        不能留一个点了没反应的死按钮。
    解析不出来返回空 dict —— 界面退化成普通工具行，**不编**。
    """
    key = (name, args, result, round(float(at or 0.0), 3))
    cached = _VOICE_ARTIFACT_CACHE.get(key)
    if cached is None:
        cached = _resolve_voice_meta(name, args, result, at)
        if len(_VOICE_ARTIFACT_CACHE) >= _VOICE_ARTIFACT_CACHE_MAX:
            _VOICE_ARTIFACT_CACHE.clear()
        _VOICE_ARTIFACT_CACHE[key] = cached
    if not cached:
        return {}
    out = dict(cached)
    try:
        out["exists"] = Path(str(cached.get("path") or "")).is_file()
    except Exception:  # noqa: BLE001
        out["exists"] = False
    return out


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
    "_unpack_tool", "_tool_label", "_tool_display", "_tool_status",
    "_unpack_status_mark", "_tool_step_class", "_tool_fail_reason",
    "_voice_result_note", "resolve_voice_artifact", "_VOICE_KIND",
    "_html_escape", "_format_time_short", "_img_open_key",
]