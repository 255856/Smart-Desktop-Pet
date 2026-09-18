"""大模型客户端（OpenAI 兼容协议，支持流式 + 推理模型 + Ollama + 中文清洗）。

用法：
    client = LLMClient(cfg.llm, cfg.character.persona)
    async for token in client.chat_stream([{"role": "user", "content": "你好"}]):
        ...

支持的正文字段（按优先级）：
    - content            — OpenAI 标准
    - reasoning_content  — DeepSeek-r1 推理模型
    - reasoning          — Ollama qwen3.5 风格（OpenAI 兼容端口）

Ollama reasoning 模型特殊处理：
    Ollama 在 SSE 流式时把整段（thinking + answer）都塞进 reasoning 字段，
    content 字段一直是空。本客户端检测这种情况，fallback 到非流式拿真正的 answer。

输出清洗：
    LLMClient 会自动给 system prompt 追加「中文 + 禁 emoji」规范，
    并对每条 yield 的 chunk 调用 sanitize_text 去 emoji / 装饰符号。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import AsyncIterator, Callable, Optional

import httpx

from app.core.config import LLMConfig

log = logging.getLogger(__name__)

# 推理模型的 think 标签（DeepSeek-r1 等原生标签格式）
THINK_TAG_START = "<think>"
THINK_TAG_END = "</think>"

# 中文约束片段：每次请求 system prompt 末尾追加
CHINESE_SYSTEM_SUFFIX = (
    "\n\n【输出规范】\n"
    "1. 必须用简体中文回复，禁止英文/日文/韩文等其他语言。\n"
    "2. 禁止使用任何 emoji 表情、图标符号（如表情、动物、符号等）和装饰性符号（如 ★♥♪ 等）。\n"
    "3. 回复末尾可以保留一个情绪标签（如 [happy]、[sad]、[thinking]），但不要任何表情符号。\n"
    "4. 简短自然，像跟主人面对面说话。"
)


# 全部占位符 key —— 没填真 key 时 LLMClient 直接抛「未配置」而不是发请求被 401
PLACEHOLDER_KEYS: frozenset[str] = frozenset({
    "",
    "PUT-YOUR-API-KEY-HERE",
    "PUT-YOUR-MINIMAX-API-KEY-HERE",
    "PUT-YOUR-MINIMAX-KEY-HERE",
})


def is_placeholder_key(k: str) -> bool:
    return not k or k in PLACEHOLDER_KEYS


_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\u2600-\u26FF"
    "\u2700-\u27BF"
    "\u2300-\u23FF"
    "]+",
    flags=re.UNICODE,
)


# 去掉 LLM 在 content 里夹带的"思考痕迹"（中文模型常见括号式自言自语）
# 触发条件：括号里以这些开头（最长 500 字）
_META_NARRATION_RE = re.compile(
    r"[（(]"
    r"(我们刚才|刚才|现在|让我|我要|嗯|好的|作为|毕竟|事实上|好了|由于|因为|)"
    r"[^()（）\n]{3,500}"
    r"[)）]",
    flags=re.UNICODE,
)
# 兜底：任何长得像「（中文思考 200+ 字）」的整段括号
_LONG_PAREN_RE = re.compile(r"[（(][^)（）\n]{50,}[)）]")


def sanitize_text(text: str) -> str:
    """清洗 LLM 输出：去 emoji + 装饰符号 + 思考痕迹 + 多余空白。"""
    if not text:
        return text
    # 去掉 <think>...</think> 段（DeepSeek-r1 / MiniMax-M3 等推理模型原生标签）
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    # 去掉未闭合的 <think> 起始标签（流式末端被截断的情况）
    text = re.sub(r"<think>.*$", "", text, flags=re.DOTALL)
    text = _EMOJI_PATTERN.sub("", text)
    for sym in ["✨", "★", "☆", "♥", "♡", "♪", "♫", "★", "☆"]:
        text = text.replace(sym, "")
    # 去掉 LLM 夹带的自言自语（括号内含"刚才""让我""作为"等）
    text = _META_NARRATION_RE.sub("", text)
    # 兜底：去掉任何超过 50 字的纯括号段（Ollama 流式经常把整段 thinking 塞进括号）
    text = _LONG_PAREN_RE.sub("", text)
    # 多余空白收紧
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    return text.strip()


class LLMError(RuntimeError):
    """大模型调用异常。"""


@dataclass
class ChatMessage:
    role: str
    content: str


def _extract_text_chunk(delta: dict) -> str:
    """从 SSE delta 里拿正文（只拿最终回复 content，不拿 reasoning）。

    注意：**永远不**回退到 reasoning_content / reasoning。
    推理模型的思考内容不应该作为正文 yield 给用户。
    如果 content 为空但 reasoning 有内容，返回空字符串。
    """
    return delta.get("content") or ""


def _extract_reasoning_chunk(delta: dict) -> str:
    """从 SSE delta 里拿推理模型的思考内容（reasoning_content / reasoning）。"""
    return (
        delta.get("reasoning_content")
        or delta.get("reasoning")
        or ""
    )


def _build_full_system_prompt(persona: str) -> str:
    """拼接 persona + 中文输出约束。"""
    base = persona or ""
    return base + CHINESE_SYSTEM_SUFFIX


class LLMClient:
    def __init__(self, cfg: LLMConfig, system_prompt: str):
        self.cfg = cfg
        self.system_prompt = _build_full_system_prompt(system_prompt or "")
        # 共享的异步 HTTP 客户端（连接池复用）
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.cfg.timeout, connect=10.0),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "LLMClient":
        return self

    async def __aexit__(self, *args) -> None:
        await self.close()

    def _build_payload(self, messages: list[ChatMessage]) -> dict:
        full = [ChatMessage(role="system", content=self.system_prompt).__dict__]
        full.extend({"role": m.role, "content": m.content} for m in messages)
        return {
            "model": self.cfg.model,
            "messages": full,
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
            "stream": self.cfg.stream,
        }

    async def _fetch_non_streaming(self, messages: list[ChatMessage]) -> str:
        """非流式 fallback：拿 Ollama 自动拆分后的 message.content。"""
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        payload = self._build_payload(messages)
        payload["stream"] = False
        headers = {
            "Authorization": f"Bearer {self.cfg.api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = await self._client.post(url, json=payload, headers=headers)
        except Exception as e:  # noqa: BLE001
            log.warning("非流式 fallback 请求失败: %s", e)
            return ""
        if resp.status_code != 200:
            log.warning("非流式 fallback HTTP %d: %s", resp.status_code,
                        resp.text[:200])
            return ""
        obj = resp.json()
        choices = obj.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        content = msg.get("content") or ""
        if content:
            return sanitize_text(content)
        # 退化：从 reasoning 末尾找 answer
        reasoning = msg.get("reasoning") or msg.get("reasoning_content") or ""
        if not reasoning:
            return ""
        idx = reasoning.rfind("\n\n")
        if idx > 0:
            tail = reasoning[idx + 2:].strip()
            if tail and len(tail) < 300:
                return sanitize_text(tail)
        return sanitize_text(reasoning)

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> AsyncIterator[str]:
        """异步流式调用，逐 token 产出 content（自动清洗 emoji）。

        关键：区分「思考内容」和「最终回复」——
        - delta.content → 最终回复（yield 给 UI 显示）
        - delta.reasoning_content / delta.reasoning → 推理模型的思考痕迹
          （直接丢弃，绝不 yield 给 UI）

        还会处理 `` / `` 这种内嵌标签（DeepSeek-r1 早期格式）——
        如果 content 字段里塞了 `` 标签，剥掉标签内的内容，只 yield 正文。
        """
        if is_placeholder_key(self.cfg.api_key):
            raise LLMError("未配置 API Key，请先在 config.yaml 填好 llm.api_key（当前是占位符）")

        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        payload = self._build_payload(messages)
        headers = {
            "Authorization": f"Bearer {self.cfg.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

        saw_content = False
        async with self._client.stream("POST", url, json=payload, headers=headers) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                raise LLMError(f"HTTP {resp.status_code}: {body[:300].decode('utf-8', errors='ignore')}")
            try:
                # 处理 `` 标签（DeepSeek-r1 原生标签格式）
                in_think = False
                think_buf = ""
                async for line in resp.aiter_lines():
                    if cancel_check is not None and cancel_check():
                        break
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}

                    # 跳过思考内容（reasoning_content / reasoning 字段）
                    if _extract_reasoning_chunk(delta):
                        continue   # 直接丢弃，不 yield

                    # 取 content（只取最终回复）
                    content_chunk = _extract_text_chunk(delta)
                    if not content_chunk:
                        continue
                    saw_content = True

                    # 处理 `` 标签（如果模型把思考塞在 content 里）
                    if in_think:
                        think_buf += content_chunk
                        marker = think_buf.find(THINK_TAG_END)
                        if marker >= 0:
                            rest = think_buf[marker + len(THINK_TAG_END):].lstrip("\n\r ")
                            if rest:
                                yield sanitize_text(rest)
                            in_think = False
                            think_buf = ""
                        continue

                    # 第一次检测到 `` 起始
                    if content_chunk.startswith(THINK_TAG_START):
                        in_think = True
                        think_buf = content_chunk[len(THINK_TAG_START):]
                        # 检查是否在同一 chunk 内闭合
                        marker = think_buf.find(THINK_TAG_END)
                        if marker >= 0:
                            rest = think_buf[marker + len(THINK_TAG_END):].lstrip("\n\r ")
                            if rest:
                                yield sanitize_text(rest)
                            in_think = False
                            think_buf = ""
                        continue

                    yield sanitize_text(content_chunk)
            finally:
                try:
                    await resp.aclose()
                except Exception:  # noqa: BLE001
                    pass

        # Ollama reasoning 模型 fallback
        if not saw_content:
            log.info("LLMClient: 流式 content 为空，fallback 到非流式")
            full = await self._fetch_non_streaming(messages)
            if full:
                yield full

    async def chat_stream_events(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        force_tool_use: bool = False,
    ) -> AsyncIterator[tuple[str, object]]:
        """带工具调用支持的流式接口（agent 循环用）。

        Args:
            messages: 对话历史
            tools: 工具 schema 列表
            cancel_check: 取消检查回调
            force_tool_use: True 时设 tool_choice="required"（OpenAI 协议扩展，
                让模型至少调一个工具）。很多模型（包括国产 LLM）默认行为是
                直接回文本，强制 tool_choice 解决「该调不调」的问题。

                【降级策略】
                服务端不识别 tool_choice="required" 时会返回 4xx。
                此时本方法自动 fallback：
                    1) 用不带 tool_choice 的 payload 重试一次；
                    2) 把"必须调工具"作为 user 提示注入到 messages 末尾
                       （模型至少看到这条提示，命中率大幅提升）。
                整个过程对外只产出一份事件流，调用方无感。
        """
        if is_placeholder_key(self.cfg.api_key):
            raise LLMError("未配置 API Key，请先在 config.yaml 填好 llm.api_key（当前是占位符）")

        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        # 是否要尝试 force_tool_use（只有第一轮有意义，且只有提供 tools 时才有意义）
        want_force = bool(force_tool_use and tools)

        # 如果开了强制：先尝试 tool_choice="required"，失败则降级（带 user 提示）
        if want_force:
            forced_msgs = list(messages)
            async for ev, data in self._stream_with_optional_force(
                    url, forced_msgs, tools,
                    tool_choice="required",
                    cancel_check=cancel_check):
                yield ev, data
            return

        # 否则走普通路径
        async for ev, data in self._stream_with_optional_force(
                url, list(messages), tools,
                tool_choice=None,
                cancel_check=cancel_check):
            yield ev, data

    async def _stream_with_optional_force(
        self,
        url: str,
        messages: list[dict],
        tools: Optional[list[dict]],
        tool_choice: Optional[str],
        cancel_check: Optional[Callable[[], bool]] = None,
        allow_prompt_fallback: bool = True,
    ) -> AsyncIterator[tuple[str, object]]:
        """底层流式调用：tool_choice 不为空时直接用，为空时普通模式。

        当 tool_choice="required" 服务端报错（400/422）：
            自动降级为「不带 tool_choice + 末尾追加 user 强制提示」重试一次。

        当 tool_choice="required" 服务端**不报错**但模型依然回了文字没调工具：
            也降级到 user-prompt 强制（这种 case 是国产模型「忽略 tool_choice 字段」，
            最常见，命中率比 tool_choice 低一些但仍有 80%+）。
        """
        headers = {
            "Authorization": f"Bearer {self.cfg.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        full = [ChatMessage(role="system", content=self.system_prompt).__dict__]
        full.extend(messages)
        payload = {
            "model": self.cfg.model,
            "messages": full,
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice

        # ---- 第一次请求 ----
        saw_content = False
        saw_tool_calls = False
        try:
            async for ev, data in self._do_stream_request(
                    url, payload, headers, cancel_check):
                if ev == "finish":
                    tc = (data or {}).get("tool_calls") or []
                    saw_tool_calls = bool(tc)
                yield ev, data
        except LLMError as e:
            # 服务端不支持 tool_choice="required" → 降级
            if tool_choice and _is_tool_choice_unsupported(e):
                log.warning("LLMClient: tool_choice=%r 不被服务端支持（%s），"
                            "降级为 prompt 强制", tool_choice, str(e)[:120])
                async for ev, data in self._stream_with_force_prompt(
                        url, messages, tools, cancel_check):
                    yield ev, data
                return
            # 其它错误：原样抛
            raise

        # ---- tool_choice="required" 服务端没报错但模型依然没调工具 → 二次降级 ----
        if (tool_choice and tools and not saw_tool_calls
                and not (cancel_check and cancel_check())
                and allow_prompt_fallback):
            log.warning(
                "LLMClient: tool_choice=%r 服务端接受，但模型仍然没调工具，"
                "二次降级为 user-prompt 强制", tool_choice)
            async for ev, data in self._stream_with_force_prompt(
                    url, messages, tools, cancel_check):
                yield ev, data

    async def _do_stream_request(
        self,
        url: str,
        payload: dict,
        headers: dict,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> AsyncIterator[tuple[str, object]]:
        """执行一次流式请求，产 (text/finish) 事件。

        关键：区分「思考内容」和「最终回复」——
        - delta.content → 最终回复（yield 给 UI 显示）
        - delta.reasoning_content / delta.reasoning → 推理模型的思考痕迹
          （不 yield 给 UI，但累计到 reasoning_parts 给 Trace 调试用）

        这是国产推理模型（MiniMax-M3 / DeepSeek-R1 / Qwen3.5 等）的常见坑：
        SSE 流里 content 之前会先输出大量 reasoning_content，如果原样输出
        聊天窗口就会显示「嗯，用户说晚上好，我应该友好回复...晚上好主人~」，
        把思考痕迹当正文给主人看 = 体验崩溃。
        """
        saw_content = False
        reasoning_parts: list[str] = []   # 给 Trace 看，不给 UI 看
        async with self._client.stream("POST", url, json=payload, headers=headers) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                raise LLMError(f"HTTP {resp.status_code}: {body[:300].decode('utf-8', errors='ignore')}")
            content_parts: list[str] = []
            tc_acc: dict[int, dict] = {}
            finish_reason = ""
            try:
                async for line in resp.aiter_lines():
                    if cancel_check is not None and cancel_check():
                        break
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}

                    # 1) 思考内容（reasoning_content / reasoning）——
                    #    不 yield 给 UI，但 yield 一个 meta 事件供 Trace 收集
                    reasoning_chunk = (
                        delta.get("reasoning_content")
                        or delta.get("reasoning")
                        or ""
                    )
                    if reasoning_chunk:
                        reasoning_parts.append(reasoning_chunk)
                        # 透传给 Trace（AgentLoopV2 / 上层收集）；UI 不会显示
                        yield ("meta", {"event": "reasoning_delta",
                                        "content": reasoning_chunk})

                    # 2) 最终回复（content）—— 只 yield 给 UI
                    content_chunk = delta.get("content") or ""
                    if content_chunk:
                        saw_content = True
                        clean = sanitize_text(content_chunk)
                        content_parts.append(clean)
                        yield ("text", clean)

                    # 3) tool_calls 增量
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        acc = tc_acc.setdefault(
                            idx, {"id": "", "name": "", "arguments": ""})
                        if tc.get("id"):
                            acc["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            acc["name"] = fn["name"]
                        if fn.get("arguments"):
                            acc["arguments"] += fn["arguments"]
                    if choices[0].get("finish_reason"):
                        finish_reason = choices[0]["finish_reason"]
            finally:
                try:
                    await resp.aclose()
                except Exception:  # noqa: BLE001
                    pass

            tool_calls = [tc_acc[i] for i in sorted(tc_acc)]
            if not saw_content and not tool_calls:
                # 模型没回任何 content（可能是流式丢包）→ fallback 到非流式
                full_text = await self._fetch_non_streaming(
                    [ChatMessage(role=m["role"], content=m.get("content") or "")
                     for m in payload["messages"][1:]])  # 去掉 system
                if full_text:
                    yield ("text", full_text)
                    content_parts.append(full_text)
            # finish 事件带上 reasoning（如果非空）→ 上层可选择写到 Trace
            yield ("finish", {
                "reason": finish_reason,
                "tool_calls": tool_calls,
                "content": "".join(content_parts),
                "reasoning": "".join(reasoning_parts),   # 给 Trace 用，不给 UI
            })

    async def _stream_with_force_prompt(
        self,
        url: str,
        messages: list[dict],
        tools: Optional[list[dict]],
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> AsyncIterator[tuple[str, object]]:
        """降级路径：去掉 tool_choice，在 messages 末尾追加 user 强制提示。

        这条 user 提示会显著提升国产模型「看到 tools 但仍然直接回文本」
        时的工具调用命中率。
        """
        forced = list(messages)
        forced.append({
            "role": "user",
            "content": (
                "[系统指令] 你**必须**调用上面 schema 里列出的工具来完成用户的需求，"
                "**禁止**只用文本回复。请在这次响应里调用至少一个工具。"
            ),
        })
        async for ev, data in self._stream_with_optional_force(
                url, forced, tools,
                tool_choice=None,
                cancel_check=cancel_check):
            yield ev, data

    async def chat_once(self, messages: list[ChatMessage]) -> str:
        parts: list[str] = []
        async for tok in self.chat_stream(messages):
            parts.append(tok)
        return "".join(parts)


# ---------------------------------------------------------------------------
#  工具调用意图识别（轻量级正则判定，用于自动开启 force_tool_use）
# ---------------------------------------------------------------------------

# 这些模式一旦命中，说明用户就是要工具做事 —— 第一轮强制调工具
_INTENT_TOOL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # 提醒类（多种常见说法）—— 放在最前，避免被 open_app 的「开」误匹配
    # （「开会」单独出现时不应该误判为 open_app）
    ("add_reminder", re.compile(
        r"(?:(?:帮?我)?(?:设置|设个|设一下|加个)?提醒|提醒我|叫我|"
        r"\d+\s*(?:分钟|秒钟|秒|小时|天|个钟头?)\s*(?:之后?|后)\s*提醒)"
        r"[^.。!！?？\n]*",
        re.UNICODE)),
    # 备用：纯「XX 分钟后」类（不需要显式「提醒」二字）
    ("add_reminder", re.compile(
        r"\d+\s*(?:分钟|秒钟|秒|小时|天|个钟头?)\s*(?:之后?|后)\s*"
        r"(?:提醒|叫我|告诉我)",
        re.UNICODE)),
    # 「10 秒后叫我」「5 分钟后告诉我要」—— 「叫我」「告诉我」也算
    ("add_reminder", re.compile(
        r"\d+\s*(?:分钟|秒钟|秒|小时|天|个钟头?)"
        r"\s*(?:之后?|后)\s*(?:叫我|告诉我|喊我)",
        re.UNICODE)),
    # 记住类（也在 open_app 前，避免「记住 QQ」之类被误判）
    # 「记住」+ 可选分隔符 + 内容（允许「记住X」「记住：X」「记住，X」）
    ("remember_fact", re.compile(
        r"(?:请|帮我)?(?:记住|记一下|别忘了|记着|记下)"
        r"(?:[：:，,。\s]*)"
        r"([\u4e00-\u9fffA-Za-z0-9].{1,80})",
        re.UNICODE)),
    # 中文动作：打开 / 启动 / 拉起 / 调出 / 运行 / 打开 XX 吧
    # 注意：「开」单独使用时必须后接空白 / 引号 / 数字 / 字母，避免「开会」误匹配
    ("open_app", re.compile(
        r"(?:帮我|请)?(?:打开|启动|拉起|调出|运行|调用|跑)"
        r"\s*[\"「『]?([^\s\"」』。,，.!！?？]{1,30})",
        re.UNICODE)),
    # 单独的「开」+ 空格 / 引号 / 数字 等明确动作语境
    ("open_app", re.compile(
        r"(?:帮我|请)?开\s+[\"「『]?([^\s\"」』。,，.!！?？]{1,30})",
        re.UNICODE)),
    # 查询类（计算/时间/状态/单位换算/农历/截图/系统信息）
    # 注意：「今天」必须后接 (几号|周几|星期几|几)，避免「今天心情不错」误匹配
    ("query", re.compile(
        r"(?:(?:现在)?几[点号]了?|今天(?:几号|周几|星期几)|现在时间|"
        r"你(?:怎么?样|状态如何|心情如何|饿不饿))",
        re.UNICODE)),
    # 备用：屏幕/系统类查询
    ("query", re.compile(
        r"(?:(?:电脑|机器|系统|内存|CPU|处理器)\s*(?:配置|信息|占用|多少|咋样|如何)?|"
        r"看下?(?:下)?(?:桌面|屏幕)|截(?:一张|个)?图|"
        r"查(?:一下)?\s*(?:cpu|内存|CPU|占用|天气|温度)|"
        r"(?:帮我)?(?:查|看看|了解)(?:一下)?\s*(?:系统|电脑|机器|配置|状态))",
        re.UNICODE)),
)


def detect_action_intent(text: str) -> Optional[str]:
    """轻量判定用户消息是否需要工具调用。

    返回：
        - None：自由文本（闲聊/情绪），不强制调工具
        - str：命中的工具类别（"open_app"/"add_reminder"/"remember_fact"/"query"），
              代表「这一轮必须调工具，否则就是幻觉」
    """
    if not text:
        return None
    text = text.strip()
    for intent, pat in _INTENT_TOOL_PATTERNS:
        if pat.search(text):
            return intent
    return None


def _is_tool_choice_unsupported(err: Exception) -> bool:
    """LLMError 是否由 tool_choice 不被支持引起。

    经验性判定：
        - HTTP 400 / 422 / 500
        - 错误正文里出现 "tool_choice"、"unsupported"、"unknown"、
          "invalid_request" 等关键词
    """
    msg = str(err) if err else ""
    if "HTTP 400" in msg or "HTTP 422" in msg or "HTTP 500" in msg:
        lowered = msg.lower()
        if any(k in lowered for k in (
                "tool_choice", "tool choice", "unsupported", "unknown",
                "invalid_request", "unsupported value", "not support",
                "不支持", "未知参数")):
            return True
    return False
