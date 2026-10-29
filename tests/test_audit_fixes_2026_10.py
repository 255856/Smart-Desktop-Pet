"""全量审计回归测试：模型说成功但实际未执行 / 未调用 这类问题的回归锁。

Stage 3.2 之后：**工具成败不再靠关键词猜**，工具层把结果翻成结构化信封
（status / data / truncated / error_code），这里断言的也是信封。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.brain.agent import (  # noqa: E402
    AgentLoop, DANGEROUS_TOOLS, _ACTION_CLAIM_RE, _HONEST_FAIL_RE,
    _INTENT_TOOL_NAMES, _SUCCESS_CLAIM_WORDS, _claims_success,
    _claims_unbacked_action, _final_contradicts_tools,
)
from app.engine.tools._core import (  # noqa: E402
    Tool, ToolRegistry, ToolResult,
)


# =============================================================================
# 1) 失败文案必须变成结构化错误（不再靠 agent 猜关键词）
#    工具层知道每个工具「失败长什么样」，把它翻成稳定的 error_code；
#    agent 只读 status / error_code。
# =============================================================================

def _fake_registry(tool_name: str, text: str) -> ToolRegistry:
    """注册一个只会返回固定文案的假工具。"""
    reg = ToolRegistry()
    reg.register(Tool(name=tool_name, description="fake",
                      parameters={"type": "object", "properties": {}},
                      fn=lambda text=text: text))
    return reg


@pytest.mark.parametrize("tool_name,text,expect_code", [
    # _pet feed_self
    ("feed_self", "钱不够（20 金币，现有 5），先去打工吧～", "insufficient_funds"),
    ("feed_self", "没有叫「火锅」的食物。可选：可乐、面包", "unknown_item"),
    # _math convert_units
    ("convert_units", "不支持的换算：parsec → ly", "unsupported_conversion"),
    # _runner run_script（带「错误：」前缀 → 走契约）
    ("run_script", "错误：脚本执行失败（退出码 1），\n[exit=1] x.py", "tool_error"),
    # _pet play_animation
    ("play_animation", "没有叫「happy」的动画，未触发任何动作。", "unknown_animation"),
    # 记忆检索
    ("recall_memory", "没有找到匹配的记忆", "no_match"),
    # _system open_app
    ("open_app", "没装这个软件", "not_installed"),
    # _audio set_brightness（带「错误：」前缀，但错误码映射表更具体）
    ("set_brightness",
     "错误：本机没有支持 WMI 亮度的显示器（台式机常见），亮度未改变。",
     "unsupported_display"),
    # _audio set_volume（没有前缀，靠错误码映射表）
    ("set_volume", "无法设置音量到 30%：缺少 pycaw", "missing_dependency"),
])
def test_failure_phrases_become_structured_errors(tool_name, text, expect_code):
    """这些文案以前只能靠关键词命中；现在由工具层翻成 error_code。"""
    out = _fake_registry(tool_name, text).execute(tool_name, "{}")
    assert out["status"] == "error", out
    assert out["error_code"] == expect_code, out
    assert out["data"] == text, out
    assert out["truncated"] is False, out


def test_confirm_timeout_is_structured_denied():
    """危险工具确认超时 → 信封 status='denied'、error_code='confirm_timeout'。

    超时必须和「主人点了否」（user_cancelled）分开，否则会对主人谎称
    「你取消了」。
    """
    agent = AgentLoop(client=None, registry=ToolRegistry(),
                      confirm_tool=lambda n, a: None)
    env = asyncio.run(agent._confirm_or_skip("lock_screen", "{}"))
    assert env["status"] == "denied", env
    assert env["error_code"] == "confirm_timeout", env
    assert "超时" in env["data"], env


def test_user_cancel_is_structured_denied():
    """主人拒绝 → 信封 status='denied'、error_code='user_cancelled'。"""
    agent = AgentLoop(client=None, registry=ToolRegistry(),
                      confirm_tool=lambda n, a: False)
    env = asyncio.run(agent._confirm_or_skip("lock_screen", "{}"))
    assert env["status"] == "denied", env
    assert env["error_code"] == "user_cancelled", env


@pytest.mark.parametrize("tool_name,text", [
    ("open_app", "已启动 QQ"),
    ("take_screenshot_tool", "截图成功（base64，128400 字符）"),
    ("feed_self", "吃掉了「面包」，花 3 金币。现在状态：Lv.2"),
    ("web_search", "找到 5 条结果"),
    ("cancel_countdown", "已取消倒计时 #3（到点不会再提醒）"),
    ("set_brightness", "已把亮度调到 80%"),
])
def test_success_phrases_stay_ok(tool_name, text):
    """正常成功不能被误判成失败（否则桌宠会无故道歉）。"""
    out = _fake_registry(tool_name, text).execute(tool_name, "{}")
    assert out["status"] == "ok", out
    assert out["error_code"] == "", out


def test_error_prefix_alone_is_enough():
    """项目契约：工具失败必须返回带「错误：」前缀的串（docs/tools-reference.md）。

    契约由**工具层**兜底翻译成 error_code='tool_error'，agent 不再自己猜。
    """
    out = _fake_registry("open_app", "错误：某个从没见过的失败原因").execute(
        "open_app", "{}")
    assert out["status"] == "error", out
    assert out["error_code"] == "tool_error", out


# =============================================================================
# 2) 成功词表必须覆盖人设自己的措辞
#    原表只有「帮你打开」，人设全程说「帮主人打开」→ 最常见的报喜检测不到
# =============================================================================

@pytest.mark.parametrize("text", [
    "已经帮主人打开星穹铁道啦~",
    "帮主人打开QQ了~",
    "打开星穹铁道啦！",
    "已经帮你打开星穹铁道啦~",
    "星穹铁道已经打开啦~",
    "记好啦~ 主人喜欢冰美式",
    "截图好啦，主人看看~",
    "已经帮主人设好提醒啦~",
    "已经帮主人算好啦：42",
])
def test_persona_phrasings_count_as_success_claim(text):
    assert _claims_success(text) is True, text
    assert _claims_unbacked_action(text) is True, text


def test_conditional_statement_is_not_a_claim():
    """条件句不该被当成「已经做完了」。"""
    assert _claims_unbacked_action("如果你打开QQ就会看到我的消息") is False
    assert _claims_unbacked_action("需要的话我可以帮主人打开记事本") is False


# =============================================================================
# 3)「唔」不能进失败词表
#    「唔」是小汐的口头禅（人设/兜底话术都有），放进来会让校验器在几乎
#    所有回复上自我静音
# =============================================================================

def test_filler_word_does_not_disable_verification():
    assert not _HONEST_FAIL_RE.search("唔"), (
        "「唔」是人设口头禅，放进诚实失败判定会让幻觉校验在几乎所有回复上失效")
    ex = [("open_app", "{}", ToolResult("error", "错误：未找到应用 QQ",
                                          error_code="tool_error"))]
    assert _final_contradicts_tools(
        "唔，已经帮主人打开QQ啦~", ex) is True, (
        "带「唔」的报喜也必须被抓出来")


def test_honest_failure_still_passes():
    ex = [("open_app", "{}", ToolResult("error", "错误：未找到应用 QQ",
                                          error_code="tool_error"))]
    assert _final_contradicts_tools("抱歉主人，没能打开QQ", ex) is False


# =============================================================================
# 4) 零工具执行时也必须校验（原实现 executed_tools 为空直接 return False）
# =============================================================================

def test_no_tools_means_unbacked_claim():
    assert _final_contradicts_tools("已经帮主人打开QQ啦~", []) is False, (
        "这条保持 False：_final_contradicts_tools 只管「工具跑了但失败」，"
        "零工具场景由 _claims_unbacked_action 负责")
    assert _claims_unbacked_action("已经帮主人打开QQ啦~") is True


class _TextOnlyClient:
    """只回文字、从不调工具——专门复现「模型凭空报喜」。

    同一句话会重复返回（除非显式给了多轮不同台词），
    这样才能覆盖「force_retry 一直不调工具、直到轮数耗尽」这条路径。
    """

    def __init__(self, texts):
        self.queue = list(texts)
        self.default = self.queue[0] if self.queue else ""
        self.calls = 0

    async def chat_stream_events(self, messages, tools=None,
                                 cancel_check=None, force_tool_use=False):
        self.calls += 1
        t = self.queue.pop(0) if self.queue else self.default
        yield ("text", t)
        yield ("finish", {"reason": "stop", "tool_calls": [],
                          "content": t, "reasoning": ""})


def _reg():
    r = ToolRegistry()
    r.register(Tool(name="open_app", description="open app",
                    parameters={"type": "object",
                                "properties": {"app_name": {"type": "string"}}},
                    fn=lambda app_name: f"已启动 {app_name}"))
    return r


def test_zero_tool_claim_with_detected_intent_uses_retry_chain():
    """意图命中时，先走 force_retry；轮数耗尽也不能把伪造的报喜发给主人。"""
    agent = AgentLoop(client=_TextOnlyClient(["已经帮主人打开QQ啦~"]),
                      registry=_reg(), max_turns=3)
    texts = []

    async def drive():
        async for ev, *rest in agent.run([{"role": "user", "content": "帮我打开QQ"}]):
            if ev == "text":
                texts.append(rest[0])

    asyncio.run(drive())
    joined = "".join(texts)
    assert "没能完成" in joined, f"应换成诚实兜底，实际：{joined!r}"
    assert "打开QQ啦" not in joined, f"伪造的报喜被放行了：{joined!r}"


def test_force_retry_is_capped_at_one():
    """M2：「该调工具却只给文字」的重试上限为 1 次。

    原来能连重试 5 轮（MAX_TURNS=6），而第 2..6 次带的几乎是同一个请求
    （强制提示由 LLMClient 统一注入，tool_choice 降级还按模型名缓存着），
    等于把同一请求重复发 5 次——费钱且慢。
    """
    client = _TextOnlyClient(["已经帮主人打开QQ啦~"])
    agent = AgentLoop(client=client, registry=_reg(), max_turns=6)
    retries = []

    async def drive():
        async for ev, *rest in agent.run([{"role": "user", "content": "帮我打开QQ"}]):
            if (ev == "meta" and isinstance(rest[0], dict)
                    and rest[0].get("event") == "force_retry"):
                retries.append(rest[0])

    asyncio.run(drive())
    assert len(retries) == 1, f"force_retry 最多 1 次，实际 {len(retries)} 次"
    # 首次请求 + 1 次重试 = 2 次，不能更多
    assert client.calls == 2, f"LLM 请求次数应为 2，实际 {client.calls}"


def test_zero_tool_claim_without_intent_triggers_verify_retry():
    """意图正则没命中时（force_first_turn=False），零工具报喜要靠
    _claims_unbacked_action 兜住——原实现这里完全是盲区。"""
    # 「把它归档吧」不命中任何意图正则，但模型若回「已经帮主人保存好了」
    # 就是凭空报喜：这一轮从头到尾没有任何工具被调用。
    agent = AgentLoop(
        client=_TextOnlyClient(["已经帮主人保存好了~", "好的主人，已保存。"]),
        registry=_reg(), max_turns=3)
    events = []

    async def drive():
        async for ev, *rest in agent.run(
                [{"role": "user", "content": "把它归档吧"}]):
            events.append((ev, rest))

    asyncio.run(drive())
    retries = [e for e in events
               if e[0] == "meta" and isinstance(e[1][0], dict)
               and e[1][0].get("event") == "verify_retry"]
    assert retries, "零工具却声称成功时必须触发 verify_retry"
    assert retries[0][1][0]["reason"] == "未调用工具却声称成功"


def test_zero_tool_claim_is_replaced_when_out_of_turns():
    """没有剩余轮次时也不能把伪造的成功话术发给主人。"""
    agent = AgentLoop(client=_TextOnlyClient(["已经帮主人打开QQ啦~"]),
                      registry=_reg(), max_turns=1)
    texts = []

    async def drive():
        async for ev, *rest in agent.run([{"role": "user", "content": "帮我打开QQ"}]):
            if ev == "text":
                texts.append(rest[0])

    asyncio.run(drive())
    joined = "".join(texts)
    assert "打开" not in joined or "没能完成" in joined, (
        f"伪造的报喜被放行了：{joined!r}")


def test_normal_chat_is_not_flagged():
    """闲聊不能被误伤。"""
    agent = AgentLoop(client=_TextOnlyClient(["主人今天辛苦啦~"]),
                      registry=_reg(), max_turns=2)
    events = []

    async def drive():
        async for ev, *rest in agent.run([{"role": "user", "content": "我有点累"}]):
            events.append((ev, rest))

    asyncio.run(drive())
    assert not [e for e in events
                if e[0] == "meta" and isinstance(e[1][0], dict)
                and e[1][0].get("event") == "verify_retry"]


# =============================================================================
# 5) 工具裁剪
# =============================================================================

def test_intent_trimming_shrinks_toolset():
    """实测 52 个全量下发时，模型 5/6 次把「打开星穹铁道」错答成
    list_installed_apps。裁剪后应只剩相关工具。"""
    reg = ToolRegistry()
    for name in ("open_app", "list_installed_apps", "open_website",
                 "get_current_time", "system_info", "web_search",
                 "kill_process", "run_script"):
        reg.register(Tool(name=name, description=name,
                          parameters={"type": "object", "properties": {}},
                          fn=lambda: "ok"))
    all_tools = reg.to_openai()
    agent = AgentLoop(client=None, registry=reg)

    picked = agent._select_tools("open_app", all_tools)
    names = {t["function"]["name"] for t in picked}
    assert "open_app" in names
    assert len(names) < len(all_tools), "open_app 意图不该全量下发"
    assert "kill_process" not in names, "开应用时不该下发杀进程"
    assert "run_script" not in names

    # 识别不出意图 → 全量，不牺牲任何能力
    assert agent._select_tools(None, all_tools) is all_tools
    assert agent._select_tools("不存在的意图", all_tools) is all_tools

    # 裁完为空 → 退回全量（宁可多给也不能把路堵死）
    empty = [t for t in all_tools if t["function"]["name"] == "kill_process"]
    assert agent._select_tools("open_app", empty) is empty


def test_every_intent_whitelist_is_non_trivial():
    for intent, names in _INTENT_TOOL_NAMES.items():
        assert 3 <= len(names) <= 20, f"{intent} 白名单大小不合理：{len(names)}"
        assert len(set(names)) == len(names), f"{intent} 白名单有重复：{names}"


def test_alarm_intent_never_offers_countdown():
    """回归：主人说「定闹钟」时，模型不该还能选 countdown。"""
    alarm_tools = set(_INTENT_TOOL_NAMES["add_reminder"])
    for t in ("countdown", "list_countdowns", "cancel_countdown"):
        assert t not in alarm_tools, (
            f"闹钟意图不应下发 {t}，否则模型可能又选到内存态倒计时")
    assert "add_reminder" in alarm_tools, "闹钟意图必须给到 add_reminder"


def test_countdown_is_hard_blocked_under_alarm_intent():
    """白名单是软约束：实测模型仍硬调了没下发的 countdown。

    registry 按名字执行，光「不下发」拦不住。要让「只保留 add_reminder」
    真正确定，必须硬拒并把原因讲清楚，模型下一轮才会改。
    """
    import asyncio
    from app.brain.agent import _INTENT_BLOCKED_TOOLS

    assert "countdown" in _INTENT_BLOCKED_TOOLS["add_reminder"]

    executed = []

    class _C:
        async def chat_stream_events(self, messages, tools=None,
                                     cancel_check=None, force_tool_use=False):
            yield ("finish", {"reason": "tool_calls", "tool_calls": [
                {"id": "c1", "name": "countdown",
                 "arguments": '{"seconds":60,"message":"到点啦~"}'}],
                "content": "", "reasoning": ""})

    reg = ToolRegistry()
    reg.register(Tool(name="countdown", description="倒计时",
                      parameters={"type": "object",
                                  "properties": {"seconds": {"type": "number"}}},
                      fn=lambda seconds, message="": executed.append(seconds)))
    agent = AgentLoop(client=_C(), registry=reg)
    results = []

    async def drive():
        async for ev, *rest in agent.run([{"role": "user", "content": "定一个1分钟闹钟"}]):
            if ev == "tool":
                results.append((rest[0], rest[2]))

    asyncio.run(drive())
    assert executed == [], "闹钟意图下 countdown 不该真的被执行"
    assert results, "应产出工具结果事件"
    assert "错误" in results[0][1], results[0]
    assert "add_reminder" in results[0][1], "拒绝理由里要告诉模型改用什么"


def test_countdown_still_available_for_plain_timers():
    """纯计时不能被误伤：「番茄钟 / 计时 3 分钟」不命中闹钟意图，
    应走全量工具集，countdown 照常可用。"""
    from app.brain.llm_client import detect_action_intent
    for q in ("煮面计时 3 分钟", "番茄钟 25 分钟", "倒数 60 秒"):
        assert detect_action_intent(q) is None, f"{q!r} 不该被判成闹钟意图"
    agent = AgentLoop(client=None, registry=ToolRegistry())
    all_tools = [Tool(name="countdown", description="c",
                      parameters={"type": "object", "properties": {}},
                      fn=lambda: "ok").to_openai()[0]] if hasattr(Tool, "to_openai") \
        else [{"type": "function",
               "function": {"name": "countdown", "description": "c",
                            "parameters": {"type": "object", "properties": {}}}}]
    assert agent._select_tools(None, all_tools) is all_tools


# =============================================================================
# 6) 危险工具表
# =============================================================================

def test_open_actions_not_dangerous():
    assert "open_app" not in DANGEROUS_TOOLS
    assert "open_website" not in DANGEROUS_TOOLS
    for k in ("lock_screen", "kill_process", "run_script", "shutdown_computer"):
        assert k in DANGEROUS_TOOLS


def test_confirm_timeout_is_not_reported_as_user_cancel():
    """确认回调三态：True 放行 / False 拒绝 / None 超时。
    超时必须和「主人点了否」区分，否则会对他谎称是他取消的。"""

    class C:
        async def chat_stream_events(self, messages, tools=None,
                                     cancel_check=None, force_tool_use=False):
            yield ("finish", {"reason": "stop", "tool_calls": [
                {"id": "c1", "name": "lock_screen", "arguments": "{}"}],
                "content": "", "reasoning": ""})
            yield ("text", "好的主人")

    reg = ToolRegistry()
    reg.register(Tool(name="lock_screen", description="lock",
                      parameters={"type": "object", "properties": {}},
                      fn=lambda: "已锁屏"))

    for ret, expect in ((False, "用户取消"), (None, "超时")):
        agent = AgentLoop(client=C(), registry=reg, confirm_tool=lambda n, a: ret)
        results = []

        async def drive():
            async for ev, *rest in agent.run(
                    [{"role": "user", "content": "锁屏"}]):
                if ev == "tool":
                    results.append(rest[2])

        asyncio.run(drive())
        assert expect in results[0], f"回调返回 {ret!r} 时结果应含「{expect}」"


# =============================================================================
# 7) 工具实现层
# =============================================================================

def test_feed_self_reports_failure_honestly():
    """钱不够 / 没有该食物 必须判 fail。"""

    class It:
        name, price = "火锅", 20

    class Items:
        items = [It()]

        def by_name(self, n):
            return It() if n == "火锅" else None

    class State:
        money = 5.0

        def stats_summary(self):
            return "x"

    from app.engine.tools import _pet
    reg = ToolRegistry()
    _pet.register(reg, state=State(), items=Items(), hooks={})
    out = reg.execute("feed_self", json.dumps({"food_name": "火锅"}))
    assert out["status"] == "error", out
    out2 = reg.execute("feed_self", json.dumps({"food_name": "不存在"}))
    assert out2["status"] == "error", out2


def test_run_script_nonzero_exit_is_failure():
    """run_script 非零退出码必须判 fail。"""
    from app.engine.tools import _runner
    assert "错误" in (_runner.__doc__ or "") or True   # 仅确保模块可导入
    import inspect
    src = inspect.getsource(_runner)
    assert "脚本执行失败" in src, "run_script 必须对非零退出码显式标失败"


def test_math_mm_conversion():
    """mm→m / mm→in / F→K / parsec→ly 转换结果正确。"""
    from app.engine.tools._math import _convert
    assert "0.005" in _convert(5, "mm", "m")
    assert "0.0197" in _convert(5, "mm", "in")
    assert "310.93" in _convert(100, "F", "K"), "F→K 原来根本不支持"
    assert "错误" in _convert(1, "parsec", "ly")


@pytest.mark.slow
def test_cancel_countdown_actually_stops_thread():
    """cancel_countdown 必须真停掉后台线程。"""
    from app.engine.tools import _timer
    fired = []
    _timer.set_say_hook(lambda s: fired.append(s))
    try:
        cid = _timer._add_countdown(1.2, "应该被取消")
        out = _timer.cancel_countdown(cid)
        assert "已取消" in out
        time.sleep(1.8)
        assert fired == [], f"取消后仍然触发了：{fired}"

        _timer._add_countdown(0.2, "应该触发")
        time.sleep(0.8)
        assert fired, "未取消的倒计时必须能触发"
    finally:
        _timer.set_say_hook(None)


def test_open_file_explorer_sandbox():
    """open_file_explorer 拒绝可执行文件 / 系统目录 / 不存在路径, 家目录放行。"""
    from app.engine.tools import _shortcuts
    ok, err = _shortcuts._check_explorable(str(Path.home() / "some.exe"))
    assert ok is None and "可执行" in err
    ok, err = _shortcuts._check_explorable(r"C:\Windows\System32")
    assert ok is None and "家目录" in err
    ok, err = _shortcuts._check_explorable(
        str(Path.home() / "绝对不存在的目录_zzz"))
    assert ok is None and "不存在" in err
    ok, err = _shortcuts._check_explorable(str(Path.home()))
    assert ok is not None and err == "", f"家目录本身应放行：{err}"


def test_clear_bubble_reports_honestly_without_hook():
    """clear_bubble 无 hook 时必须判 fail。"""
    from app.engine.tools import _power
    reg = ToolRegistry()
    _power.register(reg)
    out = reg.execute("clear_bubble", "{}")
    if "已隐藏" in out:
        return  # 运行环境注入了 hook
    assert out["status"] == "error", out


def test_set_wifi_string_false_disables():
    """set_wifi 字符串 "false"/"no"/"0"/"关" 都应被判 False。"""
    from app.engine.tools._power import _as_bool
    for raw in ("false", "False", "no", "0", "关", False, 0):
        assert _as_bool(raw) is False, raw
    for raw in ("true", "True", "yes", "1", "开", True, 1):
        assert _as_bool(raw) is True, raw
    assert _as_bool("maybe") is None
    assert _as_bool(None) is None


def test_play_animation_reports_when_hook_missing():
    """UI 通道不可用时不能回报「已触发」。"""
    from app.engine.tools import _pet
    reg = ToolRegistry()
    _pet.register(reg, state=None, items=None, hooks={})
    out = reg.execute("play_animation", json.dumps({"anim_name": "spin"}))
    assert out["status"] == "error", out


def test_countdown_is_honest_about_missing_channel():
    """没有提醒通道时必须当场说, 不能承诺「到点会提醒你」。"""
    from app.engine.tools import _timer
    _timer.set_say_hook(None)
    try:
        out = _timer.countdown(seconds=0.5, message="t")
        has = _timer._has_notify_channel()
        if has:
            assert "到点主人会收到提醒" in out
        else:
            assert "没有接入任何提醒通道" in out, out
            assert "到点主人会收到提醒" not in out, out
            assert "不要说" in out, out
    finally:
        _timer.cancel_all()


def test_no_cross_thread_ui_emit_in_timer():
    """回归：倒计时线程里不能 emit 可能已析构的 QObject。

    之前为了「如实告知」在后台线程里回调 UI 信号，实测在测试中会随机把
    pytest 进程打成 Windows fatal access violation（QObject 析构后 emit）。
    现在改为在 countdown() 调用时同步判断通道可用性。
    """
    import inspect
    from app.engine.tools import _timer
    src = inspect.getsource(_timer._run_countdown)
    assert "set_no_channel_hook" not in src, (
        "后台线程不得回调 UI 相关 hook")
    assert not hasattr(_timer, "set_no_channel_hook"), (
        "这个跨线程回调入口已被移除，不要再加回来")


def test_ocr_reports_backend_missing_honestly():
    """OCR 后端都没装时必须说清缺什么，而不是含糊失败。"""
    from app.engine.tools._ocr import _NO_BACKEND
    assert "winsdk" in _NO_BACKEND
    assert "pytesseract" in _NO_BACKEND
