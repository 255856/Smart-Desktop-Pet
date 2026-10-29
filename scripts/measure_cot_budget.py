# -*- coding: utf-8 -*-
"""测量：MiniMax-M3 的英文 <think> CoT 会不会吃光 max_tokens（真实 API）。

背景
----
config.yaml 里 llm.max_tokens=1024。MiniMax-M3 把英文思维链以
<think>...</think> 的形式塞进 message.content，**CoT token 计入
max_tokens**。CoT 过长 → 预算在 CoT 中间用完 → 清洗管线把整条剥光 →
主人看到的是兜底话术而不是答案。

Stage 2.9 实测：20 条真实回复里有 1 条（long_question）整段没有可见内容。

阶段
----
  P1  生产 prompt × 1024（带工具）
  P2  生产 prompt × 4096（带工具）
  M1  录制 prompt × 1024（**无工具**）→ 复现 Stage 2.9 的「1/20」
  M2  录制 prompt × 4096（无工具）→ 更大预算的影响
  M3  生产 prompt × 1024（无工具）→ 生产视角的发生率
  M4  生产 prompt × 4096（无工具，只跑 M3 里危险的几条）
  P4  「不要思考」提示 → CoT 会不会变短
  P5  CoT-only 之后的「重问一次」→ 同预算 vs 大预算

用法
----
    python scripts/measure_cot_budget.py --measure
    python scripts/measure_cot_budget.py --measure --phases M1,M2
    python scripts/measure_cot_budget.py --dry-run

安全
----
- **API key 只从 config.yaml 读进内存**，不打印、不落盘、不进日志。
- 结果写到 scripts/measure_cot_budget_results.json（不含 key）。
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 实测流量**不写**用户的成本台账（data/token-usage.jsonl 是面板成本页的数据源）。
# 把台账指向临时文件；调用方自己设了 DESKTOP_PET_TOKEN_LEDGER 就尊重它。
# 见 app/brain/token_ledger.py 的 LEDGER_PATH_ENV。
os.environ.setdefault(
    "DESKTOP_PET_TOKEN_LEDGER",
    str(Path(tempfile.gettempdir()) / "desktop-pet-measure-cot-ledger.jsonl"),
)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

DEFAULT_CONFIG = ROOT / "config.yaml"
DEFAULT_RESULTS = ROOT / "scripts" / "measure_cot_budget_results.json"
CASES_FIXTURE = ROOT / "tests" / "fixtures" / "naturalness_cases.json"

#: 追问（重试）用的提示：只要答案，不要思考过程。
NUDGE_ANSWER_ONLY = (
    "（上一条回复的思考过程太长被截断了，主人什么都没看到。"
    "这次直接用简体中文给出最终答复，最多 3 句，不要输出思考过程。）"
)

#: 重问时放在 assistant 位置的标记（跟 app/brain/agent.py 里的常量一致）
MARK = "（上一条回复只有思考过程，没有可见内容）"

#: P4 用的提示：显式要求不输出思考过程。
NUDGE_NO_THINK = "（不要输出思考过程，直接给出答案。）"


def _parse_args(argv=None):
    p = argparse.ArgumentParser(description="测量 CoT 吃光 max_tokens 的真实情况")
    p.add_argument("--measure", action="store_true", help="确认要真调模型")
    p.add_argument("--dry-run", action="store_true", help="只打印计划，不发请求")
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--results", default=str(DEFAULT_RESULTS))
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--budgets", default="1024,4096")
    p.add_argument("--phases", default="M1,M2,M3,M4,P4,P5",
                   help="要跑的阶段（逗号分隔）")
    p.add_argument("--sleep", type=float, default=0.5, help="请求之间的间隔秒")
    p.add_argument("--reps", type=int, default=3, help="P6 每条问题重复几遍")
    return p.parse_args(argv)


# --------------------------------------------------------------------------
# 用例（Stage 2.9 的 20 条自然度评测集）
# --------------------------------------------------------------------------

def load_cases() -> list[dict]:
    data = json.loads(CASES_FIXTURE.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("cases") or []
    out = []
    for c in data:
        out.append({
            "id": c["id"],
            "input": c["input"],
            "recorded_prompt": "\n\n".join(c.get("system_prompt_fragments") or []),
        })
    return out


# --------------------------------------------------------------------------
# 生产 system prompt（唯一拼装点：app/brain/prompt_layer.py）
# --------------------------------------------------------------------------

def build_production_prompt(cfg) -> tuple[str, list | None, str]:
    """按生产路径拼 system prompt + 工具 schema + 本轮易变上下文。

    临时 root：记忆库 / 会话库 / 提醒库全在临时目录，data/ 下的真库不碰。
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from app.core.qt_compat import QApplication
    QApplication.instance() or QApplication(sys.argv[:1])

    from app.brain.brain_controller import BrainController
    from app.brain.prompt_layer import (
        build_dynamic_block,
        build_system_prompt,
        build_tool_handbook,
        build_turn_context,
        load_style_guide,
    )
    from app.engine.reminder import ReminderStore

    import record_naturalness as rn

    tmp_root = Path(tempfile.mkdtemp(prefix="cot_measure_"))
    rn._prepare_temp_root(ROOT, tmp_root)

    cfg2 = copy.deepcopy(cfg)
    cfg2.brain.memory_file = str(tmp_root / "memory.json")
    cfg2.brain.proactive_enabled = False
    if getattr(cfg2, "mcp", None) is not None:
        cfg2.mcp.enabled = False

    state = rn._load_live_state(ROOT)
    level = rn._current_level(state)
    state.level = level
    reminders = ReminderStore(tmp_root / "data" / "reminders.json")
    brain = BrainController(tmp_root, cfg2, state, reminders)

    persona_blob = cfg2.character.persona + "\n\n" + brain.chat_context()
    system_prompt = build_system_prompt(
        persona=persona_blob,
        style_guide=load_style_guide(),
        tool_handbook=build_tool_handbook(registry=brain.tool_registry, level=level),
        dynamic=build_dynamic_block(),
    )
    try:
        tools = brain.tool_registry.to_openai_for_level(level)
    except Exception:  # noqa: BLE001
        tools = brain.tool_registry.to_openai()
    # 本轮易变上下文（独立 user 消息，跟生产一致）
    turn_ctx = build_turn_context(state=state)
    return system_prompt, tools, turn_ctx


