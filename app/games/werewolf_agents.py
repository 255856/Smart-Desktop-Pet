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

import asyncio
import json
import logging
import random
import re
from typing import Dict, List, Optional, Tuple

from app.games.werewolf import (
    WerewolfGame, ROLE_LABEL,
    WOLF, SEER, WITCH, HUNTER, CAMP_WOLF,
)

log = logging.getLogger(__name__)

PERSONAS: List[Dict[str, str]] = [
    {"name": "阿橘", "style": "大大咧咧、爱打抱不平，说话直来直去，口头禅“我说啊”。"},
    {"name": "小豆子", "style": "胆小谨慎、说话带犹豫，容易随大流，口头禅“那个……”。"},
    {"name": "糖糖", "style": "机灵古怪、喜欢带节奏，爱观察细节，口头禅“哈哈”。"},
    {"name": "铁柱", "style": "憨厚直爽、认死理、投票凭直觉，口头禅“俺觉得”。"},
    {"name": "小满", "style": "冷静理性、爱盘逻辑、注重发言漏洞，口头禅“从逻辑上讲”。"},
    {"name": "布丁", "style": "软萌可爱、容易紧张，被怀疑会慌，口头禅“呜……”。"},
    {"name": "薄荷", "style": "毒舌犀利、爱怀疑人、喜欢诈身份，口头禅“有意思”。"},
    {"name": "汤圆", "style": "老好人、劝和为主、不爱冲突，口头禅“大家别吵”。"},
]


JSON_SPEECH = ('{"analysis": "简短内部推理：结合哪些发言/查验/投票决定怎么说（不显示给玩家）", '
               '"speech": "你要说的话"}')
JSON_LAST = JSON_SPEECH
JSON_TARGET = '{"target": 座位号}'
JSON_VOTE = ('{"analysis": "简短内部推理：回顾起跳/查验/站队/投票，锁定疑点（不显示）", '
             '"target": 座位号, "reason": "对外的一两句话理由，必须引用具体发言、查验或投票站队"}')
JSON_WOLF = '{"target": 座位号, "reason": "一句话理由"}'
JSON_WITCH = ('{"use": "antidote 或 poison 或 none", '
              '"target": 座位号(仅毒需要)}')
JSON_RUN = '{"run": true 或 false}'
JSON_TRANSFER = '{"target": 座位号 或 null（撕掉警徽）}'


def _fn(name: str, desc: str, props: dict, required: list) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props,
                       "required": required}}}


# 各阶段游戏工具：每个阶段只暴露对应工具，模型自主决策、填参数
def _build_tools() -> Dict[str, dict]:
    seat_p = {"type": "integer", "description": "目标玩家座位号"}
    text_p = {"type": "string", "description": "你要说的话（1-3句，符合性格与当前局势）"}
    return {
        "public_speak": _fn(
            "public_speak", "在公共频道发言（所有存活玩家都能听到）",
            {"text": text_p}, ["text"]),
        "wolf_plan": _fn(
            "wolf_plan", "在狼频道（仅狼可见，与公共频道隔离）讨论，并给出今晚建议击杀的目标",
            {"text": {"type": "string", "description": "狼频道讨论发言（队友能看到）"},
             "target": {"type": "integer",
                        "description": "建议刀的座位号；可填自己=自刀骗解药"}},
            ["text", "target"]),
        "ballot": _fn(
            "ballot", "白天投票放逐一名玩家",
            {"action": {"type": "string", "enum": ["vote", "abstain"],
                        "description": "vote=投给 seat，abstain=弃票"},
             "seat": seat_p}, ["action"]),
        "seer_check": _fn(
            "seer_check", "预言家查验一名玩家的身份",
            {"seat": seat_p}, ["seat"]),
        "witch_action": _fn(
            "witch_action", "女巫使用药水（一夜最多用一瓶）",
            {"action": {"type": "string", "enum": ["antidote", "poison", "none"],
                        "description": "antidote=用解药救今晚被杀者；poison=用毒药毒 seat；none=不用药"},
             "seat": seat_p}, ["action"]),
        "hunter_action": _fn(
            "hunter_action", "猎人出局后是否开枪带走一人",
            {"action": {"type": "string", "enum": ["shoot", "skip"],
                        "description": "shoot=开枪带走 seat；skip=放弃"},
             "seat": seat_p}, ["action"]),
        "sheriff_run": _fn(
            "sheriff_run", "决定是否举手竞选警长",
            {"run": {"type": "boolean",
                      "description": "true=上台竞选，false=不参选"}},
            ["run"]),
        "badge_action": _fn(
            "badge_action", "警长出局后移交警徽或撕掉",
            {"action": {"type": "string", "enum": ["transfer", "tear"],
                        "description": "transfer=移交给 seat；tear=撕掉警徽"},
             "seat": seat_p}, ["action"]),
    }


