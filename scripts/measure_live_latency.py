# -*- coding: utf-8 -*-
"""真实链路计时：LLM 一轮要多久 / voice MCP 卡在哪。

回答两个问题：
  1. MiniMax-M3 出一段回复**本身**要多久（这是下限，比不了就别比）；
  2. voice MCP server 的 tools/call 会不会卡（单线程 server 一次调用能占多久）。

用法：
    python scripts/measure_live_latency.py            # 全测
    python scripts/measure_live_latency.py --llm-only
"""
from __future__ import annotations

import argparse
import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _cfg():
    sys.path.insert(0, str(ROOT))
    from app.core.config import load_config
    return load_config(str(ROOT / "config.yaml"))


def measure_llm() -> dict:
    """真 API：一轮闲聊要多久。"""
    import urllib.request

    cfg = _cfg()
    key = (cfg.llm.api_key or "").strip()
    if not key:
        return {"error": "没有 api_key"}
    payload = {
        "model": cfg.llm.model,
        "messages": [
            {"role": "system", "content": "你是桌面宠物桌宠，说话简短自然。"},
            {"role": "user", "content": "今天好累啊"},
        ],
        "max_tokens": 200,
        "stream": False,
    }
    t0 = time.time()
    req = urllib.request.Request(
        cfg.llm.base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + key},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, str(e)[:120])}
    dt = time.time() - t0
    u = body.get("usage") or {}
    text = ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    return {
        "seconds": round(dt, 2),
        "model": cfg.llm.model,
        "prompt_tokens": u.get("prompt_tokens"),
        "completion_tokens": u.get("completion_tokens"),
        "cached": (u.get("prompt_tokens_details") or {}).get("cached_tokens"),
        "reply": text[:60],
    }


def measure_voice_server() -> dict:
    """真 MCP server：tools/call 会不会卡。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.mcp.voice_server"],
        cwd=str(ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, text=True, encoding="utf-8", env=env,
    )

    # Windows 的管道不能用 select —— 用**后台读线程 + 队列**（跨平台）。
    import queue
    import threading
    q: queue.Queue = queue.Queue()

    def _reader():
        for line in proc.stdout:
            q.put(line)
        q.put(None)

    threading.Thread(target=_reader, daemon=True).start()

    def call(method, params, timeout=90):
        proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}) + "\n")
        proc.stdin.flush()
        t0 = time.time()
        try:
            line = q.get(timeout=timeout)
        except queue.Empty:
            return {"timeout": True, "seconds": round(time.time() - t0, 2)}
        if line is None:
            return {"eof": True, "seconds": round(time.time() - t0, 2)}
        line = proc.stdout.readline()
        return {"seconds": round(time.time() - t0, 2),
                "resp": json.loads(line) if line.strip() else None}

    out = {}
    try:
        out["initialize"] = call("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "clientInfo": {"name": "probe", "version": "1"},
        }, timeout=20)
        out["tools_list"] = call("tools/list", {}, timeout=20)
        out["status"] = call("tools/call",
                             {"name": "status", "arguments": {}}, timeout=60)
    finally:
        try:
            proc.kill()
        except Exception:
            pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm-only", action="store_true")
    args = ap.parse_args()
    print("=== 真实 LLM（MiniMax）===")
    print(json.dumps(measure_llm(), ensure_ascii=False, indent=2))
    if not args.llm_only:
        print("\n=== 真实 voice MCP server ===")
        print(json.dumps(measure_voice_server(), ensure_ascii=False, indent=2)[:2000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
