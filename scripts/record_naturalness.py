#!/usr/bin/env python3
"""录制自然度评测集的 golden reply（Stage 0.4；Stage 1.7 用**生产 prompt** 重录）。

用法
----
    # 1) 先在 config.yaml 里填好 llm.api_key（base_url / model 用默认值）
    # 2) 显式加 --record 才允许跑（防手滑烧钱）
    python scripts/record_naturalness.py --record
    # 3) 想重录已经录过的条目，再加 --overwrite
    python scripts/record_naturalness.py --record --overwrite
    # 4) 只拼 prompt 对账、不联网：
    python scripts/record_naturalness.py --record --dry-run
    # 5) 给**别的角色**录一份黄金集（下一个角色就差这一条命令）：
    python scripts/record_naturalness.py --record --character characters/dfy.yaml
    #    -> 写 tests/fixtures/naturalness_cases_dfy.json

行为
----
- 没带 --record -> 打印用法 + 非零退出，一个网络请求都不发
- 没有 key / key 还是占位符 -> 打印怎么用 + 非零退出，一个网络请求都不发
- 默认幂等：已经录好的条目直接跳过，重复跑不花钱
- 只写黄金集文件（tests/fixtures/naturalness_cases*.json），不碰别的文件
- **角色感知**：默认录**当前加载的角色**（config.yaml + data/character_selection.yaml
  解析出来的那个）；--character characters/xxx.yaml 可以给任意角色录一份，
  黄金集按角色分文件（小汐 = naturalness_cases.json，大肥鱼 = naturalness_cases_dfy.json）。
  场景（id / input）取自种子集 naturalness_cases.json，场景里提到的角色名换成目标角色的。
- **system prompt 走生产路径**（app/brain/prompt_layer.build_system_prompt）：
  拼完之后跟两个后端**真正发出去**的那条 system 消息逐字节对账，
  对不上直接失败退出（不再拿一个「看起来差不多」的 prompt 去录）。
- 长期记忆库 / 会话库 / 提醒库全在**临时目录**，绝不碰 data/ 下的真库
  （data/memory_小汐.db 连读都不读）。
- 录的是**模型原始回复**（没过清洗管线）：评测时由 tests/test_naturalness.py
  过一遍清洗管线再断言，这样「过渡语被误删」这类问题才测得出来
- 模型走**函数调用**通道时，工具调用不在 message.content 里（content 只剩
  思维链）—— 录制时把 API 的 tool_calls 字段还原成 MiniMax 的文本工具调用
  格式一起录下来。否则这条 golden 会退化成「一段思维链」，清洗管线剥完
  什么都不剩，会被误判成「正常回复被剥光了」（假失败）。

退出码
------
0  成功（或条目全都录过了，什么都没做）
1  有条目录制失败 / prompt 对账失败
2  用法不对 / 没配 key
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: 黄金集目录（每个角色一份）
FIXTURES_DIR = ROOT / "tests" / "fixtures"
#: 场景种子（20 条 id / input 的来源）：小汐那份
DEFAULT_CASES = FIXTURES_DIR / "naturalness_cases.json"
DEFAULT_CONFIG = ROOT / "config.yaml"

#: 角色文件 stem -> 黄金集文件名。
#: 小汐是**默认角色**，沿用没有后缀的老文件名；别的角色一律 naturalness_cases_<stem>.json。
_FIXTURE_BY_STEM = {"xiaoxi": "naturalness_cases.json"}

#: BrainController 只认 root 下的这几个 SKILL.md 路径
#: （app/brain/brain_controller.py::_SKILL_PATH_CANDIDATES）
_SKILL_RELPATH = ".agents/skills/desktop-pet-tool-usage/SKILL.md"


def fixture_name_for(character_file: str) -> str:
    """角色文件 -> 黄金集文件名（录制脚本和测试门禁共用这一份口径）。"""
    stem = Path(str(character_file or "").strip()).stem or "default"
    return _FIXTURE_BY_STEM.get(stem, f"naturalness_cases_{stem}.json")


def fixture_path_for(character_file: str) -> Path:
    """角色文件 -> 黄金集路径（tests/fixtures/ 下）。"""
    return FIXTURES_DIR / fixture_name_for(character_file)

def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="录制自然度评测集的 golden reply（需要 API key，只跑一次）")
    parser.add_argument("--record", action="store_true",
                        help="确认要真调模型（不加就退出）")
    parser.add_argument("--overwrite", action="store_true",
                        help="覆盖已经录好的 golden reply")
    parser.add_argument("--dry-run", action="store_true",
                        help="只拼 system prompt 并对账，不发任何网络请求")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG),
                        help="配置文件路径（默认 config.yaml）")
    parser.add_argument("--cases", default=str(DEFAULT_CASES),
                        help="场景种子路径（默认 tests/fixtures/naturalness_cases.json，只取 id / input）")
    parser.add_argument("--character", default="",
                        help="要录的角色文件（默认当前加载的角色），如 characters/dfy.yaml")
    parser.add_argument("--out", default="",
                        help="黄金集输出路径（默认按角色推导：tests/fixtures/naturalness_cases_<stem>.json）")
    parser.add_argument("--model", default="",
                        help="临时换模型（默认用配置里的 llm.model）")
    parser.add_argument("--timeout", type=int, default=60,
                        help="单次请求超时秒数（默认 60）")
    return parser.parse_args(argv)


# --------------------------------------------------------------------------
# 生产 system prompt（唯一拼装点：app/brain/prompt_layer.py）
# --------------------------------------------------------------------------

def _sha256(text: str) -> str:
    """文本的 sha256（utf-8 字节级）。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _temp_root(prefix: str) -> Path:
    """建一个临时 root（记忆库 / 会话库 / 提醒库都落这儿）。

    不用 TemporaryDirectory：Windows 上 memory sidecar 还攥着日志文件时，
    rmtree 会抛 PermissionError，把整次录制带崩 —— 钱花了、黄金集却没落盘。
    """
    return Path(tempfile.mkdtemp(prefix=prefix))


