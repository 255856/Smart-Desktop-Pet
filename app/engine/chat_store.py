"""聊天历史持久化：把对话记录保存到 JSON 文件。"""
import json
import logging
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

@dataclass
class StoredMessage:
    role: str
    content: str
    emotion: str = ""
    ts: float = field(default_factory=time.time)
    tools: list = field(default_factory=list)

class ChatStore:
    """聊天历史存储（JSON 文件）。"""
    
    def __init__(self, data_file: str | Path = "data/chat_history.json", max_messages: int = 200):
        self.data_file = Path(data_file)
        self.max_messages = max_messages
        self._messages: list[StoredMessage] = []
        self._load()
    
    def _load(self):
        """从 JSON 文件加载历史消息。"""
        if not self.data_file.is_file():
            return
        try:
            raw = json.loads(self.data_file.read_text("utf-8"))
            self._messages = [StoredMessage(**m) for m in raw]
        except Exception as e:
            log.warning("聊天历史加载失败：%s", e)
    
    def _save(self):
        """将历史消息写入 JSON 文件。"""
        try:
            self.data_file.parent.mkdir(parents=True, exist_ok=True)
            self.data_file.write_text(
                json.dumps([asdict(m) for m in self._messages], ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception as e:
            log.warning("聊天历史保存失败：%s", e)
    
    def add(self, role: str, content: str, emotion: str = "", tools: list = None):
        """添加一条消息并持久化。"""
        msg = StoredMessage(role=role, content=content, emotion=emotion, tools=tools or [])
        self._messages.append(msg)
        # 限制最大条数，只保留最近的 max_messages 条
        if len(self._messages) > self.max_messages:
            self._messages = self._messages[-self.max_messages:]
        self._save()
    
    def all(self) -> list[StoredMessage]:
        """返回所有消息的副本列表。"""
        return list(self._messages)
    
    def clear(self):
        """清空所有消息并持久化。"""
        self._messages = []
        self._save()
