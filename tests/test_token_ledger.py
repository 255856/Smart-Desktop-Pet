"""Token 台账单元测试。

覆盖：append-only 行格式 / 同 key 覆盖（不累加）/ 压实 / 降级 / 汇总 / 命中率。
全部离线：只用 tmp_path，不需要 API Key、不碰网络。
"""
from __future__ import annotations

import json
from pathlib import Path

from app.brain.token_ledger import (
    BUCKET_FIELDS,
    TokenLedger,
    current_session,
    default_ledger_path,
    get_ledger,
    next_request_index,
    record_usage,
    set_session,
)


def _usage(prompt=100, completion=10, hit=80, miss=20, write=0):
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "cache_hit_tokens": hit,
        "cache_miss_tokens": miss,
        "cache_write_tokens": write,
        "got_real": True,
        "cache_hit_rate": round(hit / (hit + miss), 4) if (hit + miss) else 0.0,
    }


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_record_writes_one_jsonl_line(tmp_path):
    path = tmp_path / "token-usage.jsonl"
    led = TokenLedger(path)
    assert led.record("s1", 1, "lightweight", 0, _usage(), model="m1") is True
    rows = _rows(path)
    assert len(rows) == 1
    row = rows[0]
    assert row["session_id"] == "s1"
    assert row["request_index"] == 1
    assert row["backend"] == "lightweight"
    assert row["retry"] == 0
    assert row["model"] == "m1"
    assert row["prompt_tokens"] == 100
    assert row["cache_hit_tokens"] == 80
    assert row["cache_miss_tokens"] == 20
    assert row["cache_write_tokens"] == 0
    assert row["total_tokens"] == 110
    # 行里不能出现金额字段
    for key in row:
        assert key not in ("price", "cost", "money", "usd", "amount", "total_cost")


def test_duplicate_key_overwrites_instead_of_accumulating(tmp_path):
    path = tmp_path / "token-usage.jsonl"
    led = TokenLedger(path)
    led.record("s1", 1, "lightweight", 0, _usage(hit=80, miss=20))
    led.record("s1", 1, "lightweight", 0, _usage(hit=90, miss=10))
    summary = led.summary()
    # 同一个 key 只能算一次（后到的覆盖先到的）
    assert summary["requests"] == 1
    assert summary["prompt_tokens"] == 100
    assert summary["cache_hit_tokens"] == 90
    assert summary["cache_miss_tokens"] == 10


def test_summary_totals_and_cache_hit_rate(tmp_path):
    path = tmp_path / "token-usage.jsonl"
    led = TokenLedger(path)
    led.record("s1", 1, "lightweight", 0, _usage(prompt=100, completion=10,
                                                  hit=90, miss=10))
    led.record("s1", 2, "standard", 0, _usage(prompt=200, completion=20,
                                               hit=50, miss=150))
    summary = led.summary()
    assert summary["requests"] == 2
    assert summary["sessions"] == 1
    assert summary["prompt_tokens"] == 300
    assert summary["completion_tokens"] == 30
    assert summary["total_tokens"] == 330
    assert summary["cache_hit_tokens"] == 140
    assert summary["cache_miss_tokens"] == 160
    assert summary["cache_hit_rate"] == round(140 / 300, 4)
    # 分后端拆分
    assert set(summary["by_backend"]) == {"lightweight", "standard"}
    assert summary["by_backend"]["standard"]["prompt_tokens"] == 200
    assert summary["by_backend"]["lightweight"]["cache_hit_rate"] == 0.9


def test_summary_filtered_by_session(tmp_path):
    path = tmp_path / "token-usage.jsonl"
    led = TokenLedger(path)
    led.record("s1", 1, "lightweight", 0, _usage())
    led.record("s2", 2, "standard", 0, _usage(prompt=7))
    assert led.summary("s1")["prompt_tokens"] == 100
    assert led.summary("s2")["prompt_tokens"] == 7
    assert led.summary()["prompt_tokens"] == 107


