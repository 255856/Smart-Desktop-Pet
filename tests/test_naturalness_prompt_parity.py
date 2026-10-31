# -*- coding: utf-8 -*-
"""自然度评测集的「录制用 prompt == 生产 prompt」回归测试（Stage 1.7）。

钉死三件事：

1. scripts/record_naturalness.py 拼出来的 system prompt，跟两个后端
   **真正发出去**的那条 system 消息逐字节相同 ——
   录制用的必须是生产 prompt（Stage 1.4 删掉硬上限之后，评测集就过期了，
   必须拿生产 prompt 重录）；
2. 评测集里记的 system_prompt_fragments 是生产 prompt 的**逐字子串**
   —— 录制脚本的 staleness 检查不能报 mismatch；
3. 片段里**必须**有中文输出约束（CHINESE_SYSTEM_SUFFIX）全文 ——
   句数硬上限「回复 1~3 句」原来住在那儿。少了它，用例里那条 HARD_CAP
   检查就成了空转：谁把硬上限加回 suffix 都不会被发现。

角色感知（persona-aware）
------------------------
这套断言**跟着当前加载的角色走**（config.yaml + data/character_selection.yaml）：
片段必须对得上**那个角色**的生产 prompt，而不是某个写死的角色。
角色换了，门禁自动换一份黄金集来对账。
另外每个**有黄金集**的角色还会单独对一遍 —— 小汐 / 大肥鱼 两份都得绿。

不需要 API key / 网络。记忆库 / 会话库 / 提醒库全在临时目录，
data/ 下的真库一个都不碰。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

SCRIPT_PATH = ROOT / "scripts" / "record_naturalness.py"

#: system prompt 里的句数硬上限（Stage 1.4 已删）
HARD_CAP = "回复 1~3 句"


def _load_script():
    """按文件路径加载 scripts/record_naturalness.py（scripts 不是 python 包）。"""
    spec = importlib.util.spec_from_file_location("record_naturalness", SCRIPT_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def script():
    return _load_script()


def _cfg_for_character(entry):
    """按角色条目拼一份配置（人设 / 角色文件都换成那个角色）。"""
    from app.core.config import load_config

    cfg = load_config(str(ROOT / "config.yaml"))
    cfg.character.name = entry.name
    cfg.character.persona = entry.persona
    cfg.character.character_file = entry.path
    return cfg


@pytest.fixture(scope="module")
def cfg_current():
    """配置 = **当前加载的角色**。

    load_config() 先看运行时选过的角色（data/character_selection.yaml），
    再回退 config.yaml 的 character_file —— 跟桌宠真跑的时候一个口径。
    门禁对着当前角色评，这里就对账当前角色。

    环境变量 NATURALNESS_CHARACTER 可以**手动**指定角色（验证用），
    跟 tests/test_naturalness.py 用的是同一个开关。
    """
    from app.core.config import load_config, load_character_entry

    cfg = load_config(str(ROOT / "config.yaml"))
    override = os.environ.get("NATURALNESS_CHARACTER", "").strip()
    if override:
        entry = load_character_entry(ROOT, override)
        assert entry.ok, (
            f"NATURALNESS_CHARACTER 指定的角色不可用：{override}（{entry.error}）")
        cfg.character.name = entry.name
        cfg.character.persona = entry.persona
        cfg.character.character_file = entry.path
    return cfg


@pytest.fixture(scope="module")
def production(script, cfg_current):
    """生产 system prompt（临时 root：不碰 data/ 下的真库）。"""
    tmp_root = script._temp_root("naturalness_parity_")
    try:
        return script.build_production_prompt(
            cfg_current, root=ROOT, tmp_root=tmp_root)
    finally:
        script._cleanup_temp_root(tmp_root)


@pytest.fixture(scope="module")
def cases(script, cfg_current):
    """**当前角色**的黄金集；还没有 -> 跳过（写明原因）。"""
    path = script.fixture_path_for(cfg_current.character.character_file)
    if not path.is_file():
        pytest.skip(
            f"角色「{cfg_current.character.name}」还没有自然度黄金集"
            f"（{path.name}）—— 跳过。"
            f"录制：python scripts/record_naturalness.py --record "
            f"--character {cfg_current.character.character_file}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("cases") or []
    return data


@pytest.fixture(scope="module")
def character_productions(script):
    """每个**有黄金集**的角色 -> (角色, 配置, 生产 prompt, 黄金集路径)。

    每个角色一个独立的临时 root —— 记忆库 / 会话库互不干扰。
    """
    from app.core.config import list_characters

    out = []
    for entry in list_characters(ROOT):
        if not entry.ok:
            continue
        path = script.fixture_path_for(entry.path)
        if not path.is_file():
            continue
        cfg = _cfg_for_character(entry)
        tmp_root = script._temp_root("naturalness_parity_one_")
        try:
            built = script.build_production_prompt(
                cfg, root=ROOT, tmp_root=tmp_root)
        finally:
            script._cleanup_temp_root(tmp_root)
        out.append((entry, cfg, built, path))
    return out


def test_recording_prompt_is_the_production_prompt(production):
    """录制用的 prompt 必须跟两个后端真正发出去的那条**逐字节**相同。"""
    system_prompt = production["system_prompt"]
    react_prompt = production["react_prompt"]
    standard_prompt = production["standard_prompt"]

    assert system_prompt.encode("utf-8") == react_prompt.encode("utf-8"), (
        "录制用的 prompt 跟 ReAct 后端发的不一致")
    assert system_prompt.encode("utf-8") == standard_prompt.encode("utf-8"), (
        "录制用的 prompt 跟 standard 后端发的不一致")
    assert system_prompt.strip(), "生产 prompt 是空的"


def test_production_prompt_has_no_hard_cap(production):
    """生产 prompt 里不能再有句数硬上限。"""
    assert HARD_CAP not in production["system_prompt"]
    assert "【回复长度】" in production["system_prompt"]


def test_fixture_fragments_are_verbatim_substrings(production, cases):
    """评测集里的每个片段都必须是生产 prompt 的**逐字子串**。"""
    system_prompt = production["system_prompt"]
    assert cases, "评测集是空的"
    for case in cases:
        fragments = case.get("system_prompt_fragments") or []
        assert fragments, f"{case.get('id')} 没有片段"
        for frag in fragments:
            assert frag in system_prompt, (
                f"{case.get('id')} 的片段不是生产 prompt 的逐字子串：{frag[:40]!r}")


def test_fixture_fragments_keep_the_chinese_suffix_and_length_strategy(
        production, cases):
    """片段必须含中文输出约束全文 + 长度策略（否则 HARD_CAP 检查是空转）。"""
    from app.brain.prompt_layer import CHINESE_SYSTEM_SUFFIX, LENGTH_STRATEGY

    for case in cases:
        fragments = case.get("system_prompt_fragments") or []
        assert CHINESE_SYSTEM_SUFFIX in fragments, (
            f"{case.get('id')} 的片段里没有中文输出约束全文")
        assert LENGTH_STRATEGY in fragments, (
            f"{case.get('id')} 的片段里没有长度策略")
        # 拼起来（评测集里的拼法）也不能再有硬上限
        assembled = "\n\n".join(fragments)
        assert HARD_CAP not in assembled, (
            f"{case.get('id')} 的片段里还残留硬上限")


def test_fixture_fragments_match_a_fresh_cut(production, cases, script,
                                             cfg_current):
    """片段 = 从生产 prompt 现切的三段（人设开头 / 中文约束 / 长度策略）。

    录制脚本写回的就是这三段 —— 这里钉住，防止有人手改片段。
    """
    expected = script._build_fragments(
        production["system_prompt"], cfg_current.character.persona)
    for case in cases:
        assert case.get("system_prompt_fragments") == expected, (
            f"{case.get('id')} 的片段跟现切的不一致")


def test_current_character_is_resolved(cfg_current):
    """跟的那个角色 = 桌宠现在真跑的角色（不是写死的名字）。"""
    assert cfg_current.character.name, "当前角色没有名字"
    assert cfg_current.character.persona.strip(), "当前角色没有人设"
    assert cfg_current.character.character_file, "当前角色没有角色文件"


def test_every_character_with_a_golden_set_matches_its_own_prompt(
        script, character_productions):
    """小汐 / 大肥鱼：每个角色的黄金集，都必须对得上**它自己**的生产 prompt。

    门禁只看当前角色；这条把**所有有黄金集的角色**都单独对一遍 ——
    角色换了、或者某个角色的黄金集录错了，都会被逮到。
    """
    assert character_productions, "一个有黄金集的角色都没有 —— 门禁形同虚设"
    for entry, cfg, built, path in character_productions:
        prompt = built["system_prompt"]
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("cases") or []
        assert data, f"{path.name} 是空的"
        for case in data:
            fragments = case.get("system_prompt_fragments") or []
            assert fragments, f"{entry.name} / {case.get('id')} 没有片段"
            for frag in fragments:
                assert frag in prompt, (
                    f"{entry.name} / {case.get('id')} 的片段不是它自己的生产 "
                    f"prompt 的逐字子串：{frag[:40]!r}")
            # 片段 = 从**它自己的**生产 prompt 现切的三段
            assert case.get("system_prompt_fragments") == script._build_fragments(
                prompt, cfg.character.persona), (
                f"{entry.name} / {case.get('id')} 的片段跟现切的不一致")
            # 人设片段里必须有角色自己的名字（拿错角色的黄金集一眼看出来）
            assert entry.name in (fragments[0] or ""), (
                f"{entry.name} 的黄金集人设片段里没有它自己：{fragments[0][:40]!r}")
