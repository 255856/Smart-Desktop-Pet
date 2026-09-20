# -*- coding: utf-8 -*-
"""狼人杀多 Agent 系统：8 个独立 NPC Agent + 主持人 Agent。

设计要点（"多 Agent"名副其实）：
    - 每个 NPC 是一个独立 WerewolfAgent 实例：独立人格（名字/性格/口头禅）、
      独立私密记忆、独立 LLMClient（独立 system_prompt 与对话上下文）。
    - 狼队夜晚各自独立提名击杀目标并给出理由（狼人频道），由 Director 多数决，
      体现多个 agent 之间的协商。
    - 每个 agent 只接收自己视角内的信息（见 werewolf.WerewolfGame.perspective），
      防止 NPC "作弊"。
    - 结构化 JSON 协议，解析失败自动重试 1 次，再失败降级为脚本决策。
    - 无 API key / 调用失败时走 ScriptedAgent 规则行为，保证可玩、可离线单测。

本文件只产出"决策 / 台词"，不做规则结算（结算在 WerewolfGame）。
"""
from __future__ import annotations

import json
import logging
import random
import re
from typing import Dict, List, Optional, Tuple

from app.games.werewolf import (
    WerewolfGame, ROLE_LABEL,
    WOLF, SEER, WITCH, HUNTER, VILLAGER,
    CAMP_WOLF, CAMP_GOOD,
)

log = logging.getLogger(__name__)

# ---------------- NPC 人格（8 席） ----------------
PERSONAS: List[Dict[str, str]] = [
    {"name": "阿橘", "style": "大大咧咧、爱打抱不平，说话直来直去，口头禅“我说啊”。"},
    {"name": "小豆子", "style": "胆小谨慎、说话带犹豫，容易随大流，口头禅“那个……”。"},
    {"name": "糖糖", "style": "机灵古怪、喜欢带节奏，爱观察细节，口头禅“嘿嘿”。"},
    {"name": "铁柱", "style": "憨厚直爽、认死理、投票凭直觉，口头禅“俺觉得”。"},
    {"name": "小满", "style": "冷静理性、爱盘逻辑、注重发言漏洞，口头禅“从逻辑上讲”。"},
    {"name": "布丁", "style": "软萌可爱、容易紧张，被怀疑会慌，口头禅“呜……”。"},
    {"name": "薄荷", "style": "毒舌犀利、爱怀疑人、喜欢诈身份，口头禅“有意思”。"},
    {"name": "汤圆", "style": "老好人、劝和为主、不爱冲突，口头禅“大家别吵”。"},
]


# ---------------- 结构化输出解析 ----------------
JSON_SPEECH = '{"speech": "你的发言"}'
JSON_LAST = '{"speech": "遗言"}'
JSON_TARGET = '{"target": 座位号}'
JSON_WOLF = '{"target": 座位号, "reason": "一句话理由"}'
JSON_WITCH = ('{"use": "antidote 或 poison 或 none", '
              '"target": 座位号(仅毒需要)}')


def extract_json(text: str) -> Optional[dict]:
    """从模型回复中提取 JSON 对象（兼容 ```json 代码块 / 多余文本）。"""
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = fence.group(1) if fence else None
    if candidate is None:
        m = re.search(r"\{.*\}", text, re.S)
        candidate = m.group(0) if m else None
    if candidate is None:
        return None
    try:
        return json.loads(candidate)
    except Exception:
        try:
            return json.loads(candidate.replace("“", '"').replace("”", '"')
                              .replace("‘", "'").replace("’", "'"))
        except Exception:
            return None


# ---------------- 上下文文本 ----------------
def _perspective_text(view: dict) -> str:
    lines = []
    lines.append(f"现在是第 {view['day']} 天。你是 {view['self_role']}"
                 f"（{ROLE_LABEL[view['self_role']]}，阵营："
                 f"{'狼人' if view['self_camp'] == CAMP_WOLF else '好人'}）。")
    lines.append("座位信息：")
    for s in view["seats"]:
        tag = "（你）" if s["seat"] == view["self_seat"] else ""
        state = "存活" if s["alive"] else "已死亡"
        role = f"，身份：{ROLE_LABEL[s['role']]}" if s["role"] else ""
        lines.append(f"  {s['seat']}号 {s['name']}{tag}：{state}{role}")
    if view.get("wolf_teammates"):
        lines.append("你的狼人队友座位：" + "、".join(map(str, view["wolf_teammates"])))
    if view.get("seer_checks"):
        if view["seer_checks"]:
            lines.append("你的查验结果：")
            for c in view["seer_checks"]:
                lines.append(f"  {c['target']}号 -> {'狼人' if c['is_wolf'] else '好人'}")
    if view.get("witch_antidote") is not None:
        lines.append(f"你的解药：{'有' if view['witch_antidote'] else '无'}；"
                     f"毒药：{'有' if view['witch_poison'] else '无'}")
    if view.get("public_events"):
        lines.append("公开事件：")
        for e in view["public_events"]:
            lines.append(f"  · {e}")
    return "\n".join(lines)