def test_compaction_bounds_file_and_keeps_totals(tmp_path):
    path = tmp_path / "token-usage.jsonl"
    led = TokenLedger(path, max_lines=20)
    for i in range(1, 60):
        led.record("s1", i, "standard", 0,
                   _usage(prompt=10, completion=1, hit=5, miss=5))
    # 文件被压实（有上限）
    assert led.line_count() <= 20
    # 总量没丢（聚合行 + 保留的样本行）
    summary = led.summary()
    assert summary["requests"] == 59
    assert summary["prompt_tokens"] == 59 * 10
    assert summary["cache_hit_tokens"] == 59 * 5
    # 压实后分后端拆分还在
    assert summary["by_backend"]["standard"]["prompt_tokens"] == 59 * 10


def test_compaction_is_idempotent(tmp_path):
    path = tmp_path / "token-usage.jsonl"
    led = TokenLedger(path, max_lines=20)
    for i in range(1, 40):
        led.record("s1", i, "standard", 0, _usage(prompt=10))
    before = led.summary()
    led.compact()
    led.compact()
    after = led.summary()
    assert after["prompt_tokens"] == before["prompt_tokens"]
    assert after["requests"] == before["requests"]


def test_never_writes_money_fields_even_if_caller_passes_them(tmp_path):
    path = tmp_path / "token-usage.jsonl"
    led = TokenLedger(path)
    usage = _usage()
    usage.update({"cost": 1.23, "price": 0.5, "usd": 9, "money": 4})
    led.record("s1", 1, "lightweight", 0, usage)
    row = _rows(path)[0]
    assert "cost" not in row
    assert "price" not in row
    assert "usd" not in row
    assert "money" not in row
    assert set(BUCKET_FIELDS).issubset(set(row))


def test_degrades_on_bad_input_without_raising(tmp_path):
    path = tmp_path / "token-usage.jsonl"
    led = TokenLedger(path)
    # 垃圾输入
    assert led.record("s1", 1, "lightweight", 0, None) is False
    assert led.record("s1", 1, "lightweight", 0, "not-a-dict") is False
    assert led.record("s1", "not-an-int", "lightweight", "x", _usage()) is True
    assert led.record(None, None, None, None, _usage()) is True
    assert led.record("s1", 1, "lightweight", 0, {"prompt_tokens": "12"}) is True
    # 读接口也不能炸
    assert isinstance(led.summary(), dict)
    assert isinstance(led.turns("s1"), list)
    assert isinstance(led.series(60000), list)
    assert isinstance(led.rows(), list)


def test_reads_corrupt_lines_without_raising(tmp_path):
    path = tmp_path / "token-usage.jsonl"
    path.write_text("not json\n\n{\"prompt_tokens\": 5}\n", encoding="utf-8")
    led = TokenLedger(path)
    summary = led.summary()
    assert summary["prompt_tokens"] == 5


def test_unwritable_path_degrades(tmp_path):
    # 路径的「父目录」其实是个文件 → 建目录会失败 → 不能抛
    blocker = tmp_path / "a_file"
    blocker.write_text("x", encoding="utf-8")
    led = TokenLedger(blocker / "nope.jsonl")
    assert led.record("s1", 1, "lightweight", 0, _usage()) is False
    assert led.summary()["requests"] == 0
    assert led.turns("s1") == []


def test_turns_and_series(tmp_path):
    path = tmp_path / "token-usage.jsonl"
    led = TokenLedger(path)
    led.record("s1", 1, "lightweight", 0, _usage(prompt=10), ts=1000.0)
    led.record("s1", 2, "lightweight", 0, _usage(prompt=20), ts=2000.0)
    turns = led.turns("s1")
    assert len(turns) == 2
    assert turns[0]["prompt_tokens"] == 10
    assert turns[1]["prompt_tokens"] == 20
    series = led.series(1000)
    assert len(series) == 2
    assert series[0]["ts"] == 1000
    assert series[0]["prompt_tokens"] == 10
    assert series[1]["ts"] == 2000


def test_default_ledger_path_honours_env_override(tmp_path, monkeypatch):
    """C3: 台账路径可以用环境变量覆盖（测试期间指向临时文件）。"""
    from app.brain.token_ledger import LEDGER_PATH_ENV

    # 没有覆盖时 -> 仓库的 data/ 目录
    monkeypatch.delenv(LEDGER_PATH_ENV, raising=False)
    path = default_ledger_path()
    assert path.name == "token-usage.jsonl"
    assert path.parent.name == "data"

    # 环境变量覆盖生效
    override = tmp_path / "custom-ledger.jsonl"
    monkeypatch.setenv(LEDGER_PATH_ENV, str(override))
    assert default_ledger_path() == override


