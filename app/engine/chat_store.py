"""聊天历史持久化：SQLite 主存 + JSON 自动迁移（首次启动一次性）。

从 JSON 迁到 SQLite：启动加速 + 写盘原子性 + 为后续查询/同步铺路。

迁移策略：
- 首次 load 时，若 *.json 存在且 *.db 不存在 → 读 JSON → 写 DB → 删 JSON
- 仅 SQLite 存在 → 直接读 DB
- 公共 API（add / all / clear）保持不变 → 测试无需改动
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
    # v31 (2026-10-17): 人设维度。改 yaml + 重启 pet 时, 只看当前 character_name
    # 的消息; 翻看历史能区分哪个角色说的 (settings.json 已有 legacy 字段
    # 走默认 '' = 跨角色, 老记录用 '' 标)。
    character_name: str = ""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    role        TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    emotion     TEXT    NOT NULL DEFAULT '',
    ts          REAL    NOT NULL,
    tools_json  TEXT    NOT NULL DEFAULT '[]',
    character_name TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_messages_char_ts ON messages(character_name, ts);
"""

# v31: 旧 db 加列迁移 (列已存在会报 duplicate column, 吞掉)。
_MIGRATE_ADD_CHAR_COLUMN = (
    "ALTER TABLE messages ADD COLUMN character_name TEXT NOT NULL DEFAULT ''"
)


