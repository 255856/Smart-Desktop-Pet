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

    def set_many(self, values: dict[str, Any]) -> None:
        """批量设置 + **只落盘一次**。

        设置面板一次改动往往涉及多个键（模型配置就是 7 个）。逐个 set()
        会把整份 JSON 序列化重写 N 遍——输 40 字符 API key 就是 280 次
        整文件写（实测把面板卡得打字都跟不上）。
        """
        if not values:
            return
        self._data.update(values)
        self._save()