def _speech_text(view: dict) -> str:
    sp = [s for s in view["speeches"] if s["kind"] == "speech"]
    if not sp:
        return "（今天还没有人发言）"
    return "\n".join(f"{s['name']}：{s['text']}" for s in sp[-20:])


# ---------------- NPC Agent ----------------
class WerewolfAgent:
    """一个独立 NPC 玩家。绑定 client 前或失败时走脚本决策。"""

    def __init__(self, game: WerewolfGame, seat: int, persona: Dict[str, str],
                 client=None):
        self.game = game
        self.seat = seat
        self.persona = persona
        self.name = persona["name"]
        self.client = client          # LLMClient（在 Director 的事件循环里创建）
        self.rng = random.Random(1000 + seat)

    @property
    def player(self):
        return self.game.player(self.seat)

    def bind_client(self, client) -> None:
        self.client = client

    # 基础系统提示
    def _system(self) -> str:
        view = self.game.perspective(self.seat)
        p = self.player
        base = (
            f"你正在和人类玩家玩一场标准 9 人狼人杀。你的座位号是 {self.seat}，"
            f"名字叫“{self.name}”。\n"
            f"你的性格：{self.persona['style']}\n"
            f"你必须始终贴合这个性格说话，发言 1-3 句话，口语化，不要复述规则、"
            f"不要使用 Markdown 或列表，不要暴露自己的真实身份（除非你是预言家"
            f"选择跳明身份，或狼人打配合）。\n"
            f"规则：3 狼人、1 预言家、1 女巫、1 猎人、3 平民。夜晚狼刀、预言家查验、"
            f"女巫救/毒；白天轮流发言后投票，平票无人出局；死者翻牌公开身份。\n"
            f"你是 {ROLE_LABEL[p.role]}。"
        )
        if p.role == WOLF:
            base += "夜晚你要和狼队友商量击杀目标；白天要伪装成好人、混淆视听。"
        elif p.role == SEER:
            base += "你可以在白天选择是否跳预言家报查验结果，引导好人投票。"
        elif p.role == WITCH:
            base += "你有一瓶解药一瓶毒药，关键时刻才暴露身份。"
        elif p.role == HUNTER:
            base += "你被出局时可开枪带走一人；发言可以强势一点。"
        else:
            base += "你是平民，没有技能，靠逻辑和投票帮好人。"
        return base + "\n\n" + _perspective_text(view)

    async def _ask(self, user_prompt: str, want_json: bool = True,
                   retry: bool = True):
        """调用 LLM；返回解析后的 dict（want_json）或纯文本；失败返回 None。"""
        if self.client is None:
            return None
        from app.brain.llm_client import ChatMessage
        msgs = [ChatMessage(role="user", content=user_prompt)]
        try:
            # system_prompt 在 client 构造时已固定；这里把当前局面放进 system 通道
            self.client.system_prompt = self._system()
            raw = await self.client.chat_once(msgs)
        except Exception as e:  # noqa: BLE001
            log.warning("狼人杀 agent %d 调用失败：%r", self.seat, e)
            return None
        if want_json:
            obj = extract_json(raw)
            if obj is None and retry:
                return await self._ask(user_prompt + "\n请严格只输出一个 JSON 对象。",
                                       want_json=True, retry=False)
            return obj
        return (raw or "").strip().strip("“”\"'") or None

    # ---------------- 白天发言 ----------------
    async def day_speech(self, already: str) -> str:
        prompt = (
            f"现在轮到你白天发言（座位 {self.seat} 号 {self.name}）。\n"
            f"今天的发言记录：\n{already}\n\n"
            "请以你的性格说一段符合当前局势的发言（1-3 句，口语化）。\n"
            f"输出 JSON：{JSON_SPEECH}")
        obj = await self._ask(prompt, want_json=True)
        if isinstance(obj, dict) and str(obj.get("speech", "")).strip():
            return str(obj["speech"]).strip()
        # 兼容模型直接吐纯文本
        return self._scripted_speech()

    # ---------------- 遗言 ----------------
    async def last_words(self) -> str:
        prompt = ("你出局了，请说一句简短遗言（符合你的性格，可表水、可点出怀疑对象）。\n"
                  f"输出 JSON：{JSON_LAST}")
        obj = await self._ask(prompt, want_json=True)
        if isinstance(obj, dict) and str(obj.get("speech", "")).strip():
            return str(obj["speech"]).strip()
        return self._scripted_last_words()

    # ---------------- 投票 ----------------
    async def vote(self, candidates: List[int]) -> Optional[int]:
        prompt = (
            "白天发言结束，现在投票。请从以下存活且非自己的座位中选一个投票：\n"
            f"候选：{candidates}\n（不能投自己）\n"
            f"输出 JSON：{JSON_TARGET}")
        obj = await self._ask(prompt, want_json=True)
        if isinstance(obj, dict) and self._valid_target(obj.get("target"),
                                                        candidates):
            return int(obj["target"])
        return self._scripted_vote(candidates)

    # ---------------- 狼：夜晚提名击杀 ----------------
    async def wolf_nominate(self, candidates: List[int]) -> Tuple[int, str]:
        prompt = (
            "夜晚降临，你是狼人。请从下列存活的非狼人玩家中提名今晚击杀目标：\n"
            f"候选：{candidates}\n"
            "优先击杀疑似预言家、女巫等神职。请给狼队友一句简短理由。\n"
            f"输出 JSON：{JSON_WOLF}")
        obj = await self._ask(prompt, want_json=True)
        if isinstance(obj, dict) and self._valid_target(obj.get("target"),
                                                        candidates):
            return int(obj["target"]), str(obj.get("reason", "")).strip() or "直觉"
        return self._scripted_wolf_nominate(candidates)

    # ---------------- 预言家：查验 ----------------
    async def seer_check(self, unchecked: List[int]) -> Optional[int]:
        prompt = (
            "你是预言家，请选择今晚要查验身份的存活玩家座位（不要重复查验）：\n"
            f"未查验的存活座位：{unchecked}\n"
            f"输出 JSON：{JSON_TARGET}")
        obj = await self._ask(prompt, want_json=True)
        if isinstance(obj, dict) and self._valid_target(obj.get("target"),
                                                        unchecked):
            return int(obj["target"])
        return self._scripted_seer_check(unchecked)

    # ---------------- 女巫：救 / 毒 ----------------
    async def witch_decide(self, killed: Optional[int],
                           poison_candidates: List[int]) -> Dict[str, object]:
        me = self.player
        prompt = (
            "你是女巫。\n"
            f"今晚狼人击杀的目标是：{killed if killed is not None else '无（空刀）'}。\n"
            f"解药：{'有' if me.has_antidote else '无'}；"
            f"毒药：{'有' if me.has_poison else '无'}。\n"
            f"一夜最多用一瓶药。候选毒目标：{poison_candidates or '无'}。\n"
            f"输出 JSON：{JSON_WITCH}")
        obj = await self._ask(prompt, want_json=True)
        if isinstance(obj, dict):
            use = str(obj.get("use", "none")).lower()
            if use == "antidote" and me.has_antidote and killed is not None:
                return {"use": "antidote"}
            if use == "poison" and me.has_poison and poison_candidates:
                t = obj.get("target")
                if self._valid_target(t, poison_candidates):
                    return {"use": "poison", "target": int(t)}
            if use == "none":
                return {"use": "none"}
        return self._scripted_witch(killed, poison_candidates)

    # ---------------- 猎人：开枪 ----------------
    async def hunter_shoot(self, candidates: List[int]) -> Optional[int]:
        prompt = (
            "你是猎人，现在出局可以开枪带走一人。请从存活且非自己的座位中选：\n"
            f"候选：{candidates}\n输出 JSON：{JSON_TARGET}")
        obj = await self._ask(prompt, want_json=True)
        if isinstance(obj, dict) and self._valid_target(obj.get("target"),
                                                        candidates):
            return int(obj["target"])
        return self._scripted_hunter(candidates)

    # ================= 脚本降级（无 key / 解析失败） =================
    def _alive_non_self(self) -> List[int]:
        return [p.seat for p in self.game.alive_players()
                if p.seat != self.seat]

    def _valid_target(self, target, candidates: List[int]) -> bool:
        try:
            t = int(target)
        except (TypeError, ValueError):
            return False
        return t in candidates

    def _known_wolves(self) -> List[int]:
        """根据视角已知的狼（预言家查验结果、狼队友等）。"""
        v = self.game.perspective(self.seat)
        wolves = set()
        for s in v["seats"]:
            if s["role"] == WOLF:
                wolves.add(s["seat"])
        for c in v.get("seer_checks", []) or []:
            if c["is_wolf"]:
                wolves.add(c["target"])
        return [w for w in wolves if w != self.seat and
                self.game.player(w).alive]

    def _scripted_speech(self) -> str:
        p = self.player
        known = self._known_wolves()
        if p.role == SEER and self.game.seer_history:
            d, t, is_wolf = self.game.seer_history[-1]
            verdict = "狼人" if is_wolf else "好人"
            return f"我是预言家！我昨晚查验了 {t} 号，是{verdict}！"
        if known:
            return f"我觉得 {known[0]} 号很可疑，大家重点盯一下。"
        if p.role == WOLF:
            return "我是铁好人啊，这把先别出我，听预言家报验人。"
        return "我是好人，目前信息不多，先听预言家的。"

    def _scripted_last_words(self) -> str:
        return f"我是 {ROLE_LABEL[self.player.role]}，大家加油，别投错人……"

    def _scripted_vote(self, candidates: List[int]) -> int:
        known = self._known_wolves()
        pool = [w for w in known if w in candidates] or candidates
        return self.rng.choice(pool)

    def _scripted_wolf_nominate(self, candidates: List[int]) -> Tuple[int, str]:
        # 一半概率优先杀神职，一半随机，避免兜底 AI 过度神准
        gods = [s for s in candidates
                if self.game.player(s).role in (SEER, WITCH, HUNTER)]
        if gods and self.rng.random() < 0.5:
            return self.rng.choice(gods), "优先处理神职"
        t = self.rng.choice(candidates)
        return t, "感觉这个比较像神职"

    def _scripted_seer_check(self, unchecked: List[int]) -> Optional[int]:
        return self.rng.choice(unchecked) if unchecked else None

    def _scripted_witch(self, killed, poison_candidates) -> Dict[str, object]:
        me = self.player
        # 第一晚被刀优先救；之后随机救
        if killed is not None and me.has_antidote and \
                (self.game.day <= 1 or self.rng.random() < 0.6):
            return {"use": "antidote"}
        if me.has_poison and poison_candidates and self.game.day >= 2 \
                and self.rng.random() < 0.3:
            return {"use": "poison",
                    "target": self.rng.choice(poison_candidates)}
        return {"use": "none"}

    def _scripted_hunter(self, candidates: List[int]) -> Optional[int]:
        known = self._known_wolves()
        pool = [w for w in known if w in candidates] or candidates
        return self.rng.choice(pool) if pool else None


