"""桌宠主动行为：空闲时主动找主人说话（时间/状态/记忆驱动的主动关怀）。

v54 增强：感知主人「是否在场」（aw-watcher-afk）与「是否在会议」
（窗口标题关键词），空闲过久或正在开会就跳过本轮主动搭话，避免打扰。
开会场景还会自动把 TTS 静音 30 分钟，免得突然冒一句话。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import tempfile
import threading
import time
from typing import Callable, Optional

from app.core.qt_compat import QObject, QThread, Signal
from app.core.api_keys import is_placeholder_key
from app.brain.routine_profiler import RoutineProfiler

log = logging.getLogger(__name__)


# v55: OCR 进主动关心的限速常量
# 只对「内容创作型」窗口做 OCR, 5 分钟最多 1 次, 截前 200 字
_OCR_APP_KEYWORDS = (
    # 编辑器 / IDE
    "Code", "Visual Studio", "PyCharm", "IntelliJ", "Sublime",
    "Atom", "Notepad++", "WebStorm", "GoLand", "CLion",
    "Cursor", "Trae", "Zed", "Xcode",
    # 浏览器
    "Chrome", "chrome", "Edge", "edge", "Firefox", "firefox",
    "Brave", "Opera", "Vivaldi", "Arc",
    # 办公 / 文档
    "Word", "WPS", "PowerPoint", "Excel", "Acrobat",
    "Adobe Reader", "Obsidian", "Typora", "Notion",
    # 终端 / 命令行 (有时主人 tail 日志需要上下文)
    "PowerShell", "WindowsTerminal", "iTerm", "Terminal",
)
_OCR_RATE_LIMIT_S = 300   # 5 分钟一次
_OCR_MAX_CHARS = 200     # 截前 200 字, 够 LLM 拿类别


# v57: RoutineProfiler 数据目录 (在项目根 cwd/data 下, 默认)
_ROUTINE_DATA_DIR = "data"
_ROUTINE_REFRESH_THREAD_NAME = "proactive-rp-refresh"


class _ChatOnceWorker(QThread):
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, llm_cfg, system_prompt, messages,
                 request_timeout_s: int | None = None, prepare=None):
        super().__init__()
        # 每次独立构造 LLMClient + httpx.AsyncClient，避免连接绑在已 close 的子 loop 上
        self.llm_cfg = llm_cfg
        self.system_prompt = system_prompt
        self.messages = messages
        self.request_timeout_s = request_timeout_s
        # prepare: 可选的回调，在**子线程**里返回 (system_prompt, messages)。
        # 主动发言用它把阻塞的 ActivityWatch 抓取挪出 GUI 线程
        # （原来是 GUI 线程上最多 6 次 urlopen，超时 2+4+6 秒 → 整只桌宠冻 24 秒）。
        self._prepare = prepare
        self._cancelled = threading.Event()

    def request_stop(self) -> None:
        self._cancelled.set()

    def run(self) -> None:
        client = None
        try:
            if self._prepare is not None:
                system_prompt, messages = self._prepare()
            else:
                system_prompt, messages = self.system_prompt, self.messages
            # v54: _prepare 返 (None, None) = 跳过本轮（主人不在场 / 在会议）
            if system_prompt is None and messages is None:
                self.done.emit("")
                return
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            from app.brain.llm_client import LLMClient
            client = LLMClient(self.llm_cfg, system_prompt,
                               request_timeout_s=self.request_timeout_s)

            async def drive() -> str:
                if self._cancelled.is_set():
                    return ""
                return await client.chat_once(messages)

            text = loop.run_until_complete(drive())
            self.done.emit(text)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"{e!r}")
        finally:
            if client is not None:
                try:
                    loop.run_until_complete(client.close())
                except Exception:
                    pass
            try:
                loop.close()
            except Exception:
                pass


def _get_time_context(now) -> str:
    """根据当前小时生成时间段提示，注入主动发言的 system prompt。"""
    hour = now.hour
    if 6 <= hour < 12:
        return "现在是上午/早上。可以问候主人、提醒吃饭。"
    elif 12 <= hour < 18:
        return "现在是下午。可以关心主人的工作/学习。"
    elif 18 <= hour < 22:
        return "现在是晚上。可以关心主人的休息。"
    else:
        return "现在是深夜。应该温柔地提醒主人早点休息。"


class ProactiveBrain(QObject):
    """定时主动打招呼 / 关心主人。"""

    remark_ready = Signal(str)     # 一句话（已去掉情绪标签）
    emotion_hint = Signal(object)  # Emotion 枚举

    def __init__(
        self,
        *,
        llm_cfg,
        persona: str,
        state,                    # PetState
        memory,                   # 长期记忆读取口（sidecar 客户端：recent / recent_shared）
        char_name: str,
        min_minutes: int = 25,
        max_minutes: int = 45,
        is_sleeping: Optional[Callable[[], bool]] = None,
        chat_cfg=None,            # app.core.config.ChatConfig, 可选
        aw_cfg=None,              # app.core.config.ActivityWatchConfig, 可选
        idle_skip_minutes: int = 30,           # v54: 主人空闲多久（分钟）跳过主动搭话
        meeting_auto_mute_minutes: int = 30,   # v54: 检测到会议自动静音 TTS 多长时间
    ):
        super().__init__()
        self.llm_cfg = llm_cfg
        self.persona = persona
        self.state = state
        self.memory = memory
        self.char_name = char_name
        # 间隔下限最少 2 分钟、上限最少 6 分钟（面板范围跟这里保持一致）
        self.min_minutes = max(2, int(min_minutes))
        self.max_minutes = max(6, self.min_minutes, int(max_minutes))
        self.is_sleeping = is_sleeping or (lambda: False)
        # 来自 config.yaml 的 chat.request_timeout_s；None 表示让 LLMClient 用 cfg.timeout
        self._request_timeout_s: int | None = (
            chat_cfg.request_timeout_s if chat_cfg is not None else None
        )
        # ActivityWatch 配置：未启用 / aw-server 未跑 / 无 watcher 时 _fire 内优雅降级
        self._aw_cfg = aw_cfg
        self._timer = None
        self._worker: Optional[_ChatOnceWorker] = None
        self._last_remarks_by_char: dict[str, list[str]] = {}
        # v54: 闲置 / 会议感知阈值
        self.idle_skip_minutes = max(1, int(idle_skip_minutes))
        self.meeting_auto_mute_minutes = max(1, int(meeting_auto_mute_minutes))
        # v55: 上次 OCR 屏幕的时间戳 (限速用)
        self._last_screen_ocr_ts: float = 0.0
        # v57: 规律学习 — 离线聚合 AW 历史的 RoutineProfiler, 失败时不阻塞
        try:
            self.rp = RoutineProfiler(_ROUTINE_DATA_DIR)
        except Exception:  # noqa: BLE001
            self.rp = None
        # worker 每次独立构造 LLMClient（不复用），避免 httpx 连接绑在已 close 的子 loop 上

    def apply_aw_config(self, aw_cfg) -> None:
        """设置面板 / 热重载改了 AW 配置 — 下一次 _fire 用新配置。无副作用。"""
        self._aw_cfg = aw_cfg
        log.info("ProactiveBrain: AW 配置更新 enabled=%s server=%s",
                 getattr(aw_cfg, "enabled", False),
                 getattr(aw_cfg, "server_url", ""))

    def start(self) -> None:
        self._schedule()
        # v57: 首次启动触发一次规律聚合 (后台线程, 不阻塞 GUI)
        self._maybe_refresh_routine(force=True)

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        if self._worker is not None:
            self._worker.request_stop()

    def apply_interval(self, min_minutes: int, max_minutes: int) -> None:
        """设置面板：调整主动发言间隔（分钟）并重新排程。"""
        # 下限最少 2、上限最少 6（跟构造时同一套规则，面板显示才准）
        self.min_minutes = max(2, int(min_minutes))
        self.max_minutes = max(6, self.min_minutes, int(max_minutes))
        if self._timer is not None:
            self._timer.stop()
        self._schedule()
        log.info("ProactiveBrain: 间隔调整为 %d-%d 分钟", self.min_minutes, self.max_minutes)

    def apply_idle_skip_minutes(self, minutes: int) -> None:
        """设置面板：调整「主人空闲多久跳过主动搭话」的阈值（分钟）。"""
        self.idle_skip_minutes = max(1, int(minutes))
        log.info("ProactiveBrain: 空闲跳过阈值 → %d 分钟", self.idle_skip_minutes)

    def apply_meeting_auto_mute_minutes(self, minutes: int) -> None:
        """设置面板：调整「检测到会议自动静音 TTS」的时长（分钟）。"""
        self.meeting_auto_mute_minutes = max(1, int(minutes))
        log.info("ProactiveBrain: 会议自动静音时长 → %d 分钟", self.meeting_auto_mute_minutes)

    def update_llm_config(self, new_cfg) -> None:
        """用户修改了模型配置，下一次 _fire 自动用新配置。"""
        self.llm_cfg = new_cfg
        log.info("ProactiveBrain: LLM 配置已更新 → %s @ %s", new_cfg.model, new_cfg.base_url)

    def set_request_timeout(self, seconds: int | None) -> None:
        """设置面板改了「超时时间」—— 主动关心这边也跟。

        之前主动关心固定走 chat.request_timeout_s（默认 20 秒），面板上那个
        超时时间对它不生效 —— 面板显示 60、实际 20，行为跟界面说的不一样。
        """
        self._request_timeout_s = None if seconds is None else int(seconds)
        log.info("ProactiveBrain: 请求超时 → %s", self._request_timeout_s)

    def _read_idle_minutes(self) -> Optional[float]:
        """查 AW aw-watcher-afk 拿主人空闲分钟数；任何环节失败都返 None（不打扰策略）。"""
        try:
            from app.integrations.activitywatch import get_idle_status
            return get_idle_status(
                server_url=(getattr(self._aw_cfg, "server_url", None) or None),
                hostname=(getattr(self._aw_cfg, "hostname", None) or None),
            )
        except Exception:  # noqa: BLE001
            return None

    def _is_in_meeting(self) -> bool:
        """当前窗口是否是会议软件；AW 不可达或失败返 False（保守不自动静音）。"""
        try:
            from app.integrations.activitywatch import is_focus_meeting
            return is_focus_meeting(
                server_url=(getattr(self._aw_cfg, "server_url", None) or None),
                hostname=(getattr(self._aw_cfg, "hostname", None) or None),
            )
        except Exception:  # noqa: BLE001
            return False

    def _app_supports_ocr(self, focus_line: str) -> bool:
        """当前窗口是否值得 OCR (编辑器 / 浏览器 / 文档类)。"""
        return bool(focus_line) and any(kw in focus_line for kw in _OCR_APP_KEYWORDS)

    def _should_run_ocr_now(self) -> bool:
        """是否过 5 分钟限速窗口。"""
        return (time.time() - self._last_screen_ocr_ts) >= _OCR_RATE_LIMIT_S

    def _read_screen_excerpt(self) -> Optional[str]:
        """截屏 + OCR + 截前 N 字。任何环节失败都返 None（不阻塞主动搭话）。

        用本地 OCR (Windows.Media.Ocr / Tesseract), 不走云端。
        """
        try:
            from app.engine.screenshot import take_screenshot
            from app.engine.tools._ocr import _ocr as run_ocr
            png = take_screenshot()
            if not png:
                return None
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                f.write(png)
                tmp_path = f.name
            try:
                text = run_ocr(tmp_path) or ""
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            text = (text or "").strip()
            # 错误信息 / 未识别 都不要当线索
            if not text or text.startswith("错误：") or text == "(未识别到文字)":
                return None
            return text[:_OCR_MAX_CHARS]
        except Exception:  # noqa: BLE001
            return None

    def _fetch_focus_line(self) -> Optional[str]:
        """当前焦点窗口一句话描述 (复用 AW 集成)。失败返 None。"""
        try:
            from app.integrations.activitywatch import get_current_window_one_liner
            return get_current_window_one_liner(
                server_url=(getattr(self._aw_cfg, "server_url", None) or None),
                hostname=(getattr(self._aw_cfg, "hostname", None) or None),
            )
        except Exception:  # noqa: BLE001
            return None

    # ---- v57: RoutineProfiler ----

    def _routine_hint_for_now(self) -> str:
        """当前时间的历史规律 hint, 失败/无数据返空串 (不阻塞主动搭话)。"""
        if self.rp is None:
            return ""
        try:
            return self.rp.hint_for_now() or ""
        except Exception:  # noqa: BLE001
            return ""

    def _maybe_refresh_routine(self, *, force: bool = False) -> None:
        """后台线程拉 AW 历史聚合成规律; 陈旧时自动重拉。"""
        if self.rp is None:
            return
        if not force and not self.rp.is_stale():
            return
        server_url = (getattr(self._aw_cfg, "server_url", None) or None)
        hostname = (getattr(self._aw_cfg, "hostname", None) or None)

        def _worker():
            try:
                self.rp.refresh_from_aw(
                    server_url=server_url, hostname=hostname,
                )
            except Exception:  # noqa: BLE001
                log.exception("RoutineProfiler: 后台刷新失败")

        threading.Thread(
            target=_worker, name=_ROUTINE_REFRESH_THREAD_NAME, daemon=True,
        ).start()

    def _build_aw_context(self) -> str:
        """拿 AW 活动数据；未启用 / aw-server 未跑 / 无 watcher 时返回空字符串。"""
        aw_cfg = self._aw_cfg
        if aw_cfg is None or not getattr(aw_cfg, "enabled", False):
            return ""
        try:
            from app.integrations.activitywatch import (
                get_current_window_one_liner,
                get_recent_activities_summary,
            )
        except Exception:
            return ""
        server_url = getattr(aw_cfg, "server_url", None) or None
        lookback = max(1, int(getattr(aw_cfg, "proactive_lookback_minutes", 30) or 30))
        n_max = max(1, int(getattr(aw_cfg, "proactive_max_events", 15) or 15))
        try:
            current = get_current_window_one_liner(server_url=server_url)
            recent = get_recent_activities_summary(
                minutes=lookback, limit=n_max, server_url=server_url,
            )
        except Exception as e:
            log.debug("AW context fetch failed: %s", e)
            return ""
        parts = []
        if current:
            parts.append(current)
        if recent:
            parts.append(recent)
        if not parts:
            return ""
        return "\n".join(parts)

    def _schedule(self) -> None:
        from app.core.qt_compat import QTimer
        delay_ms = int(random.uniform(self.min_minutes, self.max_minutes) * 60 * 1000)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fire)
        self._timer.start(delay_ms)
        log.info("ProactiveBrain: 下一次主动发言在 %.1f 分钟后", delay_ms / 60000)

    def _fire(self) -> None:
        try:
            self._schedule()   # 先排下一轮，本轮失败也不影响节奏
        except Exception:  # noqa: BLE001
            pass
        # v57: 陈旧时异步刷新规律 (后台线程, 不阻塞本轮)
        if self.rp is not None and self.rp.is_stale():
            self._maybe_refresh_routine(force=False)
        if self.is_sleeping():
            log.info("ProactiveBrain: 桌宠在睡觉，跳过")
            return
        if is_placeholder_key(self.llm_cfg.api_key):
            return

        import datetime
        from app.engine.state import is_night
        now = datetime.datetime.now()
        wd = "一二三四五六日"[now.weekday()]
        # 记忆从 sidecar 客户端读（读不到就是空列表，绝不影响主动发言）
        try:
            mem = self.memory.recent(10) if self.memory is not None else []
        except Exception:  # noqa: BLE001
            mem = []
        # v56: 共同经历记忆 (我们之间发生过的事), 单独抽出来给规则 8 用
        try:
            shared = self.memory.recent_shared(3) if self.memory is not None else []
        except Exception:  # noqa: BLE001
            shared = []
        # 下面这些是纯内存/轻量读取，放 GUI 线程没关系。
        context_base = {
            "now": now.strftime(f"%Y-%m-%d %H:%M 星期{wd}"),
            "is_night": is_night(now.timestamp()),
            "time_hint": _get_time_context(now),
            "pet_status": self.state.stats_summary(),
            "memories": [m.content for m in mem],
            "shared_memories": [s.content for s in shared],
            "recent_remarks": self._last_remarks_by_char.get(self.char_name, [])[-3:],
            # v57: 规律 hint — RoutineProfiler 读盘是轻量 IO, GUI 线程没关系
            "routine_hint": self._routine_hint_for_now(),
        }

        def _prepare():
            """在 worker 线程里准备 system_prompt / messages。

            ActivityWatch 抓取是阻塞 HTTP（最多 6 次 urlopen，超时 2+4+6 秒）。
            原来在 GUI 线程做 → 桌宠动画/气泡/托盘/输入全部冻住（最长 24 秒）。
            现在整个 prompt 组装都放进 worker 线程，GUI 线程只负责调度。

            v54 起还承担「在窗口场景过滤」：
              - 主人空闲 > idle_skip_minutes → (None, None) 跳过本轮
              - 当前窗口是会议软件 → (None, None) + 自动静音 TTS
            """
            # v54: 主人空闲太久 → 不打扰
            idle_min = self._read_idle_minutes()
            if idle_min is not None and idle_min >= self.idle_skip_minutes:
                log.info(
                    "ProactiveBrain: 主人空闲 %.1f 分钟 (> %d), 跳过本轮主动搭话",
                    idle_min, self.idle_skip_minutes,
                )
                return None, None
            # v54: 会议窗口 → 自动静音 TTS + 不打扰
            if self._is_in_meeting():
                log.info(
                    "ProactiveBrain: 检测到会议窗口, 自动静音 TTS %d 分钟",
                    self.meeting_auto_mute_minutes,
                )
                try:
                    from app.engine.tools._tts_mute import set_tts_mute_until_threadsafe
                    set_tts_mute_until_threadsafe(self.meeting_auto_mute_minutes)
                except Exception:  # noqa: BLE001
                    log.exception("ProactiveBrain: 会议自动静音失败")
                return None, None

            aw_context = self._build_aw_context()
            context = dict(context_base)
            context["aw_context"] = aw_context
            # 第 6 条规则只在 AW 实际有数据时挂上，避免 prompt 噪声
            aw_rule = ""
            if aw_context and "未运行" not in aw_context and "无数据" not in aw_context:
                aw_rule = (
                    "\n6. 若 `aw_context` 里有数据, 可以结合主人当下在做的事"
                    "（写代码 / 刷视频 / 看文档 等）挑更贴切的切入口, "
                    "比如「看了半天 VSCode, 休息一下吧」; "
                    "对敏感标题（私人聊天/银行）只概括类别不复读标题。"
                )

            # v55: 低频 OCR — 当窗口是编辑器 / 浏览器 / 文档类时截屏 OCR 拿屏幕文字
            focus_line = self._fetch_focus_line() or ""
            if (focus_line and self._app_supports_ocr(focus_line)
                    and self._should_run_ocr_now()):
                excerpt = self._read_screen_excerpt()
                # 不管成功失败都更新时间戳, 避免 OCR 持续失败时反复触发
                self._last_screen_ocr_ts = time.time()
                if excerpt:
                    context["screen_excerpt"] = excerpt

            screen_rule = ""
            if context.get("screen_excerpt"):
                screen_rule = (
                    "\n7. `screen_excerpt` 是当前窗口 OCR 文字摘要 (可能与本地代码差异), "
                    "可以用来更具体地关心主人在看什么, 比如「在看这篇 XX 啊, 累不累」; "
                    "只概括类别/主题, 不要复述完整内容。"
                )

            # v56: 共同经历 — 自然引用「我们一起 XX」的故事, 比普通 fact 更贴切
            shared_rule = ""
            if context.get("shared_memories"):
                shared_rule = (
                    "\n8. `shared_memories` 是我们之间共同经历过的事 (关系叙事), "
                    "如果跟当前话题相关, 可以自然引用一句 (比如「上次那盘棋你还记得吗」), "
                    "但不要每轮都引用, 偶尔提一次就好; 没相关就别提。"
                )

            # v57: 规律 — 不强加「应该做什么」, 只在合适时提一句历史规律
            routine_rule = ""
            if context.get("routine_hint"):
                routine_rule = (
                    "\n9. `routine_hint` 是主人这个时段的历史规律 (比如通常在写代码), "
                    "可以拿来当关心入口 (「这个时间又在写啊」), 不要硬套 — "
                    "跟现在做的不一致时不要纠正主人。"
                )
            system = (
                self.persona
                + "\n\n【主动互动模式】现在没有用户消息，由你主动发起一次互动。"
                  "规则：\n"
                  "1. 只输出一句话（40 字以内），像自然地冒出来的一句话："
                  "可以是关心、闲聊、提起记忆里的事、或根据时间/状态作出的提醒；\n"
                  f"2. 时间段提示：{context['time_hint']}\n"
                  "3. 不要重复 recent_remarks 里说过的内容；\n"
                  "4. 如果此刻真的没什么好说的，只输出 [skip]；\n"
                  "5. 不要在结尾追加 [emotion] 标签或「~ happy」等情绪字样，"
                  "情绪由语气自然表达即可。"
                  + aw_rule
                  + screen_rule
                  + shared_rule
                  + routine_rule
            )
            from app.brain.llm_client import ChatMessage
            user = json.dumps(context, ensure_ascii=False)
            return system, [ChatMessage(role="user", content=user)]

        # 每次新建 worker（独立 httpx 客户端，绑定本次子 loop）—— 见 _ChatOnceWorker 注释
        worker = _ChatOnceWorker(
            llm_cfg=self.llm_cfg,
            system_prompt="",
            messages=[],
            request_timeout_s=self._request_timeout_s,
            prepare=_prepare,
        )
        worker.done.connect(self._on_done)
        worker.failed.connect(
            lambda e: log.info("ProactiveBrain 生成失败：%s", e))
        self._worker = worker
        worker.start()

    def _on_done(self, text: str) -> None:
        text = (text or "").strip()
        if not text or "[skip]" in text:
            return
        from app.voice.character import parse_reply
        from app.brain.llm_client import sanitize_text
        parsed = parse_reply(text)
        # 剥掉推理模型漏到正文的 CoT / 规则复读 / 英文思考
        remark = sanitize_text(parsed.text).strip()
        if not remark:
            return
        # v31: per-character 隔离 — 旧 self._last_remarks 改 dict
        bucket = self._last_remarks_by_char.setdefault(self.char_name, [])
        bucket.append(remark)
        # 限长: 每个角色最多保留 5 句 (但 prompt 只取 -3)
        if len(bucket) > 5:
            self._last_remarks_by_char[self.char_name] = bucket[-5:]
        self.emotion_hint.emit(parsed.emotion)
        self.remark_ready.emit(remark)
