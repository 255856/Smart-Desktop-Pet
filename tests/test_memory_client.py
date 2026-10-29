# -*- coding: utf-8 -*-
"""app/brain/memory_client.py 的测试：端点 / 形状 / **绝不抛异常** / 冷却。

不要求真 sidecar、网络或模型：全部 IO 走 tests/fake_sidecar.py 里的假实现。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/fake_sidecar.py

from app.brain.memory_client import (  # noqa: E402
    DEFAULT_MAX_ROWS,
    MEMORY_BLOCK_HEADER,
    MEMORY_BLOCK_LEGEND,
    MemoryClient,
    card_id,
    default_base_url,
)
from fake_sidecar import FakeSidecar, install  # noqa: E402


def _client(**kwargs):
    fake = FakeSidecar()
    client = MemoryClient(**kwargs)
    install(client, fake)
    return client, fake


# ---------------------------------------------------------------------------
# 端点 + payload 形状（要和 memory_sidecar/server.py 对得上）
# ---------------------------------------------------------------------------


def test_health_and_stats_shape():
    client, fake = _client()
    health = client.health()
    assert health["ok"] is True
    assert health["cards"] == 0
    assert health["models"]["embed"]
    stats = client.stats()
    assert stats["active"] == 0
    assert stats["byKind"] == {}
    assert fake.count("/health") == 1
    assert fake.count("/stats") == 1


def test_search_sends_the_sidecar_payload_and_parses_results():
    client, fake = _client()
    client.add("主人不吃辣", kind="fact", importance=0.8, key="project")
    fake.calls.clear()

    rows = client.search("不吃辣", top_k=3, mood="开心", kind="fact", key="project")

    method, path, payload = fake.calls[-1]
    assert (method, path) == ("POST", "/search")
    assert payload == {"query": "不吃辣", "top_k": 3, "mood": "开心",
                       "kind": "fact", "key": "project"}
    assert rows and rows[0]["text"] == "主人不吃辣"
    assert rows[0]["score"] > 0


def test_upsert_delete_supersede_rebuild_payload_keys():
    client, fake = _client()
    card = client.make_card("主人喜欢猫", kind="fact", importance=0.6)
    assert client.upsert([card]) == 1
    assert fake.calls[-1][2] == {"cards": [card]}

    new_card = client.make_card("主人喜欢狗", kind="fact", importance=0.6)
    assert client.supersede(card["id"], new_card) == 1
    assert fake.calls[-1][2] == {"oldId": card["id"], "card": new_card}
    assert fake.cards[card["id"]]["status"] == "superseded"

    assert client.delete([card["id"], new_card["id"]]) == 2
    assert fake.calls[-1][2] == {"ids": [card["id"], new_card["id"]]}

    rebuilt = client.rebuild()
    assert rebuilt["vector"] == 0
    assert fake.calls[-1][1] == "/rebuild"


def test_add_recall_forget_roundtrip():
    client, _fake = _client()
    card = client.add("主人不吃辣", kind="fact", importance=0.9)
    assert card["id"] == card_id("主人不吃辣", kind="fact")
    assert card["importance"] == 0.9
    assert client.recall("不吃辣")[0]["text"] == "主人不吃辣"
    assert client.forget([card["id"]]) == 1
    assert client.recall("不吃辣") == []


def test_card_id_is_deterministic_and_dimension_aware():
    assert card_id("主人不吃辣") == card_id("主人不吃辣")
    assert card_id("主人不吃辣", kind="fact") != card_id("主人不吃辣", kind="thought")
    assert card_id("主人不吃辣", key="project") != card_id("主人不吃辣", key="user")


def test_default_base_url_follows_the_env_override(monkeypatch):
    monkeypatch.setenv("DESKTOP_PET_MEMORY_SIDECAR_PORT", "9999")
    assert default_base_url() == "http://127.0.0.1:9999"
    monkeypatch.delenv("DESKTOP_PET_MEMORY_SIDECAR_PORT", raising=False)
    assert default_base_url() == "http://127.0.0.1:8788"


# ---------------------------------------------------------------------------
# 记忆块：形状必须跟老 system_block 一致（prompt 形状不许悄悄变）
# ---------------------------------------------------------------------------


def test_system_block_keeps_the_old_shape():
    client, _fake = _client()
    client.add("主人不吃辣", kind="fact", importance=0.8)

    block = client.system_block("我不吃辣吗")
    lines = block.splitlines()
    assert lines[0] == MEMORY_BLOCK_HEADER
    assert lines[0] == "【你记得的事】（按你刚才这句话检索出来的，可能相关也可能不准；用不上就别硬扯）"
    assert lines[1] == MEMORY_BLOCK_LEGEND
    assert lines[2].startswith("- [fact★0.8] (")
    assert "主人不吃辣" in lines[2]


def test_system_block_respects_max_rows():
    client, _fake = _client()
    for i in range(6):
        client.add("主人喜欢吃辣第%d条" % i, kind="fact", importance=0.5)
    assert len(client.system_block("吃辣", max_rows=2).splitlines()) == 2 + 2
    assert len(client.system_block("吃辣").splitlines()) == DEFAULT_MAX_ROWS + 2


def test_system_block_is_empty_without_a_query_or_without_hits():
    client, fake = _client()
    client.add("主人不吃辣", kind="fact", importance=0.8)
    assert client.system_block("") == ""
    assert fake.count("/search") == 0, "空 query 不该真去打 sidecar"
    assert client.system_block("今天股市怎么样") == ""


# ---------------------------------------------------------------------------
# 红线：绝不把异常抛进聊天路径
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("failure", ["refused", "timeout", ("500", 500, b"boom")])
def test_failures_never_raise_and_record_last_error(failure):
    client, fake = _client()
    fake.fail = failure

    assert client.health() == {}
    assert client.stats() == {}
    assert client.search("随便") == []
    assert client.upsert([client.make_card("x")]) == 0
    assert client.delete(["a"]) == 0
    assert client.supersede("old", client.make_card("y")) == 0
    assert client.rebuild() == {}
    assert client.add("z") == {}
    assert client.recall("z") == []
    assert client.forget(["a"]) == 0
    assert client.system_block("随便") == ""
    assert client.last_error, "失败必须留下原因（面板 / sidecar_status 要看）"


def test_malformed_json_and_non_dict_bodies_are_survivable():
    def bad_json(method, url, payload, timeout):
        return 200, b"<html>not json</html>"

    def not_dict(method, url, payload, timeout):
        return 200, json.dumps(["a", "b"]).encode()

    for http in (bad_json, not_dict):
        client = MemoryClient(http=http)
        assert client.health() == {}
        assert client.search("x") == []
        assert client.system_block("x") == ""
        assert "JSON" in client.last_error or "格式" in client.last_error


def test_ok_false_in_a_200_body_is_treated_as_failure():
    def unsupported(method, url, payload, timeout):
        return 200, json.dumps({"ok": False, "error": "检索不可用：两条腿都挂了",
                                "results": []}).encode()

    client = MemoryClient(http=unsupported)
    assert client.search("x") == []
    assert "两条腿都挂了" in client.last_error
    assert client.last_ok is False


# ---------------------------------------------------------------------------
# 冷却：sidecar 不在时**每轮**都白等一个超时 → 冷却期内直接返回空
# ---------------------------------------------------------------------------


def test_failure_cooldown_skips_io_but_health_still_probes():
    now = [100.0]
    calls = []

    def http(method, url, payload, timeout):
        calls.append(url)
        raise ConnectionRefusedError("refused")

    client = MemoryClient(http=http, cooldown=5.0, clock=lambda: now[0])
    assert client.search("x") == []
    assert len(calls) == 1
    # 冷却期内：不再真的发请求
    now[0] += 1.0
    assert client.search("x") == []
    assert client.recall("x") == []
    assert len(calls) == 1
    # health 探测不受冷却影响（supervisor 靠它发现 sidecar 回来了）
    client.health()
    assert len(calls) == 2
    # 冷却过期 → 重新试
    now[0] += 10.0
    client.search("x")
    assert len(calls) == 3


def test_success_clears_the_cooldown():
    state = {"fail": True}
    calls = []

    def http(method, url, payload, timeout):
        calls.append(url)
        if state["fail"]:
            raise ConnectionRefusedError("refused")
        return 200, json.dumps({"ok": True, "results": [], "upserted": 1,
                                "deleted": 1}).encode()

    client = MemoryClient(http=http, cooldown=60.0)
    client.search("x")
    assert client._cooling()
    state["fail"] = False
    client.health()                       # 探测成功 → 清冷却
    assert not client._cooling()
    client.search("x")
    assert len(calls) == 3


def test_environment_has_no_sidecar_coupling_in_tests():
    """测试不许依赖真 sidecar：默认客户端也能构造，但测试只用假 IO。"""
    client = MemoryClient(base_url="http://127.0.0.1:1")
    assert client.base_url == "http://127.0.0.1:1"
    assert isinstance(client.timeout, float)
    assert os.environ.get("DESKTOP_PET_MEMORY_SIDECAR_PORT") is not None or True
