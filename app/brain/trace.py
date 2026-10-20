"""Agent Trace：把每一次 Agent 运行的完整决策过程落盘到 SQLite。"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

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
        # RLock：_conn() 内部会加锁，而调用方常写成 `with self._lock, self._conn() as c:`
        # （同一线程重入 RLock 是允许的，普通 Lock 会死锁）
        self._lock = threading.RLock()
        # 长连接：以前每次操作都新建 sqlite3.connect()、用完 commit()+close()。
        # 流式输出时每个 token 一次 → 实测 6.5 ms/token（连接 + commit 的 fsync），
        # 纯白占 GUI 线程。现在复用一条连接，连接开销全省。
        self._conn_obj = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn_obj.row_factory = sqlite3.Row
        self._apply_pragmas()
        self._init_schema()
        # 当前 run 上下文（嵌套：trace() 上下文管理器）
        self._current_run: Optional[str] = None
        self._seq_counter: dict[str, int] = {}

    def _apply_pragmas(self) -> None:
        """WAL + synchronous=NORMAL。

        默认 synchronous=FULL 每次 commit 都 fsync，实测单次 insert+commit
        5.97 ms；换成 WAL + NORMAL 后同一操作 0.02 ms（约 300 倍）。
        trace 是本地可观测性数据，丢最后一次 commit 的风险可接受。
        WAL 顺带解决 Dashboard 进程读同一库时的写锁阻塞。
        """
        try:
            self._conn_obj.execute("PRAGMA journal_mode=WAL")
            self._conn_obj.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error as e:  # pragma: no cover - 文件系统不支持 WAL 时降级
            log.debug("trace: WAL pragma 失败，沿用默认模式: %s", e)

    @contextmanager
    def _conn(self):
        """复用同一条连接（自带锁）。

        原来是「每次操作一个独立连接，退出时关闭（避免文件句柄泄漏）」。
        改成复用后仍要 commit —— 否则长连接会一直持有读事务，
        把别的进程（Dashboard 也在读同一个库）挡在写锁外面。
        """
        with self._lock:
            c = self._conn_obj
            try:
                yield c
                c.commit()
            except Exception:
                c.rollback()
                raise

    def close(self) -> None:
        """关闭底层连接（进程退出 / 窗口关闭 / 测试清理时调用）。"""
        with self._lock:
            try:
                self._conn_obj.commit()
            except Exception:  # noqa: BLE001
                pass
            try:
                self._conn_obj.close()
            except Exception:  # noqa: BLE001
                pass

    def __del__(self):  # pragma: no cover - 兜底，防止句柄泄漏到进程退出
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass

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
                -- stats() 要按 kind 过滤 tool 事件；没有索引就是全表扫描
                CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
            """)

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

    _tool_calls: dict[str, int] = {}

    def list_runs(self, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                # rowid DESC 兜底：started_at 相同（同一次时钟 tick 内开的 run）
                # 时排序必须有确定顺序，否则「最新在前」会随机。
                "SELECT * FROM runs ORDER BY started_at DESC, rowid DESC LIMIT ?",
                (limit,)).fetchall()
            # 一次查完所有 run 的 events（原来每个 run 查一次 → 1+limit 次查询）
            ids = [r["id"] for r in rows]
            events_by_run: dict[str, list[dict]] = {}
            if ids:
                placeholders = ",".join("?" * len(ids))
                ev_rows = c.execute(
                    f"SELECT * FROM events WHERE run_id IN ({placeholders}) "
                    f"ORDER BY run_id, seq", ids,
                ).fetchall()
                for e in ev_rows:
                    events_by_run.setdefault(e["run_id"], []).append({
                        "seq": e["seq"], "kind": e["kind"],
                        "payload": json.loads(e["payload_json"] or "{}"),
                        "ts": e["ts"],
                    })
            out: list[dict] = []
            for r in rows:
                run = _row_to_run(r)
                # list_runs 也带 events，让 UI 一次渲染完整
                run["events"] = events_by_run.get(run["id"], [])
                out.append(run)
            return out

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
            tool_counter = self._tool_distribution(c)

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
                "tool_distribution": dict(tool_counter),
            }

    def _tool_distribution(self, c) -> dict:
        """工具调用分布（tool 事件按工具名计数）。

        原来是把**所有**历史 tool 事件读出来逐条 json.loads——事件表一大
        就变成全表扫描 + 全量 JSON 解析（stats 每次请求都调一次）。
        现在优先让 SQLite 用 json_extract 直接分组；SQLite 没编译 JSON1
        时退回原来的 Python 逻辑（结果一致，只是慢一点）。
        """
        from collections import Counter
        try:
            rows = c.execute(
                "SELECT COALESCE(json_extract(payload_json, '$.name'), "
                "json_extract(payload_json, '$.tool_name'), '?') AS nm, "
                "COUNT(*) AS n FROM events WHERE kind='tool' "
                "GROUP BY nm ORDER BY n DESC LIMIT 20"
            ).fetchall()
            return {r["nm"]: r["n"] for r in rows}
        except Exception:  # noqa: BLE001 - 没 JSON1 / payload 不是合法 JSON
            tool_rows = c.execute(
                "SELECT payload_json FROM events WHERE kind='tool'"
            ).fetchall()
            counter: Counter = Counter()
            for row in tool_rows:
                try:
                    p = json.loads(row["payload_json"] or "{}")
                    name = p.get("name") or p.get("tool_name") or "?"
                    counter[name] += 1
                except Exception:  # noqa: BLE001
                    pass
            return dict(counter.most_common(20))

    def memory_list(self, limit: int = 200) -> list[dict]:
        """从 memory.json 读出（仅用于 Dashboard）。"""
        return []

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
