"""Stage 4.1：价目表 + 金额换算的单元测试。

覆盖：
  - 四个**互斥**的计费桶（cacheHit / cacheMiss / cacheWrite / output）
  - 峰谷分时（高峰窗口 / 周末 / 法定节假日）与高峰倍率
  - 未知模型兜底 + estimated 标记
  - MiniMax-M3 的官方价（美元表）与国产平台价（人民币表）
  - **读取时算钱**：改价目表 -> 金额变，而台账一个字节都不动
  - summary() 的形状（对齐 qq-bridge token-ledger.js::summary()）

全部离线：不发请求、不读 config.yaml（那里有 API key）、不碰仓库 data/。
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

from app.brain import model_prices as mp
from app.brain.token_ledger import TokenLedger

ROOT = Path(__file__).resolve().parent.parent


def _usage(prompt=100, completion=10, hit=80, miss=20, write=0):
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "cache_hit_tokens": hit,
        "cache_miss_tokens": miss,
        "cache_write_tokens": write,
    }


def _beijing_ms(date_str: str, hour: int, minute: int = 0) -> float:
    """北京时间 -> epoch 毫秒（不依赖运行机器的时区）。"""
    tz = datetime.timezone(datetime.timedelta(hours=8))
    dt = datetime.datetime.fromisoformat(
        f"{date_str}T{hour:02d}:{minute:02d}:00")
    return dt.replace(tzinfo=tz).timestamp() * 1000


def _rows(path: Path) -> list:
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# 价目表本身
# ---------------------------------------------------------------------------

def test_every_model_entry_has_four_mutually_exclusive_buckets():
    """四个桶必须齐全（缺一个就有一类 token 不计费）。"""
    for table in (mp.DEFAULT_PRICE_TABLE, mp.CNY_PRICE_TABLE):
        for name, entry in table["models"].items():
            offs = ([tier["off"] for tier in entry["tiers"]]
                    if entry.get("tiers") else [entry["off"]])
            for off in offs:
                assert set(off) >= {"cacheHit", "cacheMiss", "cacheWrite",
                                    "output"}, name
                assert all(isinstance(v, (int, float)) and v >= 0
                           for v in off.values()), name


def test_minimax_m3_price_is_present_and_not_estimated():
    """MiniMax-M3 有官方价 -> 不标 estimated。"""
    rates = mp.rates_at("MiniMax-M3", 0)
    assert rates["model"] == "minimax-m3"
    assert rates["estimated"] is False
    assert rates["currency"] == "USD"
    # 官方价（platform.minimax.io，标准档 ≤512k，永久五折后现价）
    assert rates["cacheHit"] == pytest.approx(0.06)
    assert rates["cacheMiss"] == pytest.approx(0.30)
    assert rates["cacheWrite"] == pytest.approx(0.30)   # 官方无缓存写入价 -> 按输入价
    assert rates["output"] == pytest.approx(1.20)


def test_minimax_m3_long_context_tier_is_another_price():
    """官方按输入长度分档：> 512k tokens 走更贵的那档。"""
    rates = mp.rates_at("minimax-m3", 0, input_tokens=600000)
    assert rates["cacheMiss"] == pytest.approx(0.60)
    assert rates["output"] == pytest.approx(2.40)


def test_cny_table_has_the_domestic_prices():
    """人民币表：国产平台官方价（本项目实际账单口径）。"""
    rates = mp.rates_at("MiniMax-M3", 0, table=mp.CNY_PRICE_TABLE)
    assert rates["currency"] == "CNY"
    assert rates["estimated"] is False
    assert rates["cacheMiss"] == pytest.approx(2.10)
    assert rates["output"] == pytest.approx(8.40)
    assert rates["cacheHit"] == pytest.approx(0.42)


def test_unknown_model_is_estimated():
    """不认识的模型 -> 走兜底价 + 标 estimated（不许当成真账单）。"""
    rates = mp.rates_at("totally-unknown-model", 0)
    assert rates["estimated"] is True
    assert rates["model"] == mp.DEFAULT_PRICE_TABLE["fallbackModel"]
    # 没说是哪个模型也算估的（面板就是这么调的）
    assert mp.rates_at(None, 0)["estimated"] is True


def test_price_table_records_the_source_of_every_model():
    """每个模型条目都要写清楚价格来源（不然没法核对）。"""
    for table in (mp.DEFAULT_PRICE_TABLE, mp.CNY_PRICE_TABLE):
        for name, entry in table["models"].items():
            assert entry.get("source"), name
            assert entry["source"].startswith("https://"), name


# ---------------------------------------------------------------------------
# 峰谷分时 + 节假日
# ---------------------------------------------------------------------------

def test_peak_window_weekday_and_holiday():
    peak = mp.is_peak_at(_beijing_ms("2026-01-05", 10))     # 周一 10:00
    assert peak is True
    assert mp.is_peak_at(_beijing_ms("2026-01-05", 9)) is True      # 窗口起点
    assert mp.is_peak_at(_beijing_ms("2026-01-05", 18)) is False    # 窗口右开
    assert mp.is_peak_at(_beijing_ms("2026-01-05", 20)) is False    # 晚上
    assert mp.is_peak_at(_beijing_ms("2026-01-10", 10)) is False    # 周六
    assert mp.is_peak_at(_beijing_ms("2026-01-01", 10)) is False    # 法定节假日
    assert mp.is_peak_at(_beijing_ms("2026-10-01", 10)) is False    # 国庆


def test_peak_multiplier_applies_per_provider():
    """DeepSeek 空闲价 = 高峰价的一半；MiniMax 没有分时定价 -> 不加价。"""
    off_ts = _beijing_ms("2026-01-05", 20)
    peak_ts = _beijing_ms("2026-01-05", 10)
    ds_off = mp.rates_at("deepseek-flash", off_ts, table=mp.CNY_PRICE_TABLE)
    ds_peak = mp.rates_at("deepseek-flash", peak_ts, table=mp.CNY_PRICE_TABLE)
    assert ds_peak["cacheMiss"] == pytest.approx(ds_off["cacheMiss"] * 2)
    mm_off = mp.rates_at("minimax-m3", off_ts)
    mm_peak = mp.rates_at("minimax-m3", peak_ts)
    assert mm_peak["cacheMiss"] == pytest.approx(mm_off["cacheMiss"])
    assert mm_peak["tier"] == "peak"      # 档位照样判，金额不变


def test_holiday_calendar_is_well_formed():
    assert mp.CN_HOLIDAYS
    assert "2026-01-01" in mp.CN_HOLIDAYS
    assert "2026-10-07" in mp.CN_HOLIDAYS
    assert "2027-10-07" in mp.CN_HOLIDAYS
    for day in mp.CN_HOLIDAYS:
        datetime.date.fromisoformat(day)      # 格式必须是 YYYY-MM-DD


# ---------------------------------------------------------------------------
# 桶归一化
# ---------------------------------------------------------------------------

def test_normalize_usage_reads_ledger_and_provider_shapes():
    a = mp.normalize_usage(_usage(prompt=100, hit=80, miss=20, completion=5))
    assert (a["cacheHit"], a["cacheMiss"], a["output"]) == (80, 20, 5)
    # 只给 prompt + hit -> miss = prompt - hit
    b = mp.normalize_usage({"prompt_tokens": 100, "cache_hit_tokens": 80})
    assert b["cacheMiss"] == 20
    # provider 原始口径（DSH 那套键名）
    c = mp.normalize_usage({"inputTokens": 100, "cacheReadTokens": 80,
                            "outputTokens": 5})
    assert c["cacheHit"] == 80
    assert c["output"] == 5


def test_normalize_usage_keeps_buckets_disjoint_when_miss_includes_write():
    """provider 把缓存写入算进未命中里时，要扣出来（否则重复计费）。"""
    out = mp.normalize_usage({"prompt_tokens": 100, "cache_hit_tokens": 0,
                              "cache_miss_tokens": 120,
                              "cache_write_tokens": 20})
    assert out["cacheMiss"] + out["cacheWrite"] == 100


# ---------------------------------------------------------------------------
# 读取时算钱（Stage 4.1 的核心设计）
# ---------------------------------------------------------------------------

def test_price_change_changes_money_without_rewriting_the_ledger(tmp_path):
    """改价目表 -> 金额变；**台账不用重写**（这是本阶段的关键设计）。"""
    path = tmp_path / "token-usage.jsonl"
    led = TokenLedger(path)
    led.record("s1", 1, "standard", 0,
               _usage(prompt=1000, completion=100, hit=0, miss=1000),
               model="MiniMax-M3")
    before_bytes = path.read_bytes()
    before = led.cost_summary()
    assert before["totals"]["cost"] > 0

    # 只改价目表（输入价 0.30 -> 4.20），台账一个字节都不碰
    table = mp.resolve_price_table({"models": {"minimax-m3": {
        "off": {"cacheMiss": 4.20, "cacheWrite": 4.20}}}})
    after = led.cost_summary(table=table)
    assert after["totals"]["cost"] > before["totals"]["cost"]
    assert path.read_bytes() == before_bytes


def test_ledger_rows_never_contain_money(tmp_path):
    """台账里不许出现金额字段（价格表会变，历史不该被重写）。"""
    path = tmp_path / "token-usage.jsonl"
    led = TokenLedger(path)
    usage = _usage()
    usage.update({"cost": 1.23, "price": 0.5, "usd": 9, "money": 4})
    led.record("s1", 1, "standard", 0, usage, model="MiniMax-M3")
    row = _rows(path)[0]
    for key in row:
        assert key not in ("price", "cost", "money", "usd", "amount",
                           "total_cost")


def test_estimate_cost_hook_matches_dashboard_contract():
    """面板的挂载点：estimate_cost(buckets) -> dict。"""
    out = mp.estimate_cost(_usage(prompt=1000, completion=100, hit=0, miss=1000))
    assert out["amount"] > 0
    assert out["currency"] == "USD"
    assert out["estimated"] is True      # 面板没告诉我们是哪个模型 -> 算估的
    assert set(out["rates_per_million"]) == {"cacheHit", "cacheMiss",
                                             "cacheWrite", "output"}
    # 明确指定模型且该模型有官方价 -> 不算估
    known = mp.estimate_cost(_usage(prompt=1000, completion=100, hit=0,
                                    miss=1000), model="MiniMax-M3")
    assert known["estimated"] is False


# ---------------------------------------------------------------------------
# summary() 的形状
# ---------------------------------------------------------------------------

def test_summary_shape_matches_qq_bridge():
    rows = [
        {"v": 1, "kind": "request", "session_id": "s1", "request_index": 1,
         "backend": "standard", "retry": 0, "ts": 1000.0,
         "model": "MiniMax-M3", "prompt_tokens": 1000,
         "completion_tokens": 100, "total_tokens": 1100,
         "cache_hit_tokens": 200, "cache_miss_tokens": 800,
         "cache_write_tokens": 0},
        {"v": 1, "kind": "request", "session_id": "s1", "request_index": 2,
         "backend": "standard", "retry": 0, "ts": 1010.0,
         "model": "MiniMax-M3", "prompt_tokens": 500,
         "completion_tokens": 50, "total_tokens": 550,
         "cache_hit_tokens": 100, "cache_miss_tokens": 400,
         "cache_write_tokens": 0},
    ]
    s = mp.summary(rows)
    totals = s["totals"]
    for key in ("cost", "attributedCost", "unattributedCost", "byTier",
                "cacheHitRate", "avgCostPerTurn", "avgCostPerStep",
                "estimated"):
        assert key in totals, key
    assert set(totals["byTier"]) == {"peak", "off"}
    assert totals["cost"] > 0
    assert totals["attributedCost"] > 0
    assert totals["unattributedCost"] == 0
    assert totals["turns"] == 1
    assert totals["steps"] == 2
    assert 0 < totals["cacheHitRate"] < 1
    assert s["currency"] == "USD"
    assert s["mostExpensiveTurn"]["session_id"] == "s1"


def test_aggregate_rows_are_unattributed():
    """压实后的聚合行拆不到具体回合 -> 算「未归属」。"""
    rows = [
        {"kind": "request", "session_id": "s1", "request_index": 1,
         "ts": 1000.0, "model": "MiniMax-M3", "prompt_tokens": 100,
         "completion_tokens": 10, "cache_hit_tokens": 0,
         "cache_miss_tokens": 100, "cache_write_tokens": 0},
        {"kind": "aggregate", "session_id": "s1", "n": 3, "ts": 900.0,
         "model": "", "prompt_tokens": 300, "completion_tokens": 30,
         "cache_hit_tokens": 0, "cache_miss_tokens": 300,
         "cache_write_tokens": 0},
    ]
    totals = mp.summary(rows)["totals"]
    assert totals["unattributedCost"] > 0
    assert totals["attributedCost"] > 0
    assert totals["cost"] == pytest.approx(
        totals["attributedCost"] + totals["unattributedCost"])


def test_group_turns_splits_on_idle_gap():
    rows = [
        {"session_id": "s1", "ts": 1000.0, "model": "MiniMax-M3"},
        {"session_id": "s1", "ts": 1010.0, "model": "MiniMax-M3"},
        {"session_id": "s1", "ts": 5000.0, "model": "MiniMax-M3"},
    ]
    turns = mp.group_turns(rows, idle_seconds=300)
    assert len(turns) == 2
    assert turns[0]["rows"][0]["ts"] == 1000.0
    assert len(turns[1]["rows"]) == 1


def test_summary_over_the_real_ledger_is_read_only(tmp_path):
    """拿真实台账的**副本**跑一遍汇总（真实文件一个字节都不动）。"""
    import shutil
    real = ROOT / "data" / "token-usage.jsonl"
    if not real.is_file():
        pytest.skip("仓库里没有 token-usage.jsonl")
    copy = tmp_path / "token-usage.jsonl"
    shutil.copy2(real, copy)
    before = real.read_bytes()
    rows = _rows(copy)
    if not rows:
        pytest.skip("台账是空的")
    s = mp.summary(rows)
    totals = s["totals"]
    assert totals["cost"] >= 0
    assert 0 <= totals["cacheHitRate"] <= 1
    assert totals["steps"] == len(rows)
    assert real.read_bytes() == before      # 真文件没被动过
