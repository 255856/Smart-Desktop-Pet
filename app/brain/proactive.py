"""桌宠主动行为：空闲时主动找主人说话（时间/状态/记忆驱动的主动关怀）。

节奏：
    - 每隔 [min, max] 分钟随机触发一次（QTimer.singleShot 自递归调度）
    - 触发条件：桌宠没在睡觉；且冷却时间（上次触发后至少 min 分钟）已过
    - 生成：小 LLM 调用，输入 = 当前时间 + 桌宠数值 + 记忆样本，
      输出 = 一句话（带 [emotion] 标签）；模型返回 [skip] 则本次沉默
    - 产出 remark_ready(text) 信号，主程序接气泡 + TTS
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import threading
from typing import Callable, Optional

from app.core.qt_compat import QObject, QThread, Signal

log = logging.getLogger(__name__)


class _ChatOnceWorker(QThread):
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, llm_cfg, system_prompt, messages):
        super().__init__()
        # 只携带配置与消息（不在主线程共享 httpx 客户端）——
        # ProactiveBrain 复用共享 LLMClient 会让 httpx 连接绑在已关闭的子 loop 上，
        # 触发 "Event loop is closed"。这里每次新建 LLMClient + httpx.AsyncClient。
        self.llm_cfg = llm_cfg
        self.system_prompt = system_prompt
        self.messages = messages
        self._cancelled = threading.Event()

    def request_stop(self) -> None:
        self._cancelled.set()

    def run(self) -> None:
        client = None
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            from app.brain.llm_client import LLMClient
            client = LLMClient(self.llm_cfg, self.system_prompt)

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
        self._timer = None
        self._worker: Optional[_ChatOnceWorker] = None
        self._last_remarks: list[str] = []
        # 注：worker 每次独立构造 LLMClient（不复用），避免 httpx 连接绑在已 close 的子 loop 上

    # ----- 调度 -----
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

    def _schedule(self) -> None:
        from app.core.qt_compat import QTimer
        delay_ms = int(random.uniform(self.min_minutes, self.max_minutes) * 60 * 1000)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fire)
        self._timer.start(delay_ms)
        log.info("ProactiveBrain: 下一次主动发言在 %.1f 分钟后", delay_ms / 60000)

    # ----- 生成 -----
    def _fire(self) -> None:
        try:
            self._schedule()   # 先排下一轮，本轮失败也不影响节奏
        except Exception:  # noqa: BLE001
            pass
        if self.is_sleeping():
            log.info("ProactiveBrain: 桌宠在睡觉，跳过")
            return
        if not self.llm_cfg.api_key or self.llm_cfg.api_key == "PUT-YOUR-API-KEY-HERE":
            return

        import datetime
        from app.engine.state import is_night
        now = datetime.datetime.now()
        wd = "一二三四五六日"[now.weekday()]
        mem = self.memory.recent(10)
        context = {
            "now": now.strftime(f"%Y-%m-%d %H:%M 星期{wd}"),
            "is_night": is_night(now.timestamp()),
            "time_hint": _get_time_context(now),
            "pet_status": self.state.stats_summary(),
            "memories": [m.content for m in mem],
            "recent_remarks": self._last_remarks[-3:],
        }
        system = (
            self.persona
            + "\n\n【主动互动模式】现在没有用户消息，由你主动发起一次互动。"
              "规则：\n"
              "1. 只输出一句话（40 字以内），像自然地冒出来的一句话："
              "可以是关心、闲聊、提起记忆里的事、或根据时间/状态作出的提醒；\n"
              f"2. 时间段提示：{context['time_hint']}\n"
              "3. 不要重复 recent_remarks 里说过的内容；\n"
              "4. 如果此刻真的没什么好说的，只输出 [skip]；\n"
              "5. 结尾保留一个情绪标签，如 [happy]。"
        )
        user = json.dumps(context, ensure_ascii=False)

        # 每次新建 worker（独立 httpx 客户端，绑定本次子 loop）—— 见 _ChatOnceWorker 注释
        from app.brain.llm_client import ChatMessage
        worker = _ChatOnceWorker(
            llm_cfg=self.llm_cfg,
            system_prompt=system,
            messages=[ChatMessage(role="user", content=user)],
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
        # 最终输出规范：剥掉推理模型漏到正文的 CoT / 规则复读 / 英文思考
        remark = sanitize_text(parsed.text).strip()
        if not remark:
            return
        self._last_remarks.append(remark)
        self.emotion_hint.emit(parsed.emotion)
        self.remark_ready.emit(remark)
