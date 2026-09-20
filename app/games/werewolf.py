# -*- coding: utf-8 -*-
"""桌宠小游戏：标准 9 人狼人杀（Werewolf / 米勒山谷狼人简化版）。

纯逻辑、无 Qt / 无 LLM 依赖，便于单元测试；LLM 多 Agent 与界面在其它模块。

角色配置（标准 9 人 · 屠边）：
    狼人 ×3、预言家 ×1、女巫 ×1、猎人 ×1、平民 ×3。

规则约定（第一版，偏"明牌 + 陪聊"，降低 LLM 作弊与理解成本）：
    - 9 个玩家席位：1 名真人 + 8 名 NPC；桌宠（主持人上帝）不占席位。
    - 夜晚顺序：狼人击杀 → 预言家查验 → 女巫救人/毒人（一夜最多用一瓶药）。
    - 女巫首夜可自救；女巫药各 1 瓶。
    - 猎人被狼人杀死或被投票放逐时可开枪带走 1 人；被女巫毒死不能开枪。
    - 被刀或被毒的玩家当晚结算，死者翻牌公开身份（明牌）。
    - 白天：公布死亡 → 遗言 → 依次发言 → 投票；平票则无人出局（平安日）。
    - 胜负（屠边）：狼人全灭 → 好人胜；平民全灭或神职全灭 → 狼人胜。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# ---------------- 角色 / 阵营 ----------------
WOLF = "wolf"
SEER = "seer"
WITCH = "witch"
HUNTER = "hunter"
VILLAGER = "villager"

ROLE_LABEL: Dict[str, str] = {
    WOLF: "狼人",
    SEER: "预言家",
    WITCH: "女巫",
    HUNTER: "猎人",
    VILLAGER: "平民",
}
GOD_ROLES = {SEER, WITCH, HUNTER}          # 神职
GOOD_ROLES = {SEER, WITCH, HUNTER, VILLAGER}

CAMP_WOLF = "wolves"
CAMP_GOOD = "good"

# 标准 9 人牌堆
ROLESET_9: List[str] = [WOLF, WOLF, WOLF, SEER, WITCH, HUNTER,
                        VILLAGER, VILLAGER, VILLAGER]

# ---------------- 阶段 ----------------
PHASE_NIGHT = "night"
PHASE_REVEAL = "reveal"
PHASE_SPEECH = "speech"
PHASE_VOTE = "vote"
PHASE_OVER = "over"

# 死因
CAUSE_WOLF = "wolf"
CAUSE_POISON = "poison"
CAUSE_VOTE = "vote"
CAUSE_HUNTER = "hunter"

NPC_NAMES = [
    "阿橘", "小豆子", "糖糖", "铁柱", "小满", "布丁", "薄荷", "汤圆",
]


def camp_of(role: str) -> str:
    """阵营：狼 / 好人。"""
    return CAMP_WOLF if role == WOLF else CAMP_GOOD


# ---------------- 数据结构 ----------------
@dataclass
class Player:
    seat: int
    name: str
    role: str = ""
    is_human: bool = False
    alive: bool = True
    has_antidote: bool = False
    has_poison: bool = False
    death_cause: str = ""

    @property
    def is_wolf(self) -> bool:
        return self.role == WOLF

    @property
    def is_god(self) -> bool:
        return self.role in GOD_ROLES


@dataclass
class NightOutcome:
    """一夜结算结果（由 resolve_night 产出）。"""
    day: int
    wolf_target: Optional[int]            # 狼人今晚刀的座位
    seer_target: Optional[int]             # 预言家查验的座位
    seer_found_wolf: Optional[bool]        # 被查验者是否为狼
    saved: bool                            # 女巫是否用解药
    poisoned: Optional[int]                # 女巫毒的座位
    deaths: List[int] = field(default_factory=list)   # 当晚最终死亡（自然死亡）
    peaceful: bool = False                 # 平安夜


@dataclass
class VoteOutcome:
    votes: Dict[int, int] = field(default_factory=dict)   # 投票人 → 候选人
    tally: Dict[int, int] = field(default_factory=dict)   # 候选人 → 票数
    exiled: Optional[int] = None
    tied: bool = False


@dataclass
class SpeechRecord:
    seat: int
    name: str
    text: str
    day: int
    kind: str = "speech"   # speech / last_words / host / night


@dataclass
class PublicEvent:
    """对所有人公开的事件（死亡、投票、查验结果等）。"""
    day: int
    text: str


# ---------------- 游戏主体 ----------------
class WerewolfGame:
    """标准 9 人狼人杀状态机。

    本类只负责"确定性结算"；各角色动作（说什么、投谁、刀谁）由外层
    Director 收集（LLM Agent 或真人），再交给本类方法结算。
    """

    def __init__(self, names: Optional[List[str]] = None,
                 human_seat: int = 0, rng: Optional[random.Random] = None):
        self.rng = rng or random.Random()
        self.human_seat = human_seat
        names = names or NPC_NAMES
        # 席位 0..8：human_seat 是真人，其余给 NPC 名
        full = list(names)
        if len(full) < 9:
            full = full + [f"NPC{i}" for i in range(9 - len(full))]
        full = full[:9]
        self.players: List[Player] = [
            Player(seat=i, name=full[i], is_human=(i == human_seat))
            for i in range(9)
        ]
        self.day = 0
        self.phase: str = ""
        self.winner: Optional[str] = None
        self.public_events: List[PublicEvent] = []
        self.speeches: List[SpeechRecord] = []
        # 预言家查验历史（只对预言家可见）：list[(day, target, found_wolf)]
        self.seer_history: List[Tuple[int, int, bool]] = []
        self.last_outcome: Optional[NightOutcome] = None
        self.dealt = False
        self._night_dead: List[int] = []

    # ---------------- 基础查询 ----------------
    def player(self, seat: int) -> Player:
        return self.players[seat]

    def human(self) -> Player:
        return self.players[self.human_seat]

    def alive_seats(self) -> List[int]:
        return [p.seat for p in self.players if p.alive]

    def alive_players(self) -> List[Player]:
        return [p for p in self.players if p.alive]

    def wolves(self) -> List[Player]:
        return [p for p in self.players if p.role == WOLF]

    def alive_wolves(self) -> List[Player]:
        return [p for p in self.players if p.role == WOLF and p.alive]

    def alive_villagers(self) -> List[Player]:
        return [p for p in self.players if p.role == VILLAGER and p.alive]

    def alive_gods(self) -> List[Player]:
        return [p for p in self.players if p.role in GOD_ROLES and p.alive]

    def seat_of_role(self, role: str) -> Optional[int]:
        for p in self.players:
            if p.role == role:
                return p.seat
        return None

    # ---------------- 发牌 ----------------
    def deal(self) -> Dict[str, str]:
        """随机分配身份，返回 {seat: role}（仅用于内部/测试，玩家各自看自己的）。"""
        roles = list(ROLESET_9)
        self.rng.shuffle(roles)
        assignment = {}
        for i, p in enumerate(self.players):
            p.role = roles[i]
            p.alive = True
            p.death_cause = ""
            if p.role == WITCH:
                p.has_antidote = True
                p.has_poison = True
            assignment[p.seat] = p.role
        self.dealt = True
        return assignment

    # ---------------- 夜晚结算 ----------------
    def resolve_night(self, wolf_target: Optional[int],
                      seer_target: Optional[int],
                      witch_save: bool, witch_poison: Optional[int]) -> NightOutcome:
        """结算一晚（夜晚各动作已由 Director 收集）。

        - wolf_target：狼人统一刀的座位
        - seer_target：预言家查验的座位
        - witch_save：女巫是否对狼刀目标使用解药
        - witch_poison：女巫毒药目标（None 不用）；解药与毒药一夜最多用一瓶
        """
        self.day += 1
        self._night_dead = []
        witch = next((p for p in self.players if p.role == WITCH), None)

        # 预言家查验（先于死亡结算，查验不受当晚死亡影响）
        seer_found = None
        if seer_target is not None:
            seer_found = self.players[seer_target].role == WOLF
            self.seer_history.append((self.day, seer_target, bool(seer_found)))

        saved = False
        poisoned = None
        # 女巫一夜最多用一瓶药：用了解药则毒药忽略
        use_poison = (witch_poison is not None) and not witch_save
        if witch_save and witch is not None and witch.has_antidote:
            witch.has_antidote = False
            saved = True
        if use_poison and witch is not None and witch.has_poison:
            if witch_poison != witch.seat and self.players[witch_poison].alive:
                witch.has_poison = False
                poisoned = witch_poison

        # 狼刀目标：被救则不死；同刀同毒时死因为毒（猎人不能开枪）
        if wolf_target is not None and not saved:
            cause = CAUSE_POISON if wolf_target == poisoned else CAUSE_WOLF
            self._mark_dead(wolf_target, cause)
        # 毒药目标（与刀目标不同）
        if poisoned is not None and poisoned != wolf_target:
            self._mark_dead(poisoned, CAUSE_POISON)

        deaths = list(self._night_dead)
        outcome = NightOutcome(
            day=self.day,
            wolf_target=wolf_target,
            seer_target=seer_target,
            seer_found_wolf=seer_found,
            saved=saved,
            poisoned=poisoned,
            deaths=deaths,
            peaceful=len(deaths) == 0,
        )
        self.last_outcome = outcome
        self._announce_night(outcome)
        return outcome

    def _mark_dead(self, seat: int, cause: str) -> None:
        p = self.players[seat]
        if not p.alive:
            return
        p.alive = False
        p.death_cause = cause
        if seat not in self._night_dead:
            self._night_dead.append(seat)

    # ---------------- 白天 / 投票 ----------------
    def tally_votes(self, votes: Dict[int, int]) -> VoteOutcome:
        """统计投票。votes: {投票人seat: 目标seat}；返回唱票结果，平票则无人出局。"""
        tally: Dict[int, int] = {}
        valid_votes: Dict[int, int] = {}
        for src, tgt in votes.items():
            if not self.players[src].alive or not self.players[tgt].alive:
                continue
            if src == tgt:
                continue
            valid_votes[src] = tgt
            tally[tgt] = tally.get(tgt, 0) + 1
        vo = VoteOutcome(votes=valid_votes, tally=tally)
        if not tally:
            vo.tied = True
            return vo
        maxv = max(tally.values())
        leaders = [s for s, n in tally.items() if n == maxv]
        if len(leaders) > 1:
            vo.tied = True          # 平票：无人出局
        else:
            vo.exiled = leaders[0]
        return vo

    def exile(self, seat: int) -> None:
        """投票放逐（翻牌）。"""
        self._kill_public(seat, CAUSE_VOTE)

    def hunter_shot(self, seat: int, target: int) -> bool:
        """猎人开枪：被毒不能开枪，必须已被狼刀/放逐（死亡结算时），目标须存活。"""
        hunter = self.players[seat]
        if hunter.role != HUNTER:
            return False
        if hunter.alive or hunter.death_cause == CAUSE_POISON:
            return False
        if not self.players[target].alive or target == seat:
            return False
        self._kill_public(target, CAUSE_HUNTER)
        return True

    def can_hunt(self, seat: int) -> bool:
        """该死亡猎人是否具备开枪资格（死因非毒）。"""
        p = self.players[seat]
        return p.role == HUNTER and p.death_cause != CAUSE_POISON

    def _kill_public(self, seat: int, cause: str) -> None:
        p = self.players[seat]
        if not p.alive:
            return
        p.alive = False
        p.death_cause = cause

    # ---------------- 胜负 ----------------
    def check_winner(self) -> Optional[str]:
        """返回 CAMP_WOLF / CAMP_GOOD / None。"""
        if not self.alive_wolves():
            self.winner = CAMP_GOOD
            return CAMP_GOOD
        if not self.alive_villagers() or not self.alive_gods():
            self.winner = CAMP_WOLF
            return CAMP_WOLF
        return None

    # ---------------- 视角过滤 ----------------
    def role_visible_to(self, target: int, viewer: int) -> Optional[str]:
        """viewer 视角下 target 的角色：可见返回角色，否则返回 None。"""
        t = self.players[target]
        v = self.players[viewer]
        if target == viewer:
            return t.role
        # 死者翻牌：所有人可见
        if not t.alive:
            return t.role
        # 狼队友互知
        if v.role == WOLF and t.role == WOLF:
            return WOLF
        return None

    def perspective(self, viewer: int) -> Dict[str, object]:
        """组装某玩家可见的局面（喂给对应 Agent）。"""
        v = self.players[viewer]
        seats = []
        for p in self.players:
            role = self.role_visible_to(p.seat, viewer)
            seats.append({
                "seat": p.seat,
                "name": p.name,
                "alive": p.alive,
                "role": role,   # None 表示未知
            })
        view: Dict[str, object] = {
            "self_seat": viewer,
            "self_role": v.role,
            "self_camp": camp_of(v.role),
            "seats": seats,
            "day": self.day,
            "public_events": [e.text for e in self.public_events],
            "speeches": [
                {"seat": s.seat, "name": s.name, "text": s.text,
                 "day": s.day, "kind": s.kind}
                for s in self.speeches
            ],
        }
        if v.role == WOLF:
            view["wolf_teammates"] = [p.seat for p in self.players
                                      if p.role == WOLF and p.seat != viewer]
        if v.role == SEER:
            view["seer_checks"] = [
                {"target": t, "is_wolf": f} for _, t, f in self.seer_history]
        if v.role == WITCH:
            view["witch_antidote"] = v.has_antidote
            view["witch_poison"] = v.has_poison
            # 仅在夜晚结算前由 Director 单独告知狼刀目标（不放在静态视角里）
        return view

    # ---------------- 公开事件 / 发言 ----------------
    def add_event(self, text: str) -> None:
        self.public_events.append(PublicEvent(self.day, text))

    def add_speech(self, seat: int, text: str, kind: str = "speech") -> None:
        text = (text or "").strip()
        if not text:
            return
        p = self.players[seat]
        self.speeches.append(
            SpeechRecord(seat=seat, name=p.name, text=text,
                         day=self.day, kind=kind))

    # ---------------- 夜晚事件播报（确定性模板） ----------------
    def _announce_night(self, out: NightOutcome) -> None:
        self.add_event(f"第 {out.day} 天：天黑了，请闭眼。")
        if out.peaceful:
            self.add_event(f"第 {out.day} 天：天亮了，昨夜是平安夜，无人死亡。")
        else:
            for s in out.deaths:
                p = self.players[s]
                self.add_event(
                    f"第 {out.day} 天：天亮了，{p.name}（{ROLE_LABEL[p.role]}）昨夜死亡。")
