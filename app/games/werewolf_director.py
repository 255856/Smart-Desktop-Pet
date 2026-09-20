# -*- coding: utf-8 -*-
"""狼人杀 Director：后台线程里的异步"导演"，驱动完整一局并与 UI 同步。

- 运行在独立 QThread（仿 app/brain/proactive.py 的 _ChatOnceWorker），
  在自己的 asyncio 事件循环里为每个 NPC / 主持人创建独立 LLMClient。
- 确定性规则结算全部交给 WerewolfGame；Agent 只负责台词与决策。
- 轮到真人玩家时发 request_action 信号并等待 UI 回填（threading.Event，
  用 run_in_executor 等待，不阻塞事件循环）。
- 玩家所有操作均有 120 秒倒计时，期间每秒发 countdown 信号；超时按默认值处理。

扩展规则：
- 第一天白天先进行警长竞选（举手 → 竞选演说 → 警下投票，平票 PK）。
- 白天投票平票时进入 PK（平票者演说 → 只在平票者中再投）。
- 夜晚狼队在独立的"狼频道"讨论，与公共频道分离，仅狼可见。
- 警长投票 1.5 票，死亡时可移交警徽（或撕掉）。
"""
from __future__ import annotations

import asyncio
import logging
import random
import threading
from collections import Counter
from typing import Dict, List, Optional

from app.core.qt_compat import QThread, Signal
from app.games.werewolf import (
    WerewolfGame, ROLE_LABEL,
    WOLF, SEER, WITCH, HUNTER, VILLAGER,
    CAMP_WOLF, CAMP_GOOD,
    SPEECH_SPEECH, SPEECH_LAST, SPEECH_CAMPAIGN, SPEECH_PK,
)
from app.games.werewolf_agents import PERSONAS, WerewolfAgent, HostAgent

log = logging.getLogger(__name__)

HUMAN_NAME = "你"
ACTION_TIMEOUT = 120          # 玩家操作倒计时（秒）

# 发言 kind → 中文标签
_KIND_LABEL = {
    SPEECH_SPEECH: "发言",
    SPEECH_LAST: "遗言",
    SPEECH_CAMPAIGN: "竞选",
    SPEECH_PK: "PK",
}


