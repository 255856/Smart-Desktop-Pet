# -*- coding: utf-8 -*-
"""测量：standard 后端（LangChain）上的「CoT 吃光 max_tokens」缺陷。

背景
----
config.yaml 里 brain.backend=standard —— 生产走的是 LangChain 标准后端
（app/brain/langchain_agent.py），**不是**手写 ReAct 那条。
Stage 2.10 的 CoT 预算修复只做在 ReAct 路径（app/brain/llm_client.py +
app/brain/agent.py）上，standard 路径完全没碰：
  · 不认「整轮只有思维链」（CoT 吃光输出预算 → 主人看不到答案）；
  · 不重问（ReAct 那条有 cot_only_retry 事件）；
  · 逐块清洗 → **跨块**的思维链会漏进正文。

这个脚本在**真 API**上量 standard 路径，每条记录：
  · 每次请求的原始输出（思维链 / 正文）；
  · 主人真正看到的文本（标准后端 yield 的 text 事件）；
  · 思维链有没有漏进正文；
  · 触发了几次请求、用了多少 completion token；
  · 两次请求的 system 消息是否逐字节相同（前缀缓存不失效）。

用法
----
    python scripts/measure_cot_standard.py --measure
    python scripts/measure_cot_standard.py --measure --reps 3 --budgets 256,1024
    python scripts/measure_cot_standard.py --measure --cases all
    python scripts/measure_cot_standard.py --dry-run

安全
----
- **API key 只从 config.yaml 读进内存**，不打印、不落盘、不进日志。
- 工具全部换成「假执行」（只记录调用，不真跑）—— 不开记事本、不跑脚本。
- checkpointer 关掉：不在 data/ 下写任何东西。
- 记忆库 / 会话库 / 提醒库全在临时目录。
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

DEFAULT_CONFIG = ROOT / "config.yaml"
DEFAULT_RESULTS = ROOT / "scripts" / "measure_cot_standard_results.json"
CASES_FIXTURE = ROOT / "tests" / "fixtures" / "naturalness_cases.json"

#: 思维链里最常见的英文碎片 —— 正文里出现它们就说明 CoT 漏了
COT_MARKERS = (
    "We need", "Let's ", "Need to ", "Should explain", "Now draft",
    "Let's think", "The user asks", "user wants", "We should",
)


def _parse_args(argv=None):
    p = argparse.ArgumentParser(description="测量 standard 后端的 CoT 预算缺陷")
    p.add_argument("--measure", action="store_true", help="确认要真调模型")
    p.add_argument("--dry-run", action="store_true", help="只打印计划，不发请求")
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--results", default=str(DEFAULT_RESULTS))
    p.add_argument("--reps", type=int, default=1, help="每条用例重复几遍")
    p.add_argument("--budgets", default="1024", help="输出预算，逗号分隔")
    p.add_argument("--cases", default="long_question",
                   help="用例 id，逗号分隔（'all' = 全部 20 条）")
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--sleep", type=float, default=0.5, help="请求之间的间隔秒")
    return p.parse_args(argv)


def load_cases() -> list[dict]:
    data = json.loads(CASES_FIXTURE.read_text(encoding="utf-8"))
    cases = data.get("cases") if isinstance(data, dict) else data
    return [{"id": c["id"], "input": c["input"]} for c in cases]


# --------------------------------------------------------------------------
# 生产环境准备（临时 root，不碰 data/ 下的真库）
# --------------------------------------------------------------------------

def prepare_env(cfg):
    """搭一套「生产等价」的环境：system prompt + 工具表 + 本轮易变上下文。

    - 记忆库 / 会话库 / 提醒库 → 临时目录
    - 工具的 **名字 / 描述 / 参数** 全保留（工具手册因此跟生产一致），
      只有执行函数换成「假执行」—— 测量时不该真开记事本 / 真跑脚本。
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from app.core.qt_compat import QApplication
    QApplication.instance() or QApplication(sys.argv[:1])

    from app.brain.brain_controller import BrainController
    from app.brain.prompt_layer import build_turn_context
    from app.engine.reminder import ReminderStore
    from app.engine.tools import Tool, ToolRegistry

    import record_naturalness as rn

    tmp_root = Path(tempfile.mkdtemp(prefix="cot_std_measure_"))
    rn._prepare_temp_root(ROOT, tmp_root)
    # token 台账重定向到临时目录：测量**不许**往 data/ 下写东西
    # （app/brain/token_ledger.py 支持这个环境变量）
    os.environ["DESKTOP_PET_TOKEN_LEDGER"] = str(tmp_root / "token-usage.jsonl")

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

    real = brain.tool_registry

    def _make_fake(name: str):
        def _fake(*args, **kwargs) -> str:
            return f"[测量脚本] 工具 {name} 未真正执行"
        return _fake

    reg = ToolRegistry()
    for tool in real._tools.values():
        reg.register(Tool(
            name=tool.name,
            description=tool.description,
            parameters=tool.parameters,
            fn=_make_fake(tool.name),
        ))

    persona = cfg2.character.persona + "\n\n" + brain.chat_context()
    turn_ctx = build_turn_context(state=state)
    return {"registry": reg, "persona": persona, "turn_ctx": turn_ctx,
            "level": level}


