"""v23: dashboard 加桌宠养成 tab + 3 API (state / config / trace).

覆盖:
  - create_app 能构造, /api/companion endpoint 注册
  - /api/companion 返回 200 + 5 字段 (state/config/decisions/outcomes/persist_path)
  - state 含 5 维 + quantize 4 维
  - config 含 yaml 关键字段 (range/leveling/time_decay/favor_rules/event_hooks)
  - HTML 含「桌宠养成」tab 字符串
  - 决策/数值变化 trace 各返 list (按 ts 倒序)
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.web.dashboard import create_app


@pytest.fixture
def client():
    app = create_app(trace_db="data/traces.db", memory_file="data/memory.json")
    from fastapi.testclient import TestClient
    return TestClient(app)


def test_create_app_has_companion_route():
    app = create_app(trace_db="data/traces.db", memory_file="data/memory.json")
    paths = [r.path for r in app.routes]
    assert "/api/companion" in paths, f"应注册 /api/companion. 实际: {paths}"


def test_html_contains_companion_tab():
    app = create_app(trace_db="data/traces.db", memory_file="data/memory.json")
    from fastapi.testclient import TestClient
    r = TestClient(app).get("/")
    assert r.status_code == 200
    assert "桌宠养成" in r.text
    assert "/api/companion" in r.text
    assert "renderCompanion" in r.text
    assert "csQuantize" in r.text
    assert "csDecisions" in r.text
    assert "csOutcomes" in r.text


def test_api_companion_returns_5_fields(client):
    """v23 API 应返 5 顶层字段."""
    r = client.get("/api/companion")
    assert r.status_code == 200
    data = r.json()
    for k in ("state", "config", "decisions", "outcomes", "persist_path"):
        assert k in data, f"应返 {k}. 实际 keys: {list(data.keys())}"


def test_api_companion_state_has_5_dims_and_quantize(client):
    """state 含 5 维数值 + 4 维量化."""
    r = client.get("/api/companion")
    data = r.json()
    s = data["state"]
    for k in ("strength_food", "feeling", "level", "exp", "likability"):
        assert k in s, f"state 应含 {k}. 实际: {list(s.keys())}"
    q = s["quantize"]
    for k in ("satiety", "mood", "level", "favor"):
        assert k in q, f"quantize 应含 {k}. 实际: {list(q.keys())}"
        assert q[k] in ("LOW", "MID", "HIGH"), f"{k} 应是 LOW/MID/HIGH. 实际: {q[k]}"


def test_api_companion_config_has_yaml_fields(client):
    """config 暴露 yaml 关键字段 (主人能在 dashboard 看)."""
    r = client.get("/api/companion")
    data = r.json()
    c = data["config"]
    expected_keys = {
        "range_satiety", "range_mood", "range_favor",
        "level_max", "exp_base", "exp_pow", "exp_to_next_lv2",
        "satiety_per_hour", "mood_no_interact_48h", "check_interval_seconds",
        "favor_high", "favor_low", "event_hooks",
    }
    assert expected_keys.issubset(set(c.keys())), (
        f"config 应含 {expected_keys - set(c.keys())}"
    )
    # 6 条 event_hooks
    eh = c["event_hooks"]
    for k in ("task_success", "task_failure", "user_rejected",
              "user_accepted", "user_feed", "user_interact"):
        assert k in eh, f"event_hooks 应含 {k}"
        for dim in ("mood", "favor", "exp", "satiety"):
            assert dim in eh[k], f"{k} 应含 {dim}"


def test_api_companion_decisions_outcomes_sorted_by_ts(client):
    """decisions / outcomes 按 ts 倒序 (新→旧)."""
    r = client.get("/api/companion")
    data = r.json()
    decs = data["decisions"]
    outs = data["outcomes"]
    # 至少有一条 (主程序应该写过 trace_decision 或 trace_outcome)
    if len(decs) >= 2:
        for i in range(1, len(decs)):
            assert decs[i-1]["ts"] >= decs[i]["ts"], (
                f"decisions 应按 ts 倒序. [{i-1}].ts={decs[i-1]['ts']} > [{i}].ts={decs[i]['ts']}"
            )
    if len(outs) >= 2:
        for i in range(1, len(outs)):
            assert outs[i-1]["ts"] >= outs[i]["ts"], (
                f"outcomes 应按 ts 倒序. [{i-1}].ts={outs[i-1]['ts']} > [{i}].ts={outs[i]['ts']}"
            )


def test_api_companion_persist_path_points_to_yaml_default(client):
    """persist_path 应指向 yaml persistence.file 配置的路径."""
    r = client.get("/api/companion")
    data = r.json()
    from app.core.companion_config import companion_config
    expected = companion_config.persistence.file
    # 相对路径展开
    if not Path(expected).is_absolute():
        expected = str(Path.cwd() / expected)
    assert data["persist_path"] == expected, (
        f"persist_path 应等于 yaml persistence.file. 实际: {data['persist_path']} vs 期望: {expected}"
    )
