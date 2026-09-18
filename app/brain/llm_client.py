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
    """从 SSE delta 里拿正文（兼容三种字段）。"""
    return (
        delta.get("content")
        or delta.get("reasoning_content")
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
        """异步流式调用，逐 token 产出 content（自动清洗 emoji）。"""
        if not self.cfg.api_key or self.cfg.api_key == "PUT-YOUR-API-KEY-HERE":
            raise LLMError("未配置 API Key，请先在 config.yaml 填好 llm.api_key")

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
                buffer = ""
                thinking_seen = None
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
                    if delta.get("content"):
                        saw_content = True
                    chunk = _extract_text_chunk(delta)
                    if not chunk:
                        continue
                    is_reasoning = bool(
                        delta.get("reasoning") or delta.get("reasoning_content"))

                    if thinking_seen is None:
                        if chunk.startswith(THINK_TAG_START):
                            thinking_seen = True
                            chunk = chunk[len(THINK_TAG_START):]
                            buffer = chunk
                            continue
                        thinking_seen = False
                        if not is_reasoning:
                            yield sanitize_text(chunk)
                        continue

                    if thinking_seen:
                        buffer += chunk
                        marker = buffer.find(THINK_TAG_END)
                        if marker >= 0:
                            rest = buffer[marker + len(THINK_TAG_END):].lstrip("\n\r ")
                            if rest:
                                yield sanitize_text(rest)
                            thinking_seen = False
                            buffer = ""
                        continue

                    if not is_reasoning:
                        yield sanitize_text(chunk)
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
    ) -> AsyncIterator[tuple[str, object]]:
        """带工具调用支持的流式接口（agent 循环用）。"""
        if not self.cfg.api_key or self.cfg.api_key == "PUT-YOUR-API-KEY-HERE":
            raise LLMError("未配置 API Key，请先在 config.yaml 填好 llm.api_key")

        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
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
                    if delta.get("content"):
                        saw_content = True
                    chunk = (
                        delta.get("content")
                        or delta.get("reasoning_content")
                        or delta.get("reasoning")
                        or ""
                    )
                    if chunk:
                        clean = sanitize_text(chunk)
                        content_parts.append(clean)
                        yield ("text", clean)
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
                full_text = await self._fetch_non_streaming(
                    [ChatMessage(role=m["role"], content=m.get("content") or "")
                     for m in messages])
                if full_text:
                    yield ("text", full_text)
                    content_parts.append(full_text)
            yield ("finish", {"reason": finish_reason,
                              "tool_calls": tool_calls,
                              "content": "".join(content_parts)})

    async def chat_once(self, messages: list[ChatMessage]) -> str:
        parts: list[str] = []
        async for tok in self.chat_stream(messages):
            parts.append(tok)
        return "".join(parts)
