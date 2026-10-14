"""会话(聊天 session)持久层 —— 对齐主流 IDE agent 的「会话列表 / 切换 / 续接」能力。

数据模型:
  SessionInfo
    - session_id:  短 UUID(主键,UI 拿来当列表显示 id)
    - thread_id:   LangGraph checkpointer 的 thread_id(同一个 session 的
                   所有 checkpoint 都挂在它下面)
    - title:       用户起的名 / 默认「会话 1」
    - created_at:  unix 时间戳
    - last_used_at: 最近一次对话结束时间
    - message_count: 已发生的 user message 数(轻量统计)

存储:
  SQLite 单表,DB 默认放 data/pet_sessions.db。

调用约定:
  - 新建对话:store.create() -> SessionInfo;UI 用 session.thread_id 构造 agent
  - 续接对话:store.get(session_id) -> SessionInfo;UI 复用 session.thread_id
  - 列出:store.list_all() -> [SessionInfo, ...] 按 last_used_at desc
  - 删除:store.delete(session_id) —— 同时清掉 LangGraph checkpointer 里
          对应 thread_id 的所有 checkpoint 行(否则孤儿数据撑大 DB)

不在本模块做的事:
  - 不读 LangGraph checkpoint(那是 LangChainAgent 的事)
  - 不导出 / 不加密 —— 单机本地用,跟现有 memory / reminder 同一信任域
"""
from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class SessionInfo:
    session_id: str
    thread_id: str
    title: str
    created_at: float
    last_used_at: float
    message_count: int

    def to_row(self) -> tuple:
        return (self.session_id, self.thread_id, self.title,
                self.created_at, self.last_used_at, self.message_count)

    @classmethod
    def from_row(cls, row: tuple) -> "SessionInfo":
        return cls(
            session_id=row[0], thread_id=row[1], title=row[2],
            created_at=row[3], last_used_at=row[4], message_count=row[5],
        )


class SessionStore:
    """会话持久化(单 SQLite 文件)。

    thread-safety:SQLite 连接用 check_same_thread=False + 每个调用都新建
    连接(代价是 IO 多一些,但 Pet UI 不在热路径,值)。
    """

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ---------- 内部 ----------

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self.db_path), check_same_thread=False,
                            isolation_level=None)
        c.row_factory = sqlite3.Row
        return c

    def _init_db(self) -> None:
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id     TEXT PRIMARY KEY,
                    thread_id      TEXT NOT NULL UNIQUE,
                    title          TEXT NOT NULL DEFAULT '',
                    created_at     REAL NOT NULL,
                    last_used_at   REAL NOT NULL,
                    message_count  INTEGER NOT NULL DEFAULT 0
                )
            """)
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_last_used "
                "ON sessions(last_used_at DESC)"
            )

    # ---------- CRUD ----------

    def create(self, title: str = "") -> SessionInfo:
        """新建一个 session,返回 SessionInfo。"""
        session_id = uuid.uuid4().hex[:12]
        thread_id = f"thread-{session_id}"
        now = time.time()
        info = SessionInfo(
            session_id=session_id,
            thread_id=thread_id,
            title=title or f"会话 {session_id[:6]}",
            created_at=now,
            last_used_at=now,
            message_count=0,
        )
        with self._conn() as c:
            c.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
                info.to_row(),
            )
        log.info("SessionStore: 新建 session_id=%s thread_id=%s title=%r",
                 session_id, thread_id, info.title)
        return info

    def get(self, session_id: str) -> Optional[SessionInfo]:
        """按 session_id 查。找不到返回 None。"""
        with self._conn() as c:
            r = c.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return SessionInfo.from_row(tuple(r)) if r else None

    def get_by_thread_id(self, thread_id: str) -> Optional[SessionInfo]:
        """按 thread_id 查(LangChainAgent 拿到 thread_id 时回查 session 信息用)。"""
        with self._conn() as c:
            r = c.execute(
                "SELECT * FROM sessions WHERE thread_id = ?",
                (thread_id,),
            ).fetchone()
        return SessionInfo.from_row(tuple(r)) if r else None

    def list_all(self, limit: int = 100) -> list[SessionInfo]:
        """列所有 session,按 last_used_at desc。"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM sessions ORDER BY last_used_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [SessionInfo.from_row(tuple(r)) for r in rows]

    def touch(self, session_id: str, *, message_count: Optional[int] = None,
              title: Optional[str] = None) -> bool:
        """更新 last_used_at(必要时同步更新 message_count / title)。"""
        fields = ["last_used_at = ?"]
        params: list = [time.time()]
        if message_count is not None:
            fields.append("message_count = ?")
            params.append(message_count)
        if title is not None:
            fields.append("title = ?")
            params.append(title)
        params.append(session_id)
        with self._conn() as c:
            cur = c.execute(
                f"UPDATE sessions SET {', '.join(fields)} WHERE session_id = ?",
                params,
            )
            return cur.rowcount > 0

    def delete(self, session_id: str) -> bool:
        """删除 session 记录(不直接清 checkpoint —— 留给 LangChainAgent 自己处理,
        因为 checkpointer DB 路径是它管的)。"""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM sessions WHERE session_id = ?",
                (session_id,),
            )
            return cur.rowcount > 0

    def rename(self, session_id: str, title: str) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "UPDATE sessions SET title = ? WHERE session_id = ?",
                (title, session_id),
            )
            return cur.rowcount > 0

    # ---------- 工具 ----------

    @staticmethod
    def new_session_id() -> str:
        """生成 session_id(给调用方需要预先知道 id 的场景,例如 unit test)。"""
        return uuid.uuid4().hex[:12]


__all__ = ["SessionInfo", "SessionStore"]