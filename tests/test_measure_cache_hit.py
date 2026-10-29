# -*- coding: utf-8 -*-
"""scripts/measure_cache_hit.py 的离线自检（**不需要 API key、不发网络请求**）。

这个测量台是「前缀缓存命中率」这件事的尺子，尺子本身不能坏：
所以这里把它的**纯逻辑**（请求形状 / 统计 / 表格 / 参数）全部钉死，
另外守住两条硬约束：

  1. 台账必须指向临时文件 —— 测量流量绝不能写进仓库的 data/token-usage.jsonl；
  2. 没有 key 时 main() 直接退出（返回 2），不发请求。

真请求那一面（命中率数字）靠真 key 现场量，测试里只断言「形状对不对」。
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "measure_cache_hit.py"


def _load_module():
    """把测量台当模块导入（不依赖 scripts/ 是包）。"""
    spec = importlib.util.spec_from_file_location("dp_measure_cache_hit", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    saved = os.environ.get("DESKTOP_PET_TOKEN_LEDGER")
    try:
        spec.loader.exec_module(mod)
    finally:
        # 测量台在 import 时会改环境变量（把台账指到临时文件）——
        # 测试会话里由 conftest 统一管，这里还原，别污染别的用例。
        if saved is not None:
            os.environ["DESKTOP_PET_TOKEN_LEDGER"] = saved
    return mod


mch = _load_module()


# --------------------------------------------------------------------------
# 1) 台账隔离：测量流量绝不写进仓库 data/
# --------------------------------------------------------------------------

def test_ledger_points_to_temp_file_not_repo_data():
    """import 之后台账路径必须在临时目录，且绝不是 data/token-usage.jsonl。"""
    path = Path(mch._LEDGER_PATH)
    assert path.is_absolute()
    assert "token-usage.jsonl" in path.name
    repo_ledger = (ROOT / "data" / "token-usage.jsonl").resolve()
    assert path.resolve() != repo_ledger
    # 临时目录也得真的存在（record_usage 会往里写）
    assert path.parent.is_dir()


def test_ledger_env_is_overridable_by_conftest(tmp_path):
    """台账路径由环境变量决定（conftest 已经把它指到临时文件）。"""
    from app.brain.token_ledger import default_ledger_path
    default = default_ledger_path()
    repo_ledger = (ROOT / "data" / "token-usage.jsonl").resolve()
    assert default.resolve() != repo_ledger


# --------------------------------------------------------------------------
# 2) 请求形状：跟生产一致（system + 易变段 + 主人原话）
# --------------------------------------------------------------------------

def test_build_messages_production_shape():
    """生产形状：易变上下文是**独立**的 user 消息，排在主人原话前面。"""
    msgs = mch.build_messages(user_text="今天天气怎么样？", dynamic=True,
                              order="prod")
    assert [m["role"] for m in msgs] == ["user", "user"]
    # 主人原话逐字节不变（意图判定读的是它）
    assert msgs[-1]["content"] == "今天天气怎么样？"
    # 易变段带时间戳，且在主人原话**前面**
    from app.brain.llm_client import DYNAMIC_CONTEXT_HEADER
    assert msgs[0]["content"].startswith(DYNAMIC_CONTEXT_HEADER)
    assert "当前时间" in msgs[0]["content"]


def test_build_messages_without_dynamic_context():
    """--dynamic off：不带易变段，只剩主人原话一条。"""
    msgs = mch.build_messages(user_text="你好", dynamic=False, order="prod")
    assert len(msgs) == 1
    assert msgs[0]["content"] == "你好"


def test_build_messages_dyn_last_order():
    """--order dyn-last：易变段挪到最后（对照实验用）。"""
    msgs = mch.build_messages(user_text="你好", dynamic=True, order="dyn-last")
    assert len(msgs) == 2
    assert msgs[0]["content"] == "你好"
    assert msgs[0]["content"] != msgs[1]["content"]


def test_build_messages_tail_padding_goes_to_user_text():
    """--tail-tokens 的填充只加在主人原话尾巴上（共享前缀不变）。"""
    msgs = mch.build_messages(user_text="你好", dynamic=False, order="prod",
                              tail="填充")
    assert msgs[0]["content"] == "你好填充"


def test_make_tail_length():
    assert mch.make_tail(0) == ""
    assert len(mch.make_tail(3)) > 0


# --------------------------------------------------------------------------
# 2b) 多轮形状：drop = 生产形状（易变段每轮重新插在最后一条主人原话前面）
# --------------------------------------------------------------------------

def _simulate(history_mode: str, n: int = 4):
    """把脚本里的多轮拼装逻辑抄一份来断言（不发请求）。"""
    history = []
    shapes = []
    for i in range(1, n + 1):
        cur = mch.build_messages(user_text=f"U{i}", dynamic=True, order="prod")
        reply = {"role": "assistant", "content": f"A{i}"}
        if history_mode == "keep":
            history = history + cur + [reply]
            msgs = list(history)
        else:
            history = history + [cur[-1], reply]
            msgs = list(history[:-2]) + cur
        shapes.append([("dyn" if "当前时间" in m["content"] else m["content"])
                       for m in msgs])
    return shapes


def test_history_drop_matches_production_shape():
    """生产形状：历史里只有主人原话 + 上一轮回复，易变段每轮重新插在最后。"""
    shapes = _simulate("drop")
    assert shapes[0] == ["dyn", "U1"]
    assert shapes[1] == ["U1", "A1", "dyn", "U2"]
    assert shapes[2] == ["U1", "A1", "U2", "A2", "dyn", "U3"]


def test_history_keep_keeps_volatile_message_in_history():
    """对照形状：易变段留在历史里（位置固定，可被缓存复用）。"""
    shapes = _simulate("keep")
    assert shapes[1] == ["dyn", "U1", "A1", "dyn", "U2", "A2"]


# --------------------------------------------------------------------------
# 3) 统计与表格
# --------------------------------------------------------------------------

def _rec(idx, prompt, cached, gap=1.0, tool=False, err=""):
    return {
        "index": idx, "gap_s": gap, "prompt_tokens": prompt,
        "cached_tokens": cached,
        "hit_rate": (cached / prompt) if prompt else 0.0,
        "is_tool_call": tool, "elapsed_s": 0.5, "error": err,
    }


def test_summarize_counts_hits_and_rate():
    recs = [_rec(1, 1000, 0), _rec(2, 1000, 900), _rec(3, 1000, 900)]
    s = mch.summarize(recs)
    assert s["requests"] == 3
    assert s["hit_requests"] == 2
    assert s["total_prompt_tokens"] == 3000
    assert s["total_cached_tokens"] == 1800
    assert s["overall_hit_rate"] == 0.6
    assert s["tool_call_requests"] == 0
    assert s["errors"] == 0


def test_summarize_separates_provider_floor_from_real_hit():
    """provider 自带 128 token 底座 —— 不算「真命中」。"""
    recs = [_rec(1, 8000, 128), _rec(2, 8000, 128), _rec(3, 8000, 7808)]
    s = mch.summarize(recs)
    assert s["hit_requests"] == 3           # 含底座
    assert s["real_hit_requests"] == 1      # 只有第 3 条是真复用
    assert s["real_hit_request_ratio"] == round(1 / 3, 4)


def test_summarize_empty_is_safe():
    s = mch.summarize([])
    assert s["requests"] == 0
    assert s["overall_hit_rate"] == 0.0


def test_format_table_shows_cached_tokens_and_gap():
    recs = [_rec(1, 8000, 0, gap=None), _rec(2, 8000, 7999, gap=1.5)]
    table = mch.format_table(recs, label="T")
    assert "T" in table
    assert "7999" in table
    assert "prompt_tok" in table and "cached_tok" in table
    assert "1.5" in table


# --------------------------------------------------------------------------
# 4) system prompt：逐字节稳定（P0-11 的不变量，量测台自己也得守住）
# --------------------------------------------------------------------------

def test_production_system_prompt_is_byte_stable():
    """隔一秒重建两次 system prompt，必须逐字节相同（否则缓存必失效）。"""
    import time
    from app.core.config import load_config
    cfg = load_config(ROOT / "config.yaml")
    sp1 = mch.build_system_prompt(cfg, "production")
    time.sleep(1.05)
    sp2 = mch.build_system_prompt(cfg, "production")
    assert sp1 == sp2
    assert len(sp1) > 1000


def test_short_system_prompt_is_fixed_and_small():
    from app.core.config import load_config
    cfg = load_config(ROOT / "config.yaml")
    sp = mch.build_system_prompt(cfg, "short")
    assert sp == mch.build_system_prompt(cfg, "short")
    assert len(sp) < 200


# --------------------------------------------------------------------------
# 5) CLI：参数齐 + 没 key 不发请求
# --------------------------------------------------------------------------

def test_cli_exposes_measurement_knobs():
    parser = mch.build_parser()
    args = parser.parse_args(["--repeat", "20", "--gap", "60"])
    assert args.repeat == 20
    assert args.gap == 60
    assert args.pattern == "even"
    for flag in ("--user", "--system", "--dynamic", "--order", "--turns",
                 "--tail-tokens", "--system-pad-chars", "--unique",
                 "--burst-size", "--pattern", "--json", "--dry-run"):
        assert flag in parser.format_help()


def test_main_without_api_key_exits_without_request(monkeypatch, capsys):
    """没有可用 key 时 main() 直接返回 2 —— 绝不发请求、绝不打真 key。"""
    import app.core.config as core_config

    real_load = core_config.load_config

    def _fake_load_config(path=None):
        cfg = real_load(ROOT / "config.yaml")
        return cfg.model_copy(
            update={"llm": cfg.llm.model_copy(update={"api_key": ""})})

    monkeypatch.setattr(core_config, "load_config", _fake_load_config)
    rc = mch.main(["--repeat", "1"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "api_key" in (captured.out + captured.err)