# ---------------- 主持人 Agent（桌宠） ----------------
class HostAgent:
    """主持人上帝：桌宠扮演。流程播报用模板（确定性、不阻塞），
    开场/串场/结算可由 LLM 生成；唯一会被 TTS 朗读的声音。"""

    def __init__(self, client=None, pet_name: str = "桌宠"):
        self.client = client
        self.pet_name = pet_name
        self._system = (
            f"你是{pet_name}，正在主持一局标准 9 人狼人杀，你是“上帝”主持人，"
            "不参与游戏。你的语气俏皮、活泼、有亲和力，像朋友带大家玩游戏。"
            "只说主持人该说的话，不泄露任何玩家身份。每次 1-2 句话，口语化，"
            "不使用 Markdown。")

    def bind_client(self, client) -> None:
        self.client = client

    async def flavor(self, situation: str) -> str:
        """非关键的串场台词（开场/结算等）；LLM 失败则给模板。"""
        if self.client is None:
            return self._template(situation)
        from app.brain.llm_client import ChatMessage
        try:
            self.client.system_prompt = self._system
            raw = await self.client.chat_once(
                [ChatMessage(role="user",
                             content=f"请用一两句俏皮话主持这个环节：{situation}")])
            text = (raw or "").strip().strip("“”\"'")
            if text:
                return text
        except Exception as e:  # noqa: BLE001
            log.warning("主持人 LLM 失败：%r", e)
        return self._template(situation)

    def _template(self, situation: str) -> str:
        table = {
            "opening": "欢迎来到狼人杀！天黑请闭眼，狼人要开始行动啦～",
            "good_win": "狼人全部出局，好人阵营获胜！大家配合得真棒！",
            "wolf_win": "狼人潜伏成功，狼人阵营获胜！下把我可要盯紧点了～",
            "peace_vote": "投票平票啦，今天无人出局，大家再仔细听听发言～",
        }
        return table.get(situation, "游戏继续，请大家保持安静哦～")
