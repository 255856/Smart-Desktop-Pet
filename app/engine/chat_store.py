"""聊天历史持久化：SQLite 主存 + JSON 自动迁移(首次启动时一次性)。

2026-10-14: 从 JSON 迁到 SQLite。理由:
- 启动加速(整文件解析 → 索引查询)
- 避免频繁写盘时原子性问题(JSON write_text 不原子,可能写一半进程被杀)
- 支持 future: 模糊查询 / 按日期段切片 / 多设备同步

迁移策略:
- 首次 load 时,若 *.json 存在且 *.db 不存在 → 读 JSON → 写 DB → 删 JSON
- 仅 SQLite 存在 → 直接读 DB
- 公共 API(add / all / clear)保持不变 → 测试无需改动
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class StoredMessage:
    role: str
    content: str
    emotion: str = ""
    ts: float = field(default_factory=time.time)
    tools: list = field(default_factory=list)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    role        TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    emotion     TEXT    NOT NULL DEFAULT '',
    ts          REAL    NOT NULL,
    tools_json  TEXT    NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
"""


class ChatStore:
    """聊天历史存储。"""

    def __init__(self, data_file: str | Path = "data/chat_history.db",
                 max_messages: int = 200,
                 legacy_json: str | Path | None = "data/chat_history.json"):
        # 兼容旧的 `data_file` 指向 JSON —— 检测后缀,自动转成 .db
        p = Path(data_file)
        if p.suffix.lower() == ".json":
            legacy_json = data_file
            data_file = p.with_suffix(".db")
            log.info("ChatStore: 检测到 .json data_file, 自动迁向 %s", data_file)
        self.data_file = Path(data_file)
        self.legacy_json = Path(legacy_json) if legacy_json else None
        self.max_messages = max_messages
        self._messages: list[StoredMessage] = []
        self._load()

    def _connect(self) -> sqlite3.Connection:
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.data_file))
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        conn.commit()
        return conn

    def _migrate_from_json(self) -> int:
        """从 legacy JSON 文件读历史消息写入 SQLite。返回迁移条数。

        成功 → 删除 JSON(避免下次再迁);失败 → 保留 JSON。
        """
        if not self.legacy_json or not self.legacy_json.is_file():
            return 0
        try:
            raw = json.loads(self.legacy_json.read_text("utf-8"))
            msgs = [StoredMessage(**m) for m in raw]
        except Exception as e:  # noqa: BLE001
            log.warning("ChatStore: legacy JSON 读失败(%s),跳过迁移", e)
            return 0
        if not msgs:
            self.legacy_json.unlink(missing_ok=True)
            return 0
        try:
            conn = self._connect()
            try:
                rows = [(m.role, m.content, m.emotion, m.ts,
                         json.dumps(m.tools or [], ensure_ascii=False))
                        for m in msgs]
                conn.executemany(
                    "INSERT INTO messages(role, content, emotion, ts, tools_json) "
                    "VALUES (?, ?, ?, ?, ?)", rows,
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            log.warning("ChatStore: 写 DB 失败(%s),保留 JSON", e)
            return 0
        # 迁移成功,删 JSON
        try:
            self.legacy_json.unlink(missing_ok=True)
            log.info("ChatStore: 已迁移 %d 条消息,删除 %s",
                     len(msgs), self.legacy_json)
        except OSError:
            log.warning("ChatStore: 删 JSON 失败(可手动删):%s", self.legacy_json)
        return len(msgs)

    def _load(self) -> None:
        # 0. 还在 JSON 时代 → 先迁
        if not self.data_file.is_file():
            migrated = self._migrate_from_json()
            if migrated:
                pass  # 下面会读 DB
            else:
                return
        try:
            conn = self._connect()
            try:
                # 按 ts 升序读(老 JSON 是 append 顺序,SQLite id 也是升序)
                cur = conn.execute(
                    "SELECT role, content, emotion, ts, tools_json FROM messages "
                    "ORDER BY id ASC"
                )
                self._messages = []
                for row in cur.fetchall():
                    self._messages.append(StoredMessage(
                        role=row["role"],
                        content=row["content"],
                        emotion=row["emotion"],
                        ts=row["ts"],
                        tools=json.loads(row["tools_json"]) if row["tools_json"] else [],
                    ))
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            log.warning("ChatStore: 读 DB 失败:%s", e)

    def _save_one(self, msg: StoredMessage) -> bool:
        try:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO messages(role, content, emotion, ts, tools_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (msg.role, msg.content, msg.emotion, msg.ts,
                     json.dumps(msg.tools or [], ensure_ascii=False)),
                )
                conn.commit()
                return True
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            log.warning("ChatStore: 写入失败:%s", e)
            return False

    def _prune_old(self) -> None:
        """保留最近 max_messages 条。"""
        if len(self._messages) <= self.max_messages:
            return
        drop_count = len(self._messages) - self.max_messages
        try:
            conn = self._connect()
            try:
                conn.execute(
                    "DELETE FROM messages WHERE id IN ("
                    "SELECT id FROM messages ORDER BY id ASC LIMIT ?)",
                    (drop_count,),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            log.warning("ChatStore: 清理旧消息失败:%s", e)

    def add(self, role: str, content: str, emotion: str = "", tools: list = None) -> None:
        """添加一条消息并持久化。"""
        msg = StoredMessage(role=role, content=content, emotion=emotion, tools=tools or [])
        self._messages.append(msg)
        # 写盘前先裁剪,避免 DB 越来越大
        if len(self._messages) > self.max_messages:
            self._messages = self._messages[-self.max_messages:]
        # 先 prune 再 add(避免 delete 影响新插的 id)
        self._prune_old()
        self._save_one(msg)

    def all(self) -> list[StoredMessage]:
        """返回所有消息的副本列表。"""
        return list(self._messages)

    def clear(self) -> None:
        """清空所有消息并持久化。"""
        self._messages = []
        try:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM messages")
                conn.commit()
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            log.warning("ChatStore: clear 失败:%s", e)


__all__ = ["ChatStore", "StoredMessage"]