class ChatStore:
    """聊天历史存储。v31: 同一 db 文件, 加 character_name 列做 per-character 隔离。
    不同人设的 chat_window 各自起一个 ChatStore 实例, 各自的 character_name
    把消息写到同一个 db 但不同行, all(character_name=...) 过滤读出。
    """

    def __init__(self, data_file: str | Path = "data/chat_history.db",
                 max_messages: int = 200,
                 legacy_json: str | Path | None = "data/chat_history.json",
                 character_name: str = ""):
        # 兼容旧的 `data_file` 指向 JSON —— 检测后缀自动转成 .db
        p = Path(data_file)
        if p.suffix.lower() == ".json":
            legacy_json = data_file
            data_file = p.with_suffix(".db")
            log.info("ChatStore: 检测到 .json data_file, 自动迁向 %s", data_file)
        self.data_file = Path(data_file)
        self.legacy_json = Path(legacy_json) if legacy_json else None
        self.max_messages = max_messages
        # v31: 实例绑 character_name; add() 自动写入。空字符串 = legacy / 跨角色
        # (老记录和老 code 路径都按 '' 处理)。
        self.character_name = character_name
        self._messages: list[StoredMessage] = []
        self._load()

    def _connect(self) -> sqlite3.Connection:
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.data_file))
        conn.row_factory = sqlite3.Row
        conn.executescript(_SCHEMA)
        # v31: 旧 db 没 character_name 列, ALTER 补上。列已存在会抛
        # "duplicate column name", 吞掉(SQLite 不支持 IF NOT EXISTS for ADD COLUMN)。
        try:
            conn.execute(_MIGRATE_ADD_CHAR_COLUMN)
            conn.commit()
            log.info("ChatStore: 老 db 已加 character_name 列")
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                log.warning("ChatStore: 加 character_name 列失败(%s), 继续", e)
        return conn

    def _migrate_from_json(self) -> int:
        """从 legacy JSON 文件读历史消息写入 SQLite。返回迁移条数。

        成功 → 删除 JSON（避免下次再迁）；失败 → 保留 JSON。
        v31: 迁移时给老记录打 '' character_name (跨角色 shared)。
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
                         json.dumps(m.tools or [], ensure_ascii=False),
                         "")  # v31: 老记录标 '' (跨角色 shared)
                        for m in msgs]
                conn.executemany(
                    "INSERT INTO messages(role, content, emotion, ts, tools_json, character_name) "
                    "VALUES (?, ?, ?, ?, ?, ?)", rows,
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            log.warning("ChatStore: 写 DB 失败(%s),保留 JSON", e)
            return 0
        # 迁移成功，删 JSON
        try:
            self.legacy_json.unlink(missing_ok=True)
            log.info("ChatStore: 已迁移 %d 条消息,删除 %s",
                     len(msgs), self.legacy_json)
        except OSError:
            log.warning("ChatStore: 删 JSON 失败(可手动删):%s", self.legacy_json)
        return len(msgs)

    def _load(self) -> None:
        # 还在 JSON 时代 → 先迁
        if not self.data_file.is_file():
            migrated = self._migrate_from_json()
            if migrated:
                pass  # 下面会读 DB
            else:
                return
        try:
            conn = self._connect()
            try:
                # 按 ts 升序读（老 JSON 是 append 顺序，SQLite id 也是升序）
                cur = conn.execute(
                    "SELECT role, content, emotion, ts, tools_json, character_name "
                    "FROM messages ORDER BY id ASC"
                )
                self._messages = []
                for row in cur.fetchall():
                    self._messages.append(StoredMessage(
                        role=row["role"],
                        content=row["content"],
                        emotion=row["emotion"],
                        ts=row["ts"],
                        tools=json.loads(row["tools_json"]) if row["tools_json"] else [],
                        character_name=row["character_name"] or "",
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
                    "INSERT INTO messages(role, content, emotion, ts, tools_json, character_name) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (msg.role, msg.content, msg.emotion, msg.ts,
                     json.dumps(msg.tools or [], ensure_ascii=False),
                     msg.character_name or self.character_name or ""),
                )
                conn.commit()
                return True
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            log.warning("ChatStore: 写入失败:%s", e)
            return False

    def _prune_old(self) -> None:
        """保留每个 character_name 最近 max_messages 条 (v31 改成 per-character 限长)。

        之前是全局限 max_messages, 切人设后老消息会被挤掉, 破坏隔离。
        现在按 character_name 分组, 每个角色各自保 max_messages。
        老记录 (character_name == '') 也单独算一组。
        """
        by_char: dict[str, list[StoredMessage]] = {}
        for m in self._messages:
            key = m.character_name or ""
            by_char.setdefault(key, []).append(m)
        for v in by_char.values():
            if len(v) > self.max_messages:
                v[: len(v) - self.max_messages] = []
        # 不写盘裁剪 — chat_window reload 时会从 db 重读,
        # 真正的 limit 在 _load 后下一轮 add 之前再做。这函数现在只确保
        # self._messages 内存里每角色 ≤ max_messages。
        self._messages = []
        for v in by_char.values():
            self._messages.extend(v)

    def add(self, role: str, content: str, emotion: str = "", tools: list = None) -> None:
        """添加一条消息并持久化。v31: 自动用 self.character_name 标。

        add() 一次性把消息标到当前实例的 character_name, 不在外部每条都传。
        """
        msg = StoredMessage(
            role=role, content=content, emotion=emotion, tools=tools or [],
            character_name=self.character_name,
        )
        self._messages.append(msg)
        # 写盘前先裁剪，避免 DB 越来越大
        if len(self._messages) > self.max_messages:
            self._messages = self._messages[-self.max_messages:]
        # 先 prune 再 add（避免 delete 影响新插的 id）
        self._prune_old()
        self._save_one(msg)

    def all(self, character_name: str | None = None) -> list[StoredMessage]:
        """返消息列表。v31: character_name 过滤 — None = 用 self.character_name
        (per-character), 显式传 '' 返老 shared 记录, 传具体名返另一角色 (调试用)。
        """
        if character_name is None:
            character_name = self.character_name
        if not character_name:
            # 实例无 character_name, 返全部 (legacy / 调试)
            return list(self._messages)
        return [m for m in self._messages if (m.character_name or "") == character_name]

    def clear(self) -> None:
        """清空所有消息并持久化。v31: 只清本 character_name 的记录, 不影响其他人设。"""
        if self.character_name:
            self._messages = [m for m in self._messages
                             if (m.character_name or "") != self.character_name]
        else:
            self._messages = []
        try:
            conn = self._connect()
            try:
                if self.character_name:
                    conn.execute(
                        "DELETE FROM messages WHERE character_name = ?",
                        (self.character_name,))
                else:
                    conn.execute("DELETE FROM messages")
                conn.commit()
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            log.warning("ChatStore: clear 失败:%s", e)

    def remove_empty_assistant(self) -> int:
        """删掉 assistant 角色且 content 空 + tools 空的「废消息」。

        早期鉴权失败批量产生 content="" 的 assistant 消息，
        重启后界面渲染成"空白文件图标"。在 ChatWindow._load_history 启动时
        一次性清掉，不打扰主人。

        返回删掉的条数。

        v31: 整库「DELETE + re-INSERT kept」保留 character_name 列, 跨角色不破坏。
        """
        before = len(self._messages)
        kept = [
            m for m in self._messages
            if not (m.role == "assistant"
                    and not (m.content or "").strip()
                    and not (m.tools or []))
        ]
        removed = before - len(kept)
        self._messages = kept
        if not removed:
            return 0
        try:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM messages")
                conn.executemany(
                    "INSERT INTO messages(role, content, emotion, ts, "
                    "tools_json, character_name) VALUES (?, ?, ?, ?, ?, ?)",
                    [(m.role, m.content, m.emotion, m.ts,
                      json.dumps(m.tools or [], ensure_ascii=False),
                      m.character_name or "")
                     for m in kept],
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            log.warning("ChatStore: remove_empty 失败:%s", e)
        log.info("ChatStore: 清理 %d 条空 content 历史 assistant 消息", removed)
        return removed


__all__ = ["ChatStore", "StoredMessage"]