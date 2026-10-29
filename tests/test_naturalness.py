# -*- coding: utf-8 -*-
"""自然度评测门禁（Stage 0.4）：离线、可复现、不需要 key / 网络 / GUI。

背景
----
Stage 0 的原则是「先量基线，再动代码」。Stage 0.4 落地自然度评测集：
20 条固定输入 + 当时录好的真实回复（golden reply），离线断言结构性质。

四层结构
--------
L1 现状刻画（characterization）：锁住「今天」清洗管线 / system prompt 的行为。
    今天必须绿。Stage 1.3（清洗管线）/ Stage 1.4（system prompt）落地后，
    由对应 agent 翻转对应断言 —— 每条断言都注明了是哪一行源码让它成立。
L2 golden 评测集：每条 case 的 golden reply 过一遍清洗管线，断言结构性质。
    「期望行为」那部分用 xfail 挂着 —— Stage 1.3 / 1.4 修好后自动转 pass，
    不用改代码，也不会把 master 变红。
L3 录制脚本：scripts/record_naturalness.py（需要 key，只跑一次，不进 CI）。
L4 live 测试：发版前人工确认用（slow marker + 无 key 跳过），不进硬门禁。

为什么不能直接断言期望行为
--------------------------
Stage 1.3 之前，清洗管线会删掉「我来…/让我…」这类过渡语
（app/brain/llm_client.py:343 + :355-366、:454 + :233-237），
system prompt 里还硬写着「回复 1~3 句」（app/brain/llm_client.py:87）。
现在直接断言「过渡语要活下来 / prompt 里不能有句数硬上限」会把 master 变红，
所以：期望行为一律用 xfail 挂住，现状用 characterization 锁死。

20 条用例覆盖的场景
-------------------
chat_casual 闲聊 / low_mood 情绪低落 / ask_question 提问 / wants_tool 要工具 /
tool_failure 工具失败 / wants_memory 要记东西 / asks_memory 问记不记得 /
noise 闲聊噪声 / are_you_ai 问是不是 AI / bad_state_demand 状态差时提要求 /
praise 夸奖 / complain 吐槽 / ask_recommendation 求推荐 / tell_joke 讲笑话 /
long_question 长问题 / short_question 短问题 / follow_up 连续追问 /
call_name 叫名字 / praise_cute 夸可爱 / lookup_material 查资料
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import statistics
from pathlib import Path

import pytest

# Qt 单例：app.brain.agent 会拉起 Qt 相关模块，离线跑也不能弹窗
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.brain.agent import (AgentLoop, _EMPTY_REPLY_FALLBACK,  # noqa: E402
                             _EMPTY_REPLY_FALLBACKS)
from app.brain.llm_client import (  # noqa: E402
    CHINESE_SYSTEM_SUFFIX,
    _build_full_system_prompt,
    sanitize_text,
)

ROOT = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

#: system prompt 里的句数硬上限（Stage 1.4 会删掉它）
HARD_CAP = "回复 1~3 句"

#: AI 味词表（黑名单）：命中即说明回复像「通用助手」而不像桌宠
AI_FLAVOR_WORDS = (
    # 自我暴露成 AI / 语言模型
    "作为AI", "作为一个AI", "作为一个人工智能", "作为一个语言模型",
    "我是一个AI", "我是一个人工智能", "我是一个语言模型", "语言模型",
    # 客服 / 助手腔固定句式
    "很高兴为您", "很荣幸", "希望这能帮到您", "希望这可以帮助您",
    "希望对您有所帮助", "为您解答", "为您解答以下", "如果您还有其他",
    "如有任何其他", "如果您需要进一步", "请随时告诉我", "综上所述",
    "总而言之", "总的来说", "值得注意的是", "需要注意的是",
    "非常感谢您的", "感谢您的提问", "以下是一些建议", "以下是几点建议",
)

#: 过渡语：被删掉就说明回复变得不像人说话
_TRANSITION_RE = re.compile(r"我来|让我|我看看")

#: 客服腔句式：「已帮你…」「已为主人…」
_SERVICE_PHRASE_RE = re.compile(
    r"已(?:经)?\s*(?:帮你|帮您|为你|为您|为主人|帮主人|给主人|替你|替您)"
)

#: 工具独白的**评测口径**（不是清洗规则）。
#: Stage 3 删掉了清洗管线里的 _META_TOOL_SENT_RE —— 那族正则连人话都吃，
#: 但「工具独白有没有漏进最终回复」仍然要量：这里保留一份同名口径用来测量。
_TOOL_MONOLOGUE_RE = re.compile(
    r"(?:调用|使用|借助|运行|执行|通过)\s*[A-Za-z_0-9\u4e00-\u9fff]{0,20}?工具|"
    r"工具\s*(?:来|去|打开|执行|查|完成|处理|帮)|"
    r"这应该用|应该用\s*[a-z_]+|open_[a-z_]+\s*工具|"
    r"[a-z_]+_(?:website|app|tool|reminder|fact)",
    flags=re.IGNORECASE,
)

#: 判定「这段原文本来就是元描述 / 工具独白」的标志
_META_NARRATION_MARKERS = (
    "调用", "工具", "抓取", "爬取", "根据角色", "根据人设", "主人想要",
    "主人说", "用户说", "唯一允许", "输出规范", "输出要求",
    # MiniMax 会把「工具调用」当正文吐出来（<tool_call> / <invoke name=...>），
    # 清洗管线把它整段剥掉是对的 —— 识别出来，避免被误判成「剥光了正常回复」。
    "tool_call", "<invoke", "invoke name", "minimax[>",
)

#: 拼 prompt 用的人设片段（跟 characters/xiaoxi.yaml 的第一句一致）
PERSONA_SNIPPET = "你是「小汐」，住在用户桌面上的女仆桌宠，称呼用户为主人。"


# --------------------------------------------------------------------------
# 评测集读取
# --------------------------------------------------------------------------

def _fixture_path() -> Path:
    """评测集路径；可用环境变量覆盖（临时副本验证用，不影响 CI）。"""
    override = os.environ.get("NATURALNESS_CASES", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / "fixtures" / "naturalness_cases.json"


def _load_cases() -> list:
    path = _fixture_path()
    if not path.is_file():
        raise AssertionError(f"找不到自然度评测集：{path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("cases") or []
    if not isinstance(data, list):
        raise AssertionError("评测集格式不对：顶层必须是数组")
    return data


CASES = _load_cases()
CASE_IDS = [str(c.get("id")) for c in CASES]


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def _assemble_prompt(case: dict) -> str:
    """按 case 里的 system prompt 片段拼出「当时」的完整 system prompt。"""
    return "\n\n".join(case.get("system_prompt_fragments") or [])


def _ai_flavor_hits(text: str) -> list:
    """AI 味词表命中项。"""
    return [w for w in AI_FLAVOR_WORDS if w in text]


def _transition_hits(text: str) -> list:
    """出现的过渡语（去重）。"""
    return sorted(set(_TRANSITION_RE.findall(text)))


#: 推理模型的思维链块（MiniMax 会吐 CoT）
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


def _visible_answer(text: str) -> str:
    """剥掉 <think>…</think> 思维链，只留**主人看得见**的那段回复。

    为什么必须剥：思维链草稿里也会写「让我…」「我看看…」这类词。
    拿整段原始文本（含 CoT）跟清洗后的结果比，会把「CoT 里有过渡语」
    误判成「清洗管线把过渡语删了」—— 假的（可见回复里根本没有过渡语），
    而且这种误报会盖住真正的回归。
    """
    out = _THINK_BLOCK_RE.sub("", text or "")
    # 没闭合的 <think>（流式截断）也剔掉 —— 跟 sanitize_text 的口径一致
    out = re.sub(r"<think>.*$", "", out, flags=re.DOTALL)
    return out


def _service_hits(text: str) -> list:
    """客服腔句式命中项。"""
    return _SERVICE_PHRASE_RE.findall(text)


def _looks_like_meta_narration(text: str) -> bool:
    """原文是不是本来就是元描述 / 工具独白（被剥光是合理的）。"""
    return any(m in text for m in _META_NARRATION_MARKERS)


def _recorded_cases() -> list:
    """已经录好 golden reply 的 case。"""
    return [c for c in CASES if c.get("golden_reply")]


class _GarbageClient:
    """只会吐出被剥光的文本（模拟模型复读 prompt / 工具独白）。"""

    def __init__(self, text: str):
        self.text = text

    async def chat_stream_events(self, messages, tools=None, cancel_check=None,
                                 force_tool_use=False):
        yield "text", self.text
        yield "finish", {"content": "", "tool_calls": []}


class _EmptyRegistry:
    """空工具表（走不到任何工具分支）。"""

    def names(self):
        return []

    def to_openai(self):
        return []

    def execute(self, name, args):
        return ""


def _run_agent(text: str) -> list:
    """跑一轮 AgentLoop，返回事件列表（离线，不发网络请求）。"""
    loop = asyncio.new_event_loop()
    try:
        async def _collect():
            agen = AgentLoop(_GarbageClient(text), _EmptyRegistry(),
                             max_turns=1).run(
                [{"role": "system", "content": "你是桌宠"},
                 {"role": "user", "content": "你好呀"}])
            return [ev async for ev in agen]
        return loop.run_until_complete(_collect())
    finally:
        loop.close()


# --------------------------------------------------------------------------
# L1：现状刻画（今天绿；Stage 1.3 / 1.4 会改对应断言）
# --------------------------------------------------------------------------

class TestCharacterization:
    """锁住「今天」的行为。改这里之前先看 Stage 1.3 / 1.4 的任务说明。"""

    def test_think_block_is_stripped(self):
        """推理模型的 <think>…</think> 不能进最终回复。"""
        # 现状依据：app/brain/llm_client.py:282-284
        raw = "<think>模型在思考主人的问题</think>今天天气不错呢，主人要不要出去玩~"
        assert sanitize_text(raw) == "今天天气不错呢，主人要不要出去玩~"

    def test_unclosed_think_block_is_stripped(self):
        """没闭合的 <think>（流式截断）也要剥干净。"""
        # 现状依据：app/brain/llm_client.py:283
        assert sanitize_text("<think>还在想\n今天天气不错呢~") == ""

    def test_tool_meta_narration_is_no_longer_rewritten(self):
        """Stage 3：清洗管线不再用正则猜「这句在说工具」，一律原样保留。

        原来靠 _is_pollution_sentence + _META_TOOL_SENT_RE + _META_SELF_PLAN_SENT_RE
        整句删工具独白；但那族正则同时吃掉「让我看看。」「主人说今天会下雨」。
        业主裁决：去掉硬限制。工具独白改由 prompt 层拦，管线只删模型工件。
        """
        for raw in ("我需要调用 open_app 打开记事本。今天天气不错呢。",
                    "让我调用工具查一下广州天气。",
                    "这应该用open_website工具来打开B站的网址。"):
            assert sanitize_text(raw) == raw, f"流水线仍在改写模型散文：{raw!r}"

    def test_normal_cjk_reply_survives_unchanged(self):
        """正常中文回复一个字都不能被改 —— Stage 1.3 也不能破这条契约。"""
        # 现状依据：app/brain/llm_client.py:279-317（没有任何规则命中）
        #          + :311-316（中文占比兜底不触发）
        for raw in (
            "今天天气不错呢，主人要不要出去玩~",
            "广州今天晴天33度，明天有小雨，记得带伞喵~",
            "我搜索了一下最近的新闻，主人要听吗？",
            "唔……那个……水杯是不是空了呀，先喝口水吧。",
        ):
            assert sanitize_text(raw) == raw, f"正常回复被改了：{raw!r}"

    def test_empty_reply_fallback_is_reached(self):
        """清洗把回复剥光时，必须落到空回复兜底话术（不能给主人看空气泡）。

        Stage 1.3：兜底话术从「固定一句」改成**多条轮换**，
        所以这里断言「轮换池里任意一条出现」，而不是固定那一句。

        Stage 3：删掉「规则复读」那族正则后，「唯一允许的输出语言是简体中文」
        不再被剥光（它本来也是人话），所以换两个**真正会空掉**的入口来测：
          - 纯英文（低中文占比兜底 → 空）
          - 只有 <think> 思维链（CoT 全剥 → 空）
        """
        # 现状依据：app/brain/llm_client.py 的 sanitize_text 第 1/9 步
        #          + app/brain/agent.py 的 _EMPTY_REPLY_FALLBACKS（轮换池）
        assert sanitize_text("Hello World") == ""
        assert sanitize_text("<think>只有思维链，没有可见答案</think>") == ""
        for raw in ("Hello World", "<think>只有思维链，没有可见答案</think>"):
            evs = _run_agent(raw)
            texts = [d for ev, d in evs if ev == "text"]
            assert texts and any(t.strip() for t in texts), \
                "没有兜底话术，主人会看到空气泡"
            joined = " ".join(texts)
            assert any(line in joined for line in _EMPTY_REPLY_FALLBACKS), (
                f"兜底话术不在轮换池里：{joined!r}")

    def test_emoji_is_kept_now(self):
        """Stage 1.3：emoji **保留**（原来被全局剥掉，回复因此不像人发的）。

        原名 test_emoji_is_stripped_today —— Stage 1.3 放开 emoji 后翻转断言并改名。
        """
        # 依据：app/brain/llm_client.py 的 sanitize_text 已不再调用 _EMOJI_PATTERN
        #      （Stage 1.3：emoji 不再全局剥离；TTS 走独立管线，不受影响）
        assert sanitize_text("你好\U0001F600世界") == "你好\U0001F600世界"
        assert sanitize_text("\U0001F389恭喜主人") == "\U0001F389恭喜主人"
        assert "\u2728" in sanitize_text("闪亮\u2728一下")

    def test_prompt_no_longer_contains_hard_cap(self):
        """Stage 1.4 之后：system prompt 里**不再**有「回复 1~3 句」硬上限。

        长度改成**策略**（默认短句 / 需要时 2~3 句 / 不要每次一样长），
        落在共享层 app/brain/prompt_layer.py 的 LENGTH_STRATEGY 里。
        """
        # 依据：app/brain/prompt_layer.py 的 CHINESE_SYSTEM_SUFFIX（末尾不再有硬上限）
        #      + LENGTH_STRATEGY（长度策略）
        assert HARD_CAP not in CHINESE_SYSTEM_SUFFIX
        assembled = _build_full_system_prompt(PERSONA_SNIPPET)
        assert HARD_CAP not in assembled
        assert assembled.startswith(PERSONA_SNIPPET)
        # 长度策略必须在（替代硬上限）
        assert "【回复长度】" in assembled

    def test_reply_does_not_start_with_question_mark(self):
        """现状：清洗后不会以问号开头（句子切分把开头的问号当成上一句的句末）。"""
        # 现状依据：app/brain/llm_client.py:266（_SENT_SPLIT_RE）
        #          + :509-531（_strip_meta_by_sentence 按句切分，空句直接丢弃）
        assert sanitize_text("？你今天很忙吗") == "你今天很忙吗"


# --------------------------------------------------------------------------
# L2：golden 评测集
# --------------------------------------------------------------------------

def test_fixture_has_twenty_cases():
    """评测集固定 20 条（多一条少一条都会让基线不可比）。"""
    assert len(CASES) == 20, f"评测集应该是 20 条，实际 {len(CASES)} 条"


def test_case_ids_are_unique():
    """case id 必须唯一。"""
    ids = [c.get("id") for c in CASES]
    assert len(set(ids)) == len(ids), f"case id 重复：{ids}"


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_case_shape_is_valid(case):
    """每条 case 字段要齐全（golden_reply 允许为 null = 还没录）。"""
    missing = {"id", "input", "system_prompt_fragments", "golden_reply"} - set(case)
    assert not missing, f"缺字段：{sorted(missing)}"
    assert isinstance(case["id"], str) and case["id"].strip()
    assert isinstance(case["input"], str) and case["input"].strip()
    assert isinstance(case["system_prompt_fragments"], list) and case["system_prompt_fragments"]
    for frag in case["system_prompt_fragments"]:
        assert isinstance(frag, str) and frag.strip(), "system prompt 片段不能为空"
    assert case["golden_reply"] is None or isinstance(case["golden_reply"], str)


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_golden_reply_structure(case):
    """golden reply 过一遍清洗管线，断言结构性质。"""
    golden = case.get("golden_reply")
    if golden is None:
        pytest.skip("未录制 golden reply - 运行 scripts/record_naturalness.py --record")

    raw = golden
    cleaned = sanitize_text(raw)

    # ---- 现状刻画：今天必须成立 ----
    assert "<think>" not in cleaned and "</think>" not in cleaned
    assert len(cleaned) <= len(raw), "清洗不该把回复变长"
    assert len(_ai_flavor_hits(cleaned)) <= len(_ai_flavor_hits(raw))
    if not cleaned:
        # 模型这一轮**什么都没产出**（只剩 <think> 思维链）—— 没东西可评，跳过。
        # 这跟「清洗把一段正常回复剥光了」是两回事：后者才是 Stage 1.3 的回归。
        # （实测：长技术问题会让 MiniMax 的英文思维链吃光 max_tokens=1024，
        #   可见回复被整段截掉 —— 原文里本来就没有可见内容。）
        if not _visible_answer(raw).strip():
            pytest.skip("该条 golden reply 只有思维链、没有可见回复（模型这轮没产出内容）")
        assert _looks_like_meta_narration(raw), (
            "清洗把一段普通中文回复剥光了（Stage 1.3 必须保证正常回复不被清空）"
        )
        pytest.skip("该条 golden reply 本身就是元描述 / 工具独白，清洗后为空")
    # ---- 期望门禁：Stage 1.3 / 1.4 修好后自动转 pass ----
    violations = []
    # Stage 3：清洗管线不再删工具独白（那族正则连人话都吃），
    # 所以「独白漏进最终回复」改由**评测口径**量，命中记 violation。
    if _TOOL_MONOLOGUE_RE.search(cleaned):
        violations.append(f"工具独白漏进了最终回复：{cleaned!r}")
    ai_hits = _ai_flavor_hits(cleaned)
    if ai_hits:
        violations.append(f"AI 味词命中 {ai_hits}")
    service = _service_hits(cleaned)
    if service:
        violations.append(f"客服腔句式 {service}")
    # 只比**可见回复**（剥掉 <think> 思维链）—— 见 _visible_answer 的注释：
    # 思维链里的过渡语不算数，否则会误报「过渡语被误删」。
    lost = [t for t in _transition_hits(_visible_answer(raw))
            if t not in _transition_hits(cleaned)]
    if lost:
        violations.append(f"过渡语被清洗管线误删 {lost}")
    if cleaned.startswith(("?", "？")):
        violations.append("回复以问号开场")
    prompt = _assemble_prompt(case)
    if HARD_CAP in prompt:
        violations.append(f"system prompt 残留硬上限「{HARD_CAP}」")
    if violations:
        pytest.xfail("自然度门禁未达标（Stage 1.3 / 1.4 修好后自动转 pass）："
                     + "；".join(violations))


def test_reply_length_variance_across_cases():
    """回复长度要有方差 —— 全部一样长说明回复太模板化。"""
    recorded = _recorded_cases()
    if len(recorded) < 2:
        pytest.skip("已录制的 golden reply 不足 2 条（还没录）")
    lengths = [len(sanitize_text(c["golden_reply"])) for c in recorded]
    assert statistics.pvariance(lengths) > 0, f"长度方差为 0：{lengths}"


def test_recorded_replies_are_not_all_questions():
    """不能每条回复都以问号结尾（那是客服腔，不是聊天）。"""
    recorded = _recorded_cases()
    if len(recorded) < 2:
        pytest.skip("已录制的 golden reply 不足 2 条（还没录）")
    tails = [sanitize_text(c["golden_reply"]).rstrip() for c in recorded]
    tails = [t for t in tails if t]
    if tails and all(t.endswith(("?", "？")) for t in tails):
        pytest.xfail("所有回复都以问号结尾 —— 太客服腔（Stage 1.4 调完 prompt 再看）")


def test_app_prompt_has_no_hard_cap():
    """期望门禁：system prompt 里不能再有「回复 1~3 句」硬上限。

    Stage 1.4 之前它还在（app/brain/llm_client.py:87），所以今天是 xfail；
    Stage 1.4 删掉之后自动转 pass。
    """
    assembled = _build_full_system_prompt(PERSONA_SNIPPET)
    if HARD_CAP in assembled:
        pytest.xfail(f"system prompt 还残留硬上限「{HARD_CAP}」（Stage 1.4 会删）")
    assert HARD_CAP not in assembled


# --------------------------------------------------------------------------
# L4：opt-in 的 live 测试（发版前人工确认用，不进硬门禁）
# --------------------------------------------------------------------------

def _config_api_key() -> str:
    """从正常配置路径（环境变量 / config.yaml）读 llm.api_key。"""
    for env in ("LLM__API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        value = os.environ.get(env, "").strip()
        if value:
            return value
    cfg_path = ROOT / "config.yaml"
    if not cfg_path.is_file():
        return ""
    try:
        import yaml
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return ""
    llm = raw.get("llm") or {}
    return str(llm.get("api_key") or "").strip()


def _has_real_key() -> bool:
    """本机是否配了真 key（占位符不算）。"""
    from app.core.api_keys import is_placeholder_key
    return not is_placeholder_key(_config_api_key())


@pytest.mark.slow
@pytest.mark.skipif(not _has_real_key(),
                    reason="没有配置 API Key，跳过 live 自然度测试")
def test_live_reply_is_natural():
    """真调一次模型，人工确认回复自然度（不进硬门禁）。

    默认不跑：slow marker 由 tests/conftest.py 在没有 --run-slow 时跳过。
    """
    from app.brain.llm_client import ChatMessage, LLMClient
    from app.core.config import load_config

    cfg = load_config(str(ROOT / "config.yaml"))
    client = LLMClient(cfg.llm, system_prompt=cfg.character.persona)

    async def _ask(text: str) -> str:
        chunks = []
        async for piece in client.chat_stream(
                [ChatMessage(role="user", content=text)]):
            chunks.append(piece)
        return "".join(chunks)

    loop = asyncio.new_event_loop()
    try:
        reply = loop.run_until_complete(_ask(CASES[0]["input"]))
        loop.run_until_complete(client.close())
    finally:
        loop.close()

    print(f"live 回复：{reply}")
    assert reply.strip(), "live 回复是空的"
    assert any("\u4e00" <= ch <= "\u9fff" for ch in reply), "live 回复里没有中文"
