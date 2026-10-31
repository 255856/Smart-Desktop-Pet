"""VPet 同款核心状态机（平衡版）。"""
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
    NORMAL = "Normal"
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

    decay_strength: float = 0.007
    decay_strength_food: float = 0.007
    decay_strength_drink: float = 0.007
    decay_feeling: float = 0.007

    regen_strength: float = 0.0035
    regen_strength_food: float = 0.0035
    regen_strength_drink: float = 0.0035
    regen_feeling: float = 0.0035

    regen_threshold: float = 70.0

    healthy_min: float = 40.0

    food_low: float = 25.0
    drink_low: float = 25.0
    feeling_low: float = 25.0

    night_multiplier: float = 1.3

    _last_interact_ts: float = 0.0

    feed_like_daily_cap: float = 5.0
    feed_like_date: str = ""
    feed_like_count: float = 0.0

    checkin_date: str = ""

    # 「拍一拍」互动记录（主人双击模型 —— 界面唯一的拍一拍入口）
    pat_total: int = 0             # 累计被拍次数
    pat_date: str = ""             # 最近一次被拍的日期（跨日自动清零今日计数）
    pat_count_today: int = 0       # 今天被拍次数
    pat_parts_today: dict = field(default_factory=dict)   # 今天各部位被拍次数
    pat_last_part: str = ""        # 最近一次被拍的部位
    pat_last_ts: float = 0.0       # 最近一次被拍的时间戳

    game_coin_daily_cap: float = 100.0
    game_coin_date: str = ""
    game_coin_count: float = 0.0

    on_mode_change: Optional[Callable[[Mode, Mode], None]] = None
    on_change: Optional[Callable[[], None]] = None
    on_level_up: Optional[Callable[[int], None]] = None

    @property
    def likability_max(self) -> float:
        return 100.0

    @property
    def feeling_max(self) -> float:
        return 100.0

    # 状态量化：把连续数值离散化为 LOW/MID/HIGH 三档，给 companion_policy 查表用。
    # 阈值 33 / 67，hysteresis 用 prev 防抖动，让"刚喂完食物"等瞬时变更不立刻跳档。

    @staticmethod
    def _quantize(value: float, hi: float = 100.0, prev: str = "MID") -> str:
        """通用 0..hi 三档离散化 (LOW / MID / HIGH), 带 hysteresis。

        prev 是上一档:
          - prev=LOW 时, 必须涨到 40 才回 MID (避免 33/35 边界抖动)
          - prev=HIGH 时, 必须跌到 60 才回 MID
        prev 默认 MID 用于首次量化 (没历史)。
        """
        if prev == "LOW":
            return "MID" if value >= 40.0 else "LOW"
        if prev == "HIGH":
            return "MID" if value <= 60.0 else "HIGH"
        # prev == MID: 标准三档
        third = hi / 3.0
        if value < third:
            return "LOW"
        if value > 2 * third:
            return "HIGH"
        return "MID"

    def quantize_mood(self, prev: str = "MID") -> str:
        """feeling 离散化: <33 LOW, 33-67 MID, >67 HIGH, hysteresis 复用 prev。"""
        return self._quantize(self.feeling, hi=100.0, prev=prev)

    def quantize_satiety(self, prev: str = "MID") -> str:
        """strength_food 离散化 (桌宠"饿不饿"信号)。"""
        return self._quantize(self.strength_food, hi=100.0, prev=prev)

    def quantize_level(self, prev: str = "MID") -> str:
        """level 离散化: 1-4 LOW, 5-14 MID, 15+ HIGH (论文区间)。

        等级离散化跟数值离散化不同, 范围不固定, 用区间判定而不是 1/3。
        """
        if prev == "LOW" and self.level >= 5:
            prev = "MID"
        if prev == "HIGH" and self.level <= 14:
            prev = "MID"
        if prev == "MID":
            if self.level < 5:
                return "LOW"
            if self.level >= 15:
                return "HIGH"
        return "MID"

    def quantize(self, prev_state: "Optional[dict[str, str]]" = None) -> dict[str, str]:
        """一次性返回 {mood, satiety, level, favor} 四档离散化结果。

        prev_state 用来做 hysteresis 记忆，比如上一轮 mood 是 HIGH，
        这次注入后还是 60 → 不立刻掉到 MID。
        """
        prev_state = prev_state or {}
        return {
            "mood": self.quantize_mood(prev_state.get("mood", "MID")),
            "satiety": self.quantize_satiety(prev_state.get("satiety", "MID")),
            "level": self.quantize_level(prev_state.get("level", "MID")),
            "favor": self.quantize_favor(prev_state.get("favor", "MID")),
        }

    def quantize_favor(self, prev: str = "MID") -> str:
        """likability 离散化，阈值走 companion_config.quantize。"""
        from app.core.companion_config import companion_config
        cfg = companion_config.quantize
        return self._quantize_with_cfg(self.likability, hi=100.0, prev=prev, cfg=cfg)

    def _quantize_with_cfg(self, value: float, hi: float, prev: str, cfg) -> str:
        """走 yaml 配置的量化（阈值 + hysteresis）。"""
        lo = float(cfg.low_below)
        mid = float(cfg.mid_below)
        h_lo = float(cfg.hysteresis_low_to_mid)
        h_hi = float(cfg.hysteresis_high_to_mid)

        if prev == "LOW":
            return "MID" if value >= h_lo else "LOW"
        if prev == "HIGH":
            return "MID" if value <= h_hi else "HIGH"
        # prev == MID
        if value < lo:
            return "LOW"
        if value > mid:
            return "HIGH"
        return "MID"

    # 反馈层闭环：任务执行结果反作用于 PetState

    def apply_outcome(self, outcome_type: str, tool_name: str = "",
                      risk_level: int = 0) -> None:
        """任务结果如何更新 PetState（per-run 颗粒）。

        outcome_type：
          - "success":  任务跑通   → 心情 +, 经验 +, 好感 +
          - "failure":  任务失败   → 心情 -, 好感 -
          - "rejected": 主人拒绝   → 心情 -, 好感 -
          - "accepted": 主人采纳   → 心情 +, 好感 +

        数值权重走 companion_config.event_hooks.{outcome_type}。
        未知 outcome_type 静默。喂食 / 互动走 apply_feed / apply_interact。

        升级：经验值增加后自动调 _maybe_levelup()，公式
            exp_to_next(level) = base * (level ** exp)
        走 companion_config.leveling.{base, exp}。
        """
        # 延迟导入避免循环：companion_config 在 app.core，state 在 app.engine
        from app.core.companion_config import companion_config
        cfg = companion_config

        # outcome_type → yaml 钩子字段（兼容旧字符串，新 yaml 字段名）
        key_map = {
            "success":  "task_success",
            "failure":  "task_failure",
            "rejected": "user_rejected",
            "accepted": "user_accepted",
        }
        yaml_key = key_map.get(outcome_type)
        if yaml_key is None:
            log.debug("PetState.apply_outcome: 未知 outcome_type=%r, 跳过", outcome_type)
            return
        hook = getattr(cfg.event_hooks, yaml_key)

        before = (
            round(self.feeling, 3),
            round(self.strength_food, 3),
            round(self.likability, 3),
            round(self.exp, 3),
            self.level,
        )

        # 4 维数值分别钳到合法范围
        if hook.satiety:
            self.strength_food = cfg.satiety.clamp(self.strength_food + hook.satiety)
        if hook.mood:
            self.feeling = cfg.mood.clamp(self.feeling + hook.mood)
        if hook.favor:
            self.likability = cfg.favor.clamp(self.likability + hook.favor)
        if hook.exp:
            self.exp = max(0.0, self.exp + hook.exp)
            # 升级检查走自定义公式 (覆盖原 cal_level 的 sqrt 公式)
            new_level = self._level_for_exp(self.exp, cfg.leveling)
            if new_level > self.level:
                old_level = self.level
                self.level = min(cfg.leveling.max_level, new_level)
                log.info("PetState 升级: Lv.%d → Lv.%d (exp=%.1f)",
                         old_level, self.level, self.exp)
                if self.on_level_up is not None:
                    try:
                        self.on_level_up(self.level)
                    except Exception as e:  # noqa: BLE001
                        log.warning("on_level_up 回调异常: %s", e)

        log.debug(
            "PetState.apply_outcome: %s tool=%s → "
            "(feeling=%.1f, food=%.1f, fav=%.1f, exp=%.1f, lv=%d)",
            outcome_type, tool_name,
            self.feeling, self.strength_food, self.likability, self.exp, self.level,
        )

        # v23-3: trace_outcome 写盘 (主人在 trace 页能直接看 Δ)
        try:
            from app.brain._companion_log import trace_outcome
            trace_outcome(
                thread_id=getattr(self, "_owner_thread_id", None) or "default",
                outcome=outcome_type,
                tool=tool_name or "",
                feeling_delta=round(self.feeling - before[0], 3),
                food_delta=round(self.strength_food - before[1], 3),
                favor_delta=round(self.likability - before[2], 3),
                exp_delta=round(self.exp - before[3], 3),
                level_before=before[4],
                level_after=self.level,
                success=(outcome_type == "success"),
            )
        except Exception:  # noqa: BLE001
            pass    # trace 失败不影响主流程

    def _level_for_exp(self, exp: float, leveling) -> int:
        """升级公式：反解 exp_to_next(level) = base * (level ** exp)。

        输入 exp 算最大可能等级：满足 base * (level ** exp) <= exp 的最大 level。
        """
        if exp <= 0:
            return 1
        base = max(1.0, leveling.base)
        exp_pow = max(0.1, leveling.exp)
        max_level = max(1, leveling.max_level)
        # 解 base * level^exp_pow <= exp → level <= (exp / base) ** (1/exp_pow)
        max_by_exp = int(((exp / base) ** (1.0 / exp_pow)) + 0.0001)
        return max(1, min(max_level, max_by_exp))

    # 喂食 / 互动钩子（跟 outcome 分离）

    def apply_feed(self, amount: float = 1.0) -> dict:
        """主人喂食钩子：走 yaml event_hooks.user_feed，返回 Δ 字典（trace 用）。

        amount 是倍率（1.0 = 默认）。amount < 0 = 减扣。
        """
        from app.core.companion_config import companion_config
        cfg = companion_config
        hook = cfg.event_hooks.user_feed
        before = (round(self.strength_food, 3), round(self.feeling, 3))
        self.strength_food = cfg.satiety.clamp(self.strength_food + hook.satiety * amount)
        self.feeling = cfg.mood.clamp(self.feeling + hook.mood * amount)
        log.debug("PetState.apply_feed(%.2f): food Δ=%.1f mood Δ=%.1f",
                  amount, hook.satiety * amount, hook.mood * amount)
        return {
            "food_delta": round(self.strength_food - before[0], 3),
            "mood_delta": round(self.feeling - before[1], 3),
        }

    def apply_interact(self, amount: float = 1.0) -> dict:
        """主人互动钩子: 走 yaml event_hooks.user_interact, 返回 Δ 字典。

        amount 是倍率 (1.0 = 默认 +3 心情 / +2 好感).
        """
        from app.core.companion_config import companion_config
        cfg = companion_config
        hook = cfg.event_hooks.user_interact
        before = (round(self.feeling, 3), round(self.likability, 3))
        self.feeling = cfg.mood.clamp(self.feeling + hook.mood * amount)
        self.likability = cfg.favor.clamp(self.likability + hook.favor * amount)
        self._last_interact_ts = time.time()
        log.debug("PetState.apply_interact(%.2f): mood Δ=%.1f fav Δ=%.1f",
                  amount, hook.mood * amount, hook.favor * amount)
        return {
            "mood_delta": round(self.feeling - before[0], 3),
            "favor_delta": round(self.likability - before[1], 3),
        }

    def normalize(self) -> None:
        # 把存档读入的数值钳制到合法范围（兼容旧版本越界值，如好感度曾随等级到 110）
        for _k in ("strength", "strength_food", "strength_drink",
                   "feeling", "health", "likability"):
            _hi = self.likability_max if _k == "likability" else 100.0
            setattr(self, _k, min(_hi, max(0.0, float(getattr(self, _k)))))
        for _k in ("store_strength", "store_strength_food", "store_strength_drink",
                   "money", "exp", "feed_like_count", "game_coin_count"):
            setattr(self, _k, max(0.0, float(getattr(self, _k))))
        self.level = max(1, int(self.level))

    def level_up_need(self) -> int:
        """升级公式走 yaml leveling。

        exp_to_next(level) = base * (level ** exp)
        level_up_need 返回到下一级需要的 EXP 增量。
        """
        from app.core.companion_config import companion_config
        cfg = companion_config.leveling
        return int(cfg.base * (self.level ** cfg.exp))

    @property
    def exp_to_next(self) -> float:
        return max(0.0, self.level_up_need() - self.exp)

    def cal_level(self) -> int:
        """升级公式 100 * level^1.5 走 yaml leveling。

        反解：满足 base * level^exp <= self.exp 的最大 level，钳到 [1, max_level]。
        """
        from app.core.companion_config import companion_config
        cfg = companion_config.leveling
        if self.exp <= 0:
            return 1
        base = max(1.0, cfg.base)
        exp_pow = max(0.1, cfg.exp)
        max_level = max(1, cfg.max_level)
        # base * level^exp_pow <= self.exp
        # level <= (self.exp / base) ** (1 / exp_pow)
        max_by_exp = int(((self.exp / base) ** (1.0 / exp_pow)) + 0.0001)
        return max(1, min(max_level, max_by_exp))

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

        food = self.strength_food
        drink = self.strength_drink
        feeling = self.feeling

        if food < self.healthy_min and drink < self.healthy_min and feeling < self.healthy_min:
            # 三项全部低于 40 → 健康下降
            self.health = max(0.0, self.health - 0.03 * dt_seconds * mult)
        elif food > self.healthy_min and drink > self.healthy_min and feeling > 60.0:
            # 三项都 >40 且心情 >60 → 健康缓慢恢复
            self.health = min(100.0, self.health + 0.02 * dt_seconds)

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

    # -- 「拍一拍」互动记录 --------------------------------------------------
    @property
    def pat_today(self) -> int:
        """今天被拍了几次（跨日自动归零，不用等下一次拍一拍才清）。"""
        if self.pat_date != self._today_str():
            return 0
        return int(self.pat_count_today)

    def record_pat(self, part: str = "") -> dict:
        """记一次「拍一拍」（双击模型 = model；控制台也能按部位触发）。

        今日计数跨自然日自动清零。返回 {'part', 'total', 'today', 'parts'}，
        给台词 / 记忆卡用。
        """
        today = self._today_str()
        if self.pat_date != today:
            self.pat_date = today
            self.pat_count_today = 0
            self.pat_parts_today = {}
        part = str(part or "").strip()
        if part:
            parts = dict(self.pat_parts_today or {})
            parts[part] = int(parts.get(part, 0)) + 1
            self.pat_parts_today = parts
        self.pat_total = int(self.pat_total) + 1
        self.pat_count_today = int(self.pat_count_today) + 1
        self.pat_last_part = part
        self.pat_last_ts = time.time()
        self._fire_change()
        return {"part": part, "total": self.pat_total,
                "today": self.pat_count_today,
                "parts": dict(self.pat_parts_today or {})}

    def pat_summary(self) -> str:
        """「拍一拍」小结（给聊天上下文 / 记忆卡用）。没拍过就返回空串。"""
        today = self.pat_today
        if not today:
            return ""
        return f"主人今天拍了我 {today} 次（累计 {int(self.pat_total)} 次）"

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
            "pat_total": self.pat_total,
            "pat_date": self.pat_date,
            "pat_count_today": self.pat_count_today,
            "pat_parts_today": dict(self.pat_parts_today or {}),
            "pat_last_part": self.pat_last_part,
            "pat_last_ts": self.pat_last_ts,
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
        # 「拍一拍」记录（旧存档没有这些键 → 保持 0）
        try:
            s.pat_total = max(0, int(d.get("pat_total") or 0))
            s.pat_count_today = max(0, int(d.get("pat_count_today") or 0))
        except (TypeError, ValueError):
            s.pat_total = 0
            s.pat_count_today = 0
        s.pat_date = str(d.get("pat_date") or "")
        s.pat_last_part = str(d.get("pat_last_part") or "")
        try:
            s.pat_last_ts = float(d.get("pat_last_ts") or 0.0)
        except (TypeError, ValueError):
            s.pat_last_ts = 0.0
        parts = d.get("pat_parts_today")
        s.pat_parts_today = {}
        if isinstance(parts, dict):
            for k, v in parts.items():
                try:
                    s.pat_parts_today[str(k)] = max(0, int(v))
                except (TypeError, ValueError):
                    continue
        if "game_coin_date" in d:
            s.game_coin_date = str(d["game_coin_date"])
        if "game_coin_count" in d:
            s.game_coin_count = float(d["game_coin_count"])
        if v < 3:
            log.info("存档 v=%d 已升级到 v3（含 level 保存 + 平衡数值）", v)
        s.normalize()
        return s

    def stats_summary(self) -> str:
        # 「今天被拍 N 次」也进这一行 —— 它进聊天上下文，桌宠才真能说出
        # 「主人今天拍了我 3 次」这种话（互动记录 = 它的记忆）。
        pat = f"今天被拍{self.pat_today}次  " if self.pat_today else ""
        return (f"Lv.{self.level}  体力{self.strength:.0f}  "
                f"饱食{self.strength_food:.0f}  饮水{self.strength_drink:.0f}  "
                f"心情{self.feeling:.0f}  健康{self.health:.0f}  "
                f"好感{self.likability:.0f}/{self.likability_max:.0f}  "
                f"金币{self.money:.0f}  {pat}[{self.mode.value}]")


def is_night(now_ts: float | None = None) -> bool:
    import datetime
    ts = now_ts if now_ts is not None else time.time()
    t = datetime.datetime.fromtimestamp(ts).time()
    return t.hour >= 22 or t.hour < 6
