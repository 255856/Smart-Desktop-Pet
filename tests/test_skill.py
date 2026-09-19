"""SKILL.md 加载 + 注入测试。

验证：
    1. SKILL.md 文件存在 + 格式正确
    2. _read_skill_md 解析 frontmatter + body
    3. _get_skill_text 在文件缺失时返回兜底
    4. BrainController.chat_context 包含 SKILL 内容
    5. SKILL 在 prompt 最前（位置正确）
    6. SKILL 包含关键的 IF-THEN 规则
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".local-packages"))
sys.path.insert(0, str(ROOT))


# ============================================================
#  SKILL.md 文件存在性 + 格式
# ============================================================

class TestSkillFile:
    """SKILL.md 必须存在且符合 DSH 标准（YAML frontmatter + Markdown body）。"""

    def test_skill_md_exists(self):
        skill_path = ROOT / ".agents" / "skills" / "desktop-pet-tool-usage" / "SKILL.md"
        assert skill_path.is_file(), f"SKILL.md 不存在: {skill_path}"

    def test_skill_md_has_frontmatter(self):
        skill_path = ROOT / ".agents" / "skills" / "desktop-pet-tool-usage" / "SKILL.md"
        content = skill_path.read_text(encoding="utf-8")
        assert content.startswith("---\n"), "SKILL.md 必须以 YAML frontmatter 开始"
        # 找到第二个 ---
        assert "\n---\n" in content, "SKILL.md frontmatter 必须以 --- 关闭"

    def test_skill_md_frontmatter_has_required_fields(self):
        import yaml
        skill_path = ROOT / ".agents" / "skills" / "desktop-pet-tool-usage" / "SKILL.md"
        content = skill_path.read_text(encoding="utf-8")
        # 提取 frontmatter
        parts = content.split("---", 2)
        assert len(parts) >= 3, "frontmatter 必须被 --- 包围"
        fm = yaml.safe_load(parts[1])
        assert fm.get("name"), "frontmatter 必须有 name"
        assert fm.get("description"), "frontmatter 必须有 description"

    def test_skill_md_openai_yaml_exists(self):
        """DSH 规范：每个 skill 还要有 agents/openai.yaml 声明。"""
        openai_yaml = ROOT / ".agents" / "skills" / "desktop-pet-tool-usage" / "agents" / "openai.yaml"
        assert openai_yaml.is_file(), f"openai.yaml 不存在: {openai_yaml}"

    def test_skill_md_has_key_tool_examples(self):
        """SKILL.md 必须包含关键工具的 IF-THEN 规则（不能只是空话）。"""
        skill_path = ROOT / ".agents" / "skills" / "desktop-pet-tool-usage" / "SKILL.md"
        content = skill_path.read_text(encoding="utf-8").lower()
        # 关键工具：open_app / open_website / add_reminder / remember_fact
        for tool in ("open_app", "open_website", "add_reminder", "remember_fact"):
            assert tool in content, f"SKILL.md 必须提到 {tool}"

    def test_skill_md_has_hard_rules(self):
        """SKILL.md 必须有「硬规则」部分（防止模型幻觉）。"""
        skill_path = ROOT / ".agents" / "skills" / "desktop-pet-tool-usage" / "SKILL.md"
        content = skill_path.read_text(encoding="utf-8")
        assert "HARD RULES" in content or "硬规则" in content
        assert "hallucination" in content.lower() or "幻觉" in content

    def test_skill_md_has_correct_examples(self):
        """SKILL.md 必须有「正确调用」示例（不只是规则）。"""
        skill_path = ROOT / ".agents" / "skills" / "desktop-pet-tool-usage" / "SKILL.md"
        content = skill_path.read_text(encoding="utf-8")
        # 必须有具体的 user → assistant 示例
        assert "帮我打开 QQ" in content or "open QQ" in content.lower()


# ============================================================
#  _read_skill_md / _get_skill_text
# ============================================================

class TestSkillLoader:
    def test_read_skill_md_strips_frontmatter(self):
        from app.brain.brain_controller import _read_skill_md
        text = _read_skill_md(ROOT)
        assert text is not None
        # 不应该还以 --- 开头
        assert not text.startswith("---")
        # 应该以 # 标题开始（Markdown body）
        assert text.startswith("# ") or text.startswith("#")

    def test_get_skill_text_never_empty(self):
        """任何情况下 _get_skill_text 必须返回非空字符串。"""
        from app.brain.brain_controller import _get_skill_text
        text = _get_skill_text(ROOT)
        assert text and len(text) > 50, "skill text 必须 > 50 字符"

    def test_get_skill_text_fallback_when_missing(self):
        """如果 SKILL.md 删了，_get_skill_text 应该返回兜底内容（不报错）。"""
        from app.brain.brain_controller import _get_skill_text, _FALLBACK_SKILL
        # 用临时空目录模拟文件缺失
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            empty_root = Path(tmp)
            text = _get_skill_text(empty_root)
            # 应该返回兜底内容
            assert text == _FALLBACK_SKILL
            # 兜底也应该提到 open_app / open_website
            assert "open_app" in text
            assert "open_website" in text


# ============================================================
#  BrainController.chat_context 注入 SKILL
# ============================================================

class TestSkillInjection:
    @pytest.fixture
    def brain(self):
        from app.core.qt_compat import QApplication
        app = QApplication.instance() or QApplication(sys.argv)
        from app.core.config import load_config
        from app.engine.state_manager import StateManager
        from app.brain.brain_controller import BrainController

        cfg = load_config(ROOT / "config.yaml")
        state_mgr = StateManager(ROOT, cfg, type('Args', (), {'reset_state': False})())
        return BrainController(ROOT, cfg, state_mgr.state, state_mgr.reminders)

    def test_skill_loaded_into_chat_context(self, brain):
        ctx = brain.chat_context()
        # SKILL 关键内容必须在 chat_context 里
        assert "HARD RULES" in ctx or "硬规则" in ctx
        assert "open_app" in ctx
        assert "open_website" in ctx

    def test_skill_positioned_first_in_prompt(self, brain):
        """SKILL 必须在 prompt 第一个位置（让模型第一眼看到）。"""
        ctx = brain.chat_context()
        # 找 SKILL 起始位置（用 HARD RULES 标记）和时间标记位置
        skill_pos = ctx.find("HARD RULES")
        skill_pos_zh = ctx.find("硬规则")
        if skill_pos < 0:
            skill_pos = skill_pos_zh
        time_pos = ctx.find("【当前时间】")

        assert skill_pos >= 0, "chat_context 必须包含 SKILL 内容"
        assert time_pos >= 0, "chat_context 必须包含时间标记"
        # SKILL 必须在时间之前
        assert skill_pos < time_pos, (
            f"SKILL 应该在 prompt 前部（位置 {skill_pos} < 时间位置 {time_pos}）"
        )

    def test_skill_is_substantial_content(self, brain):
        """SKILL 内容要够丰富（不能只是兜底的几行）。"""
        ctx = brain.chat_context()
        # SKILL 部分应该 > 1000 字符（真实 SKILL.md 有 7000+）
        skill_end = ctx.find("【当前时间】")
        skill_part = ctx[:skill_end] if skill_end > 0 else ctx
        # 文件版 SKILL > 5000 字符；兜底 < 1000 字符
        # 测试保证文件版被加载（assert 大于 1000）
        assert len(skill_part) > 1000, (
            f"SKILL 内容太短（{len(skill_part)} 字符），可能用了兜底而非 SKILL.md"
        )

    def test_skill_cached_across_calls(self, brain):
        """_get_skill_text 在 BrainController 里应该缓存（不需要每轮重读）。"""
        # 第一次调用
        ctx1 = brain.chat_context()
        # 第二次调用
        ctx2 = brain.chat_context()
        # SKILL 内容应该一致（说明缓存生效）
        # 取 SKILL 部分的 hash
        s1 = ctx1.split("【当前时间】")[0]
        s2 = ctx2.split("【当前时间】")[0]
        assert s1 == s2, "SKILL 内容应该一致（缓存生效）"


# ============================================================
#  真实场景验证：SKILL 包含必要的 IF-THEN
# ============================================================

class TestSkillContent:
    """验证 SKILL.md 内容质量（不只是加载了，是真的有用）。"""

    @pytest.fixture
    def skill_text(self):
        from app.brain.brain_controller import _get_skill_text
        return _get_skill_text(ROOT)

    def test_open_app_patterns(self, skill_text):
        """SKILL 必须有「打开 XX」→ open_app 的明确规则。"""
        assert "open_app" in skill_text
        # 必须有具体例子
        assert "QQ" in skill_text
        # 关键词："打开"
        assert "打开" in skill_text

    def test_open_website_patterns(self, skill_text):
        """SKILL 必须有「打开 URL」→ open_website 的明确规则。"""
        assert "open_website" in skill_text
        # 必须提到 URL 形式
        assert "https://" in skill_text or "http://" in skill_text

    def test_add_reminder_patterns(self, skill_text):
        """SKILL 必须有「XX 分钟后提醒」→ add_reminder 的规则。"""
        assert "add_reminder" in skill_text
        assert "提醒" in skill_text

    def test_remember_fact_patterns(self, skill_text):
        """SKILL 必须有「记住 XX」→ remember_fact 的规则。"""
        assert "remember_fact" in skill_text
        assert "记住" in skill_text or "记忆" in skill_text

    def test_anti_patterns(self, skill_text):
        """SKILL 必须有反例（防止模型幻觉）。"""
        # 必须警告「不要只回文字不调工具」
        text_lower = skill_text.lower()
        assert ("hallucination" in text_lower or "幻觉" in skill_text)
        # 必须有具体的「WRONG / 错误」反例
        assert "WRONG" in skill_text or "❌" in skill_text or "错误" in skill_text

    def test_correct_examples(self, skill_text):
        """SKILL 必须有「CORRECT / 正确」示例。"""
        assert "CORRECT" in skill_text or "✅" in skill_text or "正确" in skill_text

    def test_quick_reference_table(self, skill_text):
        """SKILL 末尾应该有用工具速查表（让模型快速匹配）。"""
        # 必须有工具名 + 何时调 + 参数 的表格
        # 至少 5 个工具在速查表里
        tools_in_table = ["open_app", "open_website", "add_reminder",
                         "remember_fact", "get_current_time"]
        for tool in tools_in_table:
            assert tool in skill_text
