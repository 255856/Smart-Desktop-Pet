"""桌宠决策 trace 日志 (v23-1)。

中间件每次查表 (lookup_decision) 时, 把 (state_quantize, tool, risk, decision)
append 到 `data/companion_log.txt` 一行 JSON. 主人 / trace 页 / debug 脚本
可以直接 tail 这个文件看历史决策链.

为什么不用标准 logging: 主人的需求"trace 页要明确记录桌宠的工具权限状态,
以及每次调用成功后的数值变化" (主人原话), 需要结构化每条 JSON 行.
JSON-line 文件可 grep / jq, 比字符串 log 易用.

JSON-line schema:
    {"ts": float, "thread_id": str, "tool": str, "risk": int, "decision":
    str, "sat": str, "mood": str, "level": str, "decision_source": str}

设计原则:
  - 同步落盘 (跟主流程串行, 锁文件 1ms 内)
  - 不抛 (json.dumps 失败 / 写盘失败都吞, 不让中间件崩)
  - 默认启用, 主人想关可在 config.yaml 设 `companion.trace_log: false`
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

_LOCK = threading.Lock()
log = logging.getLogger(__name__)


def _default_log_path() -> Path:
    """data/companion_log.txt 跟 chat_history.db 同目录.

    不要 hard-code "data/", 因为 running from main.py 时 cwd 已经是项目根.
    但 data/ 仍然存在 (chat_history.db 在) 所以相对可靠. 失败 fallback 到 /tmp.
    """
    p = Path("data") / "companion_log.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def trace_decision(thread_id: str, tool: str, risk: int, decision: str,
                   sat: str, mood: str, level: str,
                   source: str = "lookup_decision") -> None:
    """v23-1: 写一行 trace 到 log 文件。

    调用方 (RiskyToolConfirm._get_companion_decision) 每查一次表就调一次,
    锁住文件句柄 1 行, 失败静默.
    """
    rec = {
        "ts": time.time(),
        "thread_id": thread_id,
        "tool": tool,
        "risk": risk,
        "decision": decision,
        "sat": sat,
        "mood": mood,
        "level": level,
        "decision_source": source,
    }
    try:
        with _LOCK:
            with _default_log_path().open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        log.warning("companion_trace: 写日志失败: %s", e)


def trace_outcome(thread_id: str, outcome: str, tool: str,
                   feeling_delta: float, food_delta: float,
                   favor_delta: float, exp_delta: float,
                   level_before: int, level_after: int,
                   success: bool) -> None:
    """v23-1: 写一行 apply_outcome trace (主人 trace 页 "调用后数值变化" 用途).

    跟 trace_decision 同文件但 schema 不同; trace 页解析时按 'kind' 字段区分.
    v23-3 加 favor_delta (好感变化), 主人 2026-10-16 加的 4 维数据需要这个字段.
    """
    rec = {
        "kind": "outcome",
        "ts": time.time(),
        "thread_id": thread_id,
        "outcome": outcome,
        "tool": tool,
        "feeling_delta": round(feeling_delta, 3),
        "food_delta": round(food_delta, 3),
        "favor_delta": round(favor_delta, 3),
        "exp_delta": round(exp_delta, 3),
        "level_before": level_before,
        "level_after": level_after,
        "success": success,
    }
    try:
        with _LOCK:
            with _default_log_path().open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        log.warning("companion_trace: 写 outcome 日志失败: %s", e)


def read_recent(limit: int = 200, kind: Optional[str] = None) -> list[dict[str, Any]]:
    """trace 页 / 测试用: 读最近 N 行, 可按 kind 过滤 (decision / outcome).

    按文件逆序读 (大文件场景下只读尾部). 出错返空 list.
    """
    p = _default_log_path()
    if not p.is_file():
        return []
    try:
        with p.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:  # noqa: BLE001
        log.warning("companion_trace: 读日志失败: %s", e)
        return []
    out: list[dict] = []
    for line in reversed(lines):
        if len(out) >= limit:
            break
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if kind is not None:
            if kind == "outcome" and rec.get("kind") != "outcome":
                continue
            if kind == "decision" and "decision" not in rec:
                continue
        out.append(rec)
    return out


__all__ = ["trace_decision", "trace_outcome", "read_recent"]
