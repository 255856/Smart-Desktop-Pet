# -*- coding: utf-8 -*-
"""**真 API 端到端计时**：跑一整轮真实对话，把时间花在哪摊开。

回答三个问题：
  1. 模型自己出一段回复要多久（这是下限）；
  2. 一次**工具调用**要多久（工具链/MCP 有没有卡）；
  3. 一整轮（模型 + 工具 + 最终回复）总共多久 —— 和「首句上屏」差多少。

它用**真实**的配置、真实的 MiniMax API、真实的工具注册表（含 MCP 桥接的真工具），
不 mock 任何东西。跑完会把这一轮的时间轴打出来。

用法：
    python scripts/measure_turn_latency.py                 # 默认一句闲聊
    python scripts/measure_turn_latency.py "今天好累"      # 指定用户输入
    python scripts/measure_turn_latency.py --timeout 300
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _bootstrap():
    """Qt 离屏 + 项目路径。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("message", nargs="?", default="今天好累啊")
    ap.add_argument("--timeout", type=float, default=300.0)
    args = ap.parse_args()

    _bootstrap()
    from PyQt5.QtWidgets import QApplication
    _app = QApplication.instance() or QApplication([])

    from app.core.config import load_config
    from app.brain.brain_controller import BrainController
    from app.engine.state import PetState
    from app.brain.langchain_agent import LangChainAgent
    from app.brain.prompt_layer import build_system_prompt, build_tool_handbook

    cfg = load_config(str(ROOT / "config.yaml"))
    state = PetState()
    reminders = None

    t0 = time.time()
    brain = BrainController(ROOT, cfg, state, reminders)
    setup_s = time.time() - t0
    print("[准备] BrainController 就绪 %.1fs（MCP 桥接 + 工具注册表）" % setup_s)

    system_prompt = build_system_prompt(
        persona=cfg.character.persona,
        style_guide="",
        tool_handbook=build_tool_handbook(),
        dynamic="",
        backend_ctx={"backend": cfg.brain.backend},
    )

    # MCP 是**后台**启动的 → 等它把工具桥接进来（否则这一轮测不到 MCP 工具）
    t_wait = time.time()
    while time.time() - t_wait < 30:
        names = set(brain.tool_registry.names() or [])
        if any(n.startswith("mcp_") for n in names):
            break
        time.sleep(0.5)
    print("[准备] 工具 %d 个（含 MCP %d 个）" % (
        len(brain.tool_registry.names() or []),
        len([n for n in (brain.tool_registry.names() or []) if n.startswith("mcp_")]),
    ))

    agent = LangChainAgent(
        llm_cfg=cfg.llm,
        registry=brain.tool_registry,
        persona=system_prompt,
        cfg=getattr(cfg, "langchain", None),
        cwd=str(ROOT),
        memory_store=brain.memory_client,
        level_getter=lambda: 30,
    )

    msgs = [{"role": "user", "content": args.message}]
    print("[提问] %s" % args.message)
    print("[开始] %s" % cfg.brain.backend)

    t0 = time.time()
    first_text = None
    first_tool = None
    events = []
    def _handle(kind, payload):
        nonlocal first_text, first_tool
        now = time.time() - t0
        if kind == "text":
            txt = str(payload[0] or "")
            if first_text is None and txt.strip():
                first_text = now
            events.append({"t": round(now, 2), "kind": "text",
                           "len": len(txt), "preview": txt[:40]})
        elif kind == "tool":
            name = payload[0] if payload else "?"
            if first_tool is None:
                first_tool = now
            events.append({"t": round(now, 2), "kind": "tool", "name": str(name)})
        elif kind == "meta":
            pp = payload[0] if payload else {}
            ev = {"event": (pp or {}).get("event") if isinstance(pp, dict) else None}
            if isinstance(pp, dict) and pp.get("usage"):
                u = pp["usage"]
                ev["usage"] = {
                    "prompt": u.get("prompt_tokens"),
                    "completion": u.get("completion_tokens"),
                    "cache_hit": (u.get("cache_hit_tokens")
                                  or u.get("cache_hit")),
                    "cache_miss": (u.get("cache_miss_tokens")
                                   or u.get("cache_miss")),
                }
            events.append(dict(ev, t=round(now, 2), kind="meta"))
        elif kind == "done":
            events.append({"t": round(now, 2), "kind": "done"})

    try:
        if hasattr(agent, "run_sync_streaming"):
            # standard 后端：**边跑边回调** → 时间轴才是真的
            agent.run_sync_streaming(
                msgs, on_event=lambda ev: _handle(ev[0], list(ev[1:])))
        else:
            for kind, *payload in agent.run_sync(msgs):
                _handle(kind, payload)
    except Exception as e:  # noqa: BLE001
        print("[异常] %s: %s" % (type(e).__name__, str(e)[:200]))

    total = time.time() - t0
    print()
    print("=== 这一轮的时间轴 ===")
    for e in events:
        extra = e.get("name") or e.get("preview") or ""
        print("  %6.2fs  %-5s %s" % (e["t"], e["kind"], extra))
    print()
    print("首句文字  : %s" % ("%.2fs" % first_text if first_text else "（没有）"))
    _reg = getattr(brain, "tool_registry", None)
    _lvl = 7
    try:
        _lvl = int(getattr(state, "level", 7) or 7)
    except Exception:
        _lvl = 7
    _tools = _reg.to_openai_for_level(_lvl) if _reg is not None else []
    if _tools:
        import json as _json
        _blob = _json.dumps(_tools, ensure_ascii=False)
        print("工具 schema: %d 个 / %d 字符（粗估 %d token）"
              % (len(_tools), len(_blob), len(_blob) // 2.2))
    print("首次工具  : %s" % ("%.2fs" % first_tool if first_tool else "（没有）"))
    print("整轮总耗时: %.2fs" % total)
    try:
        brain.stop_sidecar()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
