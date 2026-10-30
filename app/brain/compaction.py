# -*- coding: utf-8 -*-
"""Stage 4.3：上下文压缩 —— **摘要并遗忘**（不再是「只丢最旧」）。

对齐 qq-bridge 挂载的 DSH 插件 dsh-compaction-basic 的参数口径
（见 docs/comparison-with-qq-bridge.md 第 4.6.4 节）：

======================  ======  ============================================
参数                    值      含义
======================  ======  ============================================
thresholdRatio          0.8     上下文用量达到预算的 80% 就触发压缩
retainRatio            0.16    压缩后保留最近 16% 的消息**原文**
keep_recent（N）        8       最新 8 条**永远原文保留**，不参与摘要
======================  ======  ============================================

三条红线
--------
1. **系统提示词不参与压缩** —— 压缩只发生在对话历史里，
   system 串逐字节不变（MiniMax 的前缀缓存按「最长公共前缀」判定，
   见 tests/test_prompt_cache_stability.py）。
2. **摘要失败不许炸聊天路径** —— 网络错误 / 超时 / 空结果 一律退回
   「丢最旧」（今天的行为），这一轮照样能聊。
3. **摘要不是唯一记忆** —— 主人说过的事实在记忆 sidecar
   （app/brain/memory_client.py）里；摘要只负责让对话读起来连续，
   不负责「记住一切」。

本模块只做**纯计算 + 一次摘要请求**：
- 「要不要压 / 压哪一段」是纯函数（plan_compaction），不发任何请求；
- 「怎么写摘要」是一次 LLM 调用（summarise_history），**永不抛异常**。
真正的编排（后台线程、失败兜底、落到 history 上）在
app/ui/chat_window/__init__.py 里。
"""
from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 参数默认值（= qq-bridge dsh-compaction-basic 的口径）
# ---------------------------------------------------------------------------

#: 上下文用量达到预算的这个比例就压缩
DEFAULT_THRESHOLD_RATIO = 0.8
#: 压缩后保留最近消息原文的比例
DEFAULT_RETAIN_RATIO = 0.16
#: 最新 N 条永远原文保留，不参与摘要
DEFAULT_KEEP_RECENT = 8
#: 摘要请求的超时（秒）—— 超时按失败处理（退回丢最旧）
DEFAULT_SUMMARY_TIMEOUT_S = 20.0
#: 摘要正文最长多少字（防止模型写一篇长文把预算又吃回去）
DEFAULT_SUMMARY_MAX_CHARS = 600

#: 摘要条目的固定标记 —— 一眼能认出「这段是压缩出来的」
SUMMARY_MARKER = "【历史摘要】"

#: yaml 段名（app/core/companion_config.yaml）
YAML_SECTION = "context_compaction"


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CompactionSettings:
    """压缩参数（值来自 companion_config.yaml 的 context_compaction 段）。"""

    threshold_ratio: float = DEFAULT_THRESHOLD_RATIO
    retain_ratio: float = DEFAULT_RETAIN_RATIO
    keep_recent: int = DEFAULT_KEEP_RECENT
    summary_timeout_s: float = DEFAULT_SUMMARY_TIMEOUT_S
    summary_max_chars: int = DEFAULT_SUMMARY_MAX_CHARS


def _clamp_float(value, default: float, low: float, high: float) -> float:
    """把 yaml 里的值转成 float 并夹到合法区间（写坏了也不抛）。"""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out:            # NaN
        return default
    return max(low, min(high, out))


def _clamp_int(value, default: int, low: int, high: int) -> int:
    """把 yaml 里的值转成 int 并夹到合法区间（写坏了也不抛）。"""
    try:
        out = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, out))


def _read_yaml_section() -> dict:
    """读 companion_config.yaml 的 context_compaction 段（只读，不改）。

    走 yaml 原文：companion_config 的 dataclass 会丢掉没登记的字段
    （跟 app/engine/tools/_core.py 读 tool_allowlist 段一个路子）。
    加载顺序跟它一致：用户覆盖表 > 仓库默认表。
    """
    try:
        from app.core import companion_config as cc
    except Exception as e:  # noqa: BLE001 - 配置读不到不影响聊天
        log.debug("读压缩配置失败（companion_config 不可用）：%s", e)
        return {}
    try:
        base = cc._read_yaml(cc._default_yaml_path()) or {}
        user = cc._read_yaml(cc._user_yaml_path()) or {}
        merged = cc._merge(base, user)
    except Exception as e:  # noqa: BLE001
        log.warning("读压缩配置失败：%s", e)
        return {}
    block = merged.get(YAML_SECTION)
    return block if isinstance(block, dict) else {}


