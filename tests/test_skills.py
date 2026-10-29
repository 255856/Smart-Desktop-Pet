"""Skills 系统的测试。

覆盖:
- list_skills:列目录 .md,按字母序,过滤非法名
- load_skill:按名加载,加 header / footer
- load_skills:多 skill 拼接 / 全部加载(names=None)/ 缺失跳过
- 非法 name 拒绝
- size cap
- 缓存命中
- LangChainAgent 接入(参数 + 注入到 system_prompt)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.brain import skills


@pytest.fixture(autouse=True)
def _clear_cache():
    skills.clear_cache()
    yield
    skills.clear_cache()


def _write(p: Path, content: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


# -------------------- list_skills --------------------

class TestListSkills:
    def test_empty_dir(self, tmp_path):
        assert skills.list_skills(tmp_path) == []

    def test_nonexistent_dir(self, tmp_path):
        assert skills.list_skills(tmp_path / "missing") == []

    def test_lists_md_files(self, tmp_path):
        _write(tmp_path / "code-review.md", "x")
        _write(tmp_path / "test-writing.md", "y")
        names = skills.list_skills(tmp_path)
        assert names == ["code-review", "test-writing"]

    def test_ignores_non_md(self, tmp_path):
        _write(tmp_path / "real.md", "x")
        _write(tmp_path / "notes.txt", "x")
        _write(tmp_path / "README", "x")   # no extension
        names = skills.list_skills(tmp_path)
        assert names == ["real"]


# -------------------- load_skill --------------------

class TestLoadSkill:
    def test_returns_content_with_header(self, tmp_path):
        _write(tmp_path / "code-review.md", "做 review 时先看测试。\n")
        content = skills.load_skill(tmp_path, "code-review")
        assert content is not None
        assert "[Skill: code-review]" in content
        assert "[/Skill]" in content
        assert "做 review 时先看测试" in content

    def test_missing_returns_none(self, tmp_path):
        assert skills.load_skill(tmp_path, "nope") is None

    def test_invalid_name_returns_none(self, tmp_path):
        # 名字含路径分隔符 / 空格是非法
        assert skills.load_skill(tmp_path, "../etc/passwd") is None
        assert skills.load_skill(tmp_path, "with space") is None
        assert skills.load_skill(tmp_path, "") is None


# -------------------- load_skills --------------------

class TestLoadSkills:
    def test_empty_when_dir_empty(self, tmp_path):
        assert skills.load_skills(tmp_path) == ""

    def test_loads_all_when_names_none(self, tmp_path):
        _write(tmp_path / "a.md", "AAA")
        _write(tmp_path / "b.md", "BBB")
        out = skills.load_skills(tmp_path)
        assert "AAA" in out
        assert "BBB" in out
        assert "Skills 结束" in out

    def test_loads_specific_names(self, tmp_path):
        _write(tmp_path / "a.md", "AAA")
        _write(tmp_path / "b.md", "BBB")
        out = skills.load_skills(tmp_path, names=["a"])
        assert "AAA" in out
        assert "BBB" not in out

    def test_missing_names_skipped(self, tmp_path):
        _write(tmp_path / "a.md", "AAA")
        out = skills.load_skills(tmp_path, names=["a", "nonexistent"])
        assert "AAA" in out
        # warning 而不是抛
        assert "nonexistent" not in out

    def test_all_missing_returns_empty(self, tmp_path):
        out = skills.load_skills(tmp_path, names=["nope1", "nope2"])
        assert out == ""


# -------------------- size cap --------------------

class TestSizeCap:
    def test_truncates_huge_file(self, tmp_path, monkeypatch):
        from app.brain import skills as s
        monkeypatch.setattr(s, "_MAX_FILE_BYTES", 100)
        _write(tmp_path / "huge.md", "X" * 5000)
        content = skills.load_skill(tmp_path, "huge")
        assert content is not None
        assert "已截断" in content


# -------------------- 缓存 --------------------

class TestCache:
    def test_cache_hits(self, tmp_path):
        _write(tmp_path / "x.md", "v1")
        c1 = skills.load_skill(tmp_path, "x")
        # 不改文件再读
        c2 = skills.load_skill(tmp_path, "x")
        assert c1 == c2

    def test_clear_cache_forces_re_read(self, tmp_path):
        _write(tmp_path / "x.md", "v1")
        c1 = skills.load_skill(tmp_path, "x")
        # 改文件
        import time
        time.sleep(1.1)
        (tmp_path / "x.md").write_text("v2", encoding="utf-8")
        # 缓存命中(秒级 mtime 没变)
        c2 = skills.load_skill(tmp_path, "x")
        # 取决于文件系统 mtime 精度,可能等于 c1 也可能等于新内容
        # 强制清缓存
        skills.clear_cache()
        c3 = skills.load_skill(tmp_path, "x")
        assert "v2" in c3


# -------------------- LangChainAgent 集成 --------------------

class TestLangChainAgentIntegration:
    def _make_agent(self, **kwargs):
        from app.brain.langchain_agent import LangChainAgent, LangChainAgentConfig
        from app.brain.llm_client import LLMConfig
        from app.engine.tools import ToolRegistry

        defaults = dict(
            llm_cfg=LLMConfig(),
            registry=ToolRegistry(),
            cfg=LangChainAgentConfig(),
            persona="base persona",
            thread_id="t-skill",
        )
        defaults.update(kwargs)
        return LangChainAgent(**defaults)

    def test_agent_accepts_skills_dir(self, tmp_path):
        agent = self._make_agent(skills_dir=str(tmp_path))
        assert agent._skills_dir == str(tmp_path)
        assert agent._auto_load_skills == []

    def test_agent_loads_no_skills_when_no_auto_load(self, tmp_path):
        """默认**一个 skill 都不注入**。

        改的是哪条行为：以前是「auto_load_skills 为空 ⇒ 把目录里全部 skill
        都塞进 system prompt」（load_skills 的 names=None 语义 = 全开）。
        skills/ 里放的是行为覆盖包（poem.md：任何问题都用七言绝句回答），
        全开 = 劫持人设 + 吃光 max_tokens 预算 → 主人看到的回复被截成空。
        现在：默认不注入，要用得显式点名（auto_load_skills）。
        """
        _write(tmp_path / "a.md", "AAA")
        _write(tmp_path / "b.md", "BBB")
        agent = self._make_agent(skills_dir=str(tmp_path))
        import asyncio
        asyncio.run(agent._ensure_agent())
        assert agent._cached_skills == ""
        assert "AAA" not in agent._cached_skills
        assert "BBB" not in agent._cached_skills

    def test_agent_loads_only_specified_auto_load_skills(self, tmp_path):
        _write(tmp_path / "a.md", "AAA")
        _write(tmp_path / "b.md", "BBB")
        agent = self._make_agent(
            skills_dir=str(tmp_path),
            auto_load_skills=["a"],
        )
        import asyncio
        asyncio.run(agent._ensure_agent())
        assert "AAA" in agent._cached_skills
        assert "BBB" not in agent._cached_skills

    def test_agent_no_skills_dir_falls_back_to_project_skills(self, tmp_path):
        """没给 skills_dir → 仍回落**项目根 / skills**（两个后端一致），但默认不注入。

        改的是哪条行为：原来是「没给 skills_dir 就没有 skills」。
        但 ReAct 后端（轻量）根本拿不到 skills_dir —— 保留旧行为的话，
        两个后端的 system prompt 就会不一样（Stage 1.1 的红线）。
        现在统一成「项目根 / skills」，两个后端拿到同一份。

        Stage 1.8：skills 改成**显式激活**才注入 —— 目录位置照旧回落，
        但默认（auto_load_skills 为空）一个都不进 prompt。
        """
        agent = self._make_agent()
        import asyncio
        asyncio.run(agent._ensure_agent())
        # 构造参数本身没变（还是没传 skills_dir）
        assert agent._skills_dir is None
        from app.brain.prompt_layer import app_root
        default_dir = app_root() / "skills"
        # 默认不注入任何 skill（skills/ 目录里的行为包要显式激活）
        assert agent._cached_skills == ""
        # 显式点名时，仍然从「项目根 / skills」回落去找
        named = self._make_agent(auto_load_skills=["poem"])
        asyncio.run(named._ensure_agent())
        if default_dir.is_dir() and any(default_dir.iterdir()):
            assert "[以下是主人激活的 Skills" in named._cached_skills
        else:
            assert named._cached_skills == ""

    def test_skills_combined_with_project_docs_in_system_prompt(self, tmp_path):
        """skills 和 AGENTS.md 同时存在时,两者都进 system_prompt。

        skills 要**显式点名**（auto_load_skills）才进 —— 默认不注入。
        """
        # 写一个 skills
        (tmp_path / "review.md").write_text("REVIEW_SKILL", encoding="utf-8")
        # AGENTS.md 在 cwd
        cwd_dir = tmp_path / "project"
        cwd_dir.mkdir()
        (cwd_dir / "AGENTS.md").write_text("AGENTS_DOC", encoding="utf-8")

        agent = self._make_agent(
            skills_dir=str(tmp_path),
            cwd=str(cwd_dir),
            auto_load_skills=["review"],
        )
        import asyncio
        asyncio.run(agent._ensure_agent())
        assert "REVIEW_SKILL" in agent._cached_skills
        assert "AGENTS_DOC" in agent._cached_project_docs