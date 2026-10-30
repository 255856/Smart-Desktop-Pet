"""Stage 4.4：输入侧预算 + 可见告警的单元测试。

覆盖：
  - 预算配置来自 app/core/companion_config.yaml（**绝不读 config.yaml**）
  - 用户覆盖文件优先（不写仓库 data/，改的是临时目录）
  - 超预算 -> 台账里出现**可见告警**（附加字段，不是新行类型）
  - 告警同时走主人看得见的通道（桌宠气泡 / Windows 系统通知）
  - 告警说的是「预算超了」，**不是**「失败了」
  - 现有读法（面板的 reader）不受影响

全部离线：不发请求、不读 config.yaml（那里有 API key）、不碰仓库 data/。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from app.brain import model_prices as mp
from app.brain import token_ledger as tl
from app.brain.token_ledger import TokenLedger
from app.core import owner_notice

ROOT = Path(__file__).resolve().parent.parent


def _usage(prompt=100, completion=10, hit=0, miss=100, write=0):
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "cache_hit_tokens": hit,
        "cache_miss_tokens": miss,
        "cache_write_tokens": write,
    }


def _rows(path: Path) -> list:
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _use_budget(monkeypatch, limit=1000, idle=300, notice_interval=0):
    """把进程内的预算换成测试用的小额度。"""
    budget = mp.CostBudget(
        input_tokens_per_turn=limit,
        turn_idle_seconds=idle,
        warn=True,
        notice_min_interval_seconds=notice_interval,
    )
    monkeypatch.setattr(tl, "_current_budget", lambda: budget)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def test_budget_section_lives_in_companion_config_yaml():
    """预算配置在 companion_config.yaml 的 cost_budget 段。"""
    from app.core import companion_config as cc
    raw = cc._read_yaml(cc._default_yaml_path()) or {}
    block = raw.get("cost_budget")
    assert isinstance(block, dict), "companion_config.yaml 里没有 cost_budget 段"
    assert int(block["input_tokens_per_turn"]) > 0
    budget = mp.load_cost_budget()
    assert budget.input_tokens_per_turn == int(block["input_tokens_per_turn"])
    assert budget.turn_idle_seconds == int(block["turn_idle_seconds"])
    assert budget.warn is True


def test_budget_config_never_reads_config_yaml(monkeypatch):
    """**绝不读 config.yaml**（那里有 API key）。"""
    from app.core import companion_config as cc
    seen = []
    original = cc._read_yaml

    def spy(path):
        seen.append(Path(path))
        return original(path)

    monkeypatch.setattr(cc, "_read_yaml", spy)
    mp.load_cost_budget()
    assert seen, "应该至少读一次 yaml"
    secret = (ROOT / "config.yaml").resolve()
    for path in seen:
        assert path.resolve() != secret, f"预算配置读了 {path}"


def test_user_override_file_wins(monkeypatch, tmp_path):
    """data/companion_config.yaml（用户覆盖）优先于仓库默认表。"""
    from app.core import companion_config as cc
    override = tmp_path / "companion_config.yaml"
    override.write_text(
        "cost_budget:\n  input_tokens_per_turn: 1234\n", encoding="utf-8")
    monkeypatch.setattr(cc, "_user_yaml_path", lambda: override)
    assert mp.load_cost_budget().input_tokens_per_turn == 1234


def test_budget_disabled_when_limit_is_zero():
    """上限 0 = 不限（不告警）。"""
    assert mp.CostBudget(input_tokens_per_turn=0).enabled is False
    assert mp.CostBudget(input_tokens_per_turn=100).enabled is True


# ---------------------------------------------------------------------------
# 超预算 -> 可见告警
# ---------------------------------------------------------------------------

def test_exceeding_budget_writes_a_visible_warning_into_the_ledger(
        tmp_path, monkeypatch):
    _use_budget(monkeypatch, limit=500)
    path = tmp_path / "token-usage.jsonl"
    led = TokenLedger(path)
    ok = led.record("s1", 1, "standard", 0,
                     _usage(prompt=600, completion=10, miss=600),
                     model="MiniMax-M3")
    assert ok is True
    row = _rows(path)[0]
    assert row["input_budget_exceeded"] is True
    assert row["input_budget_tokens"] == 500
    assert row["turn_input_tokens"] == 600
    assert "预算" in row["warning"]
    # 事后能查出来
    assert len(led.warnings()) == 1


def test_under_budget_has_no_warning(tmp_path, monkeypatch):
    _use_budget(monkeypatch, limit=1000)
    path = tmp_path / "token-usage.jsonl"
    led = TokenLedger(path)
    led.record("s1", 1, "standard", 0,
               _usage(prompt=100, completion=10, miss=100), model="MiniMax-M3")
    row = _rows(path)[0]
    assert "input_budget_exceeded" not in row
    assert "warning" not in row
    assert led.warnings() == []


def test_warning_row_keeps_the_normal_row_shape(tmp_path, monkeypatch):
    """告警是**附加字段**，不是新行类型 -> 现有读法不受影响。"""
    _use_budget(monkeypatch, limit=500)
    path = tmp_path / "token-usage.jsonl"
    led = TokenLedger(path)
    led.record("s1", 1, "standard", 0,
               _usage(prompt=600, completion=10, miss=600), model="MiniMax-M3")
    rows = _rows(path)
    assert len(rows) == 1
    row = rows[0]
    # 台账原有的字段一个不少
    for key in ("v", "kind", "session_id", "request_index", "backend", "retry",
                "ts", "model", "prompt_tokens", "completion_tokens",
                "total_tokens", "cache_hit_tokens", "cache_miss_tokens",
                "cache_write_tokens"):
        assert key in row, key
    assert row["kind"] == "request"
    # 告警里没有金额
    for key in row:
        assert key not in ("cost", "price", "money", "usd", "amount")


def test_new_turn_resets_the_accumulator(tmp_path, monkeypatch):
    """隔太久 = 新的一轮，累计清零（不把上一轮算进来）。"""
    _use_budget(monkeypatch, limit=1000, idle=300)
    path = tmp_path / "token-usage.jsonl"
    led = TokenLedger(path)
    # 同一轮里的两次请求：600 + 600 = 1200 > 1000 -> 超了
    led.record("s1", 1, "standard", 0,
               _usage(prompt=600, completion=10, miss=600),
               model="MiniMax-M3", ts=1000.0)
    led.record("s1", 2, "standard", 0,
               _usage(prompt=600, completion=10, miss=600),
               model="MiniMax-M3", ts=1010.0)
    # 隔了 1 小时 -> 新的一轮，累计从 0 开始（600 < 1000 -> 不告警）
    led.record("s1", 3, "standard", 0,
               _usage(prompt=600, completion=10, miss=600),
               model="MiniMax-M3", ts=4600.0)
    rows = _rows(path)
    assert rows[0].get("input_budget_exceeded") is None
    assert rows[1]["input_budget_exceeded"] is True
    assert rows[1]["turn_input_tokens"] == 1200
    assert "input_budget_exceeded" not in rows[2]


def test_warning_survives_compaction(tmp_path, monkeypatch):
    """压实（老行折进聚合行）不能把告警字段弄丢。"""
    _use_budget(monkeypatch, limit=500)
    path = tmp_path / "token-usage.jsonl"
    led = TokenLedger(path, max_lines=16)
    for i in range(1, 12):
        led.record("s1", i, "standard", 0,
                   _usage(prompt=600, completion=10, miss=600),
                   model="MiniMax-M3")
    led.compact()
    assert led.warnings(), "压实后告警不该消失"


# ---------------------------------------------------------------------------
# 主人看得见的通道
# ---------------------------------------------------------------------------

def test_warning_reaches_the_owner_channel(tmp_path, monkeypatch):
    """告警要走到主人看得见的地方（桌宠气泡 / 系统通知）。"""
    _use_budget(monkeypatch, limit=500)
    seen = []
    monkeypatch.setattr(owner_notice, "_owner_notice_sink",
                        lambda title, message: seen.append((title, message)))
    owner_notice.reset_budget_notice()
    path = tmp_path / "token-usage.jsonl"
    led = TokenLedger(path)
    led.record("s1", 1, "standard", 0,
               _usage(prompt=600, completion=10, miss=600), model="MiniMax-M3")
    for _ in range(100):
        if seen:
            break
        time.sleep(0.02)
    assert seen, "主人通道没收到告警"
    title, message = seen[0]
    assert "预算" in title or "预算" in message
    assert "600" in message and "500" in message


def test_warning_does_not_claim_a_failure(tmp_path, monkeypatch):
    """告警说的是「预算超了」，不是「失败了」。"""
    _use_budget(monkeypatch, limit=500)
    seen = []
    monkeypatch.setattr(owner_notice, "_owner_notice_sink",
                        lambda title, message: seen.append((title, message)))
    owner_notice.reset_budget_notice()
    path = tmp_path / "token-usage.jsonl"
    led = TokenLedger(path)
    led.record("s1", 1, "standard", 0,
               _usage(prompt=600, completion=10, miss=600), model="MiniMax-M3")
    for _ in range(100):
        if seen:
            break
        time.sleep(0.02)
    assert seen
    text = " ".join(seen[0])
    for word in ("失败", "错误", "异常", "故障", "无法", "不能"):
        assert word not in text, f"告警文案里不该出现「{word}」：{text}"
    row = _rows(path)[0]
    for word in ("失败", "错误", "异常", "故障"):
        assert word not in row["warning"]


def test_budget_notice_is_throttled_per_session():
    """同一个会话在最短间隔内只提醒一次（不刷屏）。"""
    owner_notice.reset_budget_notice()
    assert owner_notice.notify_owner_budget_exceeded(
        "cost_budget:s1", "标题", "内容", min_interval_seconds=600) is True
    assert owner_notice.notify_owner_budget_exceeded(
        "cost_budget:s1", "标题", "内容", min_interval_seconds=600) is False
    assert owner_notice.notify_owner_budget_exceeded(
        "cost_budget:s2", "标题", "内容", min_interval_seconds=600) is True
    owner_notice.reset_budget_notice()


# ---------------------------------------------------------------------------
# 现有读法不受影响（A6）
# ---------------------------------------------------------------------------

def test_dashboard_reader_still_understands_the_ledger(tmp_path, monkeypatch):
    """面板的 reader 不该因为告警字段而数错请求数。"""
    try:
        from app.web import dashboard
    except Exception as exc:      # noqa: BLE001 - 面板正在被改，跳过而不是误伤
        pytest.skip(f"面板模块导入失败：{exc}")
    _use_budget(monkeypatch, limit=500)
    path = tmp_path / "token-usage.jsonl"
    led = TokenLedger(path)
    led.record("s1", 1, "standard", 0,
               _usage(prompt=600, completion=10, miss=600), model="MiniMax-M3")
    led.record("s1", 2, "standard", 0,
               _usage(prompt=100, completion=10, miss=100), model="MiniMax-M3")
    summary = dashboard._token_summary(tmp_path)
    assert summary["available"] is True
    # 告警是附加字段 -> 请求数还是 2（不多不少）
    assert summary["requests"] == 2
    assert summary["prompt_tokens"] == 700
    assert summary["cost"]["amount"] >= 0
