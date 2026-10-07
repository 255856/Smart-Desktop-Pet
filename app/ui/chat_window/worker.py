"""chat_window 子模块：QThread worker。

把 async 流式调用包装到 QThread 里跑，避免阻塞主线程 / 处理 Python 事件。

_AgentWorker：包装 ReAct / LangChain agent 循环，吐 (text/tool/done/meta) 事件。
_StreamWorker：包装纯流式 LLM 调用（无工具），吐 (text/done/failed) 事件。
"""
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


# httpx 抛出的网络异常族：连接断 / 超时 / DNS 失败 / 远程协议错。
# 收到这些时只把 USER_FRIENDLY_NETWORK_ERROR 暴露给主人，原始异常进 log。
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
    """worker 抛异常时，给主人看的文案。

    网络异常统一改成 USER_FRIENDLY_NETWORK_ERROR，原始异常打 log。
    其他异常保留 repr（LLMError 的 message 一般已友好；`{e!r}` 是最后兜底）。
    """
    if isinstance(exc, _NETWORK_EXC):
        log.warning("worker 网络异常转友好文案：%r", exc)
        return USER_FRIENDLY_NETWORK_ERROR
    return repr(exc)


class _AgentWorker(QThread):
    """Agent 循环 worker：流式正文 + 工具调用（Function Calling）。

    agent.run 产出 ("text"|"tool"|"done"|"meta"|"reflection"|"plan", ...) 事件，
    这里转成 Qt 信号：
        chunk     —— 正文增量
        tool_used —— 一次工具执行完成 (name, args, result)
        meta      —— 内部事件（force_retry / replan 等），仅 trace + 状态显示
        done      —— 完整文本（所有正文段拼接）
        failed    —— 出错
    """

    chunk = Signal(str)
    tool_used = Signal(str, str, str)
    meta = Signal(str, dict)       # (kind, payload) —— 内部事件，给 Trace 用
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
        """在 QThread 里跑 agent 循环。

        LangChainAgent 自带持久 loop（run_sync）；其他 agent 仍走 asyncio.run()。

        修复：把 `done` 事件的 payload 也作为 final 文本的兜底（防御性）。
        历史上某些 AgentLoop 变体（如 AgentLoopV2 的 Planner+Executor 模式）
        不一定 yield text 事件就 yield done，导致 `chat reply ready: ''`。

        处理推理模型：text 事件里**不再**含 reasoning_content / reasoning 字段
        （由 LLMClient._do_stream_request 区分）。reasoning 通过 meta 事件
        {"event": "reasoning", "content": "..."} 转发给 Trace，UI 不显示。
        """
        try:
            full: list[str] = []
            done_fallback: str = ""   # 防御：done 事件 payload 作为 last-resort
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
                    """驱动 agent.run 异步迭代。

                    done_box: 用于回传 done 兜底文本（防御性）
                    reasoning_box: 用于回传推理模型思考内容（Trace 用，不给 UI）
                    """
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
                            # 特殊：reasoning meta 累积到 reasoning_box（一次性 trace）
                            if isinstance(meta_payload, dict) and \
                                    meta_payload.get("event") == "reasoning_delta":
                                reasoning_box.append(meta_payload.get("content", ""))
                                # 不 emit 给 UI（meta 仍然 emit 给 Trace 记录）
                            self.meta.emit("meta", meta_payload)
                        elif kind in ("plan", "reflection"):
                            self.meta.emit(kind, ev[1] if len(ev) > 1 else {})
                        elif kind == "done":
                            # 防御：记录 done payload 作为兜底文本
                            if len(ev) > 1 and ev[1]:
                                done_box.append(str(ev[1]))

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
            # 拼接最终文本：如果累积的 full 为空且 done 有 payload，用 done 的
            final_text = "".join(full) or done_fallback
            self.done.emit(final_text)
        except LLMError as e:
            self.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001
            self.failed.emit(_failure_message(e))


class _StreamWorker(QThread):
    """把 async 流式调用包装到 QThread 里跑。

    PR1 修复：
        - 新增 cancel_event（threading.Event），「停止」按钮只需 set()，无需走 Qt 中断
          协议 —— httpx stream 端每行会检查 cancel_check()，触发后立即断开连接。
        - run() 内现在 asyncio.set_event_loop(loop) 先于 run_until_complete，
          避免「loop 还没在主线程注册」导致的潜在 ResourceWarning。
    """
    chunk = Signal(str)
    done = Signal(str)             # 完整文本
    failed = Signal(str)

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
                async for tok in self.client.chat_stream(
                    self.messages,
                    cancel_check=self.cancel_event.is_set,
                ):
                    full.append(tok)
                    self.chunk.emit(tok)
                    if self.cancel_event.is_set():
                        # 已收到部分 token；断开 stream 跳出循环
                        break

            loop.run_until_complete(drive())
            loop.run_until_complete(asyncio.sleep(0))   # 给 aclose 一个机会落地
            loop.close()
            self.done.emit("".join(full))
        except LLMError as e:
            self.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001
            self.failed.emit(_failure_message(e))


__all__ = ["_AgentWorker", "_StreamWorker"]