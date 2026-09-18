"""智能中枢控制器：封装 MemoryStore + ToolRegistry + ProactiveBrain + AgentLoop。

职责：
    - 创建和持有长期记忆、工具注册表、主动行为大脑
    - 提供动态 chat_context 生成（时间 / 状态 / 记忆 / 工具说明）
    - 处理工具触发的动画播放
    - 处理主动行为回调
"""
from __future__ import annotations

import datetime
import logging
from pathlib import Path

from app.core.qt_compat import QObject, Signal
from app.brain.agent import AgentLoop
from app.brain.llm_client import LLMClient
from app.brain.memory import MemoryStore
from app.brain.proactive import ProactiveBrain
from app.engine.tools import build_default_tools
from app.engine.works import ItemStore

log = logging.getLogger(__name__)


class BrainController(QObject):
    """封装 memory + tools + proactive + agent 的管理逻辑。"""

    # 信号：主动发言就绪（文本）
    remark_ready = Signal(str)
    # 信号：工具触发的气泡请求（文本）
    bubble_requested = Signal(str)
    # 信号：工具触发的动画请求（动画名）
    animation_requested = Signal(str)

    def __init__(self, root: Path, cfg, state, reminders,
                 parent: QObject | None = None) -> None:
        """
        Args:
            root: 项目根目录
            cfg:  Config 对象
            state: PetState 实例
            reminders: ReminderStore 实例
            parent: Qt 父对象
        """
        super().__init__(parent)
        self.root = root
        self.cfg = cfg
        self.state = state

        # --- 长期记忆 ---
        mem_path = cfg.brain.memory_file
        if not Path(mem_path).is_absolute():
            mem_path = root / mem_path
        self.memory = MemoryStore(mem_path)

        # --- 食物库（feed_self 工具用）---
        foods_path = root / "data" / "foods.json"
        self.items = ItemStore.load(foods_path) if foods_path.is_file() else None

        # --- 工具注册表 ---
        if cfg.brain.tools_enabled:
            self.tool_registry = build_default_tools(
                state=self.state,
                reminders=reminders,
                memory=self.memory,
                items=self.items,
                hooks={
                    "bubble": self.bubble_requested.emit,
                    "animation": self.animation_requested.emit,
                },
            )
            log.info("智能中枢：注册 %d 个工具 %s",
                     len(self.tool_registry.names()), self.tool_registry.names())
        else:
            self.tool_registry = None

        # --- 主动行为 ---
        self._sleeping_checker = None
        if cfg.brain.proactive_enabled:
            self.proactive = ProactiveBrain(
                llm_cfg=cfg.llm,
                persona=cfg.character.persona,
                state=self.state,
                memory=self.memory,
                char_name=cfg.character.name,
                min_minutes=cfg.brain.proactive_min_minutes,
                max_minutes=cfg.brain.proactive_max_minutes,
                is_sleeping=self._check_sleeping,
            )
            self.proactive.remark_ready.connect(self._on_proactive_remark)
            self.proactive.start()
        else:
            self.proactive = None

    def set_sleeping_checker(self, checker) -> None:
        """设置是否睡觉的检查函数（由 App 提供）。"""
        self._sleeping_checker = checker

    def _check_sleeping(self) -> bool:
        """检查桌宠是否在睡觉。"""
        if self._sleeping_checker is None:
            return False
        try:
            return self._sleeping_checker()
        except Exception:  # noqa: BLE001
            return False

    # ----- Chat context -----
    def chat_context(self) -> str:
        """每次发消息前生成动态 system 上下文：时间 / 状态 / 记忆 / 工具说明。"""
        now = datetime.datetime.now()
        wd = "一二三四五六日"[now.weekday()]
        parts = [
            f"【当前时间】{now.strftime('%Y-%m-%d %H:%M')} 星期{wd}",
            f"【你的实时状态】{self.state.stats_summary()}",
        ]
        mem_block = self.memory.system_block()
        if mem_block:
            parts.append(mem_block)
        if self.tool_registry is not None:
            parts.append(
                "【可用工具】你可以调用工具完成实际操作："
                + "、".join(self.tool_registry.names())
                + "。当主人的请求能通过工具完成（设提醒、记住/回忆事情、打开网页或应用、"
                  "查看你的状态、买东西吃等），直接调用对应工具，再用一两句话告诉主人结果。"
                  "聊天中得知主人的重要偏好/事实时，主动用 remember_fact 记住。"
                  "回复末尾仍要带情绪标签。")
        return "\\n\\n".join(parts)

    # ----- 工具动画 -----
    def connect_tool_animation(self, pet) -> None:
        """连接工具动画信号到桌宠。"""
        self.animation_requested.connect(self._do_play_animation)
        self._pet_ref = pet

    def _do_play_animation(self, name: str) -> None:
        """工具触发的动画。"""
        try:
            if name == "eat":
                self._pet_ref.play_emotion("happy")
            elif name == "stretch":
                self._pet_ref.animator.play_stretch()
            elif name == "jump":
                self._pet_ref.animator.play_jump()
            elif name == "spin":
                self._pet_ref.animator.play_spin()
            elif name == "swim":
                self._pet_ref.animator.play_swim()
            elif name == "file":
                self._pet_ref.animator.play_file()
            elif name == "thinking":
                self._pet_ref.animator.set_thinking()
            elif name.startswith("emotion_"):
                self._pet_ref.play_emotion(name.replace("emotion_", ""))
            else:
                # 尝试当作情绪播放
                self._pet_ref.play_emotion(name)
        except Exception as e:  # noqa: BLE001
            log.warning("工具动画播放失败：%s", e)

    # ----- 主动行为 -----
    def _on_proactive_remark(self, text: str) -> None:
        """转发主动发言信号。"""
        self.remark_ready.emit(text)

    # ----- Agent 循环 -----
    def create_agent(self, client: LLMClient, confirm_tool=None) -> AgentLoop:
        """创建 AgentLoop 实例。"""
        return AgentLoop(
            client=client,
            registry=self.tool_registry,
            confirm_tool=confirm_tool,
        )
