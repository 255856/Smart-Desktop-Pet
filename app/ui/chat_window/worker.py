"""chat_window QThread worker：把 async 流式调用包装到 QThread 里跑。"""
from __future__ import annotations

import asyncio
import logging
import threading

import httpx

from app.core.qt_compat import QThread, Signal
from app.core.errors import USER_FRIENDLY_NETWORK_ERROR
from app.brain.llm_client import ChatMessage, LLMClient, LLMError
from app.brain.agent import AgentLoop

log = logging.getLogger(__name__)


# httpx 网络异常族（连接断 / 超时 / DNS 失败 / 远程协议错）
_NETWORK_EXC = (
    httpx.RemoteProtocolError,
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.NetworkError,
    ConnectionError,
)


def _failure_message(exc: BaseException) -> str:
    """worker 抛异常时给主人看的文案（网络异常统一转友好文案）。"""
    if isinstance(exc, _NETWORK_EXC):
        log.warning("worker 网络异常转友好文案：%r", exc)
        return USER_FRIENDLY_NETWORK_ERROR
    return repr(exc)


async def _close_llm_client(client) -> None:
    """本轮结束后关掉 LLMClient 的 httpx 连接池。

    每轮 asyncio.run() 都是一个新 event loop：loop 关掉后，连接池里那些
    绑在旧 loop 上的连接就废了。不显式 close() 的话，每轮都会泄漏一个
    连接 + 一个 fd（LLMClient 之前全仓库没有任何 close() 调用点）。
    """
    close = getattr(client, "close", None)
    if close is None:
        return
    try:
        await close()
    except Exception:  # noqa: BLE001
        pass


