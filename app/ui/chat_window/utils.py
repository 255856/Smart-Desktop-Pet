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
    # 两段式：结果里带**任务号** = 合成**还没好** → 现在没有产物。
    # 直接返回，别去扫产物目录（那是一次无谓的全目录扫描，
    # 而且流式重渲时每字都要扫一遍）。
    if voice_job_id(text):
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


#: 合成任务状态文件的后缀（生产端见 app/mcp/voice_server.py::VOICE_JOB_SUFFIX）。
_VOICE_JOB_SUFFIX = ".voicejob.json"

#: 工具结果文本里的任务号（「任务号：sing_20260101-120000_1a2b」）。
_VOICE_JOB_RE = re.compile(r"任务号[:：]\s*([A-Za-z0-9_\-]+)")


def voice_job_id(result: str) -> str:
    """从语音工具的结果文本里取出**合成任务号**（没有就返回空串）。

    语音工具是「先回复、后合成」的：它**立刻**返回一句「已经受理」，
    真正的合成在后台跑。界面靠这个任务号盯住**那一次**合成 ——
    只盯 pending 的任务，不扫整个产物目录。
    """
    m = _VOICE_JOB_RE.search(result or "")
    return m.group(1) if m else ""


def voice_job_path(job_id: str) -> Path:
    """合成任务状态文件的绝对路径（跟生产端同一个约定）。"""
    out_dir, _suffix = _voice_meta_settings()
    return out_dir / (str(job_id) + _VOICE_JOB_SUFFIX)


def read_voice_job(job_id: str) -> dict:
    """读一个合成任务的状态（读不到 / 不是 json 就返回空 dict）。"""
    try:
        path = voice_job_path(job_id)
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _voice_play_key(path: str) -> str:
    """给语音产物算稳定 KEY（SHA1 前 10 位）—— 语音条播放锚点用它。

    锚点写成页内锚点（#vp-KEY）而不是自定义 scheme：
    QTextBrowser 点自定义 scheme 的链接会把整份文档当新地址重载，
    聊天区直接清空（详见 chat_window/_on_chat_anchor_clicked 的说明）。
    """
    import hashlib
    return hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:10]


def _format_time_short(ts: float) -> str:
    """把时间戳格式化成 HH:MM（用于气泡上方 meta）。"""
    import datetime
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%H:%M")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 逐句投递（qq-bridge 那种「一句话一条消息」）
# ---------------------------------------------------------------------------

#: companion_config.yaml 里的段名（间隔等参数都在这段里）
REPLY_PACING_SECTION = "reply_pacing"

#: 句末标点（中英文）+ 分号 + 换行 —— 一句话一条消息的切点
_SENT_END_CHARS = "。！？!?…；;\n"

#: URL —— 不可拆分的原子（qq-bridge 的 splitLongSegment 同款规矩）
_URL_RE = re.compile(r"(?:https?://|www\.)[^\s，。；、！？\n]+", re.IGNORECASE)

#: URL 占位符（掩码用）：\x00U<n>\x00 —— 拆句时**整块**不许切开
_URL_PLACEHOLDER_RE = re.compile("\x00U(\\d+)\x00")


def _read_yaml_section(section: str) -> dict:
    """读 companion_config.yaml 的某一段（只读，不改）。

    走 yaml 原文：companion_config 的 dataclass 会丢掉没登记的字段
    （跟 app/brain/compaction.py 读 context_compaction 段一个路子）。
    加载顺序：用户覆盖表 > 仓库默认表。
    """
    try:
        from app.core import companion_config as cc
    except Exception:  # noqa: BLE001 —— 配置读不到不该影响聊天
        return {}
    try:
        base = cc._read_yaml(cc._default_yaml_path()) or {}
        user = cc._read_yaml(cc._user_yaml_path()) or {}
        merged = cc._merge(base, user)
    except Exception:  # noqa: BLE001
        return {}
    block = merged.get(section)
    return block if isinstance(block, dict) else {}


