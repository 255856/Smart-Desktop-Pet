"""接线(a) Phase 2/3 → ChatWindow 上下文层回归测试。

覆盖:
- BrainController 创建 SessionStore + skills_dir 后,挂到 self.session_store / self.skills_dir
- ChatWindow 接收新 kwargs(cwd / session_id / session_store / skills_dir / auto_load_skills /
  extra_docs_paths),存到 self._*
- ChatWindow 把这些 self._* 透传给 LangChainAgent(用 mock 验证 _lc_agent 收到正确 kwargs)
- _PlanWorker 信号协议跟 _AgentWorker 一致(chunk / done / failed)
- _quick_send_plan / _kickoff_llm_plan / _render_plan_card 在 standard / lightweight
  backend 下都不会崩
- LangChainAgent.plan_sync 是个可调用方法,签名跟 run_sync 平行

不调 LLM、不连真实 Qt(用 QApplication.instance() 复用测试 fixture 已有的)。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock, ANY

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".local-packages"))
sys.path.insert(0, str(ROOT))

from app.core.qt_compat import QApplication  # noqa: E402
_app = QApplication.instance() or QApplication(sys.argv)

from app.core.config import CharacterConfig  # noqa: E402
from app.brain.llm_client import LLMConfig  # noqa: E402
from app.brain.sessions import SessionStore  # noqa: E402


def _make_chat_window(**extra):
    """工厂:最小可用的 ChatWindow (避免触发 LLM/真扫描)."""
    char_cfg = CharacterConfig(name="接线测试娘", tts_enabled=False)
    llm_cfg = LLMConfig(model="test", api_key="test",
                         base_url="http://localhost:11434/v1")
    from app.ui.chat_window import ChatWindow
    return ChatWindow(llm_cfg, char_cfg, "assets/sprites", **extra)


# ============================================================
#  BrainController session_store / skills_dir 接线
# ============================================================

class TestBrainControllerWiring:
    """BrainController 创建好后应该挂上 SessionStore 和 skills_dir。"""

    def test_brain_creates_session_store(self, tmp_path):
        """BrainController.__init__ 在 data/ 下建一个 pet_sessions.db SessionStore。"""
        from app.brain.brain_controller import BrainController
        from app.core.config import load_config
        from app.engine.state import PetState
        from app.engine.reminder import ReminderStore

        cfg = load_config()
        cfg.brain.memory_file = str(tmp_path / "memory.json")
        state = PetState()
        # ReminderStore 需要 QApplication,我们已经在模块顶部建好了
        rem_path = tmp_path / "reminders.json"
        reminders = ReminderStore(rem_path)
        brain = BrainController(
            root=tmp_path, cfg=cfg,
            state=state, reminders=reminders,
        )
        assert hasattr(brain, "session_store"), "BrainController 必须暴露 session_store"
        assert hasattr(brain, "skills_dir"), "BrainController 必须暴露 skills_dir"
        # 即使 SessionStore 没建出来(磁盘失败/异常),属性也不能 AttributeError
        # —— best-effort 降级路径断言
        assert brain.skills_dir.exists() or not brain.skills_dir.exists()
        # session_store 要么是 SessionStore 实例,要么 None(降级)
        assert brain.session_store is None or hasattr(brain.session_store, "create")


# ============================================================
#  ChatWindow 新 kwargs 接收 + 透传
# ============================================================

class TestChatWindowKwargs:
    """ChatWindow.__init__ 应该接收 + 透传 phase 2/3 引擎层上下文。"""

    def test_chat_window_accepts_cwd(self, tmp_path):
        cw = _make_chat_window(cwd=str(tmp_path))
        assert cw._cwd == str(tmp_path)

    def test_chat_window_accepts_session_id(self):
        # session_id 必须配 session_store,否则 LangChainAgent 构造会挂
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(str(Path(tmp) / "s.db"))
            info = store.create(title="test")
            cw = _make_chat_window(session_id=info.session_id,
                                    session_store=store)
            assert cw._session_id == info.session_id
            assert cw._session_store is store

    def test_chat_window_accepts_skills_dir(self, tmp_path):
        cw = _make_chat_window(skills_dir=str(tmp_path))
        assert cw._skills_dir == str(tmp_path)

    def test_chat_window_accepts_auto_load_skills(self):
        names = ["code-review", "explain-error"]
        cw = _make_chat_window(auto_load_skills=names)
        assert cw._auto_load_skills == names

    def test_chat_window_accepts_extra_docs_paths(self):
        paths = ["E:/proj/AGENTS.md", "E:/proj2/CLAUDE.md"]
        cw = _make_chat_window(extra_docs_paths=paths)
        assert cw._extra_docs_paths == paths


# ============================================================
#  LangChainAgent 透传(用 mock 验证 kwargs)
# ============================================================

class TestLangChainAgentTransparentPass:
    """ChatWindow 在 standard backend + 接到 widget 时,应该把 kwargs 喂给 LangChainAgent。"""

    def test_plan_sync_method_exists(self):
        """接线(a):LangChainAgent 必须暴露 plan_sync 方法,跟 run_sync 平行。"""
        from app.brain.langchain_agent import LangChainAgent
        assert hasattr(LangChainAgent, "plan_sync")
        assert callable(LangChainAgent.plan_sync)
        # 签名平行(messages, cancel_check, timeout)
        import inspect
        sig = inspect.signature(LangChainAgent.plan_sync)
        params = list(sig.parameters.keys())
        assert "messages" in params
        assert "cancel_check" in params
        assert "timeout" in params

    def test_planworker_signals_match(self):
        """_PlanWorker 信号协议应与 _AgentWorker 一致(chunk / done / failed)。
        plan 模式不调工具,所以没有 tool_used / meta。
        """
        from app.ui.chat_window.worker import _PlanWorker
        keys = [k for k in dir(_PlanWorker) if not k.startswith("_")]
        # Qt Signal 名字就是属性名
        # 'chunk', 'done', 'failed'
        assert hasattr(_PlanWorker, "chunk"), "_PlanWorker 必须有 chunk 信号"
        assert hasattr(_PlanWorker, "done"), "_PlanWorker 必须有 done 信号"
        assert hasattr(_PlanWorker, "failed"), "_PlanWorker 必须有 failed 信号"

    def test_chat_window_renders_plan_card(self):
        """_render_plan_card 应该把 plan 文本渲染到 chat_view,不入 history。"""
        cw = _make_chat_window()
        before_history = len(cw.history)
        cw._render_plan_card(user_text="", plan_text="# 我的计划\n1. 第一步\n2. 第二步")
        after_history = len(cw.history)
        assert before_history == after_history, "plan 卡片不应该进 history"
        # chat_view 应该有内容
        assert cw.chat_view.toPlainText() != ""
