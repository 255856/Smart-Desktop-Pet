"""桌宠决策 trace 日志：每次查表（lookup_decision）把结果写一行 JSON 到 data/companion_log.txt。

JSON-line 文件可 grep / jq，比字符串 log 易用。
trace 页直接 tail 这个文件看历史决策链。

设计原则：
  - 同步落盘（锁文件，毫秒级开销）
  - 不抛（json.dumps / 写盘失败都吞，不让中间件崩）
  - 默认启用，可在 config.yaml 设 `companion.trace_log: false` 关闭
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

# 日志轮转阈值：超过就改名成 .1（只保留一份备份）
_MAX_LOG_BYTES = 8 * 1024 * 1024


def _default_log_path() -> Path:
    """data/companion_log.txt，跟 chat_history.db 同目录。失败 fallback 到 /tmp。

    目录只在**不存在**时才建（原来每次调用都 mkdir 一遍）。
    """
    p = Path("data") / "companion_log.txt"
    parent = p.parent
    try:
        if not parent.is_dir():
            parent.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return p


def _rotate_if_needed(p: Path) -> None:
    """日志超过阈值就轮转（保留一份 .1 备份）。

    日志文件只增不减——不轮转的话，跑几个月能涨到几百 MB。
    调用方必须持有 _LOCK。
    """
    try:
        if p.stat().st_size <= _MAX_LOG_BYTES:
            return
        backup = p.with_suffix(p.suffix + ".1")
        if backup.exists():
            backup.unlink()
        p.replace(backup)
    except Exception:  # noqa: BLE001
        pass


def _append_record(rec: dict) -> None:
    """追加一行 JSON（调用方已持有 _LOCK）。"""
    p = _default_log_path()
    _rotate_if_needed(p)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def trace_decision(thread_id: str, tool: str, risk: int, decision: str,
                   sat: str, mood: str, level: str,
                   favor: str = "MID",
                   source: str = "lookup_decision") -> None:
    """写一行 decision trace 到 log 文件。失败静默。"""
    rec = {
        "ts": time.time(),
        "thread_id": thread_id,
        "tool": tool,
        "risk": risk,
        "decision": decision,
        "sat": sat,
        "mood": mood,
        "level": level,
        "favor": favor,
        "decision_source": source,
    }
    try:
        with _LOCK:
            _append_record(rec)
    except Exception as e:  # noqa: BLE001
        log.warning("companion_trace: 写日志失败: %s", e)


def trace_outcome(thread_id: str, outcome: str, tool: str,
                   feeling_delta: float, food_delta: float,
                   favor_delta: float, exp_delta: float,
                   level_before: int, level_after: int,
                   success: bool) -> None:
    """写一行 outcome trace，跟 trace_decision 同文件，schema 不同。

    trace 页解析时按 'kind' 字段区分。
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
            _append_record(rec)
    except Exception as e:  # noqa: BLE001
        log.warning("companion_trace: 写 outcome 日志失败: %s", e)


def _read_tail_lines(p: Path, want: int) -> tuple[list[str], bool]:
    """读文件尾部最多 want 行。返回 (行列表, 是否已经读到文件头)。

    不全量 readlines()——日志只增不减（实测已到 2.87 MB / 12,872 行），
    整读进来既占内存又慢。这里从文件尾往回读块，凑够 want 行就停。
    """
    chunk = 64 * 1024
    with p.open("rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        if size <= 0:
            return [], True
        buf = b""
        pos = size
        parts: list[bytes] = []
        while True:
            step = min(chunk, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf
            parts = buf.split(b"\n")
            if pos > 0:
                parts = parts[1:]      # 首段被截断，不是完整行
            if parts and parts[-1] == b"":
                parts.pop()             # 文件以换行结尾产生的空段
            if len(parts) >= want or pos <= 0:
                break
    return [x.decode("utf-8", errors="replace") for x in parts], pos <= 0


def read_recent(limit: int = 200, kind: Optional[str] = None) -> list[dict[str, Any]]:
    """trace 页 / 测试用: 读最近 N 行, 可按 kind 过滤 (decision / outcome).

    按文件逆序读 (大文件场景下只读尾部). 出错返空 list.
    """
    p = _default_log_path()
    if not p.is_file():
        return []
    want = max(limit, 32)
    while True:
        try:
            lines, exhausted = _read_tail_lines(p, want)
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
        if len(out) >= limit or exhausted:
            return out
        # 过滤后不够数 → 窗口往前扩（最多扩到文件头）
        want *= 4


__all__ = ["trace_decision", "trace_outcome", "read_recent"]
