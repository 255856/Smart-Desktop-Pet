# -*- coding: utf-8 -*-
"""记忆三件套 + 每轮记忆块：全部走 **memory_sidecar 客户端**（A2 / A3）。

不要求真 sidecar：HTTP 层换成 tests/fake_sidecar.py 的假实现。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/fake_sidecar.py

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.brain.memory_client import MEMORY_BLOCK_HEADER, MemoryClient  # noqa: E402
from app.engine.tools import ToolRegistry  # noqa: E402
from app.engine.tools._memory import register as register_memory_tools  # noqa: E402
from fake_sidecar import FakeSidecar, install  # noqa: E402


def _tools(client):
    reg = ToolRegistry()
    register_memory_tools(reg, memory=client)
    return reg


def _fake_client():
    client = MemoryClient()
    fake = install(client)
    return client, fake, _tools(client)


def _broken_client():
    client = MemoryClient(cooldown=0.0)
    install(client).fail = "refused"
    return client, _tools(client)


# ---------------------------------------------------------------------------
# A1（进程内那一半）：记住 → 召回 → 忘记，全部经过客户端
# ---------------------------------------------------------------------------


def test_remember_recall_forget_go_through_the_client():
    client, fake, reg = _fake_client()

    out = reg.get("remember_fact").fn(content="主人不吃辣", category="preference",
                                      importance=0.8)
    assert out.startswith("已记住（id=mem_"), out
    assert "impression★0.8" in out, out          # category=preference → kind=impression
    assert fake.count("/upsert") == 1, "必须真的写到 sidecar（不是进程内的老库）"

    out = reg.get("recall_memory").fn(keyword="不吃辣")
    assert "找到 1 条相关记忆" in out, out
    assert "[mem_" in out and "主人不吃辣" in out, out

    card_id = list(fake.cards)[0]
    out = reg.get("forget_memory").fn(memory_id=card_id)
    assert out == "已删除 1 条记忆（id=%s）。" % card_id, out
    assert fake.cards == {}

    out = reg.get("forget_memory").fn(memory_id=card_id)
    assert out.startswith("错误：没有 id 为"), out


def test_remember_is_idempotent_for_the_same_fact():
    _client, fake, reg = _fake_client()
    remember = reg.get("remember_fact").fn
    first = remember(content="主人不吃辣", category="fact", importance=0.8)
    second = remember(content="主人不吃辣", category="fact", importance=0.8)
    assert first.split("]")[0] == second.split("]")[0], "同一条事实必须落到同一张卡"
    assert len(fake.cards) == 1, "内容派生的 id → 不会留下重复行"


def test_remember_with_supersedes_rewrites_the_old_card():
    _client, fake, reg = _fake_client()
    old = reg.get("remember_fact").fn(content="主人以前不吃辣", category="fact",
                                      importance=0.7)
    old_id = [c for c in fake.cards][0]
    out = reg.get("remember_fact").fn(content="主人现在能吃辣了", category="fact",
                                      importance=0.7, supersedes=old_id)
    assert out.startswith("已改写（旧的 id=%s" % old_id), out
    assert fake.cards[old_id]["status"] == "superseded"


def test_forget_by_keyword_keeps_substring_semantics():
    _client, fake, reg = _fake_client()
    remember = reg.get("remember_fact").fn
    remember(content="主人不吃辣", category="fact")
    remember(content="主人喜欢猫", category="fact")
    out = reg.get("forget_memory").fn(keyword="辣")
    assert out == "已删除 1 条匹配「辣」的记忆。", out
    assert len(fake.cards) == 1
    out = reg.get("forget_memory").fn(keyword="辣")
    assert out.startswith("错误：没有匹配「辣」的记忆"), out


def test_recall_without_keyword_reports_a_summary_instead_of_lying():
    _client, _fake, reg = _fake_client()
    reg.get("remember_fact").fn(content="主人不吃辣", category="fact")
    out = reg.get("recall_memory").fn(keyword="")
    assert "记忆库里现在有 1 条" in out, out
    assert "只支持按语句检索" in out, out


# ---------------------------------------------------------------------------
# A3：sidecar 挂了 —— 工具给一句人话，绝不抛异常
# ---------------------------------------------------------------------------


def test_tools_degrade_to_a_clear_message_when_the_sidecar_is_down():
    _client, reg = _broken_client()

    out = reg.get("remember_fact").fn(content="主人不吃辣")
    assert out.startswith("错误：记忆服务不可用"), out
    assert "请" in out and "已记住" in out, "要提醒模型别谎报已记住"

    out = reg.get("recall_memory").fn(keyword="吃什么")
    assert out.startswith("错误：记忆服务不可用"), out

    out = reg.get("recall_memory").fn(keyword="")
    assert out.startswith("错误：记忆服务不可用"), out

    out = reg.get("forget_memory").fn(memory_id="mem_x")
    assert out.startswith("错误：记忆服务不可用"), out

    out = reg.get("forget_memory").fn(keyword="辣")
    assert out.startswith("错误：记忆服务不可用"), out


# ---------------------------------------------------------------------------
# A2：记忆块来自 sidecar，且只进**易变**通道
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def brain(tmp_path_factory):
    """真 BrainController（临时 root；sidecar 用假 IO，不碰 data/）。"""
    from app.core.qt_compat import QApplication
    QApplication.instance() or QApplication(sys.argv[:1])

    from app.brain.brain_controller import BrainController
    from app.core.config import load_config
    from app.engine.state_manager import StateManager

    tmp = tmp_path_factory.mktemp("brain_sidecar")
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


def test_brain_never_spawns_a_sidecar_inside_the_test_suite(brain):
    """测试进程里不许真的拉起子进程（A7）。"""
    assert brain.sidecar.process is None
    assert brain.sidecar._thread is None
    assert brain.sidecar_status()["running"] is False


def test_memory_block_comes_from_the_sidecar_and_stays_in_the_volatile_channel(brain):
    fake = install(brain.memory_client)          # 换成假 IO：一个进程内的 sidecar

    remember = brain.tool_registry.get("remember_fact").fn
    out = remember(content="主人不吃辣", category="preference", importance=0.8)
    assert out.startswith("已记住"), out
    assert fake.cards, "工具必须真的写进 sidecar"

    stable = brain.chat_context()
    volatile = brain.chat_context_dynamic("我到底能不能吃辣")

    assert "主人不吃辣" not in stable, "记忆块不许进 system prompt（前缀缓存不变量）"
    assert MEMORY_BLOCK_HEADER in volatile, "记忆块必须来自 sidecar 并进易变通道"
    assert "主人不吃辣" in volatile, volatile

    # 换一句不相关的话 → 记忆块不该硬塞（相关性裁剪仍在）
    assert "主人不吃辣" not in brain.chat_context_dynamic("今天天气怎么样")


def test_stable_context_stays_byte_identical_while_memory_changes(brain):
    """写记忆不许改动 system 串（只有易变通道会变）。"""
    install(brain.memory_client)
    sp1 = brain.chat_context()
    brain.tool_registry.get("remember_fact").fn(content="主人的猫叫团子",
                                                category="fact", importance=0.9)
    sp2 = brain.chat_context()
    assert sp1.encode("utf-8") == sp2.encode("utf-8")


def test_chat_path_is_unaffected_when_the_sidecar_is_down(brain):
    """A3：sidecar 不在 —— 聊天照常（记忆块空、工具给人话、不抛异常）。"""
    brain.memory_client._http = _raise_refused

    stable = brain.chat_context()
    volatile = brain.chat_context_dynamic("随便聊聊")
    assert "【当前时间:" in volatile or "【你的实时状态】" in volatile
    assert MEMORY_BLOCK_HEADER not in volatile, "sidecar 不在 → 这轮就是没有记忆"
    assert MEMORY_BLOCK_HEADER not in stable, "出错提示不许漏进 system prompt"

    out = brain.tool_registry.get("remember_fact").fn(content="主人不吃辣")
    assert out.startswith("错误：记忆服务不可用"), out

    status = brain.sidecar_status()
    assert status["reachable"] is False
    assert status["error"], "面板要能看到「为什么记忆没了」"

    # 恢复：清掉冷却后同一套接口立刻又能用（不是一次失败就永久降级）
    install(brain.memory_client)
    out = brain.tool_registry.get("remember_fact").fn(content="主人不吃辣")
    assert out.startswith("已记住"), out


def _raise_refused(method, url, payload, timeout):
    raise ConnectionRefusedError("sidecar 没在跑")
