#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""前缀缓存命中率测量台（MiniMax-M3 真请求）。

为什么需要这个脚本
------------------
PLAN.md「已知遗留问题 1」：前缀缓存**命中时**能覆盖 99.5% 的 system 前缀，
但 20 条不同 query 连打只有 3 条真命中（整体 25.9%）；而 P0-11 在 3 连击的
真实对话上量到 97.7~98.4%。两个数字对不上，原因没人知道。

这个脚本是「把原因量出来」的那把尺子：

  * 走**生产请求路径**（app/brain/llm_client.py 的 LLMClient.chat_stream_events），
    请求形状与线上逐字节一致：system 段 +（可选）易变 user 段 + 主人原话，
    stream=True + stream_options.include_usage；
  * 每条请求记：prompt_tokens / cached_tokens（provider 原始 usage 里的
    prompt_tokens_details.cached_tokens）/ 与上一条的墙钟间隔 / 是否工具调用；
  * 台账指向临时文件（DESKTOP_PET_TOKEN_LEDGER），**绝不碰仓库里的 data/**。

用法
----
    # 衰减曲线
    python scripts/measure_cache_hit.py --repeat 20 --gap 0
    python scripts/measure_cache_hit.py --repeat 20 --gap 5
    python scripts/measure_cache_hit.py --repeat 20 --gap 60
    python scripts/measure_cache_hit.py --repeat 20 --pattern burst --burst-size 3 --gap 60

    # 受控对比
    python scripts/measure_cache_hit.py --repeat 10 --user same
    python scripts/measure_cache_hit.py --repeat 10 --system short
    python scripts/measure_cache_hit.py --repeat 10 --dynamic off
    python scripts/measure_cache_hit.py --repeat 10 --tail-tokens 4000
    python scripts/measure_cache_hit.py --repeat 6 --turns 3

    # 只看请求形状，不发请求
    python scripts/measure_cache_hit.py --dry-run

API key 从 config.yaml 的 llm.api_key 读，**只在内存里用**：
不打印、不写日志、不落盘、不进 --json 输出。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# --------------------------------------------------------------------------
# 台账必须指到临时文件 —— 必须在 import app.brain.* 之前设好，
# 否则 record_usage() 第一次建单例时会把仓库里的基线台账 data/token-usage.jsonl 写脏。
# --------------------------------------------------------------------------
_LEDGER_DIR = tempfile.mkdtemp(prefix="dp-cache-probe-")
_LEDGER_PATH = os.path.join(_LEDGER_DIR, "token-usage.jsonl")
os.environ["DESKTOP_PET_TOKEN_LEDGER"] = _LEDGER_PATH


# 20 条互不相同的自然问句（覆盖「20 条不同 query 连打」那个场景）
DEFAULT_USER_TEXTS: tuple[str, ...] = (
    "今天天气怎么样？",
    "现在几点了？",
    "帮我设个 10 分钟后的提醒",
    "记住我喜欢喝冰美式",
    "打开记事本",
    "我刚才在干嘛？",
    "给我讲个羊咩的故事",
    "你今天心情好不好？",
    "帮我算一下 128 * 37 等于多少",
    "查一下 CPU 现在的占用",
    "推荐一部最近不错的电影",
    "截一张当前的屏幕",
    "帮我看看还有哪些提醒",
    "我昨天睡觉了吗",
    "你会写诗吗？来一首",
    "搜一下 Python 3.13 的新特性",
    "把音量调小一点",
    "我叫什么名字？",
    "帮我建个待办：买牛奶",
    "今天是什么日子？",
)

# 补长用的填充文本（中文 1 字 ≈ 1 token 量级；真实长度以 API 回报的
# prompt_tokens 为准，这里只是把请求长度顶上去）。
_PAD_UNIT = "记忆缓存前缀测量填充文本"


# --------------------------------------------------------------------------
# 参数
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="MiniMax 前缀缓存命中率测量台（走生产请求路径）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--repeat", type=int, default=10,
                   help="发多少条请求（默认 10）")
    p.add_argument("--gap", type=float, default=0.0,
                   help="两条请求之间的间隔秒数（默认 0）")
    p.add_argument("--pattern", choices=("even", "burst"), default="even",
                   help="even=均匀间隔；burst=每 --burst-size 条连打后等 --gap 秒")
    p.add_argument("--burst-size", type=int, default=3,
                   help="burst 模式每组几条（默认 3）")
    p.add_argument("--user", choices=("varying", "same"), default="varying",
                   help="varying=每条换一句主人原话；same=每条用同一句")
    p.add_argument("--system", choices=("production", "short"), default="production",
                   help="production=生产 system prompt；short=短 system prompt")
    p.add_argument("--session-block", choices=("on", "off"), default="on",
                   help="on=system 段带生产会话块（SKILL + 记忆规则）；off=只带人设")
    p.add_argument("--dynamic", choices=("on", "off"), default="on",
                   help="on=带本轮易变上下文（时间戳）那条独立 user 消息；off=不带")
    p.add_argument("--order", choices=("prod", "dyn-last"), default="prod",
                   help="prod=[system, 易变段, 主人原话]（生产）；"
                        "dyn-last=[system, 主人原话, 易变段]")
    p.add_argument("--turns", type=int, default=1,
                   help="模拟几轮对话（>1 时维护一条增长的历史）")
    p.add_argument("--history", choices=("drop", "keep"), default="drop",
                   help="drop=生产形状（历史里只有主人原话+回复，易变段每轮重新插在"
                        "最后一条主人原话前）；keep=易变段也留在历史里（对照）")
    p.add_argument("--fixed-prefix-msg", default="",
                   help="在 system 之后插一条**固定**的 user 消息（用来测"
                        "「只有最后一条在变」时前缀还能不能复用）")
    p.add_argument("--tail-tokens", type=int, default=0,
                   help="在主人原话后面补多少个填充单元（用来扫请求长度）")
    p.add_argument("--system-pad-chars", type=int, default=0,
                   help="在 **system 段** 末尾补多少个中文字符（把共享前缀顶长，"
                        "用来测 8192 块边界）")
    p.add_argument("--unique", action="store_true",
                   help="给 system 段加一个本次运行独有的标记（保证缓存里没有"
                        "上一轮的残留，用来排除「历史缓存」干扰）")
    p.add_argument("--unique-head", action="store_true",
                   help="把唯一标记放在 system 段**最前面**（整个前缀都独占，"
                        "用来测「前缀没被缓存过」时的表现）")
    p.add_argument("--label", default="", help="本次运行的标签（打印在表头）")
    p.add_argument("--json", default="", help="把逐条记录写到哪个 JSON 文件")
    p.add_argument("--dry-run", action="store_true",
                   help="只打印请求形状（system 长度 / 消息条数），不发请求")
    p.add_argument("--model", default="", help="覆盖 config.yaml 里的模型名")
    p.add_argument("--base-url", default="", help="覆盖 config.yaml 里的 base_url")
    p.add_argument("--max-tokens", type=int, default=0, help="覆盖 max_tokens（0=用配置值）")
    return p


# --------------------------------------------------------------------------
# 请求形状（跟生产一致）
# --------------------------------------------------------------------------

def build_tool_names() -> list[str]:
    """生产工具名清单（61 个内置工具；MCP 工具运行时才挂载，量测时不在）。

    ⚠️ 用 None 当 state/reminders/memory 建注册表 —— register 阶段只绑函数，
    不会真的去碰这些子系统（也不会写 data/）。
    """
    try:
        from app.engine.tools import build_default_tools
        reg = build_default_tools(state=None, reminders=None, memory=None)
        return list(reg.names())
    except Exception:  # noqa: BLE001
        return []


def build_session_block_text() -> str:
    """生产会话块（SKILL + 【记忆规则】 + 【可用工具列表】）—— 线上 system 段里占大头的那部分。"""
    from app.brain.brain_controller import MEMORY_RULE_BLOCK
    from app.brain.prompt_layer import build_session_block, build_tool_list
    skill = ""
    try:
        from app.brain.brain_controller import _get_skill_text
        skill = _get_skill_text(ROOT)
    except Exception:  # noqa: BLE001
        skill = ""
    names = build_tool_names()
    tool_list = build_tool_list("、".join(names)) if names else None
    return build_session_block(skill=skill, memory=MEMORY_RULE_BLOCK,
                               tool_list=tool_list)


def build_system_prompt(cfg, mode: str = "production",
                       session_block: bool = True,
                       pad_chars: int = 0,
                       unique_tag: str = "") -> str:
    """造出 system prompt。

    production：走生产拼装点（app.brain.llm_client._build_full_system_prompt）
      —— 人设 + style guide + 项目文档/skills + 防幻觉段 + 中文约束 + 长度策略
      + 说话方式 + 任务协议，全部逐字节固定。
    short：一段很短固定文本（用来测「前缀长度有没有门槛」）。
    """
    if mode == "short":
        return "你是一个桌面宠物助手，请用简短中文回答。"
    from app.brain.prompt_layer import (
        build_dynamic_block,
        build_system_prompt as assemble_system_prompt,
        build_tool_handbook,
        load_style_guide,
    )
    persona = getattr(cfg.character, "persona", "") or ""
    handbook = ""
    if session_block:
        block = build_session_block_text()
        if block:
            persona = persona + "\n\n" + block
        # ReAct / standard 后端会再拼一段【工具解锁】手册（轻量后端不拼）——
        # 线上默认后端是 standard，这里补上，保证 system 与生产一致。
        try:
            from app.engine.tools import build_default_tools
            reg = build_default_tools(state=None, reminders=None, memory=None)
            handbook = build_tool_handbook(registry=reg, level=None)
        except Exception:  # noqa: BLE001
            handbook = ""
    # 共享层拼装点（app/brain/prompt_layer.build_system_prompt）——
    # 与 app/brain/agent.py 的 assemble_system_prompt 走同一条路、同一批输入。
    system = assemble_system_prompt(
        persona=persona,
        style_guide=load_style_guide(),
        tool_handbook=handbook,
        dynamic=build_dynamic_block(),
    )
    if pad_chars > 0:
        # 共享前缀补长（中文 1 字 ≈ 0.5~0.6 token）
        system = system + "\n\n" + ("缓存块边界测量填充" * (pad_chars // 8 + 1))[:pad_chars]
    if unique_tag:
        if unique_tag.startswith("HEAD:"):
            # 标记放在**最前面** —— 整个前缀都是本次独有的（真正干净的对照）
            system = ("[本次测量唯一标记 " + unique_tag[5:] + "]\n\n" + system)
        else:
            system = system + "\n\n[本次测量唯一标记 " + unique_tag + "]"
    return system


def build_dynamic_context(query: str) -> str:
    """本轮易变上下文（时间戳那条独立 user 消息）—— 生产同一个函数。"""
    from app.brain.llm_client import build_dynamic_context as _bdc
    return _bdc(state=None, now=None, query=query)


def build_messages(*, user_text: str, dynamic: bool, order: str,
                   tail: str = "", fixed_prefix: str = "") -> list[dict]:
    """拼这一轮的 messages（system 由 LLMClient 自己加在最前）。

    prod：    [易变段, 主人原话]      —— 生产形状
    dyn-last： [主人原话, 易变段]      —— 易变内容放到最后（对照实验）

    fixed_prefix：插在 system 之后的**固定** user 消息（对照实验用）。
    """
    msgs: list[dict] = []
    if fixed_prefix:
        msgs.append({"role": "user", "content": fixed_prefix})
    dyn = build_dynamic_context(user_text) if dynamic else ""
    if order == "dyn-last":
        msgs.append({"role": "user", "content": user_text + tail})
        if dyn:
            msgs.append({"role": "user", "content": dyn})
    else:
        if dyn:
            msgs.append({"role": "user", "content": dyn})
        msgs.append({"role": "user", "content": user_text + tail})
    return msgs


def make_tail(units: int) -> str:
    """按「单元」补长（中文 1 字 ≈ 1 token 量级）。"""
    if units <= 0:
        return ""
    return ("\n\n" + _PAD_UNIT) * units


# --------------------------------------------------------------------------
# 统计
# --------------------------------------------------------------------------

#: provider 自带的**缓存底座**：实测未命中时也会报 128 个 token 的命中
#: （2026-10-28 的台账与本次实测一致）。算「真命中」时要把它扣掉。
FLOOR_TOKENS = 256


def summarize(records: list[dict]) -> dict:
    """把逐条记录汇成一份统计（命中率口径与生产台账一致）。

    两种命中口径都给出：
      - hit_requests      ：cached_tokens > 0（含 provider 的 128 底座）
      - real_hit_requests ：cached_tokens > FLOOR_TOKENS（真的复用了前缀）
    """
    total_prompt = sum(int(r.get("prompt_tokens") or 0) for r in records)
    total_cached = sum(int(r.get("cached_tokens") or 0) for r in records)
    hits = [r for r in records if int(r.get("cached_tokens") or 0) > 0]
    real_hits = [r for r in records
                 if int(r.get("cached_tokens") or 0) > FLOOR_TOKENS]
    hit_rate = (total_cached / total_prompt) if total_prompt else 0.0
    per_req = [r.get("hit_rate", 0.0) for r in records]
    return {
        "requests": len(records),
        "hit_requests": len(hits),
        "real_hit_requests": len(real_hits),
        "real_hit_request_ratio": (round(len(real_hits) / len(records), 4)
                                     if records else 0.0),
        "hit_request_ratio": (len(hits) / len(records)) if records else 0.0,
        "total_prompt_tokens": total_prompt,
        "total_cached_tokens": total_cached,
        "overall_hit_rate": round(hit_rate, 4),
        "avg_per_request_hit_rate": round(
            sum(per_req) / len(per_req), 4) if per_req else 0.0,
        "tool_call_requests": sum(1 for r in records if r.get("is_tool_call")),
        "errors": sum(1 for r in records if r.get("error")),
    }


def format_table(records: list[dict], label: str = "") -> str:
    """打印逐条表格：序号 / 间隔 / prompt / cached / 命中率 / 是否工具调用 / 耗时。"""
    head = (f"{'#':>3}  {'gap(s)':>7}  {'prompt_tok':>10}  {'cached_tok':>10}  "
            f"{'hit%':>7}  {'tool':>5}  {'req(s)':>6}")
    lines = []
    if label:
        lines.append(f"=== {label} ===")
    lines.append(head)
    lines.append("-" * len(head))
    for i, r in enumerate(records, 1):
        gap = r.get("gap_s")
        gap_s = "-" if gap is None else f"{gap:.1f}"
        pr = int(r.get("prompt_tokens") or 0)
        ca = int(r.get("cached_tokens") or 0)
        hr = 100.0 * ca / pr if pr else 0.0
        lines.append(
            f"{i:>3}  {gap_s:>7}  {pr:>10}  {ca:>10}  {hr:>6.1f}%  "
            f"{('yes' if r.get('is_tool_call') else 'no'):>5}  "
            f"{float(r.get('elapsed_s') or 0):>6.1f}")
    s = summarize(records)
    lines.append("-" * len(head))
    lines.append(
        f"真命中请求 {s['real_hit_requests']}/{s['requests']} "
        f"({100.0 * s['real_hit_request_ratio']:.1f}%)   "
        f"(含 128 底座的口径 {s['hit_requests']}/{s['requests']})   "
        f"cached 合计 {s['total_cached_tokens']} / prompt 合计 "
        f"{s['total_prompt_tokens']} = {100.0 * s['overall_hit_rate']:.1f}%   "
        f"工具调用 {s['tool_call_requests']} 条   错误 {s['errors']} 条")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 真请求
# --------------------------------------------------------------------------

async def _run(args, cfg, records: list[dict], unique_tag: str = "") -> None:
    from app.brain.llm_client import LLMClient
    import app.brain.llm_client as llm_client

    # 抓 provider 的**原始** usage（prompt_tokens_details.cached_tokens 等）——
    # 只在内存里，不打印 key，不落盘。
    raw_usage_sink: list[dict] = []
    _orig_normalize = llm_client.normalize_usage

    def _normalize_spy(raw):
        try:
            raw_usage_sink.append(json.loads(json.dumps(raw, ensure_ascii=False,
                                                       default=str)))
        except Exception:  # noqa: BLE001
            pass
        return _orig_normalize(raw)

    llm_client.normalize_usage = _normalize_spy

    llm_cfg = cfg.llm
    if args.base_url:
        llm_cfg = llm_cfg.model_copy(update={"base_url": args.base_url})
    if args.model:
        llm_cfg = llm_cfg.model_copy(update={"model": args.model})
    if args.max_tokens:
        llm_cfg = llm_cfg.model_copy(update={"max_tokens": int(args.max_tokens)})

    system_prompt = build_system_prompt(cfg, args.system,
                                   session_block=(args.session_block == "on"),
                                   pad_chars=args.system_pad_chars,
                                   unique_tag=unique_tag)
    client = LLMClient(llm_cfg, getattr(cfg.character, "persona", "") or "")
    # 用真实拼装出来的 system（生产拼装点），保证跟线上逐字节一致
    client.system_prompt = system_prompt

    tail = make_tail(args.tail_tokens)
    texts = list(DEFAULT_USER_TEXTS)
    # 多轮模式：维护一条增长的历史（模拟真实聊天）
    history: list[dict] = []
    prev_start = None
    group = 0

    try:
        for i in range(1, args.repeat + 1):
            # 间隔：even=每条都等；burst=每组第一条等（组内连打）
            if args.pattern == "burst":
                need_wait = ((i - 1) % max(1, args.burst_size) == 0) and i > 1
            else:
                need_wait = i > 1
            if need_wait and args.gap > 0:
                time.sleep(args.gap)
            if args.pattern == "burst":
                group = (i - 1) // max(1, args.burst_size) + 1

            if args.user == "same":
                user_text = texts[0]
            else:
                user_text = texts[(i - 1) % len(texts)]

            if args.turns > 1:
                # 多轮：维护一条增长的历史。
                #   history=drop（生产形状）：历史里只有主人原话 + 上一轮回复，
                #     易变段每轮重新生成、插在**最后一条主人原话前面**
                #     —— 与 app/brain/llm_client.inject_dynamic_context 一致。
                #   history=keep（对照）：易变段也留在历史里，位置固定不动。
                cur = build_messages(user_text=user_text, dynamic=(args.dynamic == "on"),
                                     order=args.order, tail=tail,
                                     fixed_prefix=args.fixed_prefix_msg)
                reply = {"role": "assistant", "content": "（上一轮回复）"}
                if args.history == "keep":
                    history = history + cur + [reply]
                    msgs = list(history)
                else:
                    # 生产形状：本轮的易变段 + 主人原话接到历史末尾
                    # （历史里**不含**易变段 —— 与 inject_dynamic_context 一致）
                    history = history + [cur[-1], reply]
                    msgs = list(history[:-2]) + cur
            else:
                msgs = build_messages(user_text=user_text,
                                      dynamic=(args.dynamic == "on"),
                                      order=args.order, tail=tail,
                                      fixed_prefix=args.fixed_prefix_msg)

            start = time.perf_counter()
            gap_s = None if prev_start is None else (start - prev_start)
            prev_start = start

            usage: dict = {}
            tool_calls: list = []
            content = ""
            err = ""
            try:
                async for ev, data in client.chat_stream_events(msgs):
                    if ev == "finish":
                        fin = data or {}
                        usage = fin.get("usage") or {}
                        tool_calls = fin.get("tool_calls") or []
                        content = fin.get("content") or ""
                    elif ev == "text":
                        content += str(data)
            except Exception as e:  # noqa: BLE001
                err = f"{type(e).__name__}: {e}"[:200]
            elapsed = time.perf_counter() - start

            raw = raw_usage_sink[-1] if raw_usage_sink else {}
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            cached_tokens = int(usage.get("cache_hit_tokens") or 0)
            rec = {
                "index": i,
                "group": group,
                "gap_s": None if gap_s is None else round(gap_s, 3),
                "prompt_tokens": prompt_tokens,
                "cached_tokens": cached_tokens,
                "hit_rate": round(usage.get("cache_hit_rate") or 0.0, 4),
                "cache_miss_tokens": int(usage.get("cache_miss_tokens") or 0),
                "cache_write_tokens": int(usage.get("cache_write_tokens") or 0),
                "completion_tokens": int(usage.get("completion_tokens") or 0),
                "is_tool_call": bool(tool_calls),
                "tool_names": [((tc or {}).get("name") or "") for tc in tool_calls],
                "elapsed_s": round(elapsed, 2),
                "error": err,
                "reply_chars": len(content or ""),
                # provider 原始 usage（只留跟缓存有关的字段，key 之类的东西本来就不在里面）
                "raw_usage": {
                    k: raw.get(k) for k in (
                        "prompt_tokens", "completion_tokens", "total_tokens",
                        "prompt_tokens_details", "prompt_cache_hit_tokens",
                        "prompt_cache_miss_tokens",
                    ) if k in raw
                },
            }
            records.append(rec)
            print(f"  [{i}/{args.repeat}] prompt={prompt_tokens} cached={cached_tokens} "
                  f"gap={'-' if gap_s is None else f'{gap_s:.1f}'}s "
                  f"tool={rec['is_tool_call']} {elapsed:.1f}s"
                  + (f" ERR={err}" if err else ""), flush=True)
    finally:
        llm_client.normalize_usage = _orig_normalize
        await client.close()


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    from app.core.config import load_config
    cfg = load_config(ROOT / "config.yaml")

    # ⚠️ key 只从内存里用：不打印、不写日志、不落盘、不进 --json 输出。
    from app.core.api_keys import is_placeholder_key
    key = getattr(cfg.llm, "api_key", "") or ""
    if not key or is_placeholder_key(key):
        print("config.yaml 里没有可用的 llm.api_key —— 测量需要真 key。", file=sys.stderr)
        return 2

    unique_tag = ""
    if args.unique:
        unique_tag = f"{time.strftime('%H%M%S')}-{os.getpid()}"
        if args.unique_head:
            unique_tag = "HEAD:" + unique_tag
    system_prompt = build_system_prompt(cfg, args.system,
                                   session_block=(args.session_block == "on"),
                                   pad_chars=args.system_pad_chars,
                                   unique_tag=unique_tag)
    digest = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()[:12]
    msgs = build_messages(user_text=DEFAULT_USER_TEXTS[0],
                          dynamic=(args.dynamic == "on"),
                          order=args.order,
                          tail=make_tail(args.tail_tokens))
    print("请求形状预览")
    print(f"  model            = {cfg.llm.model}")
    print(f"  base_url         = {cfg.llm.base_url}")
    print(f"  system 长度      = {len(system_prompt)} 字符 (sha256:{digest})")
    print(f"  system 段数      = {system_prompt.count(chr(10) * 2) + 1}")
    print(f"  消息条数(system 之外) = {len(msgs)}  roles={[m['role'] for m in msgs]}")
    print(f"  主人原话         = {msgs[-1]['content'][:40]!r}")
    if args.dry_run:
        print("  --dry-run：不发请求。")
        return 0

    label = args.label or (f"repeat={args.repeat} gap={args.gap}s pattern={args.pattern} "
                           f"user={args.user} system={args.system} dynamic={args.dynamic} "
                           f"order={args.order} turns={args.turns} tail={args.tail_tokens}")
    records: list[dict] = []
    print(f"\n开始测量：{label}\n", flush=True)
    asyncio.run(_run(args, cfg, records, unique_tag=unique_tag))

    print()
    print(format_table(records, label))
    print(f"\n台账（临时文件，未写仓库 data/）：{_LEDGER_PATH}")

    if args.json:
        out = {
            "label": label,
            "args": {k: v for k, v in vars(args).items()},
            "system_sha256": digest,
            "system_chars": len(system_prompt),
            "records": records,
            "summary": summarize(records),
        }
        Path(args.json).write_text(
            json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"逐条记录已写入：{args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