# --------------------------------------------------------------------------
# 清洗模拟：跟生产管线一致
# --------------------------------------------------------------------------

def split_cot(raw: str) -> tuple[str, str, bool]:
    """把原始 content 拆成 (CoT, 可见正文, CoT 是否闭合)。"""
    import re
    think = re.findall(r"<think>(.*?)</think>", raw, flags=re.DOTALL)
    closed = bool(think)
    cot = "\n".join(think)
    if not closed and "<think>" in raw:
        cot = raw.split("<think>", 1)[1]
    visible = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    visible = re.sub(r"<think>.*$", "", visible, flags=re.DOTALL)
    visible = re.sub(r"</?think>", "", visible, flags=re.IGNORECASE)
    return cot, visible.strip(), closed


def production_visible(raw: str) -> str:
    """模拟生产清洗：逐 chunk sanitize（is_final=False）→ 整段 sanitize（is_final=True）。

    这是**主人真正看到的东西**。
    """
    from app.brain.llm_client import sanitize_text
    chunks = [sanitize_text(c, is_final=False) for c in _split_chunks(raw)]
    return sanitize_text("".join(chunks), is_final=True)


def _split_chunks(raw: str, size: int = 24) -> list[str]:
    """把原文切成「像 SSE delta 那样的小块」（清洗是逐块做的，切法会影响结果）。"""
    return [raw[i:i + size] for i in range(0, len(raw), size)] or [""]


# --------------------------------------------------------------------------
# 一次真实请求
# --------------------------------------------------------------------------