class _AgentWorker(QThread):
    """Agent 循环 worker：流式正文 + 工具调用（Function Calling）。"""

    chunk = Signal(str)
    tool_used = Signal(str, str, str)
    meta = Signal(str, dict)       # (kind, payload) — 内部事件，给 Trace 用
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, agent: AgentLoop, messages: list[dict]):
        super().__init__()
        self.agent = agent
        self.messages = messages
        self.cancel_event = threading.Event()

    def request_stop(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        """在 QThread 里跑 agent 循环。"""
        try:
            full: list[str] = []
            done_fallback: str = ""   # done 事件 payload 作为 last-resort 兜底
            reasoning_parts: list[str] = []   # 收集推理模型的思考内容（Trace 用）
            # LangChainAgent 提供 run_sync（在持久 loop 里跑，跨调用不切换）
            if hasattr(self.agent, "run_sync"):
                events = self.agent.run_sync(
                    self.messages, cancel_check=self.cancel_event.is_set)
                for kind, *payload in events:
                    if kind == "text":
                        full.append(payload[0])
                        self.chunk.emit(payload[0])
                    elif kind == "tool":
                        # payload = (name, args, result)
                        self.tool_used.emit(*payload)
                    elif kind == "meta":
                        # payload = (dict)
                        self.meta.emit("meta", payload[0] if payload else {})
                    elif kind in ("plan", "reflection", "reasoning"):
                        # 透传到 Trace + UI 折叠「思考过程」块
                        payload_dict = payload[0] if payload else {}
                        if not isinstance(payload_dict, dict):
                            payload_dict = {"text": str(payload_dict)}
                        text = payload_dict.get("text") or payload_dict.get("content", "")
                        self.meta.emit("meta", {"kind": "thought", "text": str(text)})
                    elif kind == "done":
                        # 防御：如果之前没收到任何 text，把 done payload 当兜底
                        if payload and payload[0]:
                            done_fallback = str(payload[0])
            else:
                async def drive(done_box: list[str],
                                reasoning_box: list[str]) -> None:
                    """驱动 agent.run 异步迭代。"""
                    try:
                        async for ev in self.agent.run(
                                self.messages, cancel_check=self.cancel_event.is_set):
                            kind = ev[0]
                            if kind == "text":
                                full.append(ev[1])
                                self.chunk.emit(ev[1])
                            elif kind == "tool":
                                self.tool_used.emit(ev[1], ev[2], ev[3])
                            elif kind == "meta":
                                # ev = ("meta", dict)
                                meta_payload = ev[1] if len(ev) > 1 else {}
                                # reasoning meta 累积到 reasoning_box（一次性 trace）
                                if isinstance(meta_payload, dict) and \
                                        meta_payload.get("event") == "reasoning_delta":
                                    reasoning_box.append(meta_payload.get("content", ""))
                                self.meta.emit("meta", meta_payload)
                            elif kind in ("plan", "reflection"):
                                self.meta.emit(kind, ev[1] if len(ev) > 1 else {})
                            elif kind == "done":
                                # 防御：记录 done payload 作为兜底文本
                                if len(ev) > 1 and ev[1]:
                                    done_box.append(str(ev[1]))
                    finally:
                        # 本轮结束：关掉 LLMClient 的连接池（每轮都漏一个 fd）
                        await _close_llm_client(getattr(self.agent, "client", None))

                done_box: list[str] = []
                reasoning_box: list[str] = []
                asyncio.run(drive(done_box, reasoning_box))
                if done_box:
                    done_fallback = done_box[-1]
                # 一次性把推理内容透传给 Trace（不发给 UI）
                if reasoning_box:
                    self.meta.emit("meta", {
                        "event": "reasoning",
                        "content": "".join(reasoning_box),
                    })
            # 拼接最终文本：full 为空时用 done 的 payload 兜底
            final_text = "".join(full) or done_fallback
            self.done.emit(final_text)
        except LLMError as e:
            self.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001
            self.failed.emit(_failure_message(e))


class _StreamWorker(QThread):
    """把 async 流式调用包装到 QThread 里跑。"""
    chunk = Signal(str)
    done = Signal(str)             # 完整文本
    failed = Signal(str)
    meta = Signal(str, dict)       # (kind, payload) — 内部事件（usage 等）

    def __init__(self, client: LLMClient, messages: list[ChatMessage]):
        super().__init__()
        self.client = client
        self.messages = messages
        # threading.Event 跨线程访问安全；UI 线程调 .set() 即可让 stream 端退出
        self.cancel_event = threading.Event()

    def request_stop(self) -> None:
        """「停止」按钮调用这里即可。stream 端会在下一个 chunk 前断开。"""
        self.cancel_event.set()

    def run(self) -> None:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            full: list[str] = []

            async def drive() -> None:
                try:
                    async for tok in self.client.chat_stream(
                        self.messages,
                        cancel_check=self.cancel_event.is_set,
                    ):
                        full.append(tok)
                        self.chunk.emit(tok)
                        # v31: usage 事件（UsageEvent 是「空文本的 str 子类」，
                        # 老消费方只当空串 → UI 一个字都不变）。这里把它单独
                        # 摘出来走 meta 通道, 让状态栏和 Trace 也能拿到真实
                        # token 数 —— 以前无工具的纯聊天路径 usage 整个丢了。
                        payload = getattr(tok, "payload", None)
                        if isinstance(payload, dict) \
                                and payload.get("event") == "usage":
                            self.meta.emit("meta", dict(payload))
                        if self.cancel_event.is_set():
                            # 已收到部分 token；断开 stream 跳出循环
                            break
                finally:
                    # 本轮结束：关掉 LLMClient 的连接池（每轮都漏一个 fd）
                    await _close_llm_client(self.client)

            loop.run_until_complete(drive())
            loop.run_until_complete(asyncio.sleep(0))   # 给 aclose 一个机会落地
            loop.close()
            self.done.emit("".join(full))
        except LLMError as e:
            self.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001
            self.failed.emit(_failure_message(e))


class _PlanWorker(QThread):
    """Plan-mode worker：调用 LangChainAgent.plan_sync()，只产 markdown 计划。

    复用 _AgentWorker 的 chunk / done / failed 信号协议。
    不挂 tool_used / meta — plan 不调工具。
    """

    chunk = Signal(str)
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, agent, messages: list[dict]):
        super().__init__()
        self.agent = agent
        self.messages = messages
        self.cancel_event = threading.Event()

    def request_stop(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        full: list[str] = []
        done_fallback: str = ""
        try:
            # plan_sync 在持久 loop 里跑 plan();每个事件 (text|done|error, payload)
            plan_events = self.agent.plan_sync(
                self.messages, cancel_check=self.cancel_event.is_set,
            ) if hasattr(self.agent, "plan_sync") else None
            if plan_events is None:
                self.failed.emit(
                    "当前 Agent 后端不支持 plan 模式（仅 standard / LangChain 后端可用）"
                )
                return
            for ev in plan_events:
                kind = ev[0]
                if kind == "text":
                    full.append(ev[1])
                    self.chunk.emit(ev[1])
                elif kind == "done":
                    # 防御：plan 没 yield text 但给了 done payload，用 payload 兜底
                    if len(ev) > 1 and ev[1]:
                        done_fallback = str(ev[1])
                elif kind == "error":
                    self.failed.emit(str(ev[1]) if len(ev) > 1 else "plan 失败")
                    return
            final_text = "".join(full) or done_fallback
            self.done.emit(final_text)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(_failure_message(e))


__all__ = ["_AgentWorker", "_StreamWorker", "_PlanWorker"]