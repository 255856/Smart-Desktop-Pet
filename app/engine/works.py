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


# 食物类型归一化：JSON 里 Snack / Snacks 拼写不一致，统一映射到标准 key
CATEGORY_ORDER = ["Drink", "Meal", "Snack", "Functional", "Drug", "Gift"]
CATEGORY_LABELS = {
    "Drink": "饮料",
    "Meal": "正餐",
    "Snack": "零食",
    "Functional": "功能",
    "Drug": "药品",
    "Gift": "礼物",
}
_TYPE_ALIASES = {"snacks": "Snack"}


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
    path: str = ""          # 食物图片（相对项目根，如 assets/food/rmbg-1.png）

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
            path=d.get("path", ""),
        )

    @property
    def category_key(self) -> str:
        """归一化类型（Snack/Snacks 合并为 Snack）。"""
        raw = (self.type or "").strip().lower()
        return _TYPE_ALIASES.get(raw, raw.capitalize())

    @property
    def category_label(self) -> str:
        return CATEGORY_LABELS.get(self.category_key, "其他")

    def image_path(self, base_dir: "str | Path | None" = None) -> Optional["Path"]:
        """解析食物图片绝对路径；文件不存在或未配置时返回 None。"""
        if not self.path:
            return None
        q = Path(self.path)
        if not q.is_absolute() and base_dir is not None:
            q = Path(base_dir) / q
        return q if q.is_file() else None


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
    # 物品库根目录（用于解析相对图片路径）；data/foods.json 时默认取项目根
    base_dir: Optional[Path] = None

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
        # data/foods.json 的父目录是 data/，再上一级是项目根（取绝对路径，避免 cwd 不同）
        base_dir = p.resolve().parent.parent
        log.info("ItemStore: 加载 %d 个物品", len(items))
        return cls(items=items, base_dir=base_dir)

    def by_name(self, name: str) -> Optional[Item]:
        for it in self.items:
            if it.name == name:
                return it
        return None

    def by_type(self, type_name: str) -> list[Item]:
        return [it for it in self.items if it.type == type_name]

    def grouped(self) -> list[tuple[str, list[Item]]]:
        """按归一化类别分组，返回 [(分类中文名, [物品...])]，按固定顺序排列。"""
        buckets: dict[str, list[Item]] = {}
        for it in self.items:
            buckets.setdefault(it.category_label, []).append(it)
        ordered = [CATEGORY_LABELS[k] for k in CATEGORY_ORDER
                   if CATEGORY_LABELS[k] in buckets]
        if "其他" in buckets:
            ordered.append("其他")
        return [(label, buckets[label]) for label in ordered]

    def image_path(self, item: Item) -> Optional[Path]:
        return item.image_path(self.base_dir)


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

def apply_food(state: PetState, item: Item, *, free: bool = False) -> bool:
    """喂食。成功返回 True，钱不够返回 False。

    走 VPet 同款 ``eat_food``：每个 food 数值一半入主值、一半入 store*，
    由 take_store() 每 tick 慢慢补回主值（防止瞬间爆击）。

    free=True 时为「主人手动投喂」（右键菜单），不扣金币；
    free=False 时为 LLM 用桌宠自己的金币买食物，钱不够会失败。

    另有两项防刷机制：饱食/口渴 ≥85 时对应补充效果 ×0.3（心情 ×0.5）；
    好感按自然日封顶（默认每日 5 点）。
    """
    if not free:
        if state.money < item.price:
            return False
        # 扣钱
        state.money -= item.price

    # 饱腹递减：饱食 / 口渴 ≥ 85 时「吃不下」，对应补充项 ×0.3，心情 ×0.5
    food_full = state.strength_food >= 85.0
    drink_full = state.strength_drink >= 85.0
    food_mul = 0.3 if food_full else 1.0
    drink_mul = 0.3 if drink_full else 1.0
    feel_mul = 0.5 if (food_full or drink_full) else 1.0

    state.eat_food(
        strength=item.strength * food_mul,
        strength_food=item.strength_food * food_mul,
        strength_drink=item.strength_drink * drink_mul,
        health=item.health,
        feeling=item.feeling * feel_mul,
        likability=0,   # 好感统一走 add_food_likability（每日上限）
        exp=item.exp,
    )
    state.add_food_likability(item.likability)
    log.info("喂食 %s（%s%s）：money=%.1f", item.name,
             "手动投喂" if free else "金币购买",
             "·饱腹递减" if (food_full or drink_full) else "",
             state.money)
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