def _as_bool(value, default: bool = True) -> bool:
    """yaml 里的开关（bool / "true" / "false" / 1 / 0 都认）。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("false", "no", "off", "0", "关闭", "关"):
        return False
    if text in ("true", "yes", "on", "1", "开"):
        return True
    return default


def reply_pacing_settings() -> dict:
    """逐句投递的设置（来自 companion_config.yaml 的 reply_pacing 段）。

    读不到就用内置默认值 —— 聊天不能因为配置读不出来就不说话。
    """
    raw = _read_yaml_section(REPLY_PACING_SECTION)

    def _int(key: str, default: int, low: int, high: int) -> int:
        try:
            val = int(raw.get(key, default))
        except (TypeError, ValueError):
            return default
        return max(low, min(high, val))

    def _float(key: str, default: float, low: float, high: float) -> float:
        try:
            val = float(raw.get(key, default))
        except (TypeError, ValueError):
            return default
        return max(low, min(high, val))

    min_gap = _int("min_gap_ms", 1000, 0, 60000)
    max_gap = _int("max_gap_ms", 3000, 0, 60000)
    if max_gap < min_gap:
        min_gap, max_gap = max_gap, min_gap
    long_min = _int("long_gap_min_ms", 5000, 0, 120000)
    long_max = _int("long_gap_max_ms", 10000, 0, 120000)
    if long_max < long_min:
        long_min, long_max = long_max, long_min
    return {
        "enabled": _as_bool(raw.get("enabled"), True),
        "min_gap_ms": min_gap,
        "max_gap_ms": max_gap,
        "long_gap_probability": _float("long_gap_probability", 0.15, 0.0, 1.0),
        "long_gap_min_ms": long_min,
        "long_gap_max_ms": long_max,
        "max_chars_per_bubble": _int("max_chars_per_bubble", 200, 20, 2000),
    }


def compute_reply_gap_ms(settings: dict, rng=None) -> int:
    """两句之间的间隔（毫秒）—— 对齐 qq-bridge 的 computeGapsV2（auto 模式）。

    auto：常规间隔在 [min_gap_ms, max_gap_ms] 里随机；
          按 long_gap_probability 的概率给一个**长停顿**（更像真人）。
    """
    import random as _random
    rand = rng or _random
    lo = int(settings.get("min_gap_ms", 1000))
    hi = int(settings.get("max_gap_ms", 3000))
    if hi < lo:
        lo, hi = hi, lo
    prob = float(settings.get("long_gap_probability", 0.0) or 0.0)
    if prob > 0 and rand.random() < prob:
        llo = int(settings.get("long_gap_min_ms", 5000))
        lhi = int(settings.get("long_gap_max_ms", 10000))
        if lhi < llo:
            llo, lhi = lhi, llo
        return int(rand.randint(llo, lhi))
    return int(rand.randint(lo, hi))


def _is_cjk_char(ch: str) -> bool:
    """是不是汉字（qq-bridge 的 isCjkChar 同款：只认汉字，不认中文标点）。"""
    if not ch:
        return False
    code = ord(ch[0])
    return ((0x4E00 <= code <= 0x9FFF)
            or (0x3400 <= code <= 0x4DBF)
            or (0xF900 <= code <= 0xFAFF))


def _split_by_cjk_spaces(text: str) -> list:
    """按「有意的空格」分条（qq-bridge 的 splitByCjkSpaces 思路）。

    真人不会主动用空格 → AI 回复里的空格当成「这里要分条」的信号；
    空格两侧只要有一侧是汉字就分（两边都是拉丁/数字就把空格留着）。

    跟 qq-bridge 的差别：左边**以数字结尾**时不拆 ——
    「5 秒」「3.4 KB」这种「数字 + 单位」不该被劈成两条。
    """
    tokens = [t for t in re.split(r"\s+", text) if t.strip()]
    if len(tokens) <= 1:
        return [text.strip()] if text.strip() else []
    groups = []
    cur = tokens[0]
    for i in range(1, len(tokens)):
        prev_last = cur[-1] if cur else ""
        curr_first = tokens[i][0] if tokens[i] else ""
        left_is_number = prev_last.isdigit()
        if (not left_is_number) and (
                _is_cjk_char(prev_last) or _is_cjk_char(curr_first)):
            groups.append(cur)
            cur = tokens[i]
        else:
            cur = cur + " " + tokens[i]
    if cur:
        groups.append(cur)
    return groups


def _hard_split(text: str, max_chars: int) -> list:
    """按长度硬拆：URL 占位符整块走，emoji（代理对）不被劈开。

    跟 qq-bridge 的 splitLongSegment 一个思路：宁可某段略超长，
    也绝不把网址 / 半个 emoji 切开（切开就是乱码）。
    """
    out = []
    spans = [(m.start(), m.end()) for m in _URL_PLACEHOLDER_RE.finditer(text)]
    i = 0
    n = len(text)
    while i < n:
        end = min(i + max_chars, n)
        if end < n:
            # 别在 URL 占位符中间下刀
            for s, e in spans:
                if s < end < e:
                    end = s
                    break
            # 别在代理对中间下刀（高代理后面必须跟着低代理）
            if end - i > 1 and 0xD800 <= ord(text[end - 1]) <= 0xDBFF:
                end -= 1
        piece = text[i:end]
        if piece:
            out.append(piece)
        i = end if end > i else i + max_chars
    return out


def _split_long_piece(piece: str, max_chars: int) -> list:
    """太长的句子：先按次要标点拆，再按长度硬拆（不丢字）。"""
    text = piece.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    # 次要标点（，、；：,;:）—— 标点跟着前一段
    tokens = re.split(r"([，、；：,;:])", text)
    groups = []
    for i in range(0, len(tokens), 2):
        chunk = (tokens[i] or "") + (tokens[i + 1] if i + 1 < len(tokens) else "")
        if chunk.strip():
            groups.append(chunk.strip())
    out = []
    cur = ""
    for grp in groups:
        if not cur:
            cur = grp
        elif len(cur) + len(grp) <= max_chars:
            cur += grp
        else:
            out.append(cur)
            cur = grp
    if cur:
        out.append(cur)
    final = []
    for grp in out:
        final.extend(_hard_split(grp, max_chars))
    return final


def split_reply_sentences(text: str, max_chars: int = 200,
                          split_tail: bool = False) -> Tuple[list, str]:
    """把一段回复拆成「一句一条」（对齐 qq-bridge 的分条思路）。

    返回 (sentences, tail)：
      · sentences —— 已经说完整的话，每条一句（带句末标点）；
      · tail     —— 末尾还没说完整的那半句（等下一段文字到了再拼）。

    安全规矩（qq-bridge 的 splitLongSegment 同款）：
      · URL 是原子 —— 不在网址中间断开；
      · 代理对（emoji）不被劈成两半；
      · 不丢字 —— 拼回去 == 原文本（只差切分点上的空白）。

    split_tail=True 时（收尾阶段用）连尾巴也按长度拆开；
    流式阶段要 False —— 尾巴还在长，提前拆会导致同一段文字被拆两次。
    """
    if not text:
        return [], ""
    try:
        limit = max(20, int(max_chars or 200))
    except (TypeError, ValueError):
        limit = 200

    # ① URL 先挖走（占位符是不可切分的最小单位）
    urls = []

    def _mask(m):
        urls.append(m.group(0))
        return "\x00U%d\x00" % (len(urls) - 1)

    masked = _URL_RE.sub(_mask, text)

    # ② 按句末标点切
    pieces = []
    buf = []
    i = 0
    n = len(masked)
    while i < n:
        ch = masked[i]
        buf.append(ch)
        if ch in _SENT_END_CHARS:
            j = i + 1
            # 顺手吃掉收尾的引号 / 括号（"…。" 里的引号跟着上一句）
            while j < n and masked[j] in "”’\"'）】」』":
                buf.append(masked[j])
                j += 1
            i = j
            pieces.append("".join(buf))
            buf = []
            continue
        i += 1
    tail = "".join(buf)

    # ③ 空格分条（qq-bridge 的 splitByCjkSpaces）→ 太长的再拆
    sentences = []
    for piece in pieces:
        for part in _split_by_cjk_spaces(piece):
            sentences.extend(_split_long_piece(part, limit))
    tail_parts = _split_long_piece(tail, limit) if tail.strip() else []
    if split_tail and tail_parts:
        sentences.extend(tail_parts[:-1])
        tail = tail_parts[-1]
    else:
        tail = tail.strip()

    # ④ URL 还原
    def _restore(s: str) -> str:
        def _sub(m):
            idx = int(m.group(1))
            return urls[idx] if 0 <= idx < len(urls) else ""
        return _URL_PLACEHOLDER_RE.sub(_sub, s)

    sentences = [_restore(s).strip() for s in sentences]
    sentences = [s for s in sentences if s]
    tail = _restore(tail).strip()
    return sentences, tail


# ---------------------------------------------------------------------------
# 语音条的波形（QQ 语音条那个一排小竖条）
# ---------------------------------------------------------------------------

#: 语音条上播放图标的内联 SVG（转成 data URL 塞进 <img>）
VOICE_PLAY_ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" '
    'viewBox="0 0 14 14">'
    '<path d="M3.5 2.2 L11.6 7 L3.5 11.8 Z" fill="#4f46e5"/>'
    "</svg>"
)


def voice_play_icon_data_url() -> str:
    """播放图标的 data URL（内联 SVG，不依赖外部文件）。"""
    import base64 as _b64
    b64 = _b64.b64encode(VOICE_PLAY_ICON_SVG.encode("utf-8")).decode("ascii")
    return "data:image/svg+xml;base64," + b64



def voice_bar_count(duration: float, minimum: int = 8, maximum: int = 28) -> int:
    """语音条上画几根竖条 —— 跟时长挂钩（语音越久，条越长，像 QQ）。

    时长读不出来就按中间的 12 根画。
    """
    try:
        dur = float(duration or 0.0)
    except (TypeError, ValueError):
        dur = 0.0
    if dur <= 0:
        return 12
    return max(minimum, min(maximum, int(dur * 4) + 8))


def _wav_envelope(path: str, buckets: int):
    """真波形：读 wav 的采样，算每段的 RMS 包络（返回百分比高度）。

    读不出来（文件没了 / 不是 wav / 采样宽度怪）就返回 None ——
    调用方这时改画**装饰性**图案，不拿假的当真的。
    """
    try:
        import wave
        from array import array
    except Exception:  # noqa: BLE001
        return None
    try:
        with wave.open(str(path), "rb") as wav:
            n_frames = wav.getnframes()
            width = wav.getsampwidth()
            channels = max(1, wav.getnchannels())
            if n_frames <= 0:
                return None
            raw = wav.readframes(n_frames)
    except Exception:  # noqa: BLE001 —— 不是 wav / 坏了 / 没权限
        return None
    typecode = {1: "b", 2: "h", 4: "i"}.get(int(width or 0))
    if not typecode:
        return None
    try:
        samples = array(typecode)
        samples.frombytes(raw[: len(raw) - (len(raw) % samples.itemsize)])
    except Exception:  # noqa: BLE001
        return None
    if channels > 1:
        samples = samples[::channels]
    total = len(samples)
    if total <= 0:
        return None
    step = max(1, total // max(1, buckets))
    heights = []
    import math
    for i in range(buckets):
        start = i * step
        end = min(total, start + step)
        if start >= end:
            break
        chunk = samples[start:end]
        if len(chunk) > 2000:            # 抽样一下就够画包络了
            chunk = chunk[:: max(1, len(chunk) // 2000)]
        acc = 0
        for value in chunk:
            acc += value * value
        rms = math.sqrt(acc / max(1, len(chunk)))
        heights.append(rms)
    if not heights:
        return None
    peak = max(heights) or 1.0
    out = []
    for value in heights:
        pct = int(round(12 + 88 * (value / peak)))
        out.append(max(10, min(100, pct)))
    return out


def _decorative_envelope(path: str, duration: float, buckets: int) -> list:
    """**装饰性**图案（不是真实波形）—— 读不到采样时用。

    由「文件路径 + 时长」算出来：同一段语音每次画出来一样，
    不同语音长得不一样（不至于所有语音条长得一模一样）。
    """
    import hashlib
    seed = hashlib.sha1(
        ("%s|%.2f" % (str(path), float(duration or 0.0))).encode("utf-8")
    ).digest()
    out = []
    for i in range(buckets):
        byte = seed[i % len(seed)]
        out.append(max(12, min(100, 20 + byte % 76)))
    return out


def voice_waveform_bars(path: str, duration: float = 0.0,
                        buckets: int = 12) -> list:
    """语音条上那排竖条的高度（百分比，12~100）。

    · 文件还在且是能读的 wav → **真包络**（按采样算的 RMS）；
    · 读不出来 → **装饰性图案**（由路径 + 时长算的，不是真实波形）。

    诚实说明：后者只是「像波形的装饰」，不代表音频里的真实起伏。
    """
    count = max(4, min(48, int(buckets or 12)))
    real = _wav_envelope(path, count)
    if real:
        return real
    return _decorative_envelope(path, duration, count)


__all__ = [
    "_DANGEROUS_LABELS", "_CONFIRM_TIMEOUT_S",
    "_TOOL_UI_META", "_CIRCLED_NUMS",
    "_VOICE_TOOLS", "_TOOL_STEP_CLASS",
    "_unpack_tool", "_tool_label", "_tool_display", "_tool_status",
    "_unpack_status_mark", "_tool_step_class", "_tool_fail_reason",
    "_voice_result_note", "resolve_voice_artifact", "_VOICE_KIND",
    "_html_escape", "_format_time_short", "_img_open_key",
    "REPLY_PACING_SECTION", "reply_pacing_settings", "compute_reply_gap_ms",
    "split_reply_sentences", "voice_waveform_bars", "voice_bar_count",
    "voice_play_icon_data_url", "VOICE_PLAY_ICON_SVG",
    "_voice_play_key",
    "voice_job_id", "voice_job_path", "read_voice_job", "_VOICE_JOB_SUFFIX",
]