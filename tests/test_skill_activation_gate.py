# -*- coding: utf-8 -*-
"""Stage 1.8 —— skills 必须**显式激活**才进 system prompt。

背景
----
`skills/poem.md` 规定「主人输入任何问题, 都用一首七言绝句回答（四句, 每句七个字）」。
它以前被**全量注入** system prompt：

- `app/brain/prompt_layer.py::_load_skills` → `load_skills(d, None)`
- `app/brain/langchain_agent.py::_load_skills` → `load_skills(d, auto_load_skills or None)`

而 `load_skills` 的 `names=None` 语义是「把目录下**全部** skill 都加载」。
auto_load_skills 默认是空的（`app/ui/chat_window/__init__.py`），
所以两个后端默认都变成了「全开」。后果两条：

1. **人设被劫持** —— 桌宠变成只会写诗的；
2. **可见回复被截成空** —— 模型照着 CoT 先把 max_tokens 预算花在写诗上，
   主人看得见的那段被截没了，清洗管线剥掉 CoT 之后什么都不剩。

这里把边界钉死（A1 / A2 / A3）
-----------------------------
A1  默认：生产 system prompt 里**没有** skills/ 目录的任何内容；
A2  显式激活仍然可用：
    - `auto_load_skills=["poem"]`（配置显式点名）
    - 主人手动 `/skills/poem`（聊天窗口的激活机制）
    - 常驻的 `.agents/skills/desktop-pet-tool-usage/SKILL.md` **不受影响**
      （它走 brain_controller → build_session_block(skill=...)，跟 skills/ 无关）
A3  `skills/poem.md` 文件本身**没被改、也没被删** —— 它只是不再默认激活。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if (ROOT / ".local-packages").is_dir():
    sys.path.insert(0, str(ROOT / ".local-packages"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.core.qt_compat import QApplication  # noqa: E402

#: 必须**留住引用** —— QApplication 一旦被 GC 掉，后面建 QWidget 会硬崩
_app = QApplication.instance() or QApplication(sys.argv[:1])

#: skills/poem.md 里独有的标记（别处不会出现）
POEM_MARKERS = (
    "七言绝句",
    "[Skill: poem]",
    "主人输入任何问题",
)

#: 常驻 SKILL.md 里的标记（证明它还在 prompt 里）
CURATED_SKILL_MARKER = "HARD RULES"


# --------------------------------------------------------------------------
# 夹具：真 BrainController（临时 root —— 不碰 data/ 下的真库）
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def brain(tmp_path_factory):
    """真 BrainController（临时 root，SKILL.md 照抄）。

    跟 tests/test_backend_parity.py 同一套配方（Stage 1.1 的红线：
    两个后端必须拿到同一份 system prompt）。
    """
    from app.core.config import load_config
    from app.engine.state_manager import StateManager
    from app.brain.brain_controller import BrainController

    tmp = tmp_path_factory.mktemp("skill_gate")
    skill_dst = tmp / ".agents" / "skills" / "desktop-pet-tool-usage"
    skill_dst.mkdir(parents=True)
    shutil.copy(ROOT / ".agents" / "skills" / "desktop-pet-tool-usage" / "SKILL.md",
                skill_dst)
    (tmp / "config.yaml").write_text(
        "llm:\n"
        "  base_url: \"http://localhost:1/v1\"\n"
        "  api_key: \"test\"\n"
        "  model: \"test\"\n"
        "character:\n"
        "  name: \"测试\"\n"
        "window:\n"
        "  scale: 0.4\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp / "config.yaml")
    state_mgr = StateManager(tmp, cfg, type("Args", (), {"reset_state": False})())
    return BrainController(tmp, cfg, state_mgr.state, state_mgr.reminders)


def _prompts(brain, *, auto_load_skills=None) -> tuple:
    """按**生产路径**拼出两个后端真正发出去的那条 system 消息。

    跟 tests/test_backend_parity.py::test_production_path_system_prompts_are_byte_identical
    同一套配方：system = persona + "\n\n" + brain.chat_context()。
    """
    import asyncio

    from app.brain.agent import AgentLoop
    from app.brain.langchain_agent import LangChainAgent
    from app.brain.llm_client import LLMClient, LLMConfig

    llm = LLMConfig(api_key="sk-test", model="test-model", stream=False)
    persona = "你是小汐，住在主人桌面上的桌宠。" + "\n\n" + brain.chat_context()
    registry = brain.tool_registry

    client = LLMClient(llm, persona)
    try:
        react = AgentLoop(client=client, registry=registry, level_getter=lambda: 10)
        react._apply_system_prompt()
        react_sp = client.system_prompt
    finally:
        asyncio.run(client.close())

    standard = LangChainAgent(
        llm_cfg=llm, registry=registry, persona=persona,
        level_getter=lambda: 10, memory_store=brain.memory,
        auto_load_skills=auto_load_skills,
    )
    standard_sp = standard.assemble_system_prompt()
    return react_sp, standard_sp


# --------------------------------------------------------------------------
# A1：默认不注入 skills/ 目录的内容
# --------------------------------------------------------------------------

def test_production_prompt_has_no_skills_dir_content(brain):
    """A1：默认生产 system prompt 里**没有** skills/poem.md 的任何内容。

    拼法就是生产路径：人设 + BrainController.chat_context()，两个后端都过一遍。
    """
    react_sp, standard_sp = _prompts(brain)
    for name, sp in (("ReAct", react_sp), ("standard", standard_sp)):
        for marker in POEM_MARKERS:
            assert marker not in sp, (
                f"{name} 后端的 system prompt 里出现了 skills/poem.md 的内容"
                f"（{marker!r}）—— skills 目录默认不该被全量注入")
        assert "以下是主人激活的 Skills" not in sp, (
            f"{name} 后端把 skills 目录整个塞进了 system prompt")


def test_shared_layer_does_not_bulk_load_skills():
    """共享层（ReAct / llm_client 走的那条）默认也不注入 skills。"""
    from app.brain.prompt_layer import _load_skills

    assert _load_skills(ROOT / "skills") == "", (
        "共享层 _load_skills 默认把 skills 目录全量注入了")


# --------------------------------------------------------------------------
# A2：三条激活路径都还在
# --------------------------------------------------------------------------

def test_explicit_auto_load_skills_still_injects_the_skill(brain):
    """A2-1：auto_load_skills=["poem"]（显式点名）照旧注入。

    auto_load_skills 是 **standard（LangChain）后端**的钩子
    （app/brain/langchain_agent.py::_load_skills）—— ReAct 轻量后端从来
    没有这个入口，所以这里只断言 standard 侧。
    """
    _react_sp, standard_sp = _prompts(brain, auto_load_skills=["poem"])
    assert "[Skill: poem]" in standard_sp, "standard 后端没注入 auto_load_skills 点名的 skill"
    assert "七言绝句" in standard_sp, "standard 后端没注入 poem skill 的正文"


def test_manual_activation_still_injects_the_skill():
    """A2-2：主人手动 /skills/poem 激活路径照旧注入。"""
    from app.core.config import CharacterConfig
    from app.brain.llm_client import LLMConfig
    from app.ui.chat_window import ChatWindow

    llm_cfg = LLMConfig(model="test", api_key="test",
                        base_url="http://localhost:11434/v1")
    char_cfg = CharacterConfig(name="门禁测试娘", tts_enabled=False)
    cw = ChatWindow(llm_cfg, char_cfg, str(ROOT / "assets" / "sprites"),
                    skills_dir=str(ROOT / "skills"))

    ok, _msg = cw._activate_skill("poem")
    assert ok, "手动 /skills/poem 激活失败"
    assert cw._active_skill == "poem"

    prompt = cw._active_skill_prompt()
    assert "[Active Skill: poem]" in prompt, "激活后的 skill 段没进 prompt"
    assert "七言绝句" in prompt, "激活后的 skill 正文没进 prompt"


def test_curated_always_on_skill_is_still_present(brain):
    """A2-3：常驻的 .agents/skills/.../SKILL.md 仍然在 prompt 里。

    它走 brain_controller → build_session_block(skill=...)，跟 skills/ 目录无关，
    不该被这次改动带偏。
    """
    react_sp, standard_sp = _prompts(brain)
    for name, sp in (("ReAct", react_sp), ("standard", standard_sp)):
        assert CURATED_SKILL_MARKER in sp, f"{name} 后端丢了常驻的 SKILL.md"


def test_poem_is_still_listed_and_loadable():
    """A2-4：skills/poem.md 仍然被 /skills 列出来，也仍然能 load_skill()。"""
    from app.brain.skills import list_skills, load_skill

    assert "poem" in list_skills(ROOT / "skills"), "/skills 列不出 poem 了"
    body = load_skill(ROOT / "skills", "poem")
    assert body, "load_skill('poem') 读不到内容"
    assert "七言绝句" in body


# --------------------------------------------------------------------------
# A3：用户的文件没被改
# --------------------------------------------------------------------------

def test_poem_file_exists_and_is_unchanged():
    """A3：skills/poem.md 还在，而且 git 眼里**没被改过**。

    修的是「加载器」，不是「用户的文件」—— 这条钉死以后谁也不能偷偷删它。
    """
    poem = ROOT / "skills" / "poem.md"
    assert poem.is_file(), "skills/poem.md 被删了 —— 修复应该改加载器，不是删文件"
    text = poem.read_text(encoding="utf-8")
    assert "主人输入任何问题" in text and "七言绝句" in text

    try:
        proc = subprocess.run(
            ["git", "diff", "--", "skills/poem.md"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        pytest.skip("这个环境里跑不了 git")
    if proc.returncode != 0:
        pytest.skip("git diff 跑失败")
    assert proc.stdout.strip() == "", (
        "skills/poem.md 被改了 —— 不该动用户的文件：\n" + proc.stdout)