TOOLS: Dict[str, dict] = _build_tools()


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


async def _llm_chat(client, msgs, semaphore, per_call_timeout: float = 90.0):
    """带并发限流、单次超时与 529/429 过载退避的 LLM 调用。

    - semaphore：全局 asyncio.Semaphore（由 Director 注入），限制同时请求数；
    - per_call_timeout：单次请求超时（含流式生成），超时按可重试处理；
    - 服务端过载/限流/超时退避重试最多 3 次；
    - asyncio.CancelledError 必须直接抛出（关闭线程时中断用）。
    """
    import httpx
    last_exc = None
    for attempt in range(3):
        async def _do():
            if semaphore is not None:
                async with semaphore:
                    return await client.chat_once(msgs)
            return await client.chat_once(msgs)
        try:
            return await asyncio.wait_for(_do(), timeout=per_call_timeout)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            last_exc = e
            m = str(e)
            # httpx.ConnectTimeout 等异常的 str() 为空字符串，字符串匹配全部
            # 失效——必须按类型判断。ConnectTimeout / ReadTimeout /
            # WriteTimeout / PoolTimeout 都是 httpx.TimeoutException 子类。
            retryable = (
                '529' in m or '429' in m or 'overloaded' in m.lower()
                or 'rate' in m.lower()
                or isinstance(e, (asyncio.TimeoutError, TimeoutError,
                                  httpx.TimeoutException, httpx.ConnectError)))
            if retryable:
                await asyncio.sleep(1.2 * (attempt + 1))
                continue
            raise
    raise last_exc

def _perspective_text(view: dict) -> str:
    lines = []
    lines.append(f"现在是第 {view['day']} 天。你的座位是 {view['self_seat']} 号，"
                 f"身份：{ROLE_LABEL[view['self_role']]}（阵营："
                 f"{'狼人' if view['self_camp'] == CAMP_WOLF else '好人'}）。")
    alive = [s for s in view["seats"] if s["alive"]]
    dead = [s for s in view["seats"] if not s["alive"]]

    def _one(s):
        role = f"({ROLE_LABEL[s['role']]})" if s["role"] else ""
        tag = "(你)" if s["seat"] == view["self_seat"] else ""
        return f"{s['seat']}号{s['name']}{tag}{role}"

    lines.append("【当前存活 %d 人】" % len(alive)
                 + "、".join(_one(s) for s in alive))
    if dead:
        lines.append("【已死亡 %d 人：不能再发言、投票或被选为目标】" % len(dead)
                     + "、".join(f"{s['seat']}号{s['name']}" for s in dead))
    lines.append("注意：你只能与存活玩家互动，发言、投票、技能都不得涉及已死亡玩家。")
    if view.get("wolf_teammates"):
        lines.append("你的狼人队友座位：" + "、".join(map(str, view["wolf_teammates"])))
    if view.get("sheriff") is not None:
        lines.append(f"当前警长：{view['sheriff']}号（警长投票算 1.5 票，"
                     "出局前可移交警徽）。")
    if view.get("wolf_chat"):
        lines.append("狼频道讨论记录（只有狼人能看见，切勿对好人提及）：")
        for c in view["wolf_chat"][-15:]:
            lines.append(f"  {c['name']}：{c['text']}")
    if view.get("seer_checks"):
        if view["seer_checks"]:
            lines.append("你的查验结果：")
            for c in view["seer_checks"]:
                lines.append(f"  {c['target']}号 -> {'狼人' if c['is_wolf'] else '好人'}")
    if view.get("witch_antidote") is not None:
        lines.append(f"你的解药：{'有' if view['witch_antidote'] else '无'}；"
                     f"毒药：{'有' if view['witch_poison'] else '无'}")
    if view.get("public_events"):
        lines.append("公开事件与结果（死亡、投票明细、唱票放逐、警长变更等，按时间）：")
        for e in view["public_events"][-40:]:
            lines.append(f"  · {e}")
    if view.get("speeches"):
        lines.append("公共频道发言（按时间，含座位号；竞选/遗言/PK 已标注）：")
        for s in view["speeches"][-40:]:
            tag = _KIND_TAG.get(s["kind"], "发言")
            lines.append(f"  [第{s['day']}天·{tag}] {s['seat']}号 {s['name']}：{s['text']}")
    return "\n".join(lines)