_cached_settings: Optional[CompactionSettings] = None


def load_compaction_settings(*, force: bool = False) -> CompactionSettings:
    """读压缩参数（进程内缓存；force=True 强制重读 yaml）。"""
    global _cached_settings
    if _cached_settings is not None and not force:
        return _cached_settings
    block = _read_yaml_section()
    settings = CompactionSettings(
        threshold_ratio=_clamp_float(
            block.get("threshold_ratio"), DEFAULT_THRESHOLD_RATIO, 0.05, 1.0),
        retain_ratio=_clamp_float(
            block.get("retain_ratio"), DEFAULT_RETAIN_RATIO, 0.0, 1.0),
        keep_recent=_clamp_int(
            block.get("keep_recent"), DEFAULT_KEEP_RECENT, 1, 1000),
        summary_timeout_s=_clamp_float(
            block.get("summary_timeout_s"), DEFAULT_SUMMARY_TIMEOUT_S, 1.0, 300.0),
        summary_max_chars=_clamp_int(
            block.get("summary_max_chars"), DEFAULT_SUMMARY_MAX_CHARS, 50, 20000),
    )
    _cached_settings = settings
    return settings


def reset_settings_cache() -> None:
    """测试用：清掉缓存，下次重新读 yaml。"""
    global _cached_settings
    _cached_settings = None


# ---------------------------------------------------------------------------
# 纯计算：要不要压 / 压哪一段
# ---------------------------------------------------------------------------

def message_chars(message: Any) -> int:
    """一条消息占多少字符（content 为空算 0）。"""
    return len(getattr(message, "content", "") or "")


def history_chars(history: Sequence[Any]) -> int:
    """整段历史占多少字符 —— 这就是「上下文用量」的估算口径。"""
    return sum(message_chars(m) for m in (history or []))


@dataclass
class CompactionPlan:
    """一次压缩的方案（纯数据，不含任何 IO）。"""

    #: 要摘要掉的**最旧**那段消息
    to_summarise: list
    #: 原文保留的最近消息（逐字节不变 —— 同一个对象，不复制）
    keep: list
    #: 压缩前的用量（字符）
    used_chars: int
    #: 字符预算
    budget: int

    @property
    def ratio(self) -> float:
        """用量占预算的比例。"""
        if not self.budget:
            return 0.0
        return self.used_chars / float(self.budget)

    @property
    def keep_count(self) -> int:
        return len(self.keep)


def plan_compaction(history: Sequence[Any], settings: Optional[CompactionSettings] = None,
                    *, budget: int = 0) -> Optional[CompactionPlan]:
    """算出「要不要压缩 + 压哪一段」。**纯函数**：不发任何请求。

    规则（参数口径对齐 qq-bridge dsh-compaction-basic）：
        - 用量 < 预算 × threshold_ratio → 返回 None（**什么都不动**）
        - 最新 keep_recent 条**永远原文保留**（不参与摘要）
        - 保留条数 = max(keep_recent, ceil(总条数 × retain_ratio))
        - 其余**最旧**的部分进摘要
        - 消息太少（<= keep_recent）→ 没有可摘要的部分 → None
    """
    settings = settings or load_compaction_settings()
    history = list(history or [])
    if not history or budget <= 0:
        return None
    used = history_chars(history)
    if used < settings.threshold_ratio * budget:
        return None
    total = len(history)
    keep_count = max(int(settings.keep_recent),
                      int(math.ceil(total * settings.retain_ratio)))
    if keep_count >= total:
        # 全部都要原文保留 → 没有可摘要的部分
        return None
    cut = total - keep_count
    return CompactionPlan(
        to_summarise=history[:cut],
        keep=history[cut:],
        used_chars=used,
        budget=int(budget),
    )


def summary_entry_content(summary_text: str) -> str:
    """摘要条目的正文（带固定标记，让对话读起来连续）。"""
    body = (summary_text or "").strip()
    return f"{SUMMARY_MARKER}（更早的对话已经压缩成下面这段）\n{body}"