def _cleanup_temp_root(path: Path) -> None:
    """删临时 root；删不掉就算了（反正是 %TEMP% 里的垃圾）。"""
    shutil.rmtree(path, ignore_errors=True)


def _prepare_temp_root(root: Path, tmp_root: Path) -> None:
    """把 SKILL.md 复制到临时 root。

    BrainController 只从 **root** 下读 SKILL.md；不复制的话它会退化成
    内置兜底 skill，拼出来的 prompt 就跟生产不一样了。
    """
    src = root / _SKILL_RELPATH
    dst = tmp_root / _SKILL_RELPATH
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)


def _load_live_state(root: Path):
    """读桌宠实时状态（**只读** data/pet_state.json，绝不写）。

    生产路径里桌宠状态进的是「易变通道」（独立的 user 消息），
    录出来的回复要跟真机一样，就得用真状态。
    """
    from app.engine.state import PetState

    path = root / "data" / "pet_state.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return PetState.from_dict(raw)
    except Exception:  # noqa: BLE001
        return PetState()


def _current_level(state) -> int:
    """当前桌宠等级（跟生产一致：state.level；读不到就按工具全解锁 Lv.10）。"""
    try:
        lvl = int(getattr(state, "level", 0) or 0)
    except Exception:  # noqa: BLE001
        lvl = 0
    if lvl <= 0:
        try:
            from app.engine.tools._core import _get_current_tool_level
            lvl = int(_get_current_tool_level())
        except Exception:  # noqa: BLE001
            lvl = 10
    return max(1, min(10, lvl))