class WerewolfDirector(QThread):
    """跑一整局狼人杀。开始：start()；玩家操作：submit_action(payload)。"""

    # 主持人说话（TTS 朗读）
    host_message = Signal(str)
    # 玩家 / NPC 公共发言（仅文字）：(seat, name, text, kind)
    speech = Signal(int, str, str, str)
    # 狼频道发言（仅玩家是狼时显示）：(seat, name, text)
    wolf_chat = Signal(int, str, str)
    # 开局告知玩家自己身份：role
    your_role = Signal(str)
    # 夜晚私密频道（预言家查验结果 / 女巫信息），仅玩家可见
    private_channel = Signal(str)
    # 请求玩家操作：(kind, options, context)
    request_action = Signal(str, object, dict)
    # 玩家操作倒计时（剩余秒数）
    countdown = Signal(int)
    # 座位 / 局面刷新（玩家视角 snapshot）
    state_changed = Signal(object)
    # 结束：{winner, player_won, player_role, days}
    game_over = Signal(dict)
    failed = Signal(str)

    def __init__(self, llm_cfg=None, enable_llm: bool = False,
                 player_name: str = HUMAN_NAME, pet_name: str = "桌宠",
                 parent=None):
        super().__init__(parent)
        self.llm_cfg = llm_cfg
        self.enable_llm = enable_llm
        self.player_name = player_name
        self.pet_name = pet_name
        self.rng = random.Random()

        self.game = WerewolfGame(names=[player_name] + [p["name"] for p in PERSONAS],
                                 human_seat=0, rng=self.rng)
        self.agents: Dict[int, WerewolfAgent] = {
            p.seat: WerewolfAgent(self.game, p.seat,
                                  PERSONAS[p.seat - 1])
            for p in self.game.players if not p.is_human
        }
        self.host = HostAgent(client=None, pet_name=pet_name)

        self._human_event = threading.Event()
        self._human_action: object = None
        self._stop = threading.Event()
        self.max_days = 10          # 僵局兜底：最多打 10 天

    # ---------------- 玩家操作回填（主线程调用） ----------------
    def submit_action(self, payload: object) -> None:
        self._human_action = payload
        self._human_event.set()

    def request_stop(self) -> None:
        self._stop.set()
        self._human_event.set()

    # ---------------- 线程主流程 ----------------
    def run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._play())
        except Exception as e:  # noqa: BLE001
            log.exception("狼人杀导演异常")
            self.failed.emit(f"{e!r}")
        finally:
            try:
                loop.run_until_complete(self._close_clients(loop))
            except Exception:
                pass
            loop.close()

    async def _close_clients(self, loop) -> None:
        for a in self.agents.values():
            if a.client is not None:
                try:
                    await a.client.close()
                except Exception:
                    pass
        if self.host.client is not None:
            try:
                await self.host.client.close()
            except Exception:
                pass

    # ---------------- 完整一局 ----------------
    async def _play(self) -> None:
        g = self.game
        if self.enable_llm and self.llm_cfg is not None:
            from app.brain.llm_client import LLMClient
            for a in self.agents.values():
                a.bind_client(LLMClient(self.llm_cfg, ""))
            self.host.bind_client(LLMClient(self.llm_cfg, self.host._system))

        g.deal()
        human = g.human()
        self.your_role.emit(human.role)
        self._emit_state()
        await self._host(await self.host.flavor("opening"))
        await self._pace(0.6)

        winner = None
        while winner is None and not self._stop.is_set():
            if g.day >= self.max_days:
                n_wolf = len(g.alive_wolves())
                n_good = len(g.alive_players()) - n_wolf
                winner = CAMP_WOLF if n_wolf >= n_good else CAMP_GOOD
                break
            await self._night()
            winner = g.check_winner()
            if winner is not None:
                break
            await self._day()
            winner = g.check_winner()

        if self._stop.is_set():
            return
        await self._finish(winner)

    # ---------------- 夜晚 ----------------
    async def _night(self) -> None:
        g = self.game
        night_no = g.day + 1
        await self._host(f"天黑请闭眼，第 {night_no} 夜开始。")
        await self._pace(0.4)

        alive = g.alive_seats()
        wolves = [p.seat for p in g.alive_wolves()]
        human_seat = g.human_seat
        is_human_wolf = human_seat in wolves
        non_wolf = [s for s in alive if s not in wolves]

        # ---- 狼队：独立狼频道讨论 + 提名（频道与公共频道隔离） ----
        nominations: Dict[int, int] = {}
        npc_wolves = [s for s in wolves if s != human_seat]
        if npc_wolves:
            results = await asyncio.gather(
                *[self.agents[s].wolf_chat_message(non_wolf) for s in npc_wolves])
            for s, (text, t) in zip(npc_wolves, results):
                g.add_wolf_chat(s, text)
                if is_human_wolf:
                    self._emit_wolf_chat(s, text)
                if t is not None and t in non_wolf:
                    nominations[s] = t
        if is_human_wolf:
            mates = [s for s in wolves if s != human_seat]
            self.private_channel.emit(
                "【狼人频道】夜晚到了，与队友讨论今晚刀谁（仅狼可见）。"
                + ("队友：" + "、".join(f"{s}号" for s in mates) if mates else ""))
            msg = await self._ask_human(
                "wolf_chat", None,
                {"hint": "在狼频道和队友讨论（仅狼可见）"},
                default="我建议先刀神职，听队友的。")
            msg = str(msg or "").strip()
            if msg:
                g.add_wolf_chat(human_seat, msg)
                self._emit_wolf_chat(human_seat, msg)
            t = await self._ask_human(
                "wolf_kill", list(non_wolf),
                {"teammates": mates, "hint": "选择今晚刀的目标"},
                default=non_wolf[0] if non_wolf else None)
            ti = self._as_int(t)
            if ti is not None and ti in non_wolf:
                nominations[human_seat] = ti
        wolf_target = self._majority(list(nominations.values()))

        # ---- 预言家查验 ----
        seer = g.seat_of_role(SEER)
        seer_target = None
        if seer is not None and g.player(seer).alive:
            checked = {t for _, t, _ in g.seer_history}
            unchecked = [s for s in alive if s not in checked]
            if seer == human_seat:
                seer_target = self._as_int(
                    await self._ask_human(
                        "seer_check", list(unchecked),
                        {"hint": "选择今晚查验的对象"},
                        default=unchecked[0] if unchecked else None))
            elif unchecked:
                seer_target = await self.agents[seer].seer_check(unchecked)

        # ---- 女巫 ----
        witch = g.seat_of_role(WITCH)
        witch_save = False
        witch_poison = None
        if witch is not None and g.player(witch).alive:
            w = g.player(witch)
            poison_candidates = [s for s in alive if s != witch]
            if witch == human_seat:
                decision = await self._ask_human(
                    "witch",
                    {"killed": wolf_target,
                     "can_save": bool(w.has_antidote and wolf_target is not None),
                     "can_poison": bool(w.has_poison),
                     "poison_candidates": poison_candidates},
                    {"hint": "选择解药 / 毒药 / 不用"},
                    default={"action": "none"})
                witch_save, witch_poison = self._parse_witch(decision)
            else:
                decision = await self.agents[witch].witch_decide(
                    wolf_target, poison_candidates)
                if decision.get("use") == "antidote":
                    witch_save = True
                elif decision.get("use") == "poison":
                    witch_poison = self._as_int(decision.get("target"))

        out = g.resolve_night(wolf_target, seer_target,
                              witch_save, witch_poison)

        # 预言家结果私信
        if seer == human_seat and g.player(human_seat).alive and \
                out.seer_target is not None:
            verdict = "狼人" if out.seer_found_wolf else "好人"
            self.private_channel.emit(
                f"【预言家】你查验了 {out.seer_target} 号，TA 是{verdict}。")

        if not is_human_wolf and seer != human_seat and witch != human_seat:
            self.private_channel.emit(f"【夜晚】第 {night_no} 夜，你闭上了眼睛……")

    # ---------------- 白天 ----------------
    async def _day(self) -> None:
        g = self.game
        out = g.last_outcome
        if out is None:
            return
        # 公布死亡 + 遗言 + 猎人开枪
        if out.peaceful:
            await self._host("天亮了，昨夜是平安夜，无人死亡！")
        else:
            for s in list(out.deaths):
                p = g.player(s)
                await self._host(f"天亮了，{p.name}（{ROLE_LABEL[p.role]}）昨夜死亡。")
                await self._last_words(s)
                if p.role == HUNTER and g.can_hunt(s):
                    await self._hunter_shoot(s)
        self._emit_state()
        # 夜晚死亡的警长移交警徽
        await self._maybe_transfer_sheriff()
        await self._pace(0.6)
        if g.check_winner() is not None:
            return

        # 第一天白天：警长竞选
        if g.day == 1 and g.sheriff is None:
            await self._campaign_sheriff()
            if g.check_winner() is not None:
                return

        # 白天发言：从死者下一位开始轮转，存活者依次发言
        start = (out.deaths[0] + 1) % 9 if out.deaths else (
            g.sheriff if g.sheriff is not None else 0)
        order = [s % 9 for s in range(start, start + 9)
                 if g.player(s % 9).alive]
        for s in order:
            if not g.player(s).alive:
                continue
            if s == g.human_seat:
                text = await self._ask_human(
                    "speech", None, {"hint": "轮到你发言"},
                    default="我是好人，过。")
                text = str(text or "").strip() or "我是好人，过。"
            else:
                already = self._today_speeches()
                text = await self.agents[s].day_speech(already)
                await self._pace(0.35)
            g.add_speech(s, text, SPEECH_SPEECH)
            self._emit_speech(s, text, SPEECH_SPEECH)
        await self._pace(0.3)

        # 投票（含平票 PK）
        await self._vote_phase()

    # ---------------- 警长竞选 ----------------
    async def _campaign_sheriff(self) -> None:
        g = self.game
        alive = g.alive_seats()
        human = g.human_seat
        await self._host("第一天白天，竞选警长：想要警徽的玩家请举手。")
        await self._pace(0.4)

        npc = [s for s in alive if s != human]
        runs = await asyncio.gather(
            *[self.agents[s].run_for_sheriff() for s in npc])
        runners = [s for s, r in zip(npc, runs) if r]
        if human in alive:
            r = await self._ask_human(
                "run_sheriff", ["run", "skip"],
                {"hint": "是否竞选警长？"}, default=False)
            if self._truthy(r):
                runners.append(human)
        runners = sorted(set(runners))
        if not runners:
            await self._host("没有人举手竞选，本局没有警长。")
            return
        await self._host("参选者：" + "、".join(
            f"{s}号{g.player(s).name}" for s in runners))
        await self._pace(0.3)

        # 竞选演说（公共频道）
        for s in runners:
            if s == human:
                text = await self._ask_human(
                    "campaign_speech", None, {"hint": "发表竞选演说"},
                    default="我是好人，警长给我，我来带节奏！")
                text = str(text or "").strip() or "我是好人，警长给我！"
            else:
                text = await self.agents[s].campaign_speech()
                await self._pace(0.35)
            g.add_speech(s, text, SPEECH_CAMPAIGN)
            self._emit_speech(s, text, SPEECH_CAMPAIGN)

        # 警下（未参选者）投票
        voters = [s for s in alive if s not in runners]
        if not voters:
            await self._host("全员参选，无人投票，本局没有警长。")
            return
        sheriff = await self._sheriff_vote(runners, voters)
        if sheriff is None:
            return
        g.make_sheriff(sheriff)
        await self._host(
            f"{g.player(sheriff).name}（{sheriff}号）当选警长，"
            "警长投票算 1.5 票，负责归票！")
        self._emit_state()

    async def _sheriff_vote(self, runners: List[int],
                            voters: List[int]) -> Optional[int]:
        """警下投票选警长，平票则 PK 演说再投，仍平票则无警长。"""
        g = self.game
        human = g.human_seat
        votes = await self._collect_sheriff_votes(runners, voters)
        leaders = self._leaders(g.tally_votes(votes, allowed=runners))
        if not leaders:
            await self._host("警下无人投票，本局没有警长。")
            return None
        if len(leaders) == 1:
            return leaders[0]

        await self._host("竞选平票：" + self._names(leaders) +
                         "，请平票者进行 PK 演说！")
        await self._pace(0.3)
        for s in leaders:
            if s == human:
                text = await self._ask_human(
                    "pk_speech", None, {"hint": "发表 PK 演说"},
                    default="我是好人，请把警徽投给我！")
                text = str(text or "").strip() or "我是好人，请投我！"
            else:
                text = await self.agents[s].pk_speech()
                await self._pace(0.35)
            g.add_speech(s, text, SPEECH_PK)
            self._emit_speech(s, text, SPEECH_PK)

        votes2 = await self._collect_sheriff_votes(leaders, voters)
        leaders2 = self._leaders(g.tally_votes(votes2, allowed=leaders))
        if len(leaders2) == 1:
            return leaders2[0]
        await self._host("再次平票，本局没有警长。")
        return None

    async def _collect_sheriff_votes(self, runners: List[int],
                                     voters: List[int]) -> Dict[int, int]:
        g = self.game
        human = g.human_seat
        votes: Dict[int, int] = {}
        npc = [s for s in voters if s != human]
        results = await asyncio.gather(
            *[self.agents[s].vote_sheriff(runners) for s in npc])
        for s, t in zip(npc, results):
            if t is not None and t in runners:
                votes[s] = t
        if human in voters:
            t = await self._ask_human(
                "vote_sheriff", list(runners),
                {"hint": "把警徽投给哪位参选者？（可弃票）"}, default=None)
            ti = self._as_int(t)
            if ti is not None and ti in runners:
                votes[human] = ti
        return votes

    # ---------------- 白天投票 + PK ----------------
    async def _vote_phase(self) -> None:
        g = self.game
        await self._host("发言结束，开始投票！")
        votes = await self._collect_votes()
        vo = g.tally_votes(votes, weights=g.vote_weights())
        if not vo.tied and vo.exiled is not None:
            await self._exile_player(vo.exiled, vo.tally)
            return
        if not vo.tally:
            await self._host("全员弃票，今天是平安日，无人出局。")
            return

        leaders = self._leaders(vo)
        if len(leaders) >= 2:
            await self._host("投票平票：" + self._names(leaders) + "，进入 PK！")
            await self._pace(0.3)
            human = g.human_seat
            for s in leaders:
                if s == human:
                    text = await self._ask_human(
                        "pk_speech", None, {"hint": "发表 PK 演说"},
                        default="我是好人，请不要投我！")
                    text = str(text or "").strip() or "我是好人，请不要投我！"
                else:
                    text = await self.agents[s].pk_speech()
                    await self._pace(0.35)
                g.add_speech(s, text, SPEECH_PK)
                self._emit_speech(s, text, SPEECH_PK)

            votes2 = await self._collect_votes(allowed_targets=leaders)
            vo2 = g.tally_votes(votes2, weights=g.vote_weights(),
                                allowed=leaders)
            if not vo2.tied and vo2.exiled is not None:
                await self._exile_player(vo2.exiled, vo2.tally)
                return
        await self._host("PK 后仍无法分出胜负，今天是平安日，无人出局。")
        await self._host(await self.host.flavor("peace_vote"))

    async def _collect_votes(self, allowed_targets: Optional[List[int]] = None
                             ) -> Dict[int, int]:
        """收集一轮放逐投票；allowed_targets 限定候选（PK 时）。"""
        g = self.game
        human = g.human_seat
        votes: Dict[int, int] = {}
        npc = [s for s in g.alive_seats() if s != human]
        cands_for: Dict[int, List[int]] = {}
        for s in npc:
            cands = (list(allowed_targets) if allowed_targets is not None
                     else self._vote_candidates(s))
            cands_for[s] = [t for t in cands if t != s]
        results = await asyncio.gather(
            *[self.agents[s].vote(cands_for[s]) for s in npc])
        for s, t in zip(npc, results):
            if t is not None and t in cands_for[s]:
                votes[s] = t
        if g.player(human).alive:
            cands = (list(allowed_targets) if allowed_targets is not None
                     else self._vote_candidates(human))
            cands = [t for t in cands if t != human]
            t = await self._ask_human(
                "vote", list(cands),
                {"hint": "选择投票对象（可弃票）"}, default=None)
            ti = self._as_int(t)
            if ti is not None and ti in cands:
                votes[human] = ti
        return votes

    async def _exile_player(self, ex: int, tally: Dict[int, float]) -> None:
        g = self.game
        g.exile(ex)
        ep = g.player(ex)
        await self._host(f"唱票：{self._tally_text(tally)}。{ep.name} 被投票放逐，"
                         f"TA 的身份是 {ROLE_LABEL[ep.role]}！")
        self._emit_state()
        await self._last_words(ex)
        if ep.role == HUNTER and g.can_hunt(ex):
            await self._hunter_shoot(ex)
        await self._maybe_transfer_sheriff()
        self._emit_state()

    # ---------------- 警徽移交 ----------------
    async def _maybe_transfer_sheriff(self) -> None:
        """若警长已死亡，让其移交（或撕掉）警徽。"""
        g = self.game
        if g.sheriff is None or g.player(g.sheriff).alive:
            return
        dying = g.sheriff
        candidates = g.alive_seats()
        if not candidates:
            g.transfer_sheriff(None)
            await self._host("警长阵亡，已无人可移交，警徽被撕毁。")
            return
        await self._host(f"警长 {g.player(dying).name} 出局，可以移交警徽。")
        if dying == g.human_seat:
            opts = list(candidates) + ["tear"]
            t = await self._ask_human(
                "transfer_badge", opts,
                {"hint": "把警徽移交给谁？（或撕掉）"}, default=None)
            if isinstance(t, str) and t == "tear":
                target: Optional[int] = None
            else:
                ti = self._as_int(t)
                target = ti if ti in candidates else None
        else:
            target = await self.agents[dying].transfer_badge(candidates)
            await self._pace(0.3)
        g.transfer_sheriff(target)
        if target is not None:
            await self._host(f"警徽移交给了 {g.player(target).name}"
                             f"（{target}号）！")
        else:
            await self._host("警长撕掉了警徽，本局不再有警长。")
        self._emit_state()

    # ---------------- 遗言 / 猎人 ----------------
    async def _last_words(self, seat: int) -> None:
        g = self.game
        if seat == g.human_seat:
            text = await self._ask_human(
                "last_words", None, {"hint": "请留一句遗言"},
                default="我无话可说……")
            text = str(text or "").strip() or "我无话可说……"
        else:
            text = await self.agents[seat].last_words()
            await self._pace(0.3)
        g.add_speech(seat, text, SPEECH_LAST)
        self._emit_speech(seat, text, SPEECH_LAST)

    async def _hunter_shoot(self, seat: int) -> None:
        g = self.game
        candidates = [s for s in g.alive_seats() if s != seat]
        await self._host(f"{g.player(seat).name} 是猎人，可以开枪带走一人！")
        if seat == g.human_seat:
            target = self._as_int(
                await self._ask_human("hunter", list(candidates),
                                      {"hint": "选择开枪目标（可放弃）"},
                                      default=None))
        else:
            target = await self.agents[seat].hunter_shoot(candidates)
            await self._pace(0.3)
        if target is not None and g.hunter_shot(seat, target):
            tp = g.player(target)
            await self._host(f"砰！猎人开枪带走了 {tp.name}"
                             f"（{ROLE_LABEL[tp.role]}）！")
            self._emit_state()
            await self._last_words(target)

    # ---------------- 结束 ----------------
    async def _finish(self, winner: Optional[str]) -> None:
        g = self.game
        human = g.human()
        player_won = (winner is not None and
                      (CAMP_WOLF if human.role == WOLF else CAMP_GOOD) == winner)
        if winner == CAMP_GOOD:
            await self._host(await self.host.flavor("good_win"))
        elif winner == CAMP_WOLF:
            await self._host(await self.host.flavor("wolf_win"))
        self.game_over.emit({
            "winner": winner,
            "player_won": bool(player_won),
            "player_role": human.role,
            "days": g.day,
        })

    # ---------------- 工具 ----------------
    def _vote_candidates(self, seat: int) -> List[int]:
        return [s for s in self.game.alive_seats() if s != seat]

    def _today_speeches(self) -> str:
        g = self.game
        rows = [s for s in g.speeches
                if s.day == g.day and s.kind == SPEECH_SPEECH]
        if not rows:
            return "（今天还没有人发言）"
        return "\n".join(f"{s.name}：{s.text}" for s in rows[-20:])

    def _leaders(self, vo) -> List[int]:
        if not vo.tally:
            return []
        mx = max(vo.tally.values())
        return [s for s, n in vo.tally.items() if n == mx]

    def _names(self, seats: List[int]) -> str:
        return "、".join(f"{s}号{self.game.player(s).name}" for s in seats)

    def _tally_text(self, tally: Dict[int, float]) -> str:
        items = sorted(tally.items(), key=lambda x: -x[1])
        return "，".join(f"{self.game.player(k).name} {v:g}票"
                         for k, v in items)

    def _majority(self, targets: List[int]) -> Optional[int]:
        if not targets:
            return None
        cnt = Counter(targets)
        top = cnt.most_common()
        leaders = [t for t, c in top if c == top[0][1]]
        return self.rng.choice(leaders)

    def _as_int(self, v) -> Optional[int]:
        if isinstance(v, dict):
            v = v.get("target")
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    def _truthy(self, v) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, dict):
            return bool(v.get("run") or v.get("action") == "run")
        if isinstance(v, str):
            return v in ("run", "yes", "true", "1")
        return bool(v)

    def _parse_witch(self, payload) -> tuple:
        save, poison = False, None
        if isinstance(payload, dict):
            action = payload.get("action") or payload.get("use")
            if action == "save" or action == "antidote":
                save = True
            elif action == "poison":
                poison = self._as_int(payload.get("target"))
        return save, poison

    # ---------------- 信号 / 玩家等待 ----------------
    async def _host(self, text: str) -> None:
        if text:
            self.host_message.emit(text)

    def _emit_speech(self, seat: int, text: str, kind: str) -> None:
        name = self.game.player(seat).name
        self.speech.emit(seat, name, text, kind)

    def _emit_wolf_chat(self, seat: int, text: str) -> None:
        name = self.game.player(seat).name
        self.wolf_chat.emit(seat, name, text)

    def _emit_state(self) -> None:
        self.state_changed.emit(self.game.perspective(self.game.human_seat))

    async def _pace(self, seconds: float) -> None:
        if self._stop.is_set():
            return
        await asyncio.sleep(seconds)

    async def _ask_human(self, kind: str, options, context: dict,
                         default=None, timeout: int = ACTION_TIMEOUT):
        """请求玩家操作并等待回填；倒计时到点返回 default。"""
        loop = asyncio.get_event_loop()
        self._human_event.clear()
        self._human_action = None
        self.request_action.emit(kind, options, context)
        self.countdown.emit(timeout)

        def _wait():
            remaining = timeout
            while remaining > 0:
                if self._human_event.wait(timeout=1.0):
                    return self._human_action, True     # 已提交
                if self._stop.is_set():
                    return None, True
                remaining -= 1
                self.countdown.emit(remaining)
            return default, False                        # 超时

        payload, submitted = await loop.run_in_executor(None, _wait)
        self.countdown.emit(0)
        if self._stop.is_set():
            return None
        return payload if submitted else default