# --------------------------------------------------------------------------
# 记录原始输出的 ChatOpenAI
# --------------------------------------------------------------------------

class _Recorder:
    """收集每次模型请求的原始输出 / system 消息 / usage。"""

    def __init__(self):
        self.requests: list[dict] = []

    def start(self, messages) -> dict:
        rec = {"system": None, "raw": [], "usage": None, "tool_calls": 0,
               "finish": ""}
        for m in messages or []:
            if getattr(m, "type", "") == "system":
                rec["system"] = m.content
        self.requests.append(rec)
        return rec


_RECORDER: _Recorder = _Recorder()


def _install_recording_model() -> None:
    """把 langchain_openai.ChatOpenAI 换成会记录原始输出的子类。

    标准后端 _build_model() 里是 `from langchain_openai import ChatOpenAI`
    （调用时 import）→ 打补丁对生产路径同样生效。
    """
    import langchain_openai

    base = langchain_openai.ChatOpenAI

    class _RecordingChatOpenAI(base):  # type: ignore[misc,valid-type]
        async def _astream(self, messages, *args, **kwargs):
            rec = _RECORDER.start(messages)
            async for chunk in super()._astream(messages, *args, **kwargs):
                msg = getattr(chunk, "message", None)
                if msg is not None:
                    content = getattr(msg, "content", "") or ""
                    if isinstance(content, list):
                        content = "".join(
                            p.get("text", "") if isinstance(p, dict) else str(p)
                            for p in content)
                    if content:
                        rec["raw"].append(content)
                    if getattr(msg, "tool_call_chunks", None):
                        rec["tool_calls"] += 1
                    usage = getattr(msg, "usage_metadata", None)
                    if usage:
                        rec["usage"] = dict(usage)
                    meta = getattr(msg, "response_metadata", None) or {}
                    if meta.get("finish_reason"):
                        rec["finish"] = meta.get("finish_reason")
                yield chunk

        def _stream(self, messages, *args, **kwargs):
            rec = _RECORDER.start(messages)
            for chunk in super()._stream(messages, *args, **kwargs):
                msg = getattr(chunk, "message", None)
                if msg is not None:
                    content = getattr(msg, "content", "") or ""
                    if content:
                        rec["raw"].append(content)
                    if getattr(msg, "tool_call_chunks", None):
                        rec["tool_calls"] += 1
                yield chunk

    langchain_openai.ChatOpenAI = _RecordingChatOpenAI


# --------------------------------------------------------------------------
# 一次真实运行
# --------------------------------------------------------------------------

async def one_run(agent, question: str, turn_ctx: str) -> dict:
    """跑一轮 standard 后端，返回这一轮的全部可观测事实。"""
    from app.brain.llm_client import is_cot_only_reply

    before = len(_RECORDER.requests)
    events: list[tuple] = []
    msgs = [{"role": "user", "content": turn_ctx},
            {"role": "user", "content": question}]
    async for ev in agent.run(msgs):
        events.append(ev)
    reqs = _RECORDER.requests[before:]

    text = "".join(ev[1] for ev in events if ev[0] == "text")
    metas = [ev[1] for ev in events if ev[0] == "meta" and isinstance(ev[1], dict)]
    done = next((ev[1] for ev in events if ev[0] == "done"), "")

    raw_all = "".join("".join(r["raw"]) for r in reqs)
    per_req = []
    for r in reqs:
        raw_r = "".join(r["raw"])
        m = re.search(r"<think>(.*?)(?:</think>|$)", raw_r, re.S)
        cot_len = len(m.group(1)) if m else 0
        per_req.append({
            "finish": r.get("finish", ""),
            "raw_len": len(raw_r),
            "cot_len": cot_len,
            "vis_len": len(raw_r) - cot_len,
            "ctok": int((r.get("usage") or {}).get("output_tokens") or 0),
        })
    last_raw = "".join(reqs[-1]["raw"]) if reqs else ""
    cot_only = is_cot_only_reply(last_raw) if reqs else False
    leak_markers = [m for m in COT_MARKERS if m in text]
    completion = 0
    for r in reqs:
        u = r.get("usage") or {}
        completion += int(u.get("output_tokens") or u.get("completion_tokens") or 0)

    return {
        "requests": len(reqs),
        "raw_chars": len(raw_all),
        "raw_head": raw_all[:400],
        "last_raw_head": last_raw[:400],
        # 每个 delta 一条 —— 用来查「思维链是不是跨块漏进正文」
        "chunks": [c for r in reqs for c in r["raw"]][:400],
        "raw_full": raw_all,
        "cot_only": bool(cot_only),
        "visible_chars": len(text),
        "visible": text[:400],
        "done": str(done)[:200],
        "cot_leak_markers": leak_markers,
        "cot_leaked": bool(leak_markers) or ("<think>" in text),
        "retried": any(m.get("event") == "cot_only_retry" for m in metas),
        "metas": [m.get("event") for m in metas],
        "completion_tokens": completion,
        "tool_calls": sum(r["tool_calls"] for r in reqs),
        "systems": [r["system"] for r in reqs],
        "per_req": per_req,
    }


