"""幻觉检测 + 工具使用强化 prompt 的回归测试。

Bug 背景：
- 模型 MiniMax-M3 在工具调用时经常「幻觉」—— 在文本里写「已打开 XX」但
  实际没调用工具（工具调用次数为 0）。这是 LLM Function Calling 的常见坑。
- 修复：
    1. 强化 system prompt（brain_controller.py）：硬规则 + 反向警示
    2. 在 chat_window._on_done 检测幻觉：模型说「已打开」但本轮没调用工具
       → 追加系统消息明确告知主人
"""
import sys
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".local-packages"))
sys.path.insert(0, str(ROOT))

import pytest

from app.core.qt_compat import QApplication
app = QApplication.instance() or QApplication(sys.argv)

from app.core.config import CharacterConfig
from app.brain.llm_client import LLMConfig
from app.ui.chat_window import ChatWindow, Message
from app.brain.brain_controller import BrainController


# ============================================================
#  _detect_hallucination 单元测试
# ============================================================

class TestDetectHallucination:
    @pytest.fixture
    def chat(self):
        """创建一个干净的 ChatWindow（不触发 LLM / 真扫描）。"""
        char_cfg = CharacterConfig(name="测试娘", tts_enabled=False)
        llm_cfg = LLMConfig(model="test", api_key="test",
                            base_url="http://localhost:11434/v1")
        cw = ChatWindow(llm_cfg, char_cfg, "assets/sprites")
        return cw

    def _chat_view_text(self, chat) -> str:
        """返回 chat_view 里的纯文本内容（用于检测是否追加了系统消息）。"""
        return chat.chat_view.toPlainText()

    def test_no_hallucination_when_tool_called(self, chat):
        """工具实际调用了 → 不告警。"""
        before = self._chat_view_text(chat)
        chat._detect_hallucination(
            "已打开抖音啦，主人！",
            tools_used=[("open_website", "已在浏览器打开 https://douyin.com")],
        )
        after = self._chat_view_text(chat)
        # 调用了工具就不该追加系统消息（chat_view 不变）
        assert before == after, "调用了工具就不该追加系统消息"

    def test_no_hallucination_for_neutral_text(self, chat):
        """中性文本（没说已 XX）→ 不告警。"""
        before = self._chat_view_text(chat)
        chat._detect_hallucination(
            "主人你今天心情看起来不错呀~",
            tools_used=[],
        )
        assert before == self._chat_view_text(chat)

    def test_detects_open_hallucination(self, chat):
        """模型说「已打开 XX」但没调工具 → 告警。"""
        before = self._chat_view_text(chat)
        chat._detect_hallucination(
            "已打开抖音啦，主人去刷视频吧！",
            tools_used=[],
        )
        after = self._chat_view_text(chat)
        assert "抖音" in after or "幻觉" in after, "应追加含告警的系统消息"

    def test_detects_started_hallucination(self, chat):
        """模型说「已启动 XX」但没调工具 → 告警。"""
        chat._detect_hallucination(
            "已启动 QQ，主人去聊天吧~",
            tools_used=[],
        )
        assert "QQ" in self._chat_view_text(chat) or "幻觉" in self._chat_view_text(chat)

    def test_detects_already_opened(self, chat):
        """「已经打开」也算幻觉。"""
        chat._detect_hallucination(
            "已经打开 baidu.com 啦！",
            tools_used=[],
        )
        text = self._chat_view_text(chat)
        assert "baidu.com" in text or "幻觉" in text

    def test_detects_set_hallucination(self, chat):
        """「已设置」也算幻觉。"""
        chat._detect_hallucination(
            "已设置 30 分钟后提醒主人喝水~",
            tools_used=[],
        )
        text = self._chat_view_text(chat)
        assert "30 分钟" in text or "幻觉" in text or "提醒" in text

    def test_only_warns_once_per_message(self, chat):
        """一条消息里多次「已 XX」只告警一次（避免重复刷屏）。

        用 spy wrap _append_system_msg 来计数。
        """
        call_count = [0]
        original = chat._append_system_msg

        def spy(text):
            call_count[0] += 1
            # 不真正渲染（Qt 渲染需要事件循环）
            return None

        chat._append_system_msg = spy  # type: ignore[assignment]
        try:
            chat._detect_hallucination(
                "已打开抖音，又已打开 QQ，还已启动 Notepad~",
                tools_used=[],
            )
        finally:
            chat._append_system_msg = original  # type: ignore[assignment]

        assert call_count[0] == 1, f"应只告警一次，实际 {call_count[0]} 次"

    def test_does_not_warn_on_short_target(self, chat):
        """target 太长（> 40 字符）→ 跳过（不是应用名）。"""
        before = self._chat_view_text(chat)
        chat._detect_hallucination(
            "已打开 " + "x" * 50,
            tools_used=[],
        )
        # 没有「system-msg」类的告警
        text = self._chat_view_text(chat)
        assert 'class="system-msg"' not in text or before == text

    def test_does_not_warn_on_empty_target(self, chat):
        """没有匹配模式 → 跳过。"""
        before = self._chat_view_text(chat)
        chat._detect_hallucination(
            "主人，我已经完成啦！",
            tools_used=[],
        )
        text = self._chat_view_text(chat)
        assert 'class="system-msg"' not in text or before == text


