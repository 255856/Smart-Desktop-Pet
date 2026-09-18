"""数据看板：可视化桌宠的成长历程和互动统计。"""
import json
import logging
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class DailyStats:
    """单日的互动统计。"""
    date: str
    interactions: int = 0
    chat_messages: int = 0
    food_consumed: int = 0
    avg_feeling: float = 0.0
    level: int = 1


@dataclass
class PetGrowthRecord:
    """桌宠成长记录总表。"""
    level_ups: list = field(default_factory=list)   # [{"level": int, "timestamp": float}]
    daily_stats: dict = field(default_factory=dict)  # {date_str: DailyStats}
    total_interactions: int = 0
    total_chat_messages: int = 0
    total_food_consumed: int = 0
    created_at: float = field(default_factory=time.time)


class Dashboard:
    """数据看板管理器。"""

    def __init__(self, data_file: str | Path = "data/dashboard.json"):
        self.data_file = Path(data_file)
        self.record = PetGrowthRecord()
        self._load()

    def _load(self) -> None:
        """从 JSON 文件加载数据（含 DailyStats 反序列化）。"""
        if not self.data_file.is_file():
            return
        try:
            raw = json.loads(self.data_file.read_text("utf-8"))

            # 重建 daily_stats：从 dict of dicts 恢复为 dict of DailyStats
            daily_stats = {}
            for date, data in raw.get("daily_stats", {}).items():
                daily_stats[date] = DailyStats(**data)

            self.record = PetGrowthRecord(
                level_ups=raw.get("level_ups", []),
                daily_stats=daily_stats,
                total_interactions=raw.get("total_interactions", 0),
                total_chat_messages=raw.get("total_chat_messages", 0),
                total_food_consumed=raw.get("total_food_consumed", 0),
                created_at=raw.get("created_at", time.time()),
            )
        except Exception as e:
            log.warning("看板数据加载失败：%s", e)

    def _save(self) -> None:
        """保存数据到 JSON 文件。"""
        try:
            self.data_file.parent.mkdir(parents=True, exist_ok=True)
            self.data_file.write_text(
                json.dumps(asdict(self.record), ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception as e:
            log.warning("看板数据保存失败：%s", e)

    def record_interaction(self) -> None:
        """记录一次互动。"""
        today = time.strftime("%Y-%m-%d")
        if today not in self.record.daily_stats:
            self.record.daily_stats[today] = DailyStats(date=today)
        self.record.daily_stats[today].interactions += 1
        self.record.total_interactions += 1
        self._save()

    def record_chat_message(self) -> None:
        """记录一条聊天消息。"""
        today = time.strftime("%Y-%m-%d")
        if today not in self.record.daily_stats:
            self.record.daily_stats[today] = DailyStats(date=today)
        self.record.daily_stats[today].chat_messages += 1
        self.record.total_chat_messages += 1
        self._save()

    def record_food_consumed(self) -> None:
        """记录一次喂食。"""
        today = time.strftime("%Y-%m-%d")
        if today not in self.record.daily_stats:
            self.record.daily_stats[today] = DailyStats(date=today)
        self.record.daily_stats[today].food_consumed += 1
        self.record.total_food_consumed += 1
        self._save()

    def record_level_up(self, level: int) -> None:
        """记录升级。"""
        self.record.level_ups.append({"level": level, "timestamp": time.time()})
        self._save()

    def get_summary(self) -> str:
        """生成数据摘要（用于注入 system prompt 或显示在 UI）。"""
        total_days = len(self.record.daily_stats)
        return (
            f"📊 桌宠成长记录：\n"
            f"  累计互动：{self.record.total_interactions} 次\n"
            f"  累计聊天：{self.record.total_chat_messages} 条\n"
            f"  累计喂食：{self.record.total_food_consumed} 次\n"
            f"  记录天数：{total_days} 天\n"
            f"  升级次数：{len(self.record.level_ups)} 次"
        )