def build_production_prompt(cfg, *, root: Path, tmp_root: Path) -> dict:
    """按**生产路径**拼出最终 system prompt（并跟两个后端逐字节对账）。

    生产路径（app/ui/chat_window/__init__.py::_kickoff_llm）：

        system = persona + "\n\n" + BrainController.chat_context()
        → AgentLoop / LangChainAgent 用共享层 build_system_prompt() 重拼
          （人设 → style guide → 工具手册 → 动态块 → 防幻觉基线
            → 中文输出约束 → 长度策略 → 【说话方式】+【任务执行协议】）

    这里完全照抄这条路。**临时 root + 临时记忆库**：
    data/memory_小汐.db、data/pet_sessions.db 一个都不碰。

    Returns:
        dict: system_prompt / react_prompt / standard_prompt / level / state /
              persona_blob / brain
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from app.core.qt_compat import QApplication
    QApplication.instance() or QApplication(sys.argv[:1])

    from app.brain.brain_controller import BrainController
    from app.brain.prompt_layer import (
        build_dynamic_block,
        build_system_prompt,
        build_tool_handbook,
        load_style_guide,
    )
    from app.engine.reminder import ReminderStore

    # 1) 临时 root：SKILL.md 照抄；记忆库 / 会话库 / 提醒库全在临时目录
    _prepare_temp_root(root, tmp_root)

    cfg2 = copy.deepcopy(cfg)
    cfg2.brain.memory_file = str(tmp_root / "memory.json")   # 长期记忆 → 临时
    cfg2.brain.proactive_enabled = False                    # 别起主动搭话线程
    if getattr(cfg2, "mcp", None) is not None:
        cfg2.mcp.enabled = False                            # 别拉起 MCP server 子进程

    state = _load_live_state(root)
    level = _current_level(state)
    state.level = level
    reminders = ReminderStore(tmp_root / "data" / "reminders.json")
    brain = BrainController(tmp_root, cfg2, state, reminders)

    # 2) 聊天窗口的拼法：人设 + 会话上下文（SKILL / 工具列表）
    persona_blob = cfg2.character.persona + "\n\n" + brain.chat_context()

    # 3) 共享层拼法（ReAct 后端走的就是这一条）
    system_prompt = build_system_prompt(
        persona=persona_blob,
        style_guide=load_style_guide(),
        tool_handbook=build_tool_handbook(registry=brain.tool_registry, level=level),
        dynamic=build_dynamic_block(),
    )

    # 4) 两个后端**真正发出去**的那条 system 消息（对账用）
    react_prompt = _react_backend_prompt(cfg2, brain, persona_blob, level)
    standard_prompt = _standard_backend_prompt(cfg2, brain, persona_blob, level)
    return {
        "system_prompt": system_prompt,
        "react_prompt": react_prompt,
        "standard_prompt": standard_prompt,
        "level": level,
        "state": state,
        "persona_blob": persona_blob,
        "brain": brain,
    }


def _react_backend_prompt(cfg, brain, persona_blob: str, level: int) -> str:
    """ReAct（轻量）后端真正发出去的那条 system 消息。

    参考 tests/test_backend_parity.py::test_production_path_system_prompts_are_byte_identical。
    """
    import asyncio

    from app.brain.agent import AgentLoop
    from app.brain.llm_client import LLMClient
    from app.core.config import LLMConfig

    llm = LLMConfig(api_key=cfg.llm.api_key, model=cfg.llm.model, stream=False)
    client = LLMClient(llm, persona_blob)
    try:
        agent = AgentLoop(client=client, registry=brain.tool_registry,
                          level_getter=lambda: level)
        agent._apply_system_prompt()
        return client.system_prompt
    finally:
        asyncio.run(client.close())


def _standard_backend_prompt(cfg, brain, persona_blob: str, level: int) -> str:
    """standard（LangChain）后端真正发出去的那条 system 消息。"""
    from app.brain.langchain_agent import LangChainAgent
    from app.core.config import LLMConfig

    llm = LLMConfig(api_key=cfg.llm.api_key, model=cfg.llm.model, stream=False)
    agent = LangChainAgent(
        llm_cfg=llm, registry=brain.tool_registry, persona=persona_blob,
        level_getter=lambda: level, memory_store=brain.memory,
    )
    return agent.assemble_system_prompt()


def _build_fragments(system_prompt: str, persona: str) -> list:
    """从**录制时的 prompt** 里切出逐字片段（写回评测集）。

    三段，缺一不可：
    1. 人设开头 —— 评测集要能看出「当时用的是哪个人设」
    2. CHINESE_SYSTEM_SUFFIX 全文 —— 句数硬上限「回复 1~3 句」原来住在这儿；
       留着它，以后谁再把硬上限加回 suffix，用例会自动翻回 xfail
       （片段必须是 prompt 的逐字子串，所以 suffix 变了片段就跟着变）
    3. LENGTH_STRATEGY 全文 —— Stage 1.4 的长度策略（替代硬上限）
    """
    from app.brain.prompt_layer import CHINESE_SYSTEM_SUFFIX, LENGTH_STRATEGY

    head = (persona or "").strip().splitlines()
    persona_head = head[0].strip() if head else ""
    fragments = [persona_head, CHINESE_SYSTEM_SUFFIX, LENGTH_STRATEGY]
    for frag in fragments:
        if not frag:
            raise RuntimeError("片段为空")
        if frag not in system_prompt:
            raise RuntimeError(
                f"片段不是录制用 prompt 的逐字子串：{frag[:40]!r}")
    return fragments


# --------------------------------------------------------------------------
# 角色 / 黄金集
# --------------------------------------------------------------------------

def resolve_character(cfg, character_arg: str = "") -> tuple:
    """返回 (name, persona, character_file)。

    给了 character_arg 就用那个角色文件；没给就用**当前加载的角色**
    （load_config 已经把 data/character_selection.yaml / config.yaml 的
    character_file 解析进 cfg.character 了）。
    """
    rel = (character_arg or "").strip()
    if rel:
        from app.core.config import load_character_entry

        entry = load_character_entry(ROOT, rel)
        if not entry.ok:
            raise RuntimeError(f"角色文件不可用：{rel}（{entry.error}）")
        return entry.name, entry.persona, entry.path
    return (cfg.character.name or "", cfg.character.persona or "",
            cfg.character.character_file or "")


def _load_case_list(path) -> list:
    """读一个黄金集文件 -> list[dict]（顶层是数组、或 {"cases": [...]} 都认）。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("cases") or []
    return data if isinstance(data, list) else []


