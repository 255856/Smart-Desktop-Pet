# -*- coding: utf-8 -*-
"""黑话 / 口头禅学习回路（qq-bridge slang-learner 的桌宠版）回归测试。

钉死验收标准：

* **A1** query_slang 返回 confirmed 词条；submit_slang 只产出 candidate；
* **A2** 只有 confirmed 词条进提示词（candidate / rejected 一律不进）；
* **A3** 容量上限 + 排序生效（confirmed 在裁剪时活下来）；
* **A4** 词库文件坏了 → 隔离（.corrupt-<时间戳>）并**继续往下走**，不当致命错误。

另外钉死：工具在注册表里、工具描述写清了「什么时候该提交 / 不该提交」、
提示词块是**常量**（两次拼装逐字节相同，不打掉前缀缓存）。

不需要 API key / 网络 / GUI，也不碰真 data/ 目录。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.engine.tools import _slang
from app.engine.tools._core import ToolRegistry


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------

@pytest.fixture
def library(tmp_path):
    """把词库指到临时目录（绝不碰真 data/slang.json）。"""
    path = tmp_path / "slang.json"
    _slang.set_library_path(path)
    yield path
    _slang.set_library_path(None)


@pytest.fixture
def reg():
    """只注册两个黑话工具的注册表。"""
    r = ToolRegistry()
    _slang.register(r)
    return r


def _entry(word, meaning="", status="candidate", count=1, updated="2026-01-01T00:00:00Z"):
    return {
        "id": "id-" + word,
        "content": word,
        "meaning": meaning,
        "usage": "",
        "example": "",
        "risk": "",
        "sources": [],
        "status": status,
        "source": "ai",
        "count": count,
        "evidence": [],
        "createdAt": updated,
        "updatedAt": updated,
    }


# --------------------------------------------------------------------------
# 注册 / 契约
# --------------------------------------------------------------------------

def test_two_tools_are_registered(reg):
    """两个内置工具在册。"""
    assert "query_slang" in reg.names()
    assert "submit_slang" in reg.names()


def test_default_library_path_is_under_data():
    """默认落 data/slang.json（**词表**，不是记忆 sidecar）。"""
    _slang.set_library_path(None)
    p = _slang.library_path()
    assert p.name == "slang.json"
    assert p.parent.name == "data"


def test_entry_carries_the_full_field_set():
    """一条词包含：content/meaning/usage/example/risk/sources/status/evidence/时间戳。"""
    e = _slang.normalize_slang_entry({"content": "x", "meaning": "m"})
    for field in ("content", "meaning", "usage", "example", "risk", "sources",
                  "status", "evidence", "createdAt", "updatedAt"):
        assert field in e, field
    assert e["status"] == _slang.STATUS_CANDIDATE


def test_three_statuses_exist():
    """三种状态：candidate / confirmed / rejected。"""
    assert _slang.SLANG_STATUSES == ("candidate", "confirmed", "rejected")


def test_tool_descriptions_say_when_to_submit_and_when_not(reg):
    """工具描述要写清「什么时候该提交」「什么时候不该提交」。"""
    desc = reg.get("submit_slang").description
    assert "反复" in desc, "没写什么时候该提交"
    assert "打错" in desc, "没写什么时候不该提交"
    assert "candidate" in desc
    assert "确认" in desc


# --------------------------------------------------------------------------
# A1 · query 返回 confirmed / submit 只产出 candidate
# --------------------------------------------------------------------------

def test_submit_creates_candidate_not_confirmed(library, reg):
    """submit_slang 加的是**候选**，不是 confirmed。"""
    out = reg.get("submit_slang").fn(content="yyds", meaning="永远的神",
                                      evidence="这也太强了 yyds")
    assert "候选" in out
    data = json.loads(library.read_text(encoding="utf-8"))
    assert len(data) == 1
    assert data[0]["content"] == "yyds"
    assert data[0]["status"] == _slang.STATUS_CANDIDATE
    assert data[0]["meaning"] == "永远的神"
    assert data[0]["evidence"][0]["text"] == "这也太强了 yyds"


def test_query_returns_confirmed_entries(library, reg):
    """query_slang 返回 confirmed 词条。"""
    _slang.save_slang([
        _entry("yyds", "永远的神", status="confirmed"),
        _entry("候选词", "候选含义", status="candidate"),
    ])
    out = reg.get("query_slang").fn()
    assert "yyds" in out
    assert "永远的神" in out
    assert "候选词" not in out, "候选不该出现在默认查询里"


def test_query_can_look_up_candidates_explicitly(library, reg):
    """显式要候选时能查到（模型需要知道「已经提过了」）。"""
    _slang.save_slang([_entry("候选词", "候选含义", status="candidate")])
    out = reg.get("query_slang").fn(status="candidate")
    assert "候选词" in out


def test_query_by_keyword(library, reg):
    """按关键词查已收录词。"""
    _slang.save_slang([
        _entry("yyds", "永远的神", status="confirmed"),
        _entry("xswl", "笑死我了", status="confirmed"),
    ])
    out = reg.get("query_slang").fn(keyword="yyds")
    assert "yyds" in out
    assert "xswl" not in out


def test_submit_does_not_downgrade_a_confirmed_word(library, reg):
    """已收录的词再提交一遍 → 不会退回候选。"""
    _slang.save_slang([_entry("yyds", "永远的神", status="confirmed")])
    out = reg.get("submit_slang").fn(content="yyds", meaning="瞎编的")
    assert "confirmed" in out
    data = json.loads(library.read_text(encoding="utf-8"))
    assert data[0]["status"] == _slang.STATUS_CONFIRMED
    assert data[0]["meaning"] == "永远的神"


def test_repeat_submit_counts_up_but_stays_candidate(library, reg):
    """同一个候选再提一次 → 次数 +1，状态还是候选。"""
    reg.get("submit_slang").fn(content="yyds")
    reg.get("submit_slang").fn(content="yyds")
    data = json.loads(library.read_text(encoding="utf-8"))
    assert len(data) == 1
    assert data[0]["count"] == 2
    assert data[0]["status"] == _slang.STATUS_CANDIDATE


# --------------------------------------------------------------------------
# A2 · 只有 confirmed 进提示词
# --------------------------------------------------------------------------

def test_only_confirmed_entries_are_injected(library):
    """candidate / rejected 一律不进提示词。"""
    _slang.save_slang([
        _entry("yyds", "永远的神", status="confirmed"),
        _entry("候选词", "候选含义", status="candidate"),
        _entry("否决词", "否决含义", status="rejected"),
    ])
    block = _slang.confirmed_prompt_block()
    assert "yyds" in block
    assert "候选词" not in block
    assert "否决词" not in block
    assert _slang.SLANG_USAGE_RULE in block


def test_prompt_layer_injects_the_confirmed_block(library):
    """system prompt 里带【已收录黑话】段，且只有 confirmed 词条。"""
    from app.brain.prompt_layer import build_system_prompt

    _slang.save_slang([
        _entry("yyds", "永远的神", status="confirmed"),
        _entry("候选词", "候选含义", status="candidate"),
    ])
    sp = build_system_prompt(persona="人设", style_guide="", tool_handbook="",
                             dynamic="")
    assert _slang.SLANG_PROMPT_HEADER in sp
    assert "yyds" in sp
    assert "候选词" not in sp
    assert _slang.SLANG_USAGE_RULE in sp


def test_prompt_block_is_constant_between_calls(library):
    """提示词块是**常量**：两次拼装逐字节相同（不打掉前缀缓存）。"""
    from app.brain.prompt_layer import build_system_prompt

    _slang.save_slang([_entry("yyds", "永远的神", status="confirmed")])
    sp1 = build_system_prompt(persona="人设", style_guide="", tool_handbook="",
                              dynamic="")
    sp2 = build_system_prompt(persona="人设", style_guide="", tool_handbook="",
                              dynamic="")
    assert sp1 == sp2
    assert _slang.confirmed_prompt_block() == _slang.confirmed_prompt_block()


def test_empty_library_injects_nothing(library):
    """没有已收录词 → 提示词里不出现这一段。"""
    from app.brain.prompt_layer import build_system_prompt

    sp = build_system_prompt(persona="人设", style_guide="", tool_handbook="",
                             dynamic="")
    assert _slang.SLANG_PROMPT_HEADER not in sp


# --------------------------------------------------------------------------
# A3 · 容量上限 + 排序
# --------------------------------------------------------------------------

def test_cap_keeps_confirmed_and_drops_rejected():
    """裁剪时 confirmed > candidate > rejected。"""
    entries = [_entry("c%d" % i, status="candidate") for i in range(5)]
    entries += [_entry("k%d" % i, status="confirmed") for i in range(5)]
    entries += [_entry("rej", status="rejected")]
    kept, dropped = _slang.cap_slang_entries(entries, 6)
    assert dropped == 5
    assert len(kept) == 6
    assert [e["status"] for e in kept].count("confirmed") == 5
    assert "rej" not in [e["content"] for e in kept]


def test_cap_ranks_by_hit_count_then_recency():
    """同状态内：出现次数多的先留，再比最近更新。"""
    entries = [
        _entry("old", status="confirmed", count=1, updated="2026-01-01T00:00:00Z"),
        _entry("hot", status="confirmed", count=9, updated="2026-01-01T00:00:00Z"),
        _entry("new", status="confirmed", count=1, updated="2026-06-01T00:00:00Z"),
    ]
    kept, _dropped = _slang.cap_slang_entries(entries, 2)
    assert [e["content"] for e in kept] == ["hot", "new"]


def test_save_applies_the_cap_and_keeps_confirmed(library):
    """存盘时按上限裁剪，confirmed 词条活下来。"""
    entries = [_entry("c%d" % i, status="candidate") for i in range(10)]
    entries += [_entry("k%d" % i, status="confirmed") for i in range(10)]
    _slang.save_slang(entries, library, max_entries=5)
    loaded = _slang.load_slang(library)
    assert len(loaded) == 5
    assert all(e["status"] == _slang.STATUS_CONFIRMED for e in loaded)


def test_library_has_a_capacity_cap():
    """词库有明确上限（不然它只会涨）。"""
    assert _slang.SLANG_MAX_ENTRIES > 0


# --------------------------------------------------------------------------
# A4 · 坏文件 → 隔离，不致命
# --------------------------------------------------------------------------

def test_corrupt_library_is_quarantined_not_fatal(library):
    """JSON 坏了 → 原文另存为 .corrupt-<时间戳>，并且**继续往下走**。"""
    library.write_text("{ 这不是 json", encoding="utf-8")
    entries = _slang.load_slang(library)
    assert entries == []
    quarantined = sorted(library.parent.glob("slang.json.corrupt-*"))
    assert quarantined, "坏文件没被隔离"
    assert quarantined[0].read_text(encoding="utf-8") == "{ 这不是 json"
    # 隔离之后还能继续用（submit 不报错）
    out = _slang._submit_slang("测试词", meaning="测试")
    assert "候选" in out
    assert json.loads(library.read_text(encoding="utf-8"))[0]["content"] == "测试词"


def test_non_array_library_is_quarantined(library):
    """顶层不是数组 → 同样隔离，不当致命错误。"""
    library.write_text('{"a": 1}', encoding="utf-8")
    assert _slang.load_slang(library) == []
    assert list(library.parent.glob("slang.json.corrupt-*"))


def test_missing_library_is_an_empty_table(library):
    """文件不存在 = 首次运行 = 空库（不是错误）。"""
    assert _slang.load_slang(library) == []


def test_corrupt_library_does_not_break_the_prompt(library):
    """词库坏了，提示词照样能拼（降级成空块）。"""
    from app.brain.prompt_layer import build_system_prompt

    library.write_text("}}} 坏掉的", encoding="utf-8")
    sp = build_system_prompt(persona="人设", style_guide="", tool_handbook="",
                             dynamic="")
    assert "人设" in sp
    assert _slang.SLANG_PROMPT_HEADER not in sp


# --------------------------------------------------------------------------
# 工具层行为
# --------------------------------------------------------------------------

def test_submit_rejects_empty_content(library, reg):
    """空词条 → 如实报错，不写盘。"""
    out = reg.get("submit_slang").fn(content="   ")
    assert out.startswith("错误")
    assert not library.exists()


def test_query_on_empty_library_says_so(library, reg):
    """空库 → 如实说没有，不假装查过。"""
    out = reg.get("query_slang").fn()
    assert "还没有" in out
    assert "错误" not in out


def test_submit_then_confirm_by_hand_then_it_shows_up(library, reg):
    """完整回路：提交候选 → 主人改 status → 词条可查、可进提示词。"""
    reg.get("submit_slang").fn(content="yyds", meaning="永远的神")
    data = json.loads(library.read_text(encoding="utf-8"))
    assert data[0]["status"] == "candidate"
    assert "yyds" not in reg.get("query_slang").fn()

    data[0]["status"] = "confirmed"
    library.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    _slang.invalidate_prompt_cache()

    assert "yyds" in reg.get("query_slang").fn()
    assert "yyds" in _slang.confirmed_prompt_block()


def test_normalize_clamps_field_lengths():
    """字段长度封顶（词库不会无限膨胀）。"""
    e = _slang.normalize_slang_entry({
        "content": "词" * 200,
        "meaning": "意" * 1000,
        "sources": ["s" * 1000] * 50,
    })
    assert len(e["content"]) <= _slang.SLANG_CONTENT_MAX
    assert len(e["meaning"]) <= _slang.SLANG_FIELD_MAX
    assert len(e["sources"]) <= _slang.SOURCES_MAX


def test_normalize_falls_back_to_candidate_for_bogus_status():
    """状态非法 → 落回 candidate（不许蒙成 confirmed）。"""
    e = _slang.normalize_slang_entry({"content": "x", "status": "随便写的"})
    assert e["status"] == _slang.STATUS_CANDIDATE


def test_prompt_text_is_escaped():
    """词条内容不能把提示词结构撑开（防 prompt injection）。"""
    body = _slang.build_slang_context(
        [_entry("<script>x</script>", "含义 & 味道", status="confirmed")])
    assert "<script>" not in body
    assert "&lt;" in body


def test_library_path_override_is_used(library):
    """路径覆盖生效（测试/本机换位置）。"""
    assert _slang.library_path() == Path(library)