_KIND_TAG = {
    "speech": "发言",
    "last_words": "遗言",
    "campaign": "竞选",
    "pk": "PK",
}


def _speech_text(view: dict) -> str:
    sp = [s for s in view["speeches"] if s["kind"] == "speech"]
    if not sp:
        return "（今天还没有人发言）"
    return "\n".join(f"{s['name']}：{s['text']}" for s in sp[-20:])


class WerewolfAgent:
    """一个独立 NPC 玩家。绑定 client 前或失败时走脚本决策。"""

    def __init__(self, game: WerewolfGame, seat: int, persona: Dict[str, str],
                 client=None):
        self.game = game
        self.seat = seat
        self.persona = persona
        self.name = persona["name"]
        self.client = client          # LLMClient（在 Director 的事件循环里创建）
        self.semaphore = None  # 全局并发限流信号量（Director 注入）
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
            f"女巫救/毒；白天轮流发言后投票，平票无人出局；本局为暗牌，玩家出局时不公布身份，\n"
            f"只有猎人开枪等特殊技能发动或游戏结束时才会亮明身份。\n"
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
            raw = await _llm_chat(self.client, msgs, self.semaphore)
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

    async def _tool_call(self, instruction: str, tools: List[dict]):
        """让模型基于场内信息自主思考并调用一个工具。
        返回 {"name":..., "arguments": dict}；无 client / 失败 / 未调用返回 None。"""
        if self.client is None:
            return None
        # 每次调用都刷新 system prompt，确保拿到最新的玩家状态与发言
        self.client.system_prompt = self._system()
        try:
            messages = [{"role": "user", "content": instruction}]
            final_calls = None
            async for ev, data in self.client.chat_stream_events(
                    messages, tools=tools, force_tool_use=True):
                if ev == "finish":
                    final_calls = data.get("tool_calls")
            if final_calls:
                tc = final_calls[0]
                try:
                    args = json.loads(tc.get("arguments") or "{}")
                except Exception:  # noqa: BLE001
                    args = {}
                return {"name": tc.get("name", ""), "arguments": args}
        except Exception as e:  # noqa: BLE001
            log.warning("狼人杀 agent %d 工具调用失败：%r", self.seat, e)
        return None

    async def day_speech(self, already: str) -> str:
        instruction = (
            f"现在轮到你白天发言（{self.seat} 号 {self.name}）。\n"
            f"今天此前的发言：\n{already or '（你是今天第一个发言）'}\n\n"
            "请先核对上面的【当前存活/已死亡】状态、公开事件与各位发言，独立判断，"
            "再调用 public_speak 说一段符合局势的话（1-3句，可回应/质疑/附和具体发言）。\n"
            + ("你是狼人：发言必须像好人一样推理，不能提及狼频道、刀人或狼队友。\n"
               if self.player.role == WOLF else ""))
        call = await self._tool_call(instruction, [TOOLS["public_speak"]])
        if call and call["name"] == "public_speak":
            text = str(call["arguments"].get("text", "")).strip()
            if text:
                return text
        log.warning("狼人杀 agent %d 白天发言降级到脚本台词", self.seat)
        return self._scripted_speech()

    async def last_words(self) -> str:
        instruction = (
            "你已出局，请说一句简短遗言（符合性格，可表水、可点怀疑对象）。\n"
            "本局为暗牌，遗言不能说出自己的真实身份（狼人更要伪装、别卖队友）。\n"
            "请调用 public_speak 给出遗言。")
        call = await self._tool_call(instruction, [TOOLS["public_speak"]])
        if call and call["name"] == "public_speak":
            text = str(call["arguments"].get("text", "")).strip()
            if text:
                return text
        return self._scripted_last_words()

    async def vote(self, candidates: List[int]) -> Optional[int]:
        instruction = (
            "白天发言结束，现在投票。请先核对上面的存活/死亡状态、公开事件和各位发言，\n"
            "基于具体发言、查验或投票站队独立判断（不盲目跟从警长或多数票），"
            "再调用 ballot：投给一名存活玩家，或弃票。\n"
            f"可投票的存活目标（不含自己）：{candidates}")
        call = await self._tool_call(instruction, [TOOLS["ballot"]])
        if call and call["name"] == "ballot":
            args = call["arguments"]
            if str(args.get("action", "abstain")).lower() == "vote" and \
                    self._valid_target(args.get("seat"), candidates):
                return int(args["seat"])
            return None
        return self._scripted_vote(candidates)

    async def wolf_nominate(self, candidates: List[int]) -> Tuple[int, str]:
        # 预留接口：当前 Director 用 wolf_chat_message（含讨论+目标）；此处保持工具调用范式
        instruction = (
            "夜晚降临，你是狼人。请从存活玩家中提名今晚击杀目标（含自己=自刀）。\n"
            f"候选：{candidates}\n通常优先刀预言家/女巫等神职；自刀赌女巫会救。\n"
            "请调用 wolf_plan 给出建议目标和理由。")
        call = await self._tool_call(instruction, [TOOLS["wolf_plan"]])
        if call and call["name"] == "wolf_plan":
            args = call["arguments"]
            if self._valid_target(args.get("target"), candidates):
                return int(args["target"]), str(args.get("text", "")).strip() or "直觉"
        return self._scripted_wolf_nominate(candidates)

    async def seer_check(self, unchecked: List[int]) -> Optional[int]:
        instruction = (
            "你是预言家，请选择今晚要查验的存活玩家（不要重复查验）。\n"
            f"未查验的存活座位：{unchecked}\n请调用 seer_check 给出查验座位。")
        call = await self._tool_call(instruction, [TOOLS["seer_check"]])
        if call and call["name"] == "seer_check":
            if self._valid_target(call["arguments"].get("seat"), unchecked):
                return int(call["arguments"]["seat"])
        return self._scripted_seer_check(unchecked)

    async def witch_decide(self, killed: Optional[int],
                           poison_candidates: List[int]) -> Dict[str, object]:
        me = self.player
        instruction = (
            "你是女巫。\n"
            f"今晚狼人击杀的目标是：{killed if killed is not None else '无（空刀）'}。\n"
            f"解药：{'有' if me.has_antidote else '无'}；"
            f"毒药：{'有' if me.has_poison else '无'}。\n"
            f"一夜最多用一瓶药。候选毒目标：{poison_candidates or '无'}。\n"
            "请调用 witch_action：用解药、用毒药（填 seat）或不用药。")
        call = await self._tool_call(instruction, [TOOLS["witch_action"]])
        if call and call["name"] == "witch_action":
            args = call["arguments"]
            use = str(args.get("action", "none")).lower()
            if use == "antidote" and me.has_antidote and killed is not None:
                return {"use": "antidote"}
            if use == "poison" and me.has_poison and \
                    self._valid_target(args.get("seat"), poison_candidates):
                return {"use": "poison", "target": int(args["seat"])}
            return {"use": "none"}
        return self._scripted_witch(killed, poison_candidates)

    async def hunter_shoot(self, candidates: List[int]) -> Optional[int]:
        instruction = (
            "你是猎人，现已出局可以开枪带走一人（被毒不能开枪）。\n"
            f"存活候选（非自己）：{candidates}\n请调用 hunter_action：开枪（填 seat）或放弃。")
        call = await self._tool_call(instruction, [TOOLS["hunter_action"]])
        if call and call["name"] == "hunter_action":
            args = call["arguments"]
            if str(args.get("action", "skip")).lower() == "shoot" and \
                    self._valid_target(args.get("seat"), candidates):
                return int(args["seat"])
            return None
        return self._scripted_hunter(candidates)

    async def run_for_sheriff(self) -> bool:
        instruction = (
            "第一天白天竞选警长。警长有 1.5 票、负责归票，出局前可移交。\n"
            "预言家通常必跳；平民可大胆举手；狼人可悍跳争夺；只有女巫通常隐藏。\n"
            "请调用 sheriff_run 决定是否上台。")
        call = await self._tool_call(instruction, [TOOLS["sheriff_run"]])
        if call and call["name"] == "sheriff_run":
            r = call["arguments"].get("run")
            if isinstance(r, bool):
                return r
        return self._scripted_run_sheriff()

    async def campaign_speech(self) -> str:
        instruction = (
            "你正在竞选警长，请调用 public_speak 发表一段竞选演说（1-2句，"
            "说明你值得信任；预言家可以跳明并报验人）。")
        call = await self._tool_call(instruction, [TOOLS["public_speak"]])
        if call and call["name"] == "public_speak":
            text = str(call["arguments"].get("text", "")).strip()
            if text:
                return text
        return self._scripted_campaign_speech()

    async def vote_sheriff(self, candidates: List[int]) -> Optional[int]:
        instruction = (
            "你没有上台，需从参选者中投票选警长。请调用 ballot 投给最可信的参选者，或弃票。\n"
            f"参选者：{candidates}")
        call = await self._tool_call(instruction, [TOOLS["ballot"]])
        if call and call["name"] == "ballot":
            args = call["arguments"]
            if str(args.get("action", "abstain")).lower() == "vote" and \
                    self._valid_target(args.get("seat"), candidates):
                return int(args["seat"])
            return None
        return self._scripted_vote_sheriff(candidates)

    async def pk_speech(self) -> str:
        instruction = (
            "你在投票中平票进入 PK，请调用 public_speak 发言说服大家不要出你"
            "（1-2句，可表水、可点怀疑的狼）。")
        call = await self._tool_call(instruction, [TOOLS["public_speak"]])
        if call and call["name"] == "public_speak":
            text = str(call["arguments"].get("text", "")).strip()
            if text:
                return text
        return self._scripted_pk_speech()

    async def transfer_badge(self, candidates: List[int]) -> Optional[int]:
        instruction = (
            "你是警长且即将出局，请调用 badge_action：把警徽移交给信任的存活玩家，"
            "或撕掉警徽。\n"
            f"存活候选：{candidates}")
        call = await self._tool_call(instruction, [TOOLS["badge_action"]])
        if call and call["name"] == "badge_action":
            args = call["arguments"]
            if str(args.get("action", "tear")).lower() == "transfer" and \
                    self._valid_target(args.get("seat"), candidates):
                return int(args["seat"])
            return None
        return self._scripted_transfer_badge(candidates)

    async def wolf_chat_message(self, targets: List[int]) -> Tuple[str, Optional[int]]:
        """夜晚狼频道讨论：返回 (频道发言, 建议击杀目标)。"""
        instruction = (
            "夜晚狼频道（仅狼队友可见，与公共频道隔离）。请和队友讨论今晚刀谁。\n"
            "候选包含所有存活玩家（也包括你自己）：一般优先刀预言家/女巫等神职好人；"
            "可选择刀自己（自刀）骗解药，但没被救会真的死亡。\n"
            f"可刀目标：{targets}\n请调用 wolf_plan：给出狼频道发言和建议目标。")
        call = await self._tool_call(instruction, [TOOLS["wolf_plan"]])
        if call and call["name"] == "wolf_plan":
            args = call["arguments"]
            text = str(args.get("text", "")).strip()
            if text:
                t = None
                if self._valid_target(args.get("target"), targets):
                    t = int(args["target"])
                return text, t
        return self._scripted_wolf_chat(targets)

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

    def _outward_suspect(self) -> Optional[int]:
        """对外可指控的座位：好人指控已知狼；狼人嫁祸好人（绝不卖队友）。"""
        if self.player.role == WOLF:
            goods = [p.seat for p in self.game.alive_players()
                     if p.role != WOLF and p.seat != self.seat]
            return self.rng.choice(goods) if goods else None
        known = self._known_wolves()
        return known[0] if known else None

    def _scripted_speech(self) -> str:
        """LLM 失败时的兜底台词。多候选随机 + 尽量带真实局势信息
        （最近死亡/警长/存活人数），避免全场 NPC 说同一句。"""
        v = self.game.perspective(self.seat)
        p = self.player
        # 先验身份的特例保持不变
        if p.role == SEER and self.game.seer_history:
            d, t, is_wolf = self.game.seer_history[-1]
            verdict = "狼人" if is_wolf else "好人"
            return self.rng.choice([
                f"我是预言家！我昨晚查验了 {t} 号，是{verdict}！大家听我的。",
                f"听好了，我是预言家，{t} 号验出来是{verdict}，别投错了。",
            ])
        sus = self._outward_suspect()
        sus_txt = f"{sus} 号" if sus is not None else None
        # 局势上下文：从最近事件提取死亡座位，生成自然引述（不照抄事件原文）
        ctx_prefix = ""
        for e in reversed(v.get("public_events", [])):
            m = re.search(r"(\d+)号.*(?:死亡|出局)", e)
            if m:
                ctx_prefix = f"昨晚 {m.group(1)} 号出事后，"
                break
        if p.role == WOLF:
            pool = [
                f"{ctx_prefix}我看了一圈发言，{sus_txt or '几个可疑的'} 最不对劲，大家品品。",
                f"{ctx_prefix}我是铁好人啊，先别急着出我，{sus_txt or '先听预言家的'}。",
                f"{ctx_prefix}这轮信息还不多，我倾向先听后置位怎么说再定。",
            ]
        else:
            pool = [
                f"{ctx_prefix}目前信息不多，{sus_txt or '先听预言家报验人'}再定。",
                f"{ctx_prefix}我站 {sus_txt or '发言最像好人的那位'}，欢迎反驳。",
                f"{ctx_prefix}我是好人，这轮先跟大部队，{sus_txt or '重点听后面的人怎么说'}。",
            ]
        return self.rng.choice(pool)

    def _scripted_last_words(self) -> str:
        # 暗牌局：遗言不公开身份（猎人开枪等特殊情况已在其他流程处理）
        if self.player.role == WOLF:
            return "我是好人出局，你们后面会后悔的，预言家的话再好好听听。"
        sus = self._outward_suspect()
        if sus is not None:
            return f"我是好人，我走后盯紧 {sus} 号，别投错了。"
        return "我确实是好人，大家冷静盘逻辑，别被带节奏。"

    def _scripted_vote(self, candidates: List[int]) -> int:
        sus = self._outward_suspect()
        pool = [sus] if sus in candidates else list(candidates)
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

    def _scripted_run_sheriff(self) -> bool:
        r = self.player.role
        if r == SEER:
            return True
        if r == HUNTER:
            return self.rng.random() < 0.7
        if r == WITCH:
            return False
        if r == WOLF:
            return self.rng.random() < 0.35
        return self.rng.random() < 0.25

    def _scripted_campaign_speech(self) -> str:
        if self.player.role == SEER and self.game.seer_history:
            d, t, is_wolf = self.game.seer_history[-1]
            verdict = "狼人" if is_wolf else "好人"
            return f"我是预言家！我查验了 {t} 号是{verdict}，请把警徽给我，我来带节奏！"
        if self.player.role == SEER:
            return "我是预言家，警徽给我，我每晚都能报验人！"
        return "我是铁好人，思路清晰，警长给我，我带大家归票！"

    def _scripted_vote_sheriff(self, candidates: List[int]) -> Optional[int]:
        return self.rng.choice(candidates) if candidates else None

    def _scripted_pk_speech(self) -> str:
        sus = self._outward_suspect()
        if sus is not None:
            return f"我是好人，出我就亏了，我更怀疑 {sus} 号，我们一起投他！"
        return "我真的是好人，大家别冲动，给我个机会，先投真狼！"

    def _scripted_transfer_badge(self, candidates: List[int]) -> Optional[int]:
        v = self.game.perspective(self.seat)
        # 预言家查验过的好人优先
        for c in v.get("seer_checks", []) or []:
            if not c["is_wolf"] and c["target"] in candidates:
                return c["target"]
        if self.player.role == WOLF:
            wolves = [s for s in candidates if self.game.player(s).role == WOLF]
            if wolves:
                return wolves[0]
        if self.rng.random() < 0.2:
            return None                       # 撕掉警徽
        return self.rng.choice(candidates) if candidates else None

    def _scripted_wolf_chat(self, targets: List[int]) -> Tuple[str, Optional[int]]:
        gods = [s for s in targets
                if self.game.player(s).role in (SEER, WITCH, HUNTER)]
        if gods and self.rng.random() < 0.6:
            t = self.rng.choice(gods)
            return f"我觉得 {t} 号像神职，建议今晚先刀。", t
        if targets:
            t = self.rng.choice(targets)
            return f"我看 {t} 号可以刀，看你们怎么想。", t
        return "今晚听你们的，我跟着刀。", None


class HostAgent:
    """主持人上帝：桌宠扮演。流程播报用模板（确定性、不阻塞），
    开场/串场/结算可由 LLM 生成；唯一会被 TTS 朗读的声音。"""

    def __init__(self, client=None, pet_name: str = "桌宠"):
        self.client = client
        self.semaphore = None
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
            raw = await _llm_chat(
                self.client,
                [ChatMessage(role="user",
                             content=f"请用一两句俏皮话主持这个环节：{situation}")],
                self.semaphore)
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