def _system_stable(systems: list) -> bool:
    """两次请求的 system 消息必须逐字节相同（前缀缓存不失效）。"""
    if len(systems) < 2:
        return True
    first = (systems[0] or "").encode("utf-8")
    return all((s or "").encode("utf-8") == first for s in systems[1:])


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

async def run(args) -> int:
    from app.core.api_keys import is_placeholder_key
    from app.core.config import load_config

    cfg = load_config(args.config)
    api_key = str(cfg.llm.api_key or "").strip()
    if not api_key or is_placeholder_key(api_key):
        print("config.yaml 里没有可用的 llm.api_key，先填好再跑。")
        return 2

    from app.brain.langchain_agent import LangChainAgent
    from app.brain.langchain_config import LangChainAgentConfig
    from app.core.config import LLMConfig

    env = prepare_env(cfg)
    _install_recording_model()

    llm_cfg = LLMConfig(
        api_key=api_key,
        base_url=str(cfg.llm.base_url),
        model=str(cfg.llm.model),
        stream=True,
        temperature=float(cfg.llm.temperature),
        max_tokens=int(cfg.llm.max_tokens),
        timeout=int(cfg.llm.timeout),
    )

    cases = load_cases()
    if args.cases.strip() != "all":
        want = {c.strip() for c in args.cases.split(",") if c.strip()}
        cases = [c for c in cases if c["id"] in want]
    budgets = [int(x) for x in args.budgets.split(",") if x.strip()]

    print(f"模型：{llm_cfg.model}  工具：{len(env['registry'].names())} 个  "
          f"用例：{[c['id'] for c in cases]}  预算：{budgets}")

    results: list[dict] = []
    for budget in budgets:
        for case in cases:
            for rep in range(args.reps):
                llm_cfg.max_tokens = budget
                agent = LangChainAgent(
                    llm_cfg=llm_cfg,
                    registry=env["registry"],
                    persona=env["persona"],
                    cfg=LangChainAgentConfig(enable_checkpointer=False),
                    level_getter=lambda: env["level"],
                )
                rec = await one_run(agent, case["input"], env["turn_ctx"])
                rec.update({"case": case["id"], "rep": rep, "max_tokens": budget})
                rec["system_stable"] = _system_stable(rec.pop("systems"))
                results.append(rec)
                print(f"  [{budget}] {case['id']:<16} #{rep} "
                      f"请求={rec['requests']} "
                      f"cot_only={str(rec['cot_only']):<5} "
                      f"重问={str(rec['retried']):<5} "
                      f"泄漏={str(rec['cot_leaked']):<5} "
                      f"可见={rec['visible_chars']:<5} "
                      f"ctok={rec['completion_tokens']}", flush=True)
                try:
                    agent.close()
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(args.sleep)

    out = Path(args.results)
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    print(f"\n结果写入 {out}")
    _print_table(results)
    return 0


def _print_table(results: list[dict]) -> None:
    from collections import defaultdict
    by = defaultdict(list)
    for r in results:
        by[r.get("max_tokens")].append(r)
    print("\n| 预算 | 轮数 | CoT-only | 重问 | 思维链泄漏 | 可见为空 | "
          "请求总数 | completion token |")
    print("|---|---|---|---|---|---|---|---|")
    for budget, rs in sorted(by.items()):
        print(f"| {budget} | {len(rs)} | "
              f"{sum(1 for r in rs if r['cot_only'])} | "
              f"{sum(1 for r in rs if r['retried'])} | "
              f"{sum(1 for r in rs if r['cot_leaked'])} | "
              f"{sum(1 for r in rs if not r['visible'].strip())} | "
              f"{sum(r['requests'] for r in rs)} | "
              f"{sum(r['completion_tokens'] for r in rs)} |")


def main(argv=None) -> int:
    args = _parse_args(argv)
    if args.dry_run:
        print(f"将跑 {args.reps} 遍 / 预算 {args.budgets} / 用例 {args.cases}")
        return 0
    if not args.measure:
        print("加 --measure 才会真调模型（防手滑烧钱）。")
        return 2
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