def _seed_character_name(seed_cases: list) -> str:
    """认出种子集是**哪个角色**的（人设开头形如「你是「小汐」」）。"""
    for case in seed_cases or []:
        for frag in (case.get("system_prompt_fragments") or []):
            found = re.search(r"你是「(.+?)」", frag or "")
            if found:
                return found.group(1)
    return ""


def _adapt_input(text: str, old_name: str, new_name: str) -> str:
    """场景里提到的角色名换成目标角色的名字。

    场景的 **id / 形状不变**（还是那 20 条），只有「叫名字」那条要叫新角色的名字
    —— 否则录出来的 golden 是在演别的角色。
    """
    if not text or not old_name or not new_name or old_name == new_name:
        return text
    return text.replace(old_name, new_name)


# --------------------------------------------------------------------------
# 工具调用的还原
# --------------------------------------------------------------------------

#: MiniMax 自己把工具调用写成正文时用的文本外壳。
#: 模型有时不走函数调用通道，直接把工具调用吐在 content 里；录制时用同一套
#: 格式把 API 的 tool_calls 字段还原出来 —— 评测集里两种情形长得一样。
_MINIMAX_MARK = "]<]minimax[>[<"


def _serialize_tool_calls(tool_calls) -> str:
    """把 API 的 tool_calls 字段序列化成 MiniMax 的文本工具调用格式。

    为什么必须做这一步
    ------------------
    模型走**函数调用**通道时，工具调用不在 message.content 里 —— content 只剩
    思维链。只录 content 的话，这条 golden 剥掉思维链后**什么都不剩**，评测时
    会被误判成「正常回复被剥光了」（假失败）。
    把工具调用补回去，这条 golden 就是一条**纯工具独白**：清洗管线剥掉它，
    用例按「工具独白」跳过 —— 跟模型把工具调用写成正文的情形完全一致。
    """
    if not tool_calls:
        return ""
    parts = [_MINIMAX_MARK + "tool_call>\n"]
    for call in tool_calls:
        fn = (call or {}).get("function") or {}
        name = str(fn.get("name") or "")
        raw_args = fn.get("arguments") or ""
        args_block = ""
        if raw_args:
            try:
                parsed = json.loads(raw_args)
            except (TypeError, ValueError):
                parsed = None
            # 参数原样放一行（JSON 原文）：清洗管线整段剥掉工具调用时，
            # 这一行也跟着走 —— 参数里全是中文也不会漏成「可见回复」。
            args_block = _MINIMAX_MARK + "<arguments>" + raw_args + "</arguments>"
        parts.append(_MINIMAX_MARK + 'invoke name="%s">' % name
                     + args_block + _MINIMAX_MARK + "</invoke>\n")
    parts.append(_MINIMAX_MARK + "</tool_call>")
    return "".join(parts)


def _with_tool_calls(content: str, tool_calls) -> str:
    """把 API 的工具调用补进录下来的原文（content 里已经写了就不重复补）。"""
    text = content or ""
    if not tool_calls:
        return text
    if "tool_call" in text or "<invoke" in text:
        return text
    return text + _serialize_tool_calls(tool_calls)


# --------------------------------------------------------------------------
# 录制
# --------------------------------------------------------------------------