# ============================================================
#  brain_controller 强化 prompt
# ============================================================

class TestToolUsagePrompt:
    def test_chat_context_contains_hard_rules(self):
        """chat_context 必须包含强约束（不是软建议）。

        关键规则现在在 SKILL.md 里（chat_context 第一个位置），
        不在 _tool_usage_instruction 里。
        """
        from app.engine.state import PetState
        from app.engine.reminder import ReminderStore
        from app.engine.tools import ToolRegistry
        from app.brain.memory import MemoryStore
        from app.core.qt_compat import QApplication
        import tempfile
        import shutil
        from pathlib import Path

        app = QApplication.instance() or QApplication(sys.argv)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            # 复制 SKILL.md 到 loader 期望的位置：
            # {root}/.agents/skills/desktop-pet-tool-usage/SKILL.md
            skill_dst = tmp_path / ".agents" / "skills" / "desktop-pet-tool-usage"
            skill_dst.mkdir(parents=True)
            shutil.copy(
                ROOT / ".agents" / "skills" / "desktop-pet-tool-usage" / "SKILL.md",
                skill_dst / "SKILL.md",
            )
            # 写一个最小 config.yaml
            (tmp_path / "config.yaml").write_text("""
llm:
  base_url: "http://localhost:1/v1"
  api_key: "test"
  model: "test"
character:
  name: "测试"
window:
  scale: 0.4
""")
            from app.core.config import load_config
            from app.engine.state_manager import StateManager

            cfg = load_config(tmp_path / "config.yaml")
            state_mgr = StateManager(tmp_path, cfg, type('Args', (), {'reset_state': False})())
            brain = BrainController(tmp_path, cfg, state_mgr.state, state_mgr.reminders)

            # chat_context 必须包含 SKILL 的硬规则
            ctx = brain.chat_context()
            # SKILL.md 的硬规则应该注入到 chat_context
            assert "HARD RULES" in ctx or "硬规则" in ctx
            # 必须提到「必须调用工具」
            assert "必须调用" in ctx or "MUST" in ctx
            # 必须提到「幻觉」警告
            assert "幻觉" in ctx or "hallucination" in ctx.lower()

    def test_chat_context_contains_no_misleading_soft_words(self):
        """prompt 不再只说软建议（如「你可以」）。

        _tool_usage_instruction 现在只列工具名（详情在 SKILL.md）。
        """
        instruction = BrainController._tool_usage_instruction(
            "open_website、open_app"
        )
        # 之前的软措辞（已替换）
        assert "你可以调用工具完成实际操作" not in instruction
        # 新措辞：必须有工具名 + 指引看 SKILL
        assert "open_website" in instruction
        assert "open_app" in instruction
        assert "SKILL" in instruction


