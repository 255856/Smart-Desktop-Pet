"""dashboard 把桌宠养成 trace 反馈嵌到会话详情顶部（不是独立 tab）。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.web.dashboard import create_app


@pytest.fixture
def client(tmp_path):
    # ⚠️ trace_db / memory_file 必须指向**临时目录**：
    #   原来写死 "data/traces.db"（相对 CWD = 仓库根）→ 面板只读打开它时
    #   会把仓库 data/traces.db-shm 摸一遍（用户数据目录被测试改动）。
    app = create_app(trace_db=str(tmp_path / "traces.db"),
                     memory_file=str(tmp_path / "memory.json"))
    from fastapi.testclient import TestClient
    return TestClient(app)


def test_create_app_has_companion_route(tmp_path):
    app = create_app(trace_db=str(tmp_path / "traces.db"),
                     memory_file=str(tmp_path / "memory.json"))
    paths = [r.path for r in app.routes]
    assert "/api/companion" in paths, f"应注册 /api/companion. 实际: {paths}"


def test_html_embeds_companion_in_run_detail_not_tab(tmp_path):
    """删独立 tab, 把桌宠养成反馈嵌到会话详情顶部."""
    app = create_app(trace_db=str(tmp_path / "traces.db"),
                     memory_file=str(tmp_path / "memory.json"))
    from fastapi.testclient import TestClient
    r = TestClient(app).get("/")
    assert r.status_code == 200
    # 独立 tab 删了
    assert "view-companion" not in r.text, "桌宠养成 tab 应删除 (v23-9 改嵌到详情)"
    assert "data-view=\"companion\"" not in r.text, "桌宠养成 tab 选项应删除"
    # 桌宠养成反馈段在 renderDetail 里
    assert "桌宠养成反馈" in r.text, "应在 renderDetail 里出现 '桌宠养成反馈' 段"
    assert "renderCompanionForRun" in r.text, "客户端函数 renderCompanionForRun 应存在"
    assert "loadCompanionCache" in r.text, "客户端函数 loadCompanionCache 应存在"
    # API 仍存在
    assert "/api/companion" in r.text


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
    eh = c["event_hooks"]
    for k in ("task_success", "task_failure", "user_rejected",
              "user_accepted", "user_feed", "user_interact"):
        assert k in eh, f"event_hooks 应含 {k}"
        for dim in ("mood", "favor", "exp", "satiety"):
            assert dim in eh[k], f"{k} 应含 {dim}"


def test_api_companion_supports_limit_params(client):
    """/api/companion?decision_limit=200&outcome_limit=200 支持分页拉更多数据."""
    r = client.get("/api/companion?decision_limit=10&outcome_limit=10")
    data = r.json()
    assert len(data["decisions"]) <= 10
    assert len(data["outcomes"]) <= 10


def test_api_companion_decisions_outcomes_sorted_by_ts(client):
    """decisions / outcomes 按 ts 倒序 (新→旧)."""
    r = client.get("/api/companion")
    data = r.json()
    decs = data["decisions"]
    outs = data["outcomes"]
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
    if not Path(expected).is_absolute():
        expected = str(Path.cwd() / expected)
    assert data["persist_path"] == expected, (
        f"persist_path 应等于 yaml persistence.file. 实际: {data['persist_path']} vs 期望: {expected}"
    )
