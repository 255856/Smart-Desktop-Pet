# -*- coding: utf-8 -*-
"""狼人杀 Director：后台线程里的异步"导演"，驱动完整一局并与 UI 同步。

- 运行在独立 QThread（仿 app/brain/proactive.py 的 _ChatOnceWorker），
  在自己的 asyncio 事件循环里为每个 NPC / 主持人创建独立 LLMClient。
- 确定性规则结算全部交给 WerewolfGame；Agent 只负责台词与决策。
- 轮到真人玩家时发 request_action 信号并等待 UI 回填（threading.Event，
  用 run_in_executor 等待，不阻塞事件循环）。
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
)
from app.games.werewolf_agents import PERSONAS, WerewolfAgent, HostAgent

log = logging.getLogger(__name__)

HUMAN_NAME = "你"


class WerewolfDirector(QThread):
    """跑一整局狼人杀。开始：start()；玩家操作：submit_action(payload)。"""

    # 主持人说话（TTS 朗读）
    host_message = Signal(str)
    # 玩家/ NPC 发言（仅文字）：(seat, name, text, kind)
    speech = Signal(int, str, str, str)
    # 开局告知玩家自己身份：role
    your_role = Signal(str)
    # 夜晚私密频道（狼队讨论 / 预言家查验结果 / 女巫信息），仅玩家可见
    private_channel = Signal(str)
    # 请求玩家操作：(kind, options, context)
    request_action = Signal(str, object, dict)
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
        # 独立 LLMClient（每个 agent 一个）
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
                # 僵局兜底：按存活人数判，狼数达到好人则狼胜
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
            return            # 中途关闭：不结算、不发金币
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

        # 狼队：NPC 狼并发独立提名（玩家狼单独等 UI）
        non_wolf = [s for s in alive if s not in wolves]
        nominations: Dict[int, tuple] = {}
        npc_wolves = [s for s in wolves if s != human_seat]
        if npc_wolves:
            results = await asyncio.gather(
                *[self.agents[s].wolf_nominate(non_wolf) for s in npc_wolves])
            for s, r in zip(npc_wolves, results):
                nominations[s] = r
        if is_human_wolf:
            t = await self._ask_human("wolf_kill", list(non_wolf),
                                      {"teammates": [s for s in wolves if s != human_seat]})
            nominations[human_seat] = (self._as_int(t), "我决定")
            # 狼人频道给玩家看队友的选择
            lines = ["【狼人频道】队友们的击杀建议："]
            for s, (tgt, reason) in nominations.items():
                if s != human_seat:
                    lines.append(f"{g.player(s).name}：{tgt}号，{reason}")
            self.private_channel.emit("\n".join(lines))
        if not nominations:
            wolf_target = None
        else:
            wolf_target = self._majority(
                [t for t, _ in nominations.values()])

        # 预言家查验
        seer = g.seat_of_role(SEER)
        seer_target = None
        if seer is not None and g.player(seer).alive:
            checked = {t for _, t, _ in g.seer_history}
            unchecked = [s for s in alive if s not in checked]
            if seer == human_seat:
                seer_target = self._as_int(
                    await self._ask_human("seer_check", list(unchecked), {}))
            elif unchecked:
                seer_target = await self.agents[seer].seer_check(unchecked)

        # 女巫
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
                    {"hint": "选择解药 / 毒药 / 不用"})
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

        # 预言家结果私信（只给玩家预言家）
        if seer == human_seat and g.player(human_seat).alive and \
                out.seer_target is not None:
            verdict = "狼人" if out.seer_found_wolf else "好人"
            self.private_channel.emit(
                f"【预言家】你查验了 {out.seer_target} 号，TA 是{verdict}。")

        if not is_human_wolf and seer != human_seat and witch != human_seat:
            # 普通好人的夜晚没有可视信息
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
        await self._pace(0.6)
        if g.check_winner() is not None:
            return

        # 白天发言：从死者下一位开始轮转，存活者依次发言
        start = (out.deaths[0] + 1) % 9 if out.deaths else 0
        order = [s for s in range(start, start + 9) if g.player(s % 9).alive]
        order = [s % 9 for s in order]
        for s in order:
            if not g.player(s).alive:
                continue
            if s == g.human_seat:
                text = await self._ask_human("speech", None, {"hint": "轮到你发言"})
                text = str(text or "").strip() or "我是好人，过。"
            else:
                already = self._today_speeches()
                text = await self.agents[s].day_speech(already)
                await self._pace(0.35)
            g.add_speech(s, text)
            self._emit_speech(s, text, "speech")
        await self._pace(0.3)

        # 投票
        await self._host("发言结束，开始投票！")
        votes: Dict[int, int] = {}
        human_seat = g.human_seat
        npc_voters = [s for s in g.alive_seats() if s != human_seat]
        # NPC 并发投票
        vote_results = await asyncio.gather(
            *[self.agents[s].vote(self._vote_candidates(s))
              for s in npc_voters])
        for s, t in zip(npc_voters, vote_results):
            if t is not None:
                votes[s] = t
        if g.player(human_seat).alive:
            t = await self._ask_human(
                "vote", list(self._vote_candidates(human_seat)),
                {"hint": "选择投票对象（可弃票）"})
            ti = self._as_int(t)
            if ti is not None and ti in self._vote_candidates(human_seat):
                votes[human_seat] = ti

        vo = g.tally_votes(votes)
        tally_txt = "，".join(f"{g.player(k).name} {v}票"
                              for k, v in sorted(vo.tally.items(),
                                                 key=lambda x: -x[1]))
        if vo.tied or vo.exiled is None:
            await self._host(f"唱票结果：{tally_txt}。" +
                             await self.host.flavor("peace_vote"))
        else:
            ex = vo.exiled
            g.exile(ex)
            ep = g.player(ex)
            await self._host(f"唱票：{tally_txt}。{ep.name} 被投票放逐，"
                             f"TA 的身份是 {ROLE_LABEL[ep.role]}！")
            self._emit_state()
            await self._last_words(ex)
            if ep.role == HUNTER and g.can_hunt(ex):
                await self._hunter_shoot(ex)
        self._emit_state()

    # ---------------- 遗言 / 猎人 ----------------
    async def _last_words(self, seat: int) -> None:
        g = self.game
        if seat == g.human_seat:
            text = await self._ask_human("last_words", None,
                                         {"hint": "请留一句遗言"})
            text = str(text or "").strip() or "我无话可说……"
        else:
            text = await self.agents[seat].last_words()
            await self._pace(0.3)
        g.add_speech(seat, text, "last_words")
        self._emit_speech(seat, text, "last_words")

    async def _hunter_shoot(self, seat: int) -> None:
        g = self.game
        candidates = [s for s in g.alive_seats() if s != seat]
        await self._host(f"{g.player(seat).name} 是猎人，可以开枪带走一人！")
        if seat == g.human_seat:
            target = self._as_int(
                await self._ask_human("hunter", list(candidates),
                                      {"hint": "选择开枪目标"}))
        else:
            target = await self.agents[seat].hunter_shoot(candidates)
            await self._pace(0.3)
        if target is not None and g.hunter_shot(seat, target):
            tp = g.player(target)
            await self._host(f"砰！猎人开枪带走了 {tp.name}（{ROLE_LABEL[tp.role]}）！")
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
        rows = [s for s in g.speeches if s.day == g.day and s.kind == "speech"]
        if not rows:
            return "（今天还没有人发言）"
        return "\n".join(f"{s.name}：{s.text}" for s in rows[-20:])

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

    def _emit_state(self) -> None:
        self.state_changed.emit(self.game.perspective(self.game.human_seat))

    async def _pace(self, seconds: float) -> None:
        if self._stop.is_set():
            return
        await asyncio.sleep(seconds)

    async def _ask_human(self, kind: str, options, context: dict):
        """请求玩家操作并等待回填（executor 里阻塞等 Event，不卡 loop）。"""
        loop = asyncio.get_event_loop()
        self._human_event.clear()
        self._human_action = None
        self.request_action.emit(kind, options, context)

        def _wait():
            # 兜底超时 180s，避免永久卡死
            self._human_event.wait(timeout=180)
            return self._human_action

        payload = await loop.run_in_executor(None, _wait)
        if self._stop.is_set():
            return None
        return payload
