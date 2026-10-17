"""桌宠主动行为：空闲时主动找主人说话（时间/状态/记忆驱动的主动关怀）。"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import threading
from typing import Callable, Optional

from app.core.qt_compat import QObject, QThread, Signal
from app.core.api_keys import is_placeholder_key

log = logging.getLogger(__name__)


class _ChatOnceWorker(QThread):
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, llm_cfg, system_prompt, messages,
                 request_timeout_s: int | None = None):
        super().__init__()
        # 每次独立构造 LLMClient + httpx.AsyncClient，避免连接绑在已 close 的子 loop 上
        self.llm_cfg = llm_cfg
        self.system_prompt = system_prompt
        self.messages = messages
        self.request_timeout_s = request_timeout_s
        self._cancelled = threading.Event()

    def request_stop(self) -> None:
        self._cancelled.set()

    def run(self) -> None:
        client = None
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            from app.brain.llm_client import LLMClient
            client = LLMClient(self.llm_cfg, self.system_prompt,
                               request_timeout_s=self.request_timeout_s)

            async def drive() -> str:
                if self._cancelled.is_set():
                    return ""
                return await client.chat_once(self.messages)

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
        memory,                   # MemoryStore
        char_name: str,
        min_minutes: int = 25,
        max_minutes: int = 45,
        is_sleeping: Optional[Callable[[], bool]] = None,
        chat_cfg=None,            # app.core.config.ChatConfig, 可选
        aw_cfg=None,              # app.core.config.ActivityWatchConfig, 可选
    ):
        super().__init__()
        self.llm_cfg = llm_cfg
        self.persona = persona
        self.state = state
        self.memory = memory
        self.char_name = char_name
        self.min_minutes = max(5, int(min_minutes))
        self.max_minutes = max(self.min_minutes + 5, int(max_minutes))
        self.is_sleeping = is_sleeping or (lambda: False)
        # 来自 config.yaml 的 chat.request_timeout_s；None 表示让 LLMClient 用 cfg.timeout
        self._request_timeout_s: int | None = (
            chat_cfg.request_timeout_s if chat_cfg is not None else None
        )
        # ActivityWatch 配置：未启用 / aw-server 未跑 / 无 watcher 时 _fire 内优雅降级
        self._aw_cfg = aw_cfg
        self._timer = None
        self._worker: Optional[_ChatOnceWorker] = None
        self._last_remarks: list[str] = []
        # worker 每次独立构造 LLMClient（不复用），避免 httpx 连接绑在已 close 的子 loop 上

    def apply_aw_config(self, aw_cfg) -> None:
        """设置面板 / 热重载改了 AW 配置 — 下一次 _fire 用新配置。无副作用。"""
        self._aw_cfg = aw_cfg
        log.info("ProactiveBrain: AW 配置更新 enabled=%s server=%s",
                 getattr(aw_cfg, "enabled", False),
                 getattr(aw_cfg, "server_url", ""))

    def start(self) -> None:
        self._schedule()

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        if self._worker is not None:
            self._worker.request_stop()

    def apply_interval(self, min_minutes: int, max_minutes: int) -> None:
        """设置面板：调整主动发言间隔（分钟）并重新排程。"""
        self.min_minutes = max(5, int(min_minutes))
        self.max_minutes = max(self.min_minutes + 5, int(max_minutes))
        if self._timer is not None:
            self._timer.stop()
        self._schedule()
        log.info("ProactiveBrain: 间隔调整为 %d-%d 分钟", self.min_minutes, self.max_minutes)

    def update_llm_config(self, new_cfg) -> None:
        """用户修改了模型配置，下一次 _fire 自动用新配置。"""
        self.llm_cfg = new_cfg
        log.info("ProactiveBrain: LLM 配置已更新 → %s @ %s", new_cfg.model, new_cfg.base_url)

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
        if self.is_sleeping():
            log.info("ProactiveBrain: 桌宠在睡觉，跳过")
            return
        if is_placeholder_key(self.llm_cfg.api_key):
            return

        import datetime
        from app.engine.state import is_night
        now = datetime.datetime.now()
        wd = "一二三四五六日"[now.weekday()]
        mem = self.memory.recent(10)
        # ActivityWatch 活动 context（主人开了才查，aw-server 不在就跳过）
        aw_context = self._build_aw_context()
        context = {
            "now": now.strftime(f"%Y-%m-%d %H:%M 星期{wd}"),
            "is_night": is_night(now.timestamp()),
            "time_hint": _get_time_context(now),
            "pet_status": self.state.stats_summary(),
            "memories": [m.content for m in mem],
            "recent_remarks": self._last_remarks[-3:],
            "aw_context": aw_context,
        }
        # 第 6 条规则只在 AW 实际有数据时挂上，避免 prompt 噪声
        aw_rule = ""
        if aw_context and "未运行" not in aw_context and "无数据" not in aw_context:
            aw_rule = (
                "\n6. 若 `aw_context` 里有数据, 可以结合主人当下在做的事"
                "（写代码 / 刷视频 / 看文档 等）挑更贴切的切入口, "
                "比如「看了半天 VSCode, 休息一下吧」; "
                "对敏感标题（私人聊天/银行）只概括类别不复读标题。"
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
        )
        user = json.dumps(context, ensure_ascii=False)

        # 每次新建 worker（独立 httpx 客户端，绑定本次子 loop）—— 见 _ChatOnceWorker 注释
        from app.brain.llm_client import ChatMessage
        worker = _ChatOnceWorker(
            llm_cfg=self.llm_cfg,
            system_prompt=system,
            messages=[ChatMessage(role="user", content=user)],
            request_timeout_s=self._request_timeout_s,
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
        self._last_remarks.append(remark)
        self.emotion_hint.emit(parsed.emotion)
        self.remark_ready.emit(remark)