async def one_request(client, url: str, headers: dict, model: str,
                      system_prompt: str, user_text: str,
                      max_tokens: int, tools, timeout: int,
                      history: list | None = None,
                      dynamic_ctx: str = "") -> dict:
    """发一次流式请求，返回测量结果（不含 key）。"""
    messages = [{"role": "system", "content": system_prompt}]
    if dynamic_ctx:
        messages.append({"role": "user", "content": dynamic_ctx})
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_text})
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.8,
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if tools:
        payload["tools"] = tools

    raw_parts: list[str] = []
    reasoning_parts: list[str] = []
    finish_reason = ""
    usage: dict = {}
    tool_calls = 0
    async with client.stream("POST", url, json=payload, headers=headers) as resp:
        if resp.status_code != 200:
            body = await resp.aread()
            return {"error": f"HTTP {resp.status_code}: {body[:200].decode('utf-8', 'ignore')}"}
        async for line in resp.aiter_lines():
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            if obj.get("usage"):
                usage.update(obj["usage"])
            for ch in obj.get("choices") or []:
                d = ch.get("delta") or {}
                if d.get("content"):
                    raw_parts.append(d["content"])
                rc = d.get("reasoning_content") or d.get("reasoning") or ""
                if rc:
                    reasoning_parts.append(rc)
                if d.get("tool_calls"):
                    tool_calls += 1
                if ch.get("finish_reason"):
                    finish_reason = ch["finish_reason"]
    raw = "".join(raw_parts)
    cot, visible, closed = split_cot(raw)
    prod = production_visible(raw)
    det = usage.get("completion_tokens_details") or {}
    # 修复用的判据（app/brain/llm_client.is_cot_only_reply）：
    # 原文里有 think 标签，且 think 块之外没有可见正文。
    from app.brain.llm_client import is_cot_only_reply
    # MiniMax 有时把工具调用当正文吐（<invoke ...>），清洗管线会剥掉它 ——
    # 那不是「CoT 吃光预算」，要单独标出来。
    text_tool_call = ("<invoke" in raw) or ("tool_call" in raw)
    return {
        "cot_only_fix": bool(is_cot_only_reply(raw)),
        "text_tool_call": text_tool_call,
        "max_tokens": max_tokens,
        "finish_reason": finish_reason,
        "raw_chars": len(raw),
        "cot_chars": len(cot),
        "cot_closed": closed,
        "visible_chars": len(visible),
        "prod_visible_chars": len(prod),
        # 主人口袋里有没有东西：既不是工具调用、也没剩下可见正文
        "cot_only": bool(raw.strip()) and not prod.strip() and tool_calls == 0,
        "tool_calls": tool_calls,
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": det.get("reasoning_tokens"),
        "cached_tokens": (usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
        "raw_head": raw[:200],
        "prod_head": prod[:200],
        "raw_full": raw[:8000],
    }


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

async def run(args) -> int:
    import httpx
    from app.core.api_keys import is_placeholder_key
    from app.core.config import load_config

    cfg = load_config(args.config)
    llm = cfg.llm
    api_key = str(llm.api_key or "").strip()
    if not api_key or is_placeholder_key(api_key):
        print("config.yaml 里没有可用的 llm.api_key，先填好再跑。")
        return 2

    base_url = str(llm.base_url or "").rstrip("/")
    model = str(llm.model or "")
    url = base_url + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",   # key 只在这里用，不落盘不打印
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    prod_prompt, tools, turn_ctx = build_production_prompt(cfg)
    cases = load_cases()
    budgets = [int(x) for x in args.budgets.split(",") if x.strip()]
    phases = {x.strip() for x in args.phases.split(",") if x.strip()}
    print(f"模型：{model}  生产 system：{len(prod_prompt)} 字符  工具：{len(tools or [])} 个  "
          f"用例：{len(cases)} 条  阶段：{sorted(phases)}")

    results: list[dict] = []
    timeout = httpx.Timeout(args.timeout)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async def go(phase: str, case: dict, prompt: str, user_text: str,
                     mt: int, history=None, tag: str = "",
                     with_tools: bool = True, dyn: bool = True):
            r = await one_request(client, url, headers, model, prompt, user_text,
                                  mt, tools if with_tools else None,
                                  args.timeout, history=history,
                                  dynamic_ctx=turn_ctx if dyn else "")
            r.update({"case": case["id"], "phase": phase, "tag": tag,
                      "max_tokens": mt})
            results.append(r)
            print(f"  [{phase}] {case['id']:<20} mt={mt:<5} "
                  f"finish={r.get('finish_reason'):<11} "
                  f"cot_only={str(r.get('cot_only')):<5} tc={r.get('tool_calls'):<3} "
                  f"cot_chars={r.get('cot_chars'):<6} vis={r.get('prod_visible_chars'):<5} "
                  f"rtok={r.get('reasoning_tokens'):<6} "
                  f"ctok={r.get('completion_tokens')}", flush=True)
            await asyncio.sleep(args.sleep)

        # --- M1：Stage 2.9 录制配置（录制 prompt + 无工具）× 基线预算 ---
        if "M1" in phases:
            print("\n== M1 录制 prompt x 1024 x 无工具（复现 Stage 2.9）==")
            for c in cases:
                await go("M1_stage29_notools_1024", c, c["recorded_prompt"],
                         c["input"], budgets[0], with_tools=False, dyn=False)

        # --- M2：同一批 × 更大预算 ---
        if "M2" in phases:
            print("\n== M2 录制 prompt x 4096 x 无工具（预算影响）==")
            for c in cases:
                await go("M2_stage29_notools_4096", c, c["recorded_prompt"],
                         c["input"], budgets[-1], with_tools=False, dyn=False)

        # --- M3：生产 prompt × 基线预算 × 无工具 ---
        if "M3" in phases:
            print("\n== M3 生产 prompt x 1024 x 无工具 ==")
            for c in cases:
                await go("M3_prod_notools_1024", c, prod_prompt, c["input"],
                         budgets[0], with_tools=False)

        # --- M4：生产 prompt × 更大预算 × 无工具（只跑 M3 里危险的几条）---
        if "M4" in phases:
            print("\n== M4 生产 prompt x 4096 x 无工具（只跑危险用例）==")
            m3 = [r for r in results if r["phase"] == "M3_prod_notools_1024"]
            risky = [r["case"] for r in m3
                     if r.get("cot_only") or r.get("finish_reason") == "length"
                     or (r.get("completion_tokens") or 0) >= budgets[0] * 0.9]
            print(f"  危险用例：{risky}")
            for cid in risky:
                c = next(x for x in cases if x["id"] == cid)
                await go("M4_prod_notools_4096", c, prod_prompt, c["input"],
                         budgets[-1], with_tools=False)

        # --- P4：显式要求「不要思考」---
        if "P4" in phases:
            print("\n== P4 不要思考（CoT 消耗最多的几条）==")
            base = [r for r in results
                    if r["phase"] in ("M1_stage29_notools_1024",
                                      "M3_prod_notools_1024")]
            worst = sorted(base, key=lambda r: -(r.get("reasoning_tokens") or 0))[:6]
            ids = {r["case"] for r in worst}
            for c in cases:
                if c["id"] in ids:
                    await go("P4_no_reasoning", c, prod_prompt,
                             c["input"] + "\n\n" + NUDGE_NO_THINK, budgets[0],
                             with_tools=False)

        # --- P5：CoT-only 之后的「重问一次」---
        if "P5" in phases:
            print("\n== P5 重问（同预算 vs 大预算）==")
            base = [r for r in results
                    if r["phase"] in ("M1_stage29_notools_1024",
                                      "M3_prod_notools_1024")]
            broken = [r["case"] for r in base if r.get("cot_only")]
            print(f"  CoT-only 用例：{broken}")
            for cid in broken:
                c = next(x for x in cases if x["id"] == cid)
                await go("P5_retry_same_budget", c, prod_prompt,
                         NUDGE_ANSWER_ONLY, budgets[0], tag="no_history",
                         with_tools=False)
                await go("P5_retry_big_budget", c, prod_prompt,
                         NUDGE_ANSWER_ONLY, budgets[-1], tag="no_history",
                         with_tools=False)

        # --- P6：预算尾部复现（最难的那条问题重复跑，看 CoT 吃光预算的频率）---
        if "P6" in phases:
            print("\n== P6 长问题重复跑（CoT 吃光预算的尾部）==")
            hard = [c for c in cases if c["id"] in ("long_question", "short_question",
                                                   "lookup_material", "bad_state_demand")]
            for c in hard:
                for i in range(args.reps):
                    await go("P6_repeat_1024", c, prod_prompt, c["input"],
                             budgets[0], with_tools=False)

        # --- P7：按**修复的真实形状**重问（保留原问题 + assistant 标记 + 追问）---
        if "P7" in phases:
            print("\n== P7 按修复形状重问（原问题保留）==")
            base = [r for r in results if r.get("phase") == "P6_repeat_1024"]
            broken = [r["case"] for r in base if r.get("cot_only_fix")]
            if not broken:
                broken = ["long_question", "lookup_material"]
            seen = []
            for cid in broken:
                if cid in seen:
                    continue
                seen.append(cid)
                c = next(x for x in cases if x["id"] == cid)
                hist = [{"role": "assistant", "content": MARK},
                        {"role": "user", "content": NUDGE_ANSWER_ONLY}]
                await go("P7_retry_same_budget", c, prod_prompt, c["input"],
                         budgets[0], history=hist, with_tools=False)
                await go("P7_retry_big_budget", c, prod_prompt, c["input"],
                         budgets[-1], history=hist, with_tools=False)

        # --- P8：端到端（真 AgentLoop + 真 API）：修复到底救没救回来 ---
        if "P8" in phases:
            print("\n== P8 端到端：真 AgentLoop 跑最容易出问题的长问题 ==")
            from app.brain.agent import AgentLoop
            from app.brain.llm_client import LLMClient
            from app.core.config import LLMConfig

            class _NoTools:
                def names(self):
                    return []

                def to_openai(self):
                    return []

                def execute(self, name, args):
                    return ""

            lcfg = LLMConfig(api_key=api_key, base_url=base_url, model=model,
                             stream=True, temperature=0.8,
                             max_tokens=cfg.llm.max_tokens, timeout=args.timeout)
            for cid in ("long_question", "short_question"):
                c = next(x for x in cases if x["id"] == cid)
                for i in range(args.reps):
                    lclient = LLMClient(lcfg, prod_prompt)
                    agent = AgentLoop(lclient, _NoTools(), max_turns=3)
                    seen = {"calls": 0}

                    async def _run():
                        out_events = []
                        async for ev in agent.run([
                                {"role": "user", "content": turn_ctx},
                                {"role": "user", "content": c["input"]}]):
                            if ev[0] == "meta" and isinstance(ev[1], dict):
                                out_events.append(ev[1])
                            if ev[0] == "text":
                                out_events.append({"text": ev[1]})
                        return out_events

                    try:
                        evs = await _run()
                    finally:
                        await lclient.close()
                    metas = [e for e in evs if e.get("event") == "cot_only_retry"]
                    texts = [e.get("text") for e in evs if e.get("text")]
                    # 主人**看到**的第一段文字（用来查 CoT 有没有漏进正文）
                    rec_first = texts[0] if texts else ""
                    rec = {"case": c["id"], "phase": "P8_live_agent",
                           "tag": "", "max_tokens": lcfg.max_tokens,
                           "cot_only_fix": bool(metas),
                           "cot_only": bool(metas),
                           "text_tool_call": False,
                           "tool_calls": 0,
                           "finish_reason": "",
                           "cot_chars": 0, "prod_visible_chars": len(texts[-1] or "") if texts else 0,
                           "visible_chars": len(texts[-1] or "") if texts else 0,
                           "completion_tokens": None, "reasoning_tokens": None,
                           "prompt_tokens": None,
                           "prod_head": (texts[-1] if texts else ""),
                           "first_text": rec_first,
                           "raw_head": "", "raw_full": ""}
                    results.append(rec)
                    print(f"  [P8] {c['id']} run{i} 重问={bool(metas)} "
                          f"最终回复={rec['prod_head'][:60]!r}", flush=True)
                    await asyncio.sleep(args.sleep)

    out = Path(args.results)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n结果写入 {out}")
    _print_table(results)
    return 0


def _print_table(results: list[dict]) -> None:
    from collections import defaultdict
    by_phase = defaultdict(list)
    for r in results:
        by_phase[r.get("phase")].append(r)
    print("\n| 阶段 | 请求数 | CoT-only(清洗口径) | CoT-only(修复判据) | 工具轮 | "
          "finish=length | CoT token 中位 | CoT token 最大 | completion token 合计 |")
    print("|---|---|---|---|---|---|---|---|---|")
    for phase, rs in by_phase.items():
        cot_only = sum(1 for r in rs if r.get("cot_only"))
        cot_fix = sum(1 for r in rs if r.get("cot_only_fix"))
        tool_turn = sum(1 for r in rs if r.get("tool_calls"))
        length = sum(1 for r in rs if r.get("finish_reason") == "length")
        toks = sorted((r.get("reasoning_tokens") or 0) for r in rs)
        med = toks[len(toks) // 2] if toks else 0
        mx = toks[-1] if toks else 0
        comp = sum(r.get("completion_tokens") or 0 for r in rs)
        print(f"| {phase} | {len(rs)} | {cot_only} | {cot_fix} | {tool_turn} | "
              f"{length} | {med} | {mx} | {comp} |")


def main(argv=None) -> int:
    args = _parse_args(argv)
    if args.dry_run:
        cases = load_cases()
        print(f"将跑 {len(cases)} 条用例；阶段 {args.phases}")
        for c in cases:
            print(f"  - {c['id']}: {c['input'][:36]}...")
        return 0
    if not args.measure:
        print("加 --measure 才会真调模型（防手滑烧钱）。")
        return 2
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