def _usage_of(obj: dict) -> dict:
    """从 API 响应里取 token 用量（prompt / completion / cached）。"""
    usage = (obj or {}).get("usage") or {}
    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    cached = details.get("cached_tokens")
    if cached is None:
        cached = details.get("cache_read_input_tokens") or 0
    return {
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
        "cached_tokens": int(cached or 0),
    }


def _ask_one(model: str, api_key: str, base_url: str, system_prompt: str,
             user_text: str, dynamic_context: str, temperature: float,
             max_tokens: int, timeout: int) -> tuple:
    """发一次 chat/completions，返回 (模型**原始**回复, token 用量)。

    消息排布跟生产一致：system + 本轮易变上下文（独立 user 消息）+ 主人原话。
    """
    import httpx

    url = base_url.rstrip("/") + "/chat/completions"
    messages = [{"role": "system", "content": system_prompt}]
    if dynamic_context:
        messages.append({"role": "user", "content": dynamic_context})
    messages.append({"role": "user", "content": user_text})
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(url, json=payload, headers=headers)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    obj = resp.json()
    choices = obj.get("choices") or []
    if not choices:
        raise RuntimeError(f"响应里没有 choices：{str(obj)[:200]}")
    message = choices[0].get("message") or {}
    content = message.get("content") or ""
    tool_calls = message.get("tool_calls") or message.get("function_call") or []
    if isinstance(tool_calls, dict):
        tool_calls = [tool_calls]
    content = _with_tool_calls(content, tool_calls)
    if not content.strip():
        raise RuntimeError("模型返回了空回复")
    return content, _usage_of(obj)


