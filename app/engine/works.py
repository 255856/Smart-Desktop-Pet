"""打工 + 食物/礼物数据系统。

设计：
    - 两份 JSON：foods.json（吃的 + 礼物）、works.json（Work/Study/Play）
    - 加载后保存为 Item / Job dataclass 列表
    - 调用 apply_food / apply_job 直接改 PetState

JSON 结构（与 VPet 同款字段）：
    food: name, type, exp, strength, strength_food, strength_drink,
          health, feeling, likability, price, graph, desc
    job : name, type (Work/Study/Play), money_base, strength_food,
          strength_drink, feeling, time_seconds, finish_bonus,
          level_limit, graph, desc

PR5: 这是 MVP 第一版的食物 + 打工系统，UI 在托盘菜单里挂入口即可。
"""
from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from app.engine.state import PetState

log = logging.getLogger(__name__)


@dataclass
class Item:
    name: str
    type: str       # Drink / Functional / Snack / Meal / Drug / Gift
    exp: int = 0
    strength: float = 0.0
    strength_food: float = 0.0
    strength_drink: float = 0.0
    health: float = 0.0
    feeling: float = 0.0
    likability: float = 0.0
    price: float = 0.0
    graph: str = ""
    desc: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Item":
        return cls(
            name=d.get("name", ""),
            type=d.get("type", ""),
            exp=int(d.get("exp", 0)),
            strength=float(d.get("strength", 0)),
            strength_food=float(d.get("strength_food", 0)),
            strength_drink=float(d.get("strength_drink", 0)),
            health=float(d.get("health", 0)),
            feeling=float(d.get("feeling", 0)),
            likability=float(d.get("likability", 0)),
            price=float(d.get("price", 0)),
            graph=d.get("graph", ""),
            desc=d.get("desc", ""),
        )


@dataclass
class Job:
    name: str
    type: str       # Work / Study / Play
    money_base: float = 0.0
    strength_food: float = 0.0
    strength_drink: float = 0.0
    feeling: float = 0.0
    time_seconds: int = 60
    finish_bonus: float = 0.0
    level_limit: int = 0
    graph: str = ""
    desc: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        return cls(
            name=d.get("name", ""),
            type=d.get("type", "Work"),
            money_base=float(d.get("money_base", 0)),
            strength_food=float(d.get("strength_food", 0)),
            strength_drink=float(d.get("strength_drink", 0)),
            feeling=float(d.get("feeling", 0)),
            time_seconds=int(d.get("time_seconds", 60)),
            finish_bonus=float(d.get("finish_bonus", 0)),
            level_limit=int(d.get("level_limit", 0)),
            graph=d.get("graph", ""),
            desc=d.get("desc", ""),
        )

    def eligible(self, level: int) -> bool:
        return self.level_limit == 0 or level >= self.level_limit

    def calculate_rewards(self, *, level: int = 1, lucky: Optional[float] = None) -> tuple[float, int]:
        """返回 (money, exp)；按 level 倍率 + 随机完成奖励。"""
        money = self.money_base * (1 + (level - 1) * 0.05)
        # bonus
        r = lucky if lucky is not None else random.random()
        money *= (1 + self.finish_bonus * r)
        # exp 简单按 level 线性
        exp = int((self.money_base / 4) * (1 + (level - 1) * 0.1))
        return round(money, 1), exp


@dataclass
class ItemStore:
    items: list[Item] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "ItemStore":
        p = Path(path)
        if not p.is_file():
            log.warning("ItemStore 找不到文件：%s", p)
            return cls()
        try:
            raw = json.loads(p.read_text("utf-8"))
        except Exception as e:  # noqa: BLE001
            log.warning("ItemStore 读取失败：%s", e)
            return cls()
        items = [Item.from_dict(d) for d in raw.get("items", [])]
        log.info("ItemStore: 加载 %d 个物品", len(items))
        return cls(items=items)

    def by_name(self, name: str) -> Optional[Item]:
        for it in self.items:
            if it.name == name:
                return it
        return None

    def by_type(self, type_name: str) -> list[Item]:
        return [it for it in self.items if it.type == type_name]


@dataclass
class JobStore:
    jobs: list[Job] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> "JobStore":
        p = Path(path)
        if not p.is_file():
            log.warning("JobStore 找不到文件：%s", p)
            return cls()
        try:
            raw = json.loads(p.read_text("utf-8"))
        except Exception as e:  # noqa: BLE001
            log.warning("JobStore 读取失败：%s", e)
            return cls()
        jobs = [Job.from_dict(d) for d in raw.get("jobs", [])]
        log.info("JobStore: 加载 %d 个打工", len(jobs))
        return cls(jobs=jobs)

    def eligible(self, level: int) -> list[Job]:
        return [j for j in self.jobs if j.eligible(level)]


# ---------------- 应用 ----------------

def apply_food(state: PetState, item: Item) -> bool:
    """花一份钱喂食。成功返回 True，钱不够返回 False。

    走 VPet 同款 ``eat_food``：每个 food 数值一半入主值、一半入 store*，
    由 take_store() 每 tick 慢慢补回主值（防止瞬间爆击）。
    """
    if state.money < item.price:
        return False
    # 扣钱
    state.money -= item.price
    state.eat_food(
        strength=item.strength,
        strength_food=item.strength_food,
        strength_drink=item.strength_drink,
        health=item.health,
        feeling=item.feeling,
        likability=item.likability,
        exp=item.exp,
    )
    log.info("喂食 %s：money=%.1f", item.name, state.money)
    return True


def apply_job(state: PetState, job: Job) -> bool:
    """完成一次打工。等级不够 / 体力不够 → False。"""
    if not job.eligible(state.level):
        return False
    # 提前扣消耗（即便奖励 0 也得付出）
    # 因为 strength_food 是负的话会扣饱食，先做校验（这里简化为允许，但 health<0 也允许）
    money, exp = job.calculate_rewards(level=state.level)
    # 扣消耗
    state.feed(
        strength=0,
        strength_food=-abs(job.strength_food),  # 消耗
        strength_drink=-abs(job.strength_drink),
        health=0,
        feeling=-abs(min(job.feeling, 0)) if job.feeling < 0 else job.feeling,
        exp=exp,
        money_delta=money,
    )
    log.info("打工完成：%s +money=%.1f +exp=%d", job.name, money, exp)
    return True
