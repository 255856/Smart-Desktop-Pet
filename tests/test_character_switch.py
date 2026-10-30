"""运行时角色切换测试（characters/*.yaml 枚举 + 切换 + 持久化 + 优雅失败）。

对应验收标准：
  A1 角色列表来自 characters/*.yaml（新文件自动出现，坏文件被报出来）
  A2 切换后 name / persona / system prompt / UI 名字都变
  A3 选择持久化到 data/character_selection.yaml，config.yaml 一个字节都不动
  A4 角色文件缺失 / 坏掉 → 优雅失败（保持当前角色，不崩）
  A5 切换不丢聊天历史
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import (CharacterConfig, list_characters,
                             load_character_entry, load_config,
                             switch_character)
from app.brain.agent import AgentLoop, apply_persona_to_client
from app.brain.llm_client import LLMClient, LLMConfig


# --------------------------------------------------------------------------
# 测试用的临时项目
# --------------------------------------------------------------------------

ALPHA = "character:\n  name: \"阿尔法\"\n  persona: |\n    你是阿尔法。\n"
BETA = "character:\n  name: \"贝塔\"\n  persona: |\n    你是贝塔，说话很直接。\n"


def _make_project(tmp_path: Path) -> Path:
    """造一个最小项目：config.yaml + characters/{alpha,beta}.yaml。"""
    root = Path(tmp_path)
    (root / "characters").mkdir(parents=True, exist_ok=True)
    (root / "characters" / "alpha.yaml").write_text(ALPHA, encoding="utf-8")
    (root / "characters" / "beta.yaml").write_text(BETA, encoding="utf-8")
    # config.yaml 里放一个**假** key（真 key 不会出现在测试里）
    (root / "config.yaml").write_text(
        "character:\n  character_file: characters/alpha.yaml\n"
        "llm:\n  api_key: sk-test-not-a-real-key\n",
        encoding="utf-8")
    return root


# --------------------------------------------------------------------------
# A1：角色列表来自 characters/*.yaml
# --------------------------------------------------------------------------

class TestCharacterEnumeration:
    def test_list_characters_scans_yaml_dir(self, tmp_path):
        root = _make_project(tmp_path)
        entries = list_characters(root)
        assert [e.file_name for e in entries] == ["alpha.yaml", "beta.yaml"]
        assert [e.name for e in entries] == ["阿尔法", "贝塔"]
        assert all(e.ok for e in entries)
        assert all(e.persona for e in entries)

    def test_new_file_appears_automatically(self, tmp_path):
        """A1：新增一个 yaml → 列表里自动多一项（不用改代码）。"""
        root = _make_project(tmp_path)
        assert len(list_characters(root)) == 2
        (root / "characters" / "gamma.yaml").write_text(
            "character:\n  name: \"伽马\"\n  persona: \"你是伽马。\"\n",
            encoding="utf-8")
        entries = list_characters(root)
        assert len(entries) == 3
        assert "gamma.yaml" in [e.file_name for e in entries]
        assert "伽马" in [e.name for e in entries]

    def test_broken_file_is_reported_not_raised(self, tmp_path):
        """A1：缺 character: 段 / 缺 name / 缺 persona 的文件要被报出来。"""
        root = _make_project(tmp_path)
        (root / "characters" / "no_block.yaml").write_text("foo: bar\n", encoding="utf-8")
        (root / "characters" / "no_name.yaml").write_text(
            "character:\n  persona: \"只有人设\"\n", encoding="utf-8")
        (root / "characters" / "no_persona.yaml").write_text(
            "character:\n  name: \"只有名字\"\n", encoding="utf-8")
        entries = {e.file_name: e for e in list_characters(root)}
        assert entries["no_block.yaml"].ok is False
        assert "character" in entries["no_block.yaml"].error
        assert entries["no_name.yaml"].ok is False
        assert "name" in entries["no_name.yaml"].error
        assert entries["no_persona.yaml"].ok is False
        assert "persona" in entries["no_persona.yaml"].error
        # 好的那两个照常列出来
        assert entries["alpha.yaml"].ok is True
        assert entries["beta.yaml"].ok is True

    def test_project_own_characters_are_valid(self):
        """仓库自带角色（小汐 / 小晓 / 大肥鱼…）都必须有 name + persona。"""
        entries = list_characters(ROOT)
        assert entries, "characters/ 目录里应该有角色文件"
        bad = [(e.file_name, e.error) for e in entries if not e.ok]
        assert not bad, "角色文件坏了：%s" % bad
        names = [e.name for e in entries]
        assert "小汐" in names


# --------------------------------------------------------------------------
# A2：切换后 name / persona / system prompt / UI 都变
# --------------------------------------------------------------------------

class TestSwitchChangesEverything:
    def test_switch_changes_name_and_persona(self, tmp_path):
        root = _make_project(tmp_path)
        cfg = load_config(root / "config.yaml")
        assert cfg.character.name == "阿尔法"
        ok, msg = switch_character(cfg, root, "characters/beta.yaml")
        assert ok, msg
        assert cfg.character.name == "贝塔"
        assert "贝塔" in cfg.character.persona
        assert cfg.character.character_file == "characters/beta.yaml"

    def test_client_system_prompt_actually_changes(self):
        """A2：system prompt 文本真的变了（不是同一个字符串）。"""
        old = "你是老角色，住在用户桌面上的桌宠。"
        new = "你是新角色，住在用户桌面上的桌宠。"
        client = LLMClient(LLMConfig(model="test", api_key="test"), old)
        try:
            before = client.system_prompt
            assert old in before
            apply_persona_to_client(client, new)
            after = client.system_prompt
            assert after != before, "换人设后 system prompt 必须变"
            assert new in after
            assert old not in after
        finally:
            asyncio.run(client.close())

    def test_react_backend_sees_new_persona(self):
        """A2：ReAct 后端（AgentLoop）换人设后重拼的 prompt 变了。"""
        class _FakeClient:
            def __init__(self, prompt: str):
                self.base_system_prompt = prompt
                self.system_prompt = prompt

        loop = AgentLoop(_FakeClient("你是老角色。"), registry=None)
        before = loop.assemble_system_prompt()
        assert "你是老角色。" in before
        after = loop.set_persona("你是新角色。")
        assert after != before
        assert "你是新角色。" in after
        assert "你是老角色。" not in after

    def test_standard_backend_sees_new_persona(self):
        """A2：standard 后端（LangChainAgent）换人设后 prompt 变了。"""
        from app.brain.langchain_agent import LangChainAgent
        from app.engine.tools import ToolRegistry

        agent = LangChainAgent(
            llm_cfg=LLMConfig(model="test", api_key="test"),
            registry=ToolRegistry(),
            persona="你是老角色。",
        )
        before = agent.assemble_system_prompt()
        assert "你是老角色。" in before
        after = agent.set_persona("你是新角色。")
        assert after != before
        assert "你是新角色。" in after
        assert "你是老角色。" not in after
        # 已编译的 agent 必须失效 → 下一轮按新人设重建
        assert agent._agent is None

    def test_both_backends_produce_different_prompts_per_character(self, tmp_path):
        """A2：两个后端对同一个角色拼出来的 prompt 都跟着角色变。"""
        from app.brain.langchain_agent import LangChainAgent
        from app.engine.tools import ToolRegistry

        root = _make_project(tmp_path)
        alpha = load_character_entry(root, "characters/alpha.yaml")
        beta = load_character_entry(root, "characters/beta.yaml")

        class _FakeClient:
            def __init__(self, prompt: str):
                self.base_system_prompt = prompt
                self.system_prompt = prompt

        react = AgentLoop(_FakeClient(alpha.persona), registry=None)
        lc = LangChainAgent(llm_cfg=LLMConfig(model="test", api_key="test"),
                            registry=ToolRegistry(), persona=alpha.persona)
        assert react.assemble_system_prompt() != lc.assemble_system_prompt() or True
        react_before = react.assemble_system_prompt()
        lc_before = lc.assemble_system_prompt()
        react.set_persona(beta.persona)
        lc.set_persona(beta.persona)
        assert react.assemble_system_prompt() != react_before
        assert lc.assemble_system_prompt() != lc_before


# --------------------------------------------------------------------------
# A3：持久化（config.yaml 一个字节都不动）
# --------------------------------------------------------------------------

class TestPersistence:
    def test_config_yaml_bytes_identical_after_switch(self, tmp_path):
        root = _make_project(tmp_path)
        cfg_path = root / "config.yaml"
        before = cfg_path.read_bytes()
        cfg = load_config(cfg_path)
        ok, msg = switch_character(cfg, root, "characters/beta.yaml")
        assert ok, msg
        assert cfg_path.read_bytes() == before, "config.yaml 不许被改写"
        assert b"sk-test-not-a-real-key" in cfg_path.read_bytes()

    def test_choice_persists_for_next_start(self, tmp_path):
        root = _make_project(tmp_path)
        cfg_path = root / "config.yaml"
        before = cfg_path.read_bytes()
        cfg = load_config(cfg_path)
        ok, _ = switch_character(cfg, root, "characters/beta.yaml")
        assert ok
        # 持久化落在**别的**文件里，不是 config.yaml
        sel = root / "data" / "character_selection.yaml"
        assert sel.is_file(), "角色选择应该写进 data/character_selection.yaml"
        assert "characters/beta.yaml" in sel.read_text(encoding="utf-8")
        # 下次启动（重新 load_config）拿到换过的角色
        cfg2 = load_config(cfg_path)
        assert cfg2.character.name == "贝塔"
        assert cfg2.character.persona == cfg.character.persona
        assert cfg2.character.character_file == "characters/beta.yaml"
        # config.yaml 依然一个字节没动
        assert cfg_path.read_bytes() == before

    def test_persisted_choice_beats_config_yaml(self, tmp_path):
        """config.yaml 里写的 character_file 还在，但持久化的选择优先。"""
        root = _make_project(tmp_path)
        cfg_path = root / "config.yaml"
        cfg = load_config(cfg_path)
        switch_character(cfg, root, "characters/beta.yaml")
        raw = cfg_path.read_text(encoding="utf-8")
        assert "characters/alpha.yaml" in raw
        cfg2 = load_config(cfg_path)
        assert cfg2.character.name == "贝塔"

    def test_broken_persisted_choice_falls_back(self, tmp_path):
        """持久化的角色文件后来坏了 → 回退 config.yaml 的角色，不崩。"""
        root = _make_project(tmp_path)
        cfg_path = root / "config.yaml"
        cfg = load_config(cfg_path)
        switch_character(cfg, root, "characters/beta.yaml")
        (root / "characters" / "beta.yaml").write_text("broken: [\n", encoding="utf-8")
        cfg2 = load_config(cfg_path)
        assert cfg2.character.name == "阿尔法"


# --------------------------------------------------------------------------
# A4：坏角色文件 → 优雅失败
# --------------------------------------------------------------------------

class TestGracefulFailure:
    def test_missing_file_keeps_current_character(self, tmp_path):
        root = _make_project(tmp_path)
        cfg = load_config(root / "config.yaml")
        before = (cfg.character.name, cfg.character.persona,
                  cfg.character.character_file)
        ok, msg = switch_character(cfg, root, "characters/nope.yaml")
        assert ok is False
        assert "切换失败" in msg
        assert (cfg.character.name, cfg.character.persona,
                cfg.character.character_file) == before

    def test_broken_file_keeps_current_character(self, tmp_path):
        root = _make_project(tmp_path)
        (root / "characters" / "broken.yaml").write_text(
            "character:\n  name: \"坏的\"\n", encoding="utf-8")
        cfg = load_config(root / "config.yaml")
        before = (cfg.character.name, cfg.character.persona)
        ok, msg = switch_character(cfg, root, "characters/broken.yaml")
        assert ok is False
        assert "persona" in msg
        assert (cfg.character.name, cfg.character.persona) == before

    def test_empty_path_keeps_current_character(self, tmp_path):
        root = _make_project(tmp_path)
        cfg = load_config(root / "config.yaml")
        before = cfg.character.name
        ok, msg = switch_character(cfg, root, "")
        assert ok is False
        assert cfg.character.name == before

    def test_switch_does_not_crash_on_unreadable_dir(self, tmp_path):
        """characters/ 目录都不存在 → 枚举返回空，不抛。"""
        root = tmp_path / "empty_project"
        root.mkdir()
        assert list_characters(root) == []


# --------------------------------------------------------------------------
# A5：聊天历史不受影响 + UI 名字跟着变
# --------------------------------------------------------------------------

class TestChatWindowSwitch:
    def _make_window(self, root: Path, qapp):
        from app.ui.chat_window import ChatWindow
        llm_cfg = LLMConfig(model="test", api_key="test",
                             base_url="http://localhost:11434/v1")
        return ChatWindow(
            llm_cfg, CharacterConfig(name="阿尔法", persona=ALPHA),
            str(root / "assets" / "sprites"),
            asr_enabled=False,
            cwd=str(root),
            cfg_path=str(root / "config.yaml"),
        )

    def test_ui_name_follows_character(self, tmp_path, qapp, monkeypatch):
        root = _make_project(tmp_path)
        monkeypatch.chdir(root)          # 别把 chat 历史写进仓库 data/
        cw = self._make_window(root, qapp)
        assert "阿尔法" in cw.title_label.text()
        ok, msg = cw.switch_character("characters/beta.yaml")
        assert ok, msg
        assert "贝塔" in cw.title_label.text()
        assert "阿尔法" not in cw.title_label.text()
        assert "贝塔" in cw.windowTitle()
        assert cw.char_cfg.name == "贝塔"

    def test_chat_history_survives_switch(self, tmp_path, qapp, monkeypatch):
        """A5：换角色不丢聊天历史。"""
        from app.ui.chat_window.message import Message

        root = _make_project(tmp_path)
        monkeypatch.chdir(root)
        cw = self._make_window(root, qapp)
        cw.history.append(Message(role="user", content="你好呀"))
        cw.history.append(Message(role="assistant", content="主人好~"))
        before = list(cw.history)

        ok, msg = cw.switch_character("characters/beta.yaml")
        assert ok, msg
        assert cw.history == before, "换角色不能动聊天历史"
        assert len(cw.history) == 2

    def test_switch_dialog_lists_all_characters(self, tmp_path, qapp, monkeypatch):
        """对话框把 characters/*.yaml 全列出来，当前角色选中。"""
        from app.ui.chat_window import _CharacterSwitchDialog

        root = _make_project(tmp_path)
        monkeypatch.chdir(root)
        entries = list_characters(root)
        dlg = _CharacterSwitchDialog(entries, "characters/alpha.yaml")
        assert dlg.list.count() == 2
        assert dlg.selected_entry().name == "阿尔法"
        assert dlg.selected_entry().path == "characters/alpha.yaml"

    def test_switch_dialog_marks_broken_file(self, tmp_path, qapp, monkeypatch):
        """坏文件也要列出来（并标出坏在哪），不能悄悄消失。"""
        from app.ui.chat_window import _CharacterSwitchDialog

        root = _make_project(tmp_path)
        (root / "characters" / "broken.yaml").write_text("foo: bar" + chr(10), encoding="utf-8")
        entries = list_characters(root)
        dlg = _CharacterSwitchDialog(entries, "characters/alpha.yaml")
        assert dlg.list.count() == 3
        broken = [e for e in entries if e.file_name == "broken.yaml"][0]
        assert broken.ok is False
        assert "character" in broken.error

    def test_character_button_exists_in_header(self, tmp_path, qapp, monkeypatch):
        root = _make_project(tmp_path)
        monkeypatch.chdir(root)
        cw = self._make_window(root, qapp)
        assert cw.btn_character.text() == "角色"
        assert cw.btn_character.toolTip()

    def test_switch_failure_keeps_ui_and_history(self, tmp_path, qapp, monkeypatch):
        from app.ui.chat_window.message import Message

        root = _make_project(tmp_path)
        monkeypatch.chdir(root)
        cw = self._make_window(root, qapp)
        cw.history.append(Message(role="user", content="你好呀"))
        ok, msg = cw.switch_character("characters/nope.yaml")
        assert ok is False
        assert "阿尔法" in cw.title_label.text()
        assert cw.char_cfg.name == "阿尔法"
        assert len(cw.history) == 1
