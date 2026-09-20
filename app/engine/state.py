"""VPet 同款核心状态机（平衡版）。

数值设计原则：
    1. 数值不会「一下子就掉完」——基础衰减慢（100→0 约 60-100 分钟）
    2. 数值可以「被动回复」——当某项 >70 时缓慢恢复，不会无限下降
    3. 睡觉时全部快速回复（体力 +0.20/s, 饱食/口渴 +0.10/s, 心情 +0.05/s）
    4. 吃饭/喝水/抚摸等互动能大幅提升数值（吃饭 +30 饱食 +15 体力）
    5. 夜间（22:00-06:00）衰减仅 ×1.3（不是 ×1.5，避免过于惩罚性）

衰减速率（per second, 白天，四项统一）：
    - 体力 / 饱食 / 口渴 / 心情：0.007/s（100→0 纯衰减 ≈ 4 h；
      高值因被动回复实际略慢；夜间 ×1.3 ≈ 3 h）
    - 寂寞：   10 分钟未互动 → 心情衰减 ×2（≈ 2 h）
    - 健康：   不自动衰减，仅当饱食/口渴/心情 全部 < 15 时才下降

被动回复速率（per second）：
    - 当某项 > 70 时：每秒回复 +0.0035（约抵消一半白天衰减）
    - 当某项 < 30 时：不回复（需要用户主动喂食）
    - 三项都 > 50 且心情 > 60 时：健康每秒 +0.020
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

log = logging.getLogger(__name__)


class Mode(str, Enum):
    HAPPY = "Happy"
    NORMAL = "Normal"        # 旧版拼写错误 NOMAL 已修正
    POOR = "PoorCondition"
    ILL = "Ill"

    @classmethod
    def from_string(cls, value: str) -> "Mode":
        """兼容旧版存档中的 Nomal 拼写。"""
        if value == "Nomal":
            return cls.NORMAL
        try:
            return cls(value)
        except ValueError:
            return cls.NORMAL


@dataclass
class PetState:
    strength: float = 100.0
    strength_food: float = 100.0
    strength_drink: float = 100.0
    feeling: float = 80.0
    health: float = 100.0

    store_strength: float = 0.0
    store_strength_food: float = 0.0
    store_strength_drink: float = 0.0

    money: float = 100.0
    exp: float = 0.0
    level: int = 1
    likability: float = 0.0
    mode: Mode = Mode.NORMAL

    # ---- 衰减速率（per second） ----
    decay_strength: float = 0.007
    decay_strength_food: float = 0.007
    decay_strength_drink: float = 0.007
    decay_feeling: float = 0.007

    # ---- 被动回复速率（per second，当该项 >70 时） ----
    regen_strength: float = 0.0035
    regen_strength_food: float = 0.0035
    regen_strength_drink: float = 0.0035
    regen_feeling: float = 0.0035

    # ---- 被动回复阈值：> regen_threshold 才开始回复 ----
    regen_threshold: float = 70.0

    # ---- 健康恢复阈值：所有项 > healthy_min 时才回复健康 ----
    healthy_min: float = 40.0

    # ---- 报警阈值 ----
    food_low: float = 25.0
    drink_low: float = 25.0
    feeling_low: float = 25.0

    # ---- 夜间倍数（从 1.5 降到 1.3，避免太惩罚性） ----
    night_multiplier: float = 1.3

    _last_interact_ts: float = 0.0

    # ---- 投喂好感每日上限（每次有效投喂 +1，每天最多 5 点，跨自然日重置） ----
    feed_like_daily_cap: float = 5.0
    feed_like_date: str = ""
    feed_like_count: float = 0.0

    # ---- 每日签到（每天一次，签到得金币，跨自然日重置） ----
    checkin_date: str = ""

    # ---- 小游戏金币每日上限（防刷，跨自然日重置） ----
    game_coin_daily_cap: float = 100.0
    game_coin_date: str = ""
    game_coin_count: float = 0.0

    on_mode_change: Optional[Callable[[Mode, Mode], None]] = None
    on_change: Optional[Callable[[], None]] = None
    on_level_up: Optional[Callable[[int], None]] = None

    @property
    def likability_max(self) -> float:
        return 90 + self.level * 10

    @property
    def feeling_max(self) -> float:
        return 100.0

    def level_up_need(self) -> int:
        return int(math.pow(self.cal_level() * 10, 2))

    @property
    def exp_to_next(self) -> float:
        return max(0.0, self.level_up_need() - self.exp)

    def cal_level(self) -> int:
        if self.exp < 0:
            return 1
        return int(math.sqrt(self.exp) / 10) + 1

    def cal_mode(self) -> Mode:
        realhel = 60
        if self.feeling >= 80:
            realhel -= 12
        if self.likability >= 80:
            realhel -= 12
        elif self.likability >= 40:
            realhel -= 6

        if self.health <= realhel:
            if self.health <= realhel / 2:
                return Mode.ILL
            return Mode.POOR

        realfel = 0.90
        if self.likability >= 80:
            realfel -= 0.20
        elif self.likability >= 40:
            realfel -= 0.10
        felps = self.feeling / self.feeling_max
        if felps >= realfel:
            return Mode.HAPPY
        if felps <= realfel / 2:
            return Mode.POOR
        return Mode.NORMAL

    def feed(self, *,
             strength_food: float = 0, strength_drink: float = 0,
             feeling: float = 0, health: float = 0,
             strength: float = 0, exp: float = 0,
             money_delta: float = 0, likability: float = 0) -> None:
        """通用入口：直接累加并裁剪到 [0, 100]。"""
        self.strength = min(100.0, max(0.0, self.strength + strength))
        self.strength_food = min(100.0, max(0.0, self.strength_food + strength_food))
        self.strength_drink = min(100.0, max(0.0, self.strength_drink + strength_drink))
        self.feeling = min(100.0, max(0.0, self.feeling + feeling))
        self.health = min(100.0, max(0.0, self.health + health))
        self.likability = min(self.likability_max, max(0.0, self.likability + likability))
        if exp:
            self.exp += exp
            self._maybe_levelup()
        if money_delta:
            self.money = max(0.0, self.money + money_delta)
        self._reevaluate_mode()
        self._fire_change()

    def eat_food(self, *,
                 strength: float = 0, strength_food: float = 0,
                 strength_drink: float = 0, feeling: float = 0,
                 health: float = 0, likability: float = 0,
                 exp: float = 0) -> None:
        """VPet 同款 EatFood：每个 food 数值一半入主值、一半入 store*。"""
        tmp = strength / 2.0
        self.strength = min(100.0, max(0.0, self.strength + tmp))
        self.store_strength += tmp

        tmp = strength_food / 2.0
        self.strength_food = min(100.0, max(0.0, self.strength_food + tmp))
        self.store_strength_food += tmp

        tmp = strength_drink / 2.0
        self.strength_drink = min(100.0, max(0.0, self.strength_drink + tmp))
        self.store_strength_drink += tmp

        self.feeling = min(100.0, max(0.0, self.feeling + feeling))
        self.health = min(100.0, max(0.0, self.health + health))
        self.likability = min(self.likability_max, max(0.0, self.likability + likability))

        if exp:
            self.exp += exp
            self._maybe_levelup()
        self._reevaluate_mode()
        self._fire_change()

    def take_store(self) -> None:
        """StoreTake：把 store* 的 1/8 慢慢补回主值（比原版 1/10 更快）。"""
        const_t = 8.0

        for stat_name, store_name, regen in [
            ("strength", "store_strength", None),
            ("strength_food", "store_strength_food", None),
            ("strength_drink", "store_strength_drink", None),
        ]:
            store_val = getattr(self, store_name)
            if store_val < 1.0:
                setattr(self, store_name, 0.0)
                continue
            s = store_val / const_t
            setattr(self, store_name, max(0.0, store_val - s))
            current = getattr(self, stat_name)
            setattr(self, stat_name, min(100.0, max(0.0, current + s)))

        self._fire_change()

    def add_money(self, delta: float) -> None:
        self.money = max(0.0, self.money + delta)
        self._fire_change()

    def tick(self, dt_seconds: float, *, is_night: bool = False,
             talking_pause: bool = False, sleeping: bool = False) -> None:
        """主循环 tick。

        数值规则（平衡版）：

        白天：
            体力/饱食/口渴/心情 每秒衰减 0.015-0.025（100→0 约 60-110 分钟）
            当某项 >70 时每秒被动回复 +0.008-0.010（刚好抵消一半衰减）
            当某项 <30 时不被动回复（用户必须主动喂食）
            三项都 >40 且心情 >60 → 健康每秒 +0.020

        夜间（22:00-06:00）：
            所有衰减 ×1.3（不是 ×1.5）
            被动回复照常

        睡觉时：
            体力 +0.20/s, 饱食/口渴 +0.10/s, 心情 +0.05/s
            不衰减，快速回复

        寂寞：
            10 分钟无互动 → 心情衰减 ×2
        """
        if talking_pause:
            return

        mult = self.night_multiplier if is_night else 1.0

        # 睡觉：快速回复，不衰减
        if sleeping:
            self.strength = min(100.0, self.strength + 0.20 * dt_seconds)
            self.strength_food = min(100.0, self.strength_food + 0.10 * dt_seconds)
            self.strength_drink = min(100.0, self.strength_drink + 0.10 * dt_seconds)
            self.feeling = min(100.0, self.feeling + 0.05 * dt_seconds)
            self.health = min(100.0, self.health + 0.05 * dt_seconds)
            self.take_store()
            self._reevaluate_mode()
            self._fire_change()
            return

        # ---- 白天正常衰减 ----
        for stat_name, decay_val, regen_val in [
            ("strength", self.decay_strength, self.regen_strength),
            ("strength_food", self.decay_strength_food, self.regen_strength_food),
            ("strength_drink", self.decay_strength_drink, self.regen_strength_drink),
            ("feeling", self._feeling_decay(), self.regen_feeling),
        ]:
            current = getattr(self, stat_name)
            if current < 0:
                current = 0.0

            # 衰减
            decayed = current - decay_val * dt_seconds * mult

            # 被动回复（仅当 >70 时）
            if current > self.regen_threshold:
                decayed += regen_val * dt_seconds

            # 裁剪到 [0, 100]
            decayed = max(0.0, min(100.0, decayed))
            setattr(self, stat_name, decayed)

        # ---- 健康 ----
        food = self.strength_food
        drink = self.strength_drink
        feeling = self.feeling

        if food < self.healthy_min and drink < self.healthy_min and feeling < self.healthy_min:
            # 三项全部低于 40 → 健康下降
            self.health = max(0.0, self.health - 0.03 * dt_seconds * mult)
        elif food > self.healthy_min and drink > self.healthy_min and feeling > 60.0:
            # 三项都 >40 且心情 >60 → 健康缓慢恢复
            self.health = min(100.0, self.health + 0.02 * dt_seconds)

        # ---- 寂寞计时 ----
        self._last_interact_ts = max(self._last_interact_ts, 0.0)

        # Store 回收
        self.take_store()

        # Mode 重判
        self._reevaluate_mode()
        self._fire_change()

    def _feeling_decay(self) -> float:
        """心情衰减：10 分钟未互动时翻倍。"""
        if self._last_interact_ts and time.time() - self._last_interact_ts > 600:
            return self.decay_feeling * 2.0
        return self.decay_feeling

    def on_interact(self, *, feeling_gain: float = 0, likability_gain: float = 0,
                    feed: bool = False) -> None:
        """记录互动：抚摸/聊天/双击等。"""
        self._last_interact_ts = time.time()
        if feeling_gain:
            self.feeling = min(100.0, max(0.0, self.feeling + feeling_gain))
        if likability_gain:
            self.likability = min(self.likability_max,
                                  max(0.0, self.likability + likability_gain))
        if feed:
            self.feed(strength_food=20, strength_drink=20, feeling=10, exp=2)
        self._reevaluate_mode()
        self._fire_change()

    def add_food_likability(self, like: float) -> float:
        """投喂好感：每日上限 feed_like_daily_cap 点，跨自然日重置。返回实际增加量。"""
        import datetime
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        if self.feed_like_date != today:
            self.feed_like_date = today
            self.feed_like_count = 0.0
        like = max(0.0, float(like))
        allowed = max(0.0, self.feed_like_daily_cap - self.feed_like_count)
        added = min(like, allowed)
        self.feed_like_count += added
        if added > 0:
            self.likability = min(self.likability_max, self.likability + added)
            self._fire_change()
        return added

    @staticmethod
    def _today_str() -> str:
        import datetime
        return datetime.datetime.now().strftime("%Y-%m-%d")

    def has_checked_in_today(self) -> bool:
        """今天是否已签到。"""
        return self.checkin_date == self._today_str()

    def daily_checkin(self, reward: float = 100.0) -> tuple[bool, float]:
        """每日签到：每天一次，成功返回 (True, 金币)，已签过返回 (False, 0)。"""
        today = self._today_str()
        if self.checkin_date == today:
            return False, 0.0
        self.checkin_date = today
        reward = max(0.0, float(reward))
        self.money = max(0.0, self.money + reward)
        self._fire_change()
        return True, reward

    def add_game_reward(self, amount: float) -> float:
        """小游戏金币：每日上限 game_coin_daily_cap，跨自然日重置。返回实际发放。"""
        today = self._today_str()
        if self.game_coin_date != today:
            self.game_coin_date = today
            self.game_coin_count = 0.0
        amount = max(0.0, float(amount))
        added = min(amount, max(0.0, self.game_coin_daily_cap - self.game_coin_count))
        self.game_coin_count += added
        if added > 0:
            self.money = max(0.0, self.money + added)
            self._fire_change()
        return added

    def game_coin_remaining(self) -> float:
        """今日小游戏还可获得的金币额度。"""
        count = self.game_coin_count if self.game_coin_date == self._today_str() else 0.0
        return max(0.0, self.game_coin_daily_cap - count)

    def _reevaluate_mode(self) -> None:
        new_mode = self.cal_mode()
        if new_mode != self.mode:
            old = self.mode
            self.mode = new_mode
            log.info("PetState mode: %s → %s", old, new_mode)
            if self.on_mode_change is not None:
                try:
                    self.on_mode_change(old, new_mode)
                except Exception as e:
                    log.warning("on_mode_change 回调异常：%s", e)

    def _maybe_levelup(self) -> None:
        new_level = self.cal_level()
        if new_level > self.level:
            self.level = new_level
            log.info("PetState 升级到 Lv.%d (exp=%.1f)", self.level, self.exp)
            if self.on_level_up is not None:
                try:
                    self.on_level_up(self.level)
                except Exception as e:
                    log.warning("on_level_up 回调异常：%s", e)

    def _fire_change(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception as e:
                log.warning("on_change 回调异常：%s", e)

    def to_dict(self) -> dict:
        return {
            "v": 3,
            "strength": self.strength,
            "strength_food": self.strength_food,
            "strength_drink": self.strength_drink,
            "feeling": self.feeling,
            "health": self.health,
            "money": self.money,
            "exp": self.exp,
            "level": self.level,
            "likability": self.likability,
            "store_strength": self.store_strength,
            "store_strength_food": self.store_strength_food,
            "store_strength_drink": self.store_strength_drink,
            "mode": self.mode.value,
            "last_interact": self._last_interact_ts,
            "feed_like_date": self.feed_like_date,
            "feed_like_count": self.feed_like_count,
            "checkin_date": self.checkin_date,
            "game_coin_date": self.game_coin_date,
            "game_coin_count": self.game_coin_count,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PetState":
        v = d.get("v", 1)
        s = cls()
        for k in ("strength", "strength_food", "strength_drink",
                  "feeling", "health", "money", "exp", "level",
                  "likability", "store_strength", "store_strength_food",
                  "store_strength_drink"):
            if k in d:
                setattr(s, k, float(d[k]))
        if "mode" in d:
            s.mode = Mode.from_string(d["mode"])
        if "last_interact" in d:
            s._last_interact_ts = float(d["last_interact"])
        if "feed_like_date" in d:
            s.feed_like_date = str(d["feed_like_date"])
        if "feed_like_count" in d:
            s.feed_like_count = float(d["feed_like_count"])
        if "checkin_date" in d:
            s.checkin_date = str(d["checkin_date"])
        if "game_coin_date" in d:
            s.game_coin_date = str(d["game_coin_date"])
        if "game_coin_count" in d:
            s.game_coin_count = float(d["game_coin_count"])
        if v < 3:
            log.info("存档 v=%d 已升级到 v3（含 level 保存 + 平衡数值）", v)
        return s

    def stats_summary(self) -> str:
        return (f"Lv.{self.level}  💪{self.strength:.0f}  "
                f"🍚{self.strength_food:.0f}  💧{self.strength_drink:.0f}  "
                f"😊{self.feeling:.0f}  ❤️{self.health:.0f}  "
                f"💕{self.likability:.0f}/{self.likability_max:.0f}  "
                f"💰{self.money:.0f}  [{self.mode.value}]")


def is_night(now_ts: float | None = None) -> bool:
    import datetime
    ts = now_ts if now_ts is not None else time.time()
    t = datetime.datetime.fromtimestamp(ts).time()
    return t.hour >= 22 or t.hour < 6