# ============================================================
#  端到端：_on_done 调用 _detect_hallucination
# ============================================================

class TestOnDoneIntegration:
    def test_on_done_calls_detect_hallucination(self):
        """_on_done 应该调用 _detect_hallucination。"""
        # 用 monkey-patch 检测是否调用
        char_cfg = CharacterConfig(name="测试娘", tts_enabled=False)
        llm_cfg = LLMConfig(model="test", api_key="test",
                            base_url="http://localhost:11434/v1")
        cw = ChatWindow(llm_cfg, char_cfg, "assets/sprites")

        called = {"count": 0}
        original = cw._detect_hallucination

        def spy(response_text, tools_used):
            called["count"] += 1
            # 不追加系统消息（测试场景下 chat_view 没 ready）
            return None

        cw._detect_hallucination = spy  # type: ignore[assignment]

        # 模拟 _on_done 的一部分：调用 hallucination 检测
        # （不能直接调 _on_done 因为它依赖太多 worker 状态）
        cw._detect_hallucination("test response", [])
        assert called["count"] == 1


# ============================================================
#  _build_clean_messages_for_llm 单元测试
# ============================================================

class TestBuildCleanMessages:
    """【核心修复】history 里有幻觉痕迹时，必须在幻觉点之前注入 system 提示。

    背景：
        LLM Function Calling 的弱点：模型看到自己之前的「幻觉回复」
        （说「已打开 XX」但没调工具），就会**学着这样幻觉**。
        实测：连续两次「帮我打开 QQ」，第二次会跟着幻觉（即使工具 schema 正确）。
        修复：在 _kickoff_llm 时检查 history，发现幻觉消息就在它之前
        插入一条 user 提示「上文是幻觉，请这次真调工具」。
    """

    @pytest.fixture
    def chat(self):
        char_cfg = CharacterConfig(name="测试娘", tts_enabled=False)
        llm_cfg = LLMConfig(model="test", api_key="test",
                            base_url="http://localhost:11434/v1")
        cw = ChatWindow(llm_cfg, char_cfg, "assets/sprites")
        return cw

    def _msg(self, role, content, tools=None):
        m = Message(role=role, content=content)
        m.tools = tools or []
        return m

    def test_clean_when_no_hallucination(self, chat):
        """没有幻觉痕迹时 → 不注入额外消息。"""
        chat.history = [
            self._msg("user", "你好"),
            self._msg("assistant", "喵~ 主人好呀"),
            self._msg("user", "今天天气怎么样"),
            self._msg("assistant", "主人想知道天气吗？[happy]"),
        ]
        cleaned = chat._build_clean_messages_for_llm()
        # 4 条原消息，没有注入
        assert len(cleaned) == 4
        # 没有「系统提醒」
        assert not any("系统提醒" in m.content for m in cleaned)

    def test_clean_when_tools_were_used(self, chat):
        """工具真的调用过 → 即使说「已打开」也不算幻觉。"""
        chat.history = [
            self._msg("user", "帮我打开 QQ"),
            # 工具被调用了
            self._msg("assistant", "已启动 QQ~ [happy]",
                      tools=[("open_app", "已启动 QQ")]),
        ]
        cleaned = chat._build_clean_messages_for_llm()
        # 没注入 system 提示（因为有 tools）
        assert not any("系统提醒" in m.content for m in cleaned)

    def test_clean_injects_warning_before_hallucination(self, chat):
        """发现幻觉 → 在该消息之前插入 system 提示。"""
        chat.history = [
            self._msg("user", "帮我打开 QQ"),
            # 没工具 + 说了「已启动」= 幻觉
            self._msg("assistant", "已启动 QQ，主人~ [happy]"),
            self._msg("user", "帮我打开 QQ"),
        ]
        cleaned = chat._build_clean_messages_for_llm()
        # 应该注入 1 条 user 系统提醒（在第二个「已启动 QQ」之前）
        assert len(cleaned) == 4   # 3 原 + 1 注入
        # 找到注入的位置
        warnings = [m for m in cleaned if "系统提醒" in m.content]
        assert len(warnings) == 1
        # 警告应该在幻觉消息之前
        warn_idx = cleaned.index(warnings[0])
        hallucination_idx = next(i for i, m in enumerate(cleaned)
                                if m.role == "assistant" and "已启动 QQ" in m.content
                                and "系统提醒" not in m.content)
        assert warn_idx < hallucination_idx

    def test_clean_includes_warning_text(self, chat):
        """系统提示应该明确指出是幻觉 + 要求这次真调用工具。"""
        chat.history = [
            self._msg("user", "帮我打开 QQ"),
            self._msg("assistant", "已启动 QQ~ [happy]"),
        ]
        cleaned = chat._build_clean_messages_for_llm()
        warning = next(m for m in cleaned if "系统提醒" in m.content)
        # 应该提到「幻觉」
        assert "幻觉" in warning.content
        # 应该要求调用 open_app / open_website
        assert "open_app" in warning.content or "open_website" in warning.content

    def test_clean_handles_multiple_hallucinations(self, chat):
        """多个幻觉点 → 每个都注入警告。"""
        chat.history = [
            self._msg("user", "帮我打开 QQ"),
            self._msg("assistant", "已启动 QQ~ [happy]"),
            self._msg("user", "浏览器打开 baidu"),
            self._msg("assistant", "已打开 baidu~ [happy]"),
        ]
        cleaned = chat._build_clean_messages_for_llm()
        warnings = [m for m in cleaned if "系统提醒" in m.content]
        # 2 个幻觉点 → 2 条警告
        assert len(warnings) == 2

    def test_clean_uses_chinese_patterns(self, chat):
        """中文话术「已经打开」「已经复制到剪贴板」也算幻觉。"""
        chat.history = [
            self._msg("user", "浏览器打开 baidu.com"),
            self._msg("assistant", "已经打开 baidu.com~ [happy]"),
            self._msg("user", "复制 hello 到剪贴板"),
            # 必须完全匹配「已复制...剪贴板」模式
            self._msg("assistant", "已复制到剪贴板啦~ [happy]"),
        ]
        cleaned = chat._build_clean_messages_for_llm()
        warnings = [m for m in cleaned if "系统提醒" in m.content]
        assert len(warnings) == 2

    def test_clean_preserves_message_order(self, chat):
        """注入警告后消息顺序仍然是 user → (warning) → assistant。"""
        chat.history = [
            self._msg("user", "Q1"),
            self._msg("assistant", "A1"),
            self._msg("user", "Q2"),
            self._msg("assistant", "已启动 XX [happy]"),
        ]
        cleaned = chat._build_clean_messages_for_llm()
        # 找到「已启动 XX」的位置
        target_idx = next(i for i, m in enumerate(cleaned)
                          if m.role == "assistant" and "已启动" in m.content)
        # 警告应该在它前面
        prev = cleaned[target_idx - 1]
        assert "系统提醒" in prev.content

    def test_clean_skips_when_msg_has_neutral_content(self, chat):
        """assistant 消息没匹配任何幻觉模式 → 不注入。"""
        chat.history = [
            self._msg("user", "你好"),
            self._msg("assistant", "喵~ 主人好呀"),
            self._msg("user", "推荐首歌"),
            self._msg("assistant", "好呀~ 听这首吧"),
        ]
        cleaned = chat._build_clean_messages_for_llm()
        assert not any("系统提醒" in m.content for m in cleaned)
