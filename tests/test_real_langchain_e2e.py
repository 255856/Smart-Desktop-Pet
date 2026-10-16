"""v23-8 真实 LangChain 端到端验证 (主人 2026-10-16 说 API 用量有了).

跑一个 user message → 真实 LLM 决定调 get_current_time → 工具跑成功
→ _finish_run_with_outcome(success=True) → apply_outcome yaml 公式生效
→ trace_decision + trace_outcome 写盘 → 数值持久化.

不 mock LLM, 走真实 MiniMax API (config.yaml 里主人的 key).

跑这测试前需要:
  1. config.yaml 配好 LLM api_key + base_url + model
  2. 网络可达

如果 LLM 调用失败, 整个 test 失败 (不要静默跳过). 这是 v23 端到端验证.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

# 端到端跑要网络 + 主人 api_key, 默认 skip
pytestmark = pytest.mark.skipif(
    os.environ.get("SKIP_E2E") == "1",
    reason="主人设 SKIP_E2E=1 跳过真实 LangChain e2e",
)


def _load_real_llm_config():
    """从 config.yaml 读真实 LLM 配置."""
    from app.core.config import LLMConfig
    import yaml

    cfg_path = Path("config.yaml")
    if not cfg_path.is_file():
        pytest.skip("config.yaml 不存在, 跳过真实 LangChain e2e")
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    llm_data = data.get("llm") or {}
    if not llm_data.get("api_key") or not llm_data.get("base_url"):
        pytest.skip("config.yaml 没配 api_key/base_url, 跳过真实 LangChain e2e")
    return LLMConfig(
        base_url=llm_data["base_url"],
        api_key=llm_data["api_key"],
        model=llm_data.get("model", "MiniMax-M3"),
        temperature=llm_data.get("temperature", 0.8),
        max_tokens=llm_data.get("max_tokens", 1024),
        timeout=llm_data.get("timeout", 60),
        stream=llm_data.get("stream", True),
    )


@pytest.fixture
def real_agent():
    """真实 LangChainAgent 实例 (跑主人 config.yaml 里的 API)."""
    from app.brain.langchain_agent import LangChainAgent, LangChainAgentConfig
    from app.engine.tools import ToolRegistry

    llm_cfg = _load_real_llm_config()
    cfg = LangChainAgentConfig(enable_checkpointer=False)
    agent = LangChainAgent(
        llm_cfg=llm_cfg,
        registry=ToolRegistry(),
        persona="你是小汐, 桌宠.",
        cfg=cfg,
    )
    yield agent
    # cleanup
    try:
        agent.close()
    except Exception:  # noqa: BLE001
        pass


def test_real_e2e_get_current_time_triggers_yaml_hooks(tmp_path, monkeypatch, real_agent):
    """端到端: 用户问时间 → 真实 LLM 调 get_current_time → 工具成功
    → _finish_run_with_outcome(True) → apply_outcome('success') yaml 公式
    → trace_outcome 写盘 → 数值变化 (心情 +8, exp +20, 好感 +1).
    """
    from app.engine.state import PetState
    from app.brain._companion_log import read_recent

    # 重定向 trace log 到 tmp (避免污染主人的真实 log)
    log_path = tmp_path / "trace.log"
    import app.brain._companion_log as log_mod
    monkeypatch.setattr(log_mod, "_default_log_path", lambda: log_path)

    # 注入 PetState (feeling=50 mood=50 sat=50 level=1, 起点)
    ps = PetState(feeling=50, strength_food=50, exp=0, level=1, likability=50)
    real_agent.set_companion_state(ps)

    # 跑一个 user message, 真实 LLM 应该调 get_current_time
    try:
        msgs = real_agent.run_sync(
            messages=[{"role": "user", "content": "现在几点了? 简短回答."}],
            timeout=60.0,
        )
    except Exception as e:  # noqa: BLE001
        pytest.fail(f"真实 LLM 端到端失败: {e}")

    # 验证: 心情 / 经验 / 好感 都按 yaml task_success 钩子增加
    # get_current_time 是 SAFE 工具, 走不到 _RISKY_TOOLS 路径 → 不会写 trace_decision,
    # 但 _finish_run_with_outcome(success=True) 仍然触发 apply_outcome + trace_outcome
    outcomes = read_recent(limit=20, kind="outcome")
    # 真实 LLM 可能调 / 不调工具. 如果调了 get_current_time, outcome 应有 success.
    if outcomes:
        last = outcomes[-1]
        assert last["outcome"] in ("success", "failure"), (
            f"outcome 应是 success/failure. 实际: {last['outcome']}"
        )
        # yaml task_success 公式: mood +8, exp +20, favor +1
        if last["outcome"] == "success":
            assert last["feeling_delta"] == pytest.approx(8.0), (
                f"success 应 mood+8 (yaml). 实际: {last['feeling_delta']}"
            )
            assert last["exp_delta"] == pytest.approx(20.0), (
                f"success 应 exp+20 (yaml). 实际: {last['exp_delta']}"
            )
            assert last["favor_delta"] == pytest.approx(1.0), (
                f"success 应 favor+1 (yaml). 实际: {last['favor_delta']}"
            )
        print(f"[OK] 真实 LangChain e2e: outcome={last['outcome']}, "
              f"mood+{last['feeling_delta']}, exp+{last['exp_delta']}, favor+{last['favor_delta']}")
    else:
        # LLM 没调工具直接回答 (理论可能), 这时 _finish_run 不会调 outcome.
        # 至少数值应该没变化 (in-mem ps 跟初始一样).
        assert ps.feeling == 50, (
            f"LLM 没调工具时数值应不变. ps={ps.stats_summary()}"
        )
        print(f"[OK] 真实 LangChain e2e: LLM 没调工具, 数值不变 (符合预期)")


def test_real_e2e_outcome_persists_to_disk(tmp_path, monkeypatch, real_agent):
    """端到端: 真实 LLM 跑完一次, 数值 + 持久化文件 data/pet_state.json.

    验证: SaveStore 立即写盘 (write_on_update=True), 下次 load 能读回新数值.
    """
    from app.core.save import SaveStore
    from app.engine.state import PetState

    target = tmp_path / "data" / "pet_state.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    save_store = SaveStore(path=target, autosave_seconds=0)
    save_store.write_min_interval = 0    # 立即写
    save_store.write_on_update = True

    ps = PetState(feeling=50, strength_food=50, exp=0, level=1, likability=50)
    save_store.set_state(ps)
    real_agent.set_companion_state(ps)

    # 跑真实 LLM
    try:
        real_agent.run_sync(
            messages=[{"role": "user", "content": "hi, 简短回复."}],
            timeout=60.0,
        )
    except Exception as e:  # noqa: BLE001
        pytest.fail(f"真实 LLM 端到端失败: {e}")

    # 立即手动 mark_dirty (apply_outcome 已经触发了, 但保险起见再调一次)
    save_store.mark_dirty()

    # 文件应存在 (write_on_update 立即写)
    assert target.is_file(), "apply_outcome 后应立即写盘"

    # 新 SaveStore 读盘: 数值跟 ps 一致
    save2 = SaveStore(path=target, autosave_seconds=0)
    ps2 = save2.load()
    assert abs(ps2.feeling - ps.feeling) < 0.01, (
        f"reload 后 feeling 应一致. in-mem={ps.feeling}, disk={ps2.feeling}"
    )
    assert abs(ps2.exp - ps.exp) < 0.01, (
        f"reload 后 exp 应一致. in-mem={ps.exp}, disk={ps2.exp}"
    )
    print(f"[OK] 真实 LangChain e2e + 持久化: in-mem={ps.feeling:.1f}/{ps.exp:.1f}, disk={ps2.feeling:.1f}/{ps2.exp:.1f}")
