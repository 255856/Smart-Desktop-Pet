"""v22-5 实验 1 跑数脚本测试。

覆盖:
  - run_experiment 返回字典 schema 正确
  - 决策差异率 ≥ 30% (论文 §4.4.1 主指标)
  - 规则命中率 ≥ 85% (lookup_decision 不跟 RULES_TABLE 优先级漂移)
  - 实验 1 写盘 results/state_decision_consistency.json
"""
import json
import os
import re
from pathlib import Path

from app.experiments.state_decision_consistency import (
    run_experiment, _build_scenarios, _STATE_GRID, main as exp_main,
)


def test_build_scenarios_count():
    scs = _build_scenarios(n=50, seed=42)
    assert len(scs) == 50
    for sc in scs:
        assert {"id", "intent", "tool_name"} <= set(sc.keys())
    print("[OK] _build_scenarios: n=50, schema 正确")


def test_state_grid_27_states():
    """27 个 (sat, mood, level) 组合 — 每档 3 选."""
    assert len(_STATE_GRID) == 27, (
        f"应有 27 状态组合 (3^3). 实际: {len(_STATE_GRID)}"
    )
    sats = {s for s, _, _ in _STATE_GRID}
    moods = {m for _, m, _ in _STATE_GRID}
    levels = {l for _, _, l in _STATE_GRID}
    assert len(sats) == 3, f"sat 应 3 档. 实际: {sats}"
    assert len(moods) == 3, f"mood 应 3 档. 实际: {moods}"
    assert len(levels) == 3, f"level 应 3 档. 实际: {levels}"
    print("[OK] _STATE_GRID: 27 组合 3×3×3")


def test_decision_diff_rate_target_30_percent():
    """论文 §4.4.1 主指标: 不同 state 下决策差异率 ≥ 30%."""
    summary = run_experiment(n=100, seed=42)
    rate = summary["decision_diff_rate"]
    assert rate >= 0.30, (
        f"决策差异率应 ≥ 30%. 实际: {rate:.2%}. "
        f"如果 < 30% 说明规则表对状态变化不敏感。"
    )
    print(f"[OK] 决策差异率 = {rate:.2%} (≥ 30%)")


def test_rule_hit_rate_target_85_percent():
    """lookup_decision 输出跟 RULES_TABLE 优先级一致 (100%) 表明查表无 bug.

    85% 是论文给的最低门槛, 实验脚本理论应 100% (优先级一致就一样).
    """
    summary = run_experiment(n=100, seed=42)
    rate = summary["rule_hit_rate"]
    assert rate >= 0.85, (
        f"规则命中率应 ≥ 85%. 实际: {rate:.2%}. "
        f"< 85% 说明 RULES_TABLE / lookup_decision 不一致"
    )
    print(f"[OK] 规则命中率 = {rate:.2%} (≥ 85%)")


def test_run_experiment_summary_schema():
    """返回值 schema 含必要字段 (论文 KPI 报告输入)."""
    summary = run_experiment(n=10, seed=1)
    for key in ("n_scenarios", "n_states_per_scenario",
                "scenarios_with_state_diff", "decision_diff_rate",
                "rule_hit_rate", "decision_diff_rate_target",
                "rule_hit_rate_target", "rows"):
        assert key in summary, f"summary 缺字段 {key}"
    assert isinstance(summary["rows"], list)
    assert len(summary["rows"]) == summary["n_scenarios"]
    print(f"[OK] summary schema: {list(summary.keys())}")


def test_main_writes_results_file(tmp_path=None):
    """main() 调一次能写盘 results/state_decision_consistency.json."""
    # 不真改 cwd: 实际脚本写 ../results 相对路径. 我们 import 后调 main 检查文件存在
    try:
        exp_main()
    except SystemExit:
        pass
    results = Path(__file__).parent.parent / "app" / "experiments" / "results" / "state_decision_consistency.json"
    assert results.is_file(), f"结果文件应存在: {results}"
    data = json.loads(results.read_text(encoding="utf-8"))
    # sanity
    assert "decision_diff_rate" in data
    assert "rows" in data
    assert data["n_scenarios"] >= 100
    print(f"[OK] main() 写盘 OK: {results} ({results.stat().st_size} bytes, n={data['n_scenarios']})")


def test_actual_diff_rate_above_30_percent_within_seed():
    """验证论文 §4.4.1 实证: 默认 seed=42 时决策差异率至少达到主人预期。"""
    summary = run_experiment()
    rate = summary["decision_diff_rate"]
    # 论文规划: 决策差异率至少要 >= 30%. 我们在 50% 以上是强证据.
    assert rate >= 0.50, (
        f"默认 seed 应产生强决策差异 (>50%). 实际: {rate:.2%}"
    )
    print(f"[OK] 默认 seed 决策差异率 = {rate:.2%} (>50%, 证据强)")
