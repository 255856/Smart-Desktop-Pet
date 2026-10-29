"""usage 归一化单元测试：各家 provider 的 usage 形状都要认得。

覆盖 OpenAI / DeepSeek / Anthropic / LangChain / Gemini 五种拼写，
外加「认不出来的形状 → 全 0 且不抛」。
"""
from __future__ import annotations

from app.brain.llm_client import (
    UsageEvent,
    build_usage_event,
    normalize_usage,
)

BUCKET_KEYS = {
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cache_hit_tokens",
    "cache_miss_tokens",
    "cache_write_tokens",
    "got_real",
    "cache_hit_rate",
}


def test_openai_shape():
    """OpenAI: prompt_tokens_details.cached_tokens 是 prompt 的子集。"""
    usage = normalize_usage({
        "prompt_tokens": 1000,
        "completion_tokens": 50,
        "total_tokens": 1050,
        "prompt_tokens_details": {"cached_tokens": 800},
    })
    assert usage["prompt_tokens"] == 1000
    assert usage["completion_tokens"] == 50
    assert usage["total_tokens"] == 1050
    assert usage["cache_hit_tokens"] == 800
    # 命中是 prompt 的子集 → 没命中的 = prompt - 命中
    assert usage["cache_miss_tokens"] == 200
    assert usage["cache_write_tokens"] == 0
    assert usage["got_real"] is True
    assert usage["cache_hit_rate"] == 0.8


def test_deepseek_shape():
    """DeepSeek: prompt_cache_hit_tokens / prompt_cache_miss_tokens。"""
    usage = normalize_usage({
        "prompt_tokens": 500,
        "completion_tokens": 20,
        "total_tokens": 520,
        "prompt_cache_hit_tokens": 450,
        "prompt_cache_miss_tokens": 50,
    })
    assert usage["cache_hit_tokens"] == 450
    assert usage["cache_miss_tokens"] == 50
    assert usage["cache_write_tokens"] == 0
    assert usage["cache_hit_rate"] == 0.9


def test_anthropic_shape():
    """Anthropic: input_tokens 不含 cache_read，cache_creation 单列。"""
    usage = normalize_usage({
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_input_tokens": 50,
        "cache_creation_input_tokens": 10,
        "total_tokens": 180,
    })
    assert usage["prompt_tokens"] == 100
    assert usage["completion_tokens"] == 20
    assert usage["total_tokens"] == 180
    assert usage["cache_hit_tokens"] == 50
    assert usage["cache_write_tokens"] == 10
    # 写缓存的 token 也要现算 → 算「没命中」
    assert usage["cache_miss_tokens"] == 110
    assert usage["cache_hit_rate"] == round(50 / 160, 4)


def test_langchain_shape():
    """LangChain usage_metadata: 缓存在 input_token_details 里。"""
    usage = normalize_usage({
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "input_token_details": {"cache_read": 30, "cache_creation": 10},
    })
    assert usage["prompt_tokens"] == 100
    assert usage["completion_tokens"] == 20
    assert usage["total_tokens"] == 120
    assert usage["cache_hit_tokens"] == 30
    assert usage["cache_write_tokens"] == 10
    # LangChain 的 input_tokens 就是 OpenAI 的 prompt_tokens（含命中那部分）
    # → 未命中 = input_tokens - cache_read
    assert usage["cache_miss_tokens"] == 70
    assert usage["cache_hit_rate"] == round(30 / 100, 4)


def test_gemini_shape():
    usage = normalize_usage({
        "prompt_token_count": 300,
        "candidates_token_count": 30,
        "total_token_count": 330,
        "cached_content_token_count": 120,
    })
    assert usage["prompt_tokens"] == 300
    assert usage["completion_tokens"] == 30
    assert usage["total_tokens"] == 330
    assert usage["cache_hit_tokens"] == 120
    assert usage["cache_miss_tokens"] == 180


def test_unknown_shape_degrades_to_zeros():
    usage = normalize_usage({"whatever": 1, "foo": "bar"})
    assert usage["prompt_tokens"] == 0
    assert usage["completion_tokens"] == 0
    assert usage["total_tokens"] == 0
    assert usage["cache_hit_tokens"] == 0
    assert usage["cache_miss_tokens"] == 0
    assert usage["cache_write_tokens"] == 0
    assert usage["cache_hit_rate"] == 0.0
    assert usage["got_real"] is False


def test_never_raises_on_garbage():
    for bad in (None, "", "text", 123, [1, 2], {"prompt_tokens": "abc"},
                {"prompt_tokens": None}, {"prompt_tokens": [1]}):
        usage = normalize_usage(bad)
        assert usage["prompt_tokens"] == 0
        assert usage["got_real"] in (True, False)


def test_all_shapes_share_the_same_keys():
    """两条后端路径共用同一个 normaliser → 事件载荷的键完全一致（B2）。"""
    shapes = [
        {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
        {"input_tokens": 10, "output_tokens": 1, "total_tokens": 11,
         "cache_read_input_tokens": 5},
        {"input_tokens": 10, "output_tokens": 1, "total_tokens": 11,
         "input_token_details": {"cache_read": 5, "cache_creation": 2}},
        {"prompt_cache_hit_tokens": 5, "prompt_cache_miss_tokens": 5,
         "prompt_tokens": 10, "completion_tokens": 1},
    ]
    for shape in shapes:
        assert set(normalize_usage(shape).keys()) == BUCKET_KEYS


def test_usage_event_shape_matches_langchain_path():
    """事件形状: ("meta", {"event": "usage", ...}) —— 两条路径一样。"""
    usage = normalize_usage({"prompt_tokens": 10, "completion_tokens": 1,
                             "total_tokens": 11})
    event = build_usage_event(usage)
    assert isinstance(event, UsageEvent)
    assert event[0] == "meta"
    assert event[1]["event"] == "usage"
    assert set(event[1].keys()) == BUCKET_KEYS | {"event"}
    assert tuple(event) == ("meta", event.payload)
    # 老消费方（只认 str）看到的是空文本 —— UI 一个字都不变
    assert str(event) == ""
    assert not event


def test_total_derived_when_provider_omits_it():
    usage = normalize_usage({"prompt_tokens": 30, "completion_tokens": 7})
    assert usage["total_tokens"] == 37