def test_get_ledger_is_a_process_singleton():
    a = get_ledger()
    b = get_ledger()
    assert a is b
    assert a.path == default_ledger_path()


def test_request_index_is_monotonic():
    a = next_request_index()
    b = next_request_index()
    assert b > a


def test_session_helpers():
    old = current_session()
    try:
        set_session("char:测试:main")
        assert current_session() == "char:测试:main"
    finally:
        set_session(old)


def test_record_usage_writes_to_default_ledger(tmp_path, monkeypatch):
    """默认台账真的会被创建并写进一行（幂等：同一个 key 反复跑只占一行）。"""
    from app.brain import token_ledger

    path = tmp_path / "token-usage.jsonl"
    monkeypatch.setattr(token_ledger, "_SINGLETON",
                        token_ledger.TokenLedger(path))
    ok = record_usage(_usage(), backend="lightweight", model="m",
                      session_id="test:ledger", request_index=1)
    assert ok is True
    rows = _rows(path)
    assert len(rows) == 1
    assert rows[0]["session_id"] == "test:ledger"
    assert rows[0]["backend"] == "lightweight"
    assert rows[0]["cache_hit_tokens"] == 80
    # 再记一次同一个 key → 覆盖（文件里也不长第二行，汇总里也只有一条）
    record_usage(_usage(hit=90, miss=10), backend="lightweight", model="m",
                 session_id="test:ledger", request_index=1)
    assert len(_rows(path)) == 1
    summary = token_ledger.get_ledger().summary()
    assert summary["requests"] == 1
    assert summary["cache_hit_tokens"] == 90

# --------------------------------------------------------------------------
# Stage 4.1 / 4.4：读取时算钱 + 输入侧预算
# --------------------------------------------------------------------------

def test_cost_summary_computes_money_at_read_time(tmp_path):
    """金额是**读取时**算的：台账里只有 token 桶。"""
    from app.brain import model_prices as mp

    path = tmp_path / "token-usage.jsonl"
    led = TokenLedger(path)
    led.record("s1", 1, "standard", 0, _usage(prompt=1000, completion=100),
               model="MiniMax-M3")
    before_bytes = path.read_bytes()
    summary = led.cost_summary()
    assert summary["totals"]["cost"] > 0
    assert summary["totals"]["steps"] == 1
    assert summary["totals"]["estimated"] is False   # MiniMax-M3 有官方价
    # 改价目表 -> 金额变；台账不用重写
    table = mp.resolve_price_table({"models": {"minimax-m3": {
        "off": {"cacheMiss": 4.20, "cacheWrite": 4.20}}}})
    after = led.cost_summary(table=table)
    assert after["totals"]["cost"] > summary["totals"]["cost"]
    assert path.read_bytes() == before_bytes


def test_budget_warning_does_not_change_the_row_shape(tmp_path, monkeypatch):
    """超预算只是**附加**告警字段：原有字段一个不少，也不新增行。"""
    from app.brain import model_prices as mp
    from app.brain import token_ledger as tl

    budget = mp.CostBudget(input_tokens_per_turn=500, turn_idle_seconds=300,
                            warn=True, notice_min_interval_seconds=0)
    monkeypatch.setattr(tl, "_current_budget", lambda: budget)
    path = tmp_path / "token-usage.jsonl"
    led = TokenLedger(path)
    led.record("s1", 1, "standard", 0,
               _usage(prompt=600, hit=0, miss=600), model="MiniMax-M3")
    rows = _rows(path)
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "request"
    for key in ("v", "kind", "session_id", "request_index", "backend", "retry",
                "ts", "model", "prompt_tokens", "completion_tokens",
                "total_tokens", "cache_hit_tokens", "cache_miss_tokens",
                "cache_write_tokens"):
        assert key in row, key
    assert row["input_budget_exceeded"] is True
    assert led.warnings()

