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
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "naturalness_cases.json"

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


@pytest.fixture(scope="module")
def production(script):
    """生产 system prompt（临时 root：不碰 data/ 下的真库）。"""
    from app.core.config import load_config

    cfg = load_config(str(ROOT / "config.yaml"))
    with tempfile.TemporaryDirectory(prefix="naturalness_parity_") as tmp:
        built = script.build_production_prompt(
            cfg, root=ROOT, tmp_root=Path(tmp))
        return built


@pytest.fixture(scope="module")
def cases():
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("cases") or []
    return data


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


def test_fixture_fragments_match_a_fresh_cut(production, cases, script):
    """片段 = 从生产 prompt 现切的三段（人设开头 / 中文约束 / 长度策略）。

    录制脚本写回的就是这三段 —— 这里钉住，防止有人手改片段。
    """
    from app.core.config import load_config

    cfg = load_config(str(ROOT / "config.yaml"))
    expected = script._build_fragments(
        production["system_prompt"], cfg.character.persona)
    for case in cases:
        assert case.get("system_prompt_fragments") == expected, (
            f"{case.get('id')} 的片段跟现切的不一致")