def main(argv=None) -> int:
    args = _parse_args(argv)

    if not args.record:
        print("没带 --record —— 录制要花钱、要本机的 API key，确认了就加：")
        print("    python scripts/record_naturalness.py --record")
        print("（想重录已经录过的条目，再加 --overwrite）")
        return 2

    from app.core.api_keys import is_placeholder_key
    from app.core.config import load_config

    try:
        cfg = load_config(args.config)
    except Exception as exc:  # noqa: BLE001
        print(f"读配置失败：{exc}")
        return 2

    api_key = (cfg.llm.api_key or "").strip()
    if is_placeholder_key(api_key):
        print("没有可用的 API Key —— 一个网络请求都不发，直接退出。")
        print(f"  在 {args.config} 里填 llm.api_key，或设环境变量 LLM__API_KEY")
        print("  填好之后再跑：python scripts/record_naturalness.py --record")
        return 2

    model = args.model or cfg.llm.model

    # ---- 角色：--character 指定；默认用**当前加载的角色** ------------------
    try:
        char_name, char_persona, char_file = resolve_character(cfg, args.character)
    except Exception as exc:  # noqa: BLE001
        print(f"角色解析失败：{exc}")
        return 2
    cfg.character.name = char_name
    cfg.character.persona = char_persona
    cfg.character.character_file = char_file

    # ---- 种子（场景来源）/ 输出（这个角色的黄金集）------------------------
    seed_path = Path(args.cases)
    if not seed_path.is_file():
        print(f"找不到场景种子：{seed_path}")
        return 2
    seed_cases = _load_case_list(seed_path)
    if not seed_cases:
        print(f"场景种子是空的：{seed_path}")
        return 2

    out_path = Path(args.out) if args.out else fixture_path_for(char_file)
    seed_name = _seed_character_name(seed_cases)
    cases = _load_case_list(out_path) if out_path.is_file() else []
    if cases:
        # 已有黄金集：只校正 input（幂等），已录好的 golden reply 一个都不动
        for case in cases:
            case["input"] = _adapt_input(case.get("input") or "",
                                          seed_name, char_name)
    else:
        # 新角色：从种子重建场景（角色名换成目标角色的），golden 全部待录
        cases = [{
            "id": c.get("id"),
            "input": _adapt_input(c.get("input") or "", seed_name, char_name),
            "system_prompt_fragments": [],
            "golden_reply": None,
        } for c in seed_cases]
    seed_ids = [c.get("id") for c in seed_cases]
    out_ids = [c.get("id") for c in cases]
    if out_ids != seed_ids:
        print(f"警告：{out_path.name} 的用例 id 跟种子对不上：{out_ids} != {seed_ids}")

    print(f"角色：{char_name}（{char_file}）")
    print(f"黄金集：{out_path}")

    # ---- 生产 system prompt（临时 root：不碰 data/ 下的任何真库） ----------
    tmp_root = _temp_root("naturalness_record_")
    try:
        built = build_production_prompt(cfg, root=ROOT, tmp_root=tmp_root)
        system_prompt = built["system_prompt"]
        react_prompt = built["react_prompt"]
        standard_prompt = built["standard_prompt"]

        print("system prompt 对账（生产路径 vs 两个后端真正发出去的那条）：")
        print(f"  生产路径  sha256={_sha256(system_prompt)}  {len(system_prompt.encode('utf-8'))}B")
        print(f"  ReAct     sha256={_sha256(react_prompt)}  {len(react_prompt.encode('utf-8'))}B")
        print(f"  standard  sha256={_sha256(standard_prompt)}  {len(standard_prompt.encode('utf-8'))}B")
        if system_prompt.encode("utf-8") != react_prompt.encode("utf-8"):
            print("对账失败：生产路径拼出来的 prompt 跟 ReAct 后端发的不一致 —— 不录！")
            return 1
        if system_prompt.encode("utf-8") != standard_prompt.encode("utf-8"):
            print("对账失败：生产路径拼出来的 prompt 跟 standard 后端发的不一致 —— 不录！")
            return 1
        print("  → 三者逐字节相同 ✓")

        fragments = _build_fragments(system_prompt, cfg.character.persona)

        # 本轮易变上下文（当前时间 / 桌宠实时状态）—— 生产走独立的 user 消息
        from app.brain.llm_client import build_dynamic_context
        dynamic_context = build_dynamic_context(state=built["state"])
    finally:
        _cleanup_temp_root(tmp_root)

    # prompt 跟评测集里记的片段对不上 -> 提醒（prompt 变了就会出现）
    old_fragments = cases[0].get("system_prompt_fragments") or [] if cases else []
    missing = [f for f in old_fragments if f and f not in system_prompt]
    if missing:
        print("警告：现在的 system prompt 跟评测集里记的片段对不上了，"
              "评测集里的 system_prompt_fragments 该更新了：")
        for frag in missing:
            print(f"  - {frag[:60]}")

    print(f"模型：{model}（base_url={cfg.llm.base_url}）")
    print(f"待录条目：{len(cases)} 条")
    print(f"新片段（写回评测集）：{len(fragments)} 段，长度 {[len(f) for f in fragments]}")

    if args.dry_run:
        print("--dry-run：一个网络请求都不发，什么都没写。")
        return 0

    failed = 0
    recorded = 0
    totals = {"prompt_tokens": 0, "completion_tokens": 0,
              "total_tokens": 0, "cached_tokens": 0}
    for case in cases:
        case_id = case.get("id")
        if case.get("golden_reply") and not args.overwrite:
            print(f"[跳过] {case_id}：已经录过了（要重录加 --overwrite）")
            continue
        try:
            reply, usage = _ask_one(
                model=model,
                api_key=api_key,
                base_url=cfg.llm.base_url,
                system_prompt=system_prompt,
                user_text=case.get("input") or "",
                dynamic_context=dynamic_context,
                temperature=cfg.llm.temperature,
                max_tokens=cfg.llm.max_tokens,
                timeout=args.timeout,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[失败] {case_id}：{exc}")
            failed += 1
            continue
        case["golden_reply"] = reply
        recorded += 1
        for key in totals:
            totals[key] += usage.get(key, 0)
        preview = reply.replace("\n", " ")[:60]
        tag = "｜含工具调用" if "tool_call" in reply else ""
        print(f"[录好] {case_id}：{preview}｜prompt {usage['prompt_tokens']} tok，"
              f"cached {usage['cached_tokens']} tok{tag}")

    # 片段按**这次录制用的 prompt** 统一回写（逐字子串，staleness 检查不会报 mismatch）
    for case in cases:
        case["system_prompt_fragments"] = list(fragments)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(cases, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"写回：{out_path}（新录 {recorded} 条，失败 {failed} 条）")
    print(f"token 用量合计：prompt {totals['prompt_tokens']}，"
          f"cached {totals['cached_tokens']}，"
          f"completion {totals['completion_tokens']}，total {totals['total_tokens']}")
    if totals["prompt_tokens"]:
        rate = 100.0 * totals["cached_tokens"] / totals["prompt_tokens"]
        print(f"前缀缓存命中率：{rate:.1f}%（cached / prompt）")
    if failed:
        return 1
    if recorded == 0:
        print("全部条目都录过了，什么都没改（想重录加 --overwrite）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
