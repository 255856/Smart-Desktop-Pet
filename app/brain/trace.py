"""Agent Trace：把每一次 Agent 运行的完整决策过程落盘到 SQLite。

设计：
    - 每个 run（用户一次消息 → 完整回复）一条 Run 记录
    - 每条 step（plan/reflection/tool/text）一条 Event 记录
    - 数据可回放、可分析、可统计

数据库结构（data/traces.db）：
    runs:
        id              TEXT PRIMARY KEY
        goal            TEXT       -- 用户原始消息
        mode            TEXT       -- 'react' | 'single'
        final_answer    TEXT
        started_at      REAL
        finished_at     REAL
        total_steps     INTEGER
        tool_calls      INTEGER
        status          TEXT       -- 'success' | 'failed' | 'cancelled'

    events:
        id              INTEGER PRIMARY KEY AUTOINCREMENT
        run_id          TEXT
        seq             INTEGER     -- 同一 run 内顺序
        kind            TEXT        -- 'plan'|'reflection'|'tool'|'text'|'error'|'meta'
        payload_json    TEXT        -- 事件 payload JSON
        ts              REAL
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger(__name__)


@dataclass
class TraceEvent:
    kind: str
    payload: dict
    ts: float = field(default_factory=time.time)
    seq: int = 0


@dataclass
class RunRecord:
    id: str
    goal: str
    mode: str
    final_answer: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    total_steps: int = 0
    tool_calls: int = 0
    status: str = "running"
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "goal": self.goal, "mode": self.mode,
            "final_answer": self.final_answer,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "total_steps": self.total_steps,
            "tool_calls": self.tool_calls,
            "status": self.status,
            "meta": self.meta,
        }


class TraceRecorder:
    """线程安全的 SQLite trace recorder。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()
        # 当前 run 上下文（嵌套：trace() 上下文管理器）
        self._current_run: Optional[str] = None
        self._seq_counter: dict[str, int] = {}

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        return c

    def _init_schema(self) -> None:
        with self._lock, self._conn() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    goal TEXT,
                    mode TEXT,
                    final_answer TEXT,
                    started_at REAL,
                    finished_at REAL,
                    total_steps INTEGER DEFAULT 0,
                    tool_calls INTEGER DEFAULT 0,
                    status TEXT,
                    meta_json TEXT
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT,
                    seq INTEGER,
                    kind TEXT,
                    payload_json TEXT,
                    ts REAL,
                    FOREIGN KEY(run_id) REFERENCES runs(id)
                );
                CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, seq);
            """)

    # ---- run 生命周期 ----
    def begin_run(self, goal: str, mode: str = "react",
                  meta: Optional[dict] = None) -> str:
        rid = f"r{uuid.uuid4().hex[:12]}"
        rec = RunRecord(id=rid, goal=goal, mode=mode, meta=meta or {})
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO runs (id, goal, mode, started_at, status, meta_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (rid, goal, mode, rec.started_at, "running",
                 json.dumps(rec.meta, ensure_ascii=False)),
            )
        self._current_run = rid
        self._seq_counter[rid] = 0
        log.debug("trace begin_run: %s goal=%r", rid, goal[:60])
        return rid

    def end_run(self, rid: str, final_answer: str = "",
                status: str = "success") -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE runs SET finished_at=?, final_answer=?, status=?, "
                "total_steps=?, tool_calls=? WHERE id=?",
                (time.time(), final_answer, status,
                 self._seq_counter.get(rid, 0),
                 self._tool_calls.get(rid, 0),
                 rid),
            )
        if rid == self._current_run:
            self._current_run = None
        log.debug("trace end_run: %s status=%s", rid, status)

    def record(self, rid: Optional[str], kind: str,
               payload: dict) -> None:
        if rid is None:
            return
        with self._lock:
            self._seq_counter[rid] = self._seq_counter.get(rid, 0) + 1
            seq = self._seq_counter[rid]
            if kind == "tool":
                self._tool_calls[rid] = self._tool_calls.get(rid, 0) + 1
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO events (run_id, seq, kind, payload_json, ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (rid, seq, kind,
                 json.dumps(payload, ensure_ascii=False, default=str),
                 time.time()),
            )

    # ---- 查询 ----
    _tool_calls: dict[str, int] = {}

    def list_runs(self, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?",
                (limit,)).fetchall()
            return [_row_to_run(r) for r in rows]

    def get_run(self, rid: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM runs WHERE id=?",
                            (rid,)).fetchone()
            if not row:
                return None
            run = _row_to_run(row)
            events = c.execute(
                "SELECT * FROM events WHERE run_id=? ORDER BY seq",
                (rid,)).fetchall()
            run["events"] = [
                {"seq": e["seq"], "kind": e["kind"],
                 "payload": json.loads(e["payload_json"] or "{}"),
                 "ts": e["ts"]}
                for e in events
            ]
            return run

    def stats(self) -> dict:
        """统计信息：run 数 / 成功率 / 平均步骤 / 平均耗时 / 工具调用分布。"""
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            success = c.execute(
                "SELECT COUNT(*) FROM runs WHERE status='success'").fetchone()[0]
            avg_steps = c.execute(
                "SELECT AVG(total_steps) FROM runs WHERE status='success'"
            ).fetchone()[0] or 0
            tool_rows = c.execute(
                "SELECT payload_json FROM events WHERE kind='tool'"
            ).fetchall()
            from collections import Counter
            tool_counter: Counter = Counter()
            for row in tool_rows:
                try:
                    p = json.loads(row["payload_json"] or "{}")
                    name = p.get("name") or p.get("tool_name") or "?"
                    tool_counter[name] += 1
                except Exception:  # noqa: BLE001
                    pass

            def _count_status(s: str) -> int:
                return c.execute(
                    "SELECT COUNT(*) FROM runs WHERE status=?", (s,)
                ).fetchone()[0]

            failed = _count_status("failed")
            running = _count_status("running")
            cancelled = _count_status("cancelled")
            avg_duration = c.execute(
                "SELECT AVG(finished_at - started_at) FROM runs "
                "WHERE finished_at IS NOT NULL AND started_at IS NOT NULL"
            ).fetchone()[0] or 0
            total_tool_calls = c.execute(
                "SELECT COUNT(*) FROM events WHERE kind='tool'"
            ).fetchone()[0]
            last_active_at = c.execute(
                "SELECT MAX(started_at) FROM runs"
            ).fetchone()[0]
            return {
                "total_runs": total,
                "success_runs": success,
                "failed_runs": failed,
                "running_runs": running,
                "cancelled_runs": cancelled,
                "success_rate": round(success / total, 3) if total else 0,
                "avg_steps": round(avg_steps, 2),
                "avg_duration": round(avg_duration, 2),
                "total_tool_calls": total_tool_calls,
                "last_active_at": last_active_at,
                "tool_distribution": dict(tool_counter.most_common(20)),
            }

    def memory_list(self, limit: int = 200) -> list[dict]:
        """从 memory.json 读出（仅用于 Dashboard）。"""
        # 这里不直接读 memory，单独从 store 拿
        return []

    # ---- context manager ----
    def trace(self, goal: str, mode: str = "react",
              meta: Optional[dict] = None) -> "_TraceCtx":
        return _TraceCtx(self, goal, mode, meta)


class _TraceCtx:
    def __init__(self, rec: TraceRecorder, goal: str,
                 mode: str, meta: Optional[dict]):
        self.rec = rec
        self.goal = goal
        self.mode = mode
        self.meta = meta or {}
        self.rid: Optional[str] = None
        self.final_answer = ""
        self.status = "success"

    def __enter__(self) -> "_TraceCtx":
        self.rid = self.rec.begin_run(self.goal, self.mode, self.meta)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc is not None:
            self.status = "failed"
            self.final_answer = f"异常：{exc}"
        self.rec.end_run(self.rid or "", self.final_answer, self.status)

    def event(self, kind: str, payload: dict) -> None:
        self.rec.record(self.rid, kind, payload)

    def set_final(self, text: str) -> None:
        self.final_answer = text


def _row_to_run(r: sqlite3.Row) -> dict:
    return {
        "id": r["id"], "goal": r["goal"], "mode": r["mode"],
        "final_answer": r["final_answer"] or "",
        "started_at": r["started_at"],
        "finished_at": r["finished_at"],
        "total_steps": r["total_steps"] or 0,
        "tool_calls": r["tool_calls"] or 0,
        "status": r["status"],
        "meta": json.loads(r["meta_json"] or "{}"),
    }
