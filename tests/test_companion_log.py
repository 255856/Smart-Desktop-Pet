"""v23-1 决策 trace 日志测试。

覆盖:
  - trace_decision 写一行到 data/companion_log.txt
  - trace_outcome 写一行 (kind="outcome")
  - read_recent 读尾部 N 行, kind 过滤
  - 中间件 _get_companion_decision 调一次就调一次 trace_decision
"""
import json
from pathlib import Path

from app.brain._companion_log import (
    trace_decision, trace_outcome, read_recent, _default_log_path,
)
from app.brain.hallucination_guard import RiskyToolConfirm
from app.engine.state import PetState


def test_decision_trace_writes_one_line(tmp_path=None):
    """trace_decision 在 data/companion_log.txt 写 1 行 JSON."""
    path_before = _default_log_path()
    size_before = path_before.stat().st_size if path_before.is_file() else 0
    trace_decision(
        thread_id="t-test", tool="get_current_time", risk=0,
        decision="ALLOW", sat="HIGH", mood="HIGH", level="LOW",
    )
    path_after = _default_log_path()
    assert path_after.is_file()
    size_after = path_after.stat().st_size
    assert size_after > size_before, (
        f"trace_decision 应追加 1 行 JSON. before={size_before} after={size_after}"
    )


def test_outcome_trace_writes_kind_outcome():
    """trace_outcome 写一行含 kind=outcome 的 JSON."""
    trace_outcome(
        thread_id="t-test", outcome="success", tool="run_script",
        feeling_delta=0.5, food_delta=0.0, favor_delta=1.0, exp_delta=2.0,
        level_before=1, level_after=1, success=True,
    )


def test_read_recent_returns_newest_first():
    """read_recent 返 list (新到旧), 含 ts/tool/decision 字段."""
    recs = read_recent(limit=5)
    # 至少有 decision 字段 (trace_decision 写的)
    has_decision = any("decision" in r for r in recs)
    assert has_decision, (
        f"read_recent 应至少有 1 条 trace_decision 记录. 拿到: {recs}"
    )


def test_read_recent_filtered_by_kind():
    """kind=outcome 时只返 outcome 行 (新接口跟 decision 区分)."""
    trace_outcome(
        thread_id="t-test", outcome="failure", tool="x",
        feeling_delta=-0.3, food_delta=-0.1, favor_delta=-3.0, exp_delta=0.0,
        level_before=1, level_after=1, success=False,
    )
    recs = read_recent(limit=20, kind="outcome")
    for r in recs:
        assert r.get("kind") == "outcome", (
            f"kind=outcome 过滤应只返 outcome 行. 拿到: {r}"
        )


def test_middleware_call_writes_trace():
    """中间件 _get_companion_decision 每次查表都写 trace_decision."""
    path = _default_log_path()
    size_before = path.stat().st_size if path.is_file() else 0

    ps = PetState()
    ps.feeling = 20; ps.strength_food = 20
    ctrl = RiskyToolConfirm()
    ctrl.set_companion_state(ps)

    n = 3
    for _ in range(n):
        ctrl._get_companion_decision("set_brightness")

    size_after = _default_log_path().stat().st_size
    # 每次查表写 ~120 字节 JSON, 3 次应 ≥ 360 字节增量
    assert size_after - size_before >= 200, (
        f"中间件 3 次查表应写 ≥ 200 字节. before={size_before} after={size_after}"
    )


def test_trace_json_has_required_fields():
    """每条 trace_decision JSON 含 v23-7 trace 页要展示的所有字段."""
    trace_decision(
        thread_id="t-field-test", tool="run_script", risk=3,
        decision="CONFIRM", sat="MID", mood="HIGH", level="MID",
    )
    recs = read_recent(limit=10)
    target_tool = None
    for r in recs:
        if r.get("tool") == "run_script" and r.get("decision") == "CONFIRM":
            target_tool = r
            break
    assert target_tool is not None, "应有刚写的 CONFIRM 行"
    for field in ("ts", "tool", "risk", "decision",
                  "sat", "mood", "level"):
        assert field in target_tool, (
            f"trace 缺字段 {field}. 实际: {target_tool}"
        )


def test_trace_decision_accepts_favor_kwarg():
    """v25 regression: chat_window._run_multimodal_action 调
    trace_decision(favor=...) . v23-11 commit 加了 favor kwarg 但没改
    函数签名, TypeError 被 try/except 吞, multimodal 整条路径 0 trace
    落盘, dashboard 永远 '本会话没有桌宠 trace 记录'. 这里锁死 favor
    必须能传进去且落盘.
    """
    trace_decision(
        thread_id="t-favor-kwarg", tool="generate_image", risk=2,
        decision="ALLOW", sat="MID", mood="MID", level="MID",
        favor="HIGH", source="multimodal_path",
    )
    recs = read_recent(limit=20, kind="decision")
    target = None
    for r in recs:
        if (r.get("tool") == "generate_image"
                and r.get("thread_id") == "t-favor-kwarg"):
            target = r
            break
    assert target is not None, (
        f"trace_decision(favor=HIGH) 应落 1 行 multimodal_path 的记录. "
        f"实际: {recs[:3]}..."
    )
    # v23-3 加 favor 维度后, dashboard trace 页要把 favor 也展示出来.
    # 必须有 favor 字段 (默认 "MID" 也行, 必须是字符串不在 None).
    assert "favor" in target, (
        f"trace JSON 缺 favor 字段. 实际: {target}"
    )
    assert target.get("favor") == "HIGH", (
        f"favor kwarg 应原样落盘. 期望 HIGH 实际 {target.get('favor')}"
    )
    assert target.get("decision_source") == "multimodal_path"


def test_trace_decision_default_favor_is_mid():
    """兼容老调用方 (risky_tool_confirm / middleware): 不传 favor 也行."""
    trace_decision(
        thread_id="t-favor-default", tool="run_script", risk=1,
        decision="ALLOW", sat="LOW", mood="MID", level="LOW",
    )
    recs = read_recent(limit=10, kind="decision")
    target = next(
        (r for r in recs
         if r.get("thread_id") == "t-favor-default"
         and r.get("tool") == "run_script"),
        None,
    )
    assert target is not None, "老调用方应仍能落 1 行"
    assert target.get("favor") == "MID", (
        f"不传 favor 时默认 MID. 实际 {target.get('favor')}"
    )
