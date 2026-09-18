"""设置持久化：把用户设置保存到 JSON 文件。"""
import json, logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class SettingsStore:
    """轻量 JSON 键值存储，用于持久化用户设置。"""

    def __init__(self, path: str | Path = "settings.json"):
        self.path = Path(path)
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self):
        if self.path.is_file():
            try:
                self._data = json.loads(self.path.read_text("utf-8"))
            except Exception as e:
                log.warning("设置加载失败：%s", e)

    def _save(self):
        try:
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            log.warning("设置保存失败：%s", e)

    def get(self, key: str, default: Any = None) -> Any:
        """获取设置值，不存在时返回 default。"""
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """设置值并立即落盘。"""
        self._data[key] = value
        self._save()
