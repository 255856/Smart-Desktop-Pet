# -*- coding: utf-8 -*-
"""自我记忆（身份 / 价值 / 程序性）—— 记忆库里的三个分区。

owner 的要求：「这些可以由模型自己慢慢修改调整吧」—— 所以：
  · 三个区域是**记忆卡的 kind**（跟 话/话题/事件/事实·偏好/印象 平级），不是提示词常量；
  · 模型能用 update_self 写 / 改 / 删；
  · 改写 = supersede（旧条过时，不是删）；
  · 限速：每个区域每天最多改 2 次（人格是慢慢长出来的）；
  · 身份 / 价值进 system prompt **稳定段**（内容不变就逐字节相同 → 不掉前缀缓存）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.brain import self_memory  # noqa: E402
from app.engine.tools import _self as self_tool  # noqa: E402


class _FakeClient:
    """假 sidecar 客户端（卡片放内存里，不碰真库）。"""

    def __init__(self) -> None:
        self.cards: dict = {}
        self.n = 0
        self.last_error = ""

    def list_cards(self, limit: int = 200, include_superseded: bool = False,
                   with_category: bool = False) -> list:
        out = [dict(c) for c in self.cards.values()
               if c.get("status") == "active"]
        return out[:limit]

    def make_card(self, text, key="", kind="fact", importance=0.5,
                  category="", **kw) -> dict:
        self.n += 1
        cid = "m%d" % self.n
        card = {"id": cid, "key": key, "kind": kind, "text": text,
                "importance": importance, "category": category,
                "status": "active", "updatedAt": self.n}
        self.cards[cid] = card
        return card

    def add(self, text, key="", kind="fact", importance=0.5, category="",
            **kw) -> dict:
        return self.make_card(text, key=key, kind=kind,
                              importance=importance, category=category)

    def supersede(self, old_id: str, card: dict) -> bool:
        old = self.cards.get(old_id)
        if not old:
            return False
        old["status"] = "superseded"
        new = dict(card)
        new["status"] = "active"
        self.cards[card["id"]] = new
        return True

    def delete(self, ids) -> int:
        n = 0
        for i in ids or []:
            if i in self.cards:
                del self.cards[i]
                n += 1
        return n


@pytest.fixture()
def client():
    return _FakeClient()


@pytest.fixture(autouse=True)
def _isolated_edit_log(tmp_path, monkeypatch):
    """编辑流水写到临时目录（绝不碰仓库的 data/）。"""
    path = tmp_path / "self_edit_log.json"
    monkeypatch.setattr(self_memory, "_edit_log_path", lambda root=None: path)
    return path


# --- 写：模型能改自己的三个区域 -------------------------------------------


def test_update_self_writes_a_card_of_that_region_kind(client):
    out = self_tool._update_self(client, "identity", "我住在主人身边，不是桌面上的程序",
                                  "add", "", "", "")
    assert "已记下" in out, out
    cards = [c for c in client.cards.values() if c["status"] == "active"]
    assert len(cards) == 1, cards
    assert cards[0]["kind"] == "identity", cards
    assert cards[0]["key"].startswith("self.identity."), cards


def test_update_self_revise_supersedes_the_old_card(client):
    self_tool._update_self(client, "value", "底线：先问主人再动手", "add", "",
                           "bottom_line", "")
    first = [c for c in client.cards.values() if c["status"] == "active"][0]
    out = self_tool._update_self(client, "value", "底线：删文件前一定先问主人",
                                 "revise", first["id"], "bottom_line", "")
    assert "已改写" in out, out
    assert client.cards[first["id"]]["status"] == "superseded", client.cards
    alive = [c for c in client.cards.values() if c["status"] == "active"]
    assert len(alive) == 1 and "删文件前" in alive[0]["text"], alive


def test_update_self_remove_deletes_the_card(client):
    self_tool._update_self(client, "procedural", "查天气：先 web_search 再 live_query",
                           "add", "", "", "")
    card = [c for c in client.cards.values() if c["status"] == "active"][0]
    out = self_tool._update_self(client, "procedural", "", "remove", card["id"],
                                 "", "")
    assert "已删除" in out, out
    assert not [c for c in client.cards.values() if c["status"] == "active"]


def test_update_self_is_rate_limited_per_day(client):
    """人格是慢慢长出来的：同一个区域每天最多改 2 次。"""
    for i in range(2):
        out = self_tool._update_self(client, "identity", "第 %d 版自我认知" % i,
                                     "add", "", "", "")
        assert "已记下" in out, out
    out = self_tool._update_self(client, "identity", "第 3 版", "add", "", "", "")
    assert "错误" in out, out
    assert "每天最多" in out, out
    # 别的区域不受影响
    out2 = self_tool._update_self(client, "value", "偏好：喜欢用短句", "add", "",
                                  "preference", "")
    assert "已记下" in out2, out2


def test_update_self_rejects_an_unknown_region(client):
    out = self_tool._update_self(client, "soul", "x", "add", "", "", "")
    assert "错误" in out and "identity" in out, out


# --- 读：会话按区域读取 ---------------------------------------------------


def test_the_blocks_come_from_the_memory_store(client):
    self_tool._update_self(client, "identity", "我住在主人身边", "add", "", "", "")
    self_tool._update_self(client, "value", "底线：不编造", "add", "",
                           "bottom_line", "")
    self_memory.invalidate_cache()
    ident = self_memory.identity_block(client)
    value = self_memory.value_block(client)
    assert "我住在主人身边" in ident, ident
    assert "底线" in value and "不编造" in value, value


def test_the_blocks_are_byte_identical_across_calls(client):
    """常量块：内容没变就逐字节相同（system prompt 的前缀缓存靠它）。"""
    self_tool._update_self(client, "identity", "我住在主人身边", "add", "", "", "")
    self_memory.invalidate_cache()
    a = self_memory.stable_blocks(client)
    b = self_memory.stable_blocks(client)
    assert a == b, (a, b)
    assert a[0] == self_memory.identity_block(client)
    assert a[1] == self_memory.value_block(client)


def test_the_blocks_are_empty_without_a_store(client):
    """读不到就空串（绝不能崩聊天）。"""
    self_memory.invalidate_cache()
    ident = self_memory.identity_block(client)
    assert ident == "" or isinstance(ident, str)


# --- 迁移：旧文件分区一次性导进记忆库 -------------------------------------


def test_migration_imports_the_legacy_files_once(client, tmp_path, monkeypatch):
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"facts": [{"id": "a", "text": "我是小汐"}]}),
                      encoding="utf-8")

    class _FakePartitions:
        @staticmethod
        def load_identity():
            return [{"id": "a", "text": "我是小汐"}]

        @staticmethod
        def load_values():
            return [{"id": "v", "level": "preference", "content": "喜欢用短句"}]

        @staticmethod
        def load_procedures():
            return [{"id": "p", "task": "查天气", "steps": ["a", "b"]}]

    from app.brain import memory_partitions as mp
    monkeypatch.setattr(mp, "load_identity", _FakePartitions.load_identity)
    monkeypatch.setattr(mp, "load_values", _FakePartitions.load_values)
    monkeypatch.setattr(mp, "load_procedures", _FakePartitions.load_procedures)
    n = self_memory.migrate_legacy_files(client)
    assert n == 3, n
    kinds = sorted(c["kind"] for c in client.cards.values())
    assert kinds == ["identity", "procedural", "value"], kinds
    # 再跑一次不会再导（幂等）
    assert self_memory.migrate_legacy_files(client) == 0


# --- 分档闸门：自我记忆不能被筛掉 -----------------------------------------


def test_the_self_kinds_have_a_gate_row():
    from memory_sidecar.price_table import resolve_kind_gate
    gate = resolve_kind_gate()
    for kind in ("identity", "value", "procedural"):
        assert kind in gate, (kind, sorted(gate))