def apply_compaction(history: Sequence[Any], plan: CompactionPlan, summary_text: str,
                     make_entry: Callable[[str], Any]) -> list:
    """把摘要替换掉「要摘要的那一段」，返回**新的**历史列表。

    - 摘要条目放在被摘要掉那段的**原位**（正常情况下就是最旧的位置），
      对话读起来连续；
    - 被摘要掉的原文不再出现；
    - 保留的最近消息**逐字节不变**（同一个对象，不复制、不改写）。

    make_entry(summary_text) 由调用方提供 —— 聊天窗用自己的 Message 类型，
    本模块不认识 UI 的 dataclass。
    """
    history = list(history or [])
    summarised = {id(m) for m in (plan.to_summarise or [])}
    first_idx = None
    for i, m in enumerate(history):
        if id(m) in summarised:
            first_idx = i
            break
    kept = [m for m in history if id(m) not in summarised]
    insert_at = 0
    if first_idx is not None:
        insert_at = sum(1 for m in history[:first_idx] if id(m) not in summarised)
    entry = make_entry(summary_entry_content(summary_text))
    kept.insert(insert_at, entry)
    return kept


# ---------------------------------------------------------------------------
# 摘要请求（一次 LLM 调用；**永不抛异常**）
# ---------------------------------------------------------------------------

#: 摘要器的人设（LLMClient 的 system 串 = 共享层 + 这个人设）
SUMMARY_PERSONA = (
    "你是一个对话摘要器：把旧对话压缩成几句话的中文摘要，"
    "让桌宠在上下文被压缩之后还能读懂前面的脉络。"
    "你只输出摘要正文本身，不寒暄、不解释、不加标题。"
)

#: 摘要指令（作为 user 消息发出去）
SUMMARY_INSTRUCTION = """【系统指令】你现在只做一件事：把下面这段旧对话压缩成几句话的中文摘要。

要求：
1. 2~4 句，总共不超过 200 字；
2. 按「主人说了什么 / 桌宠做了什么 / 有什么约定或待办」来写；
3. 不要寒暄，不要逐条罗列，不要复述原文；
4. 主人长期的事实（偏好、身份、家人等）由长期记忆单独保管，
   这里只要保住**对话脉络**，不要编造没发生过的内容；
5. 直接输出摘要正文，不要标题、不要解释、不要客套。

旧对话：
{transcript}"""


def build_transcript(messages: Sequence[Any]) -> str:
    """把一段历史转成「谁说了什么」的纯文本（给摘要器看）。"""
    lines: list[str] = []
    for m in (messages or []):
        role = str(getattr(m, "role", "") or "")
        content = str(getattr(m, "content", "") or "")
        who = {"user": "主人", "assistant": "桌宠"}.get(role, role or "对话")
        lines.append(f"{who}：{content}")
    return "\n".join(lines)


async def summarise_history(client: Any, messages: Sequence[Any],
                            settings: Optional[CompactionSettings] = None) -> str:
    """把一段历史摘要成几句话。**永不抛异常**。

    失败（网络错误 / 超时 / 空结果 / 任何异常）一律返回空字符串 ——
    调用方据此退回「丢最旧」（今天的行为），聊天路径不受影响。
    """
    settings = settings or load_compaction_settings()
    transcript = build_transcript(messages)
    if not transcript.strip():
        return ""
    try:
        from app.brain.llm_client import ChatMessage

        req = [ChatMessage(
            role="user",
            content=SUMMARY_INSTRUCTION.format(transcript=transcript),
        )]
        raw = await asyncio.wait_for(
            client.chat_once(req),
            timeout=float(settings.summary_timeout_s),
        )
    except Exception as e:  # noqa: BLE001 - 摘要失败不许炸聊天路径
        log.warning("上下文压缩：摘要请求失败（%s）", e)
        return ""
    text = (raw or "").strip()
    if not text:
        log.warning("上下文压缩：摘要结果是空的")
        return ""
    limit = int(settings.summary_max_chars)
    if limit > 0 and len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


__all__ = [
    "CompactionSettings", "CompactionPlan", "SUMMARY_MARKER", "SUMMARY_PERSONA",
    "DEFAULT_THRESHOLD_RATIO", "DEFAULT_RETAIN_RATIO", "DEFAULT_KEEP_RECENT",
    "apply_compaction", "build_transcript", "history_chars", "load_compaction_settings",
    "message_chars", "plan_compaction", "reset_settings_cache", "summarise_history",
    "summary_entry_content",
]
