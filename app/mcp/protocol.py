"""MCP（Model Context Protocol）stdio JSON-RPC 客户端。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


#  MCP 协议常量

PROTOCOL_VERSION = "2024-11-05"
CLIENT_NAME = "desktop-pet"
CLIENT_VERSION = "3.0.0"


@dataclass
class MCPServerConfig:
    """单个 MCP server 的启动配置。"""
    name: str
    command: str                       # 可执行文件路径
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: Optional[str] = None
    description: str = ""
    enabled: bool = True


@dataclass
class MCPTool:
    """从 MCP server 拿到的工具定义。"""
    name: str
    description: str
    parameters: dict                  # JSON Schema
    server: str                       # 来自哪个 server
    dangerous: bool = False


#  stdio JSON-RPC 客户端


class MCPStdioClient:
    """一个 stdio JSON-RPC 客户端，连接一个 MCP server 子进程。"""

    def __init__(self, config: MCPServerConfig):
        self.config = config
        self.proc: Optional[subprocess.Popen] = None
        self._next_id = 1
        self._lock = asyncio.Lock()
        self._initialized = False
        self._tools: list[dict] = []

    async def start(self) -> None:
        """启动子进程。"""
        if self.proc is not None and self.proc.poll() is None:
            return
        env = os.environ.copy()
        env.update(self.config.env)
        try:
            self.proc = subprocess.Popen(
                [self.config.command, *self.config.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=self.config.cwd,
                text=True,
                # MCP stdio 传输规定为 UTF-8。text=True 只默认取
                # locale.getpreferredencoding()，Windows CI runner 上是 cp1252
                # （charmap），子进程写中文 JSON 会 UnicodeEncodeError，
                # 表现为 tools/list 拿不到任何工具。
                encoding="utf-8",
                errors="replace",
                bufsize=1,           # 行缓冲
            )
        except FileNotFoundError as e:
            raise RuntimeError(f"MCP server {self.config.name} 启动失败：{e}")
        log.info("MCP server '%s' 已启动 (pid=%d)",
                 self.config.name, self.proc.pid)
        await self._initialize()

    async def stop(self) -> None:
        if self.proc is None:
            return
        try:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        except Exception:  # noqa: BLE001
            pass
        self.proc = None
        self._initialized = False
        log.info("MCP server '%s' 已停止", self.config.name)

    async def _send(self, method: str, params: dict | None = None,
                    timeout: float = 30.0) -> dict:
        """发送 JSON-RPC 请求，等待响应。"""
        if self.proc is None:
            raise RuntimeError(f"MCP {self.config.name} 未启动")
        async with self._lock:
            req_id = self._next_id
            self._next_id += 1
            msg = {"jsonrpc": "2.0", "id": req_id, "method": method}
            if params is not None:
                msg["params"] = params
            line = json.dumps(msg, ensure_ascii=False)
            try:
                self.proc.stdin.write(line + "\n")
                self.proc.stdin.flush()
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(f"MCP {self.config.name} 写失败：{e}")
            # 读响应
            return await asyncio.to_thread(self._read_response,
                                            req_id, timeout)

    def _read_response(self, req_id: int, timeout: float) -> dict:
        """同步读一行响应（必须在 to_thread 里跑）。"""
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                if self.proc.poll() is not None:
                    raise RuntimeError(
                        f"MCP {self.config.name} 进程退出（rc={self.proc.returncode}）")
                time.sleep(0.01)
                continue
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == req_id:
                if "error" in msg:
                    err = msg["error"]
                    raise RuntimeError(
                        f"MCP {self.config.name} 错误：{err.get('message')} "
                        f"(code={err.get('code')})")
                return msg.get("result", {})
            # 否则可能是 notification（忽略）
        raise TimeoutError(f"MCP {self.config.name} 请求超时")

    async def _initialize(self) -> None:
        """握手。"""
        result = await self._send("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
        })
        log.info("MCP %s initialized: %s", self.config.name,
                 result.get("serverInfo", {}).get("name", "?"))
        # 通知 initialized
        try:
            self.proc.stdin.write(json.dumps({
                "jsonrpc": "2.0", "method": "notifications/initialized"
            }) + "\n")
            self.proc.stdin.flush()
        except Exception:  # noqa: BLE001
            pass
        self._initialized = True

    async def list_tools(self) -> list[dict]:
        """获取 server 注册的工具列表。"""
        if not self._initialized:
            return []
        result = await self._send("tools/list", {})
        tools = result.get("tools", [])
        self._tools = tools
        return tools

    async def call_tool(self, name: str, arguments: dict,
                        timeout: float = 30.0) -> str:
        """调用一个 MCP 工具，返回文本结果。"""
        if not self._initialized:
            raise RuntimeError(f"MCP {self.config.name} 未初始化")
        result = await self._send(
            "tools/call",
            {"name": name, "arguments": arguments},
            timeout=timeout,
        )
        # MCP 工具返回 content 列表（多个 text/image 块）
        content = result.get("content") or []
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
            elif isinstance(item, dict):
                parts.append(json.dumps(item, ensure_ascii=False))
        return "\n".join(parts) if parts else "（无返回内容）"

    @property
    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None


#  MCPClientRegistry：管理多个 MCP server 客户端


class MCPClientRegistry:
    """MCP server 集合 + 工具桥接到 ToolRegistry。

    所有 async 操作走一个**常驻后台事件循环**（_ensure_loop 启动的守护线程）。
    原因：MCPStdioClient 的 asyncio.Lock / 子进程 IO 都绑定创建时的 loop；
    若 start_all 与 call_tool 各自 asyncio.run 起新 loop，锁会跨 loop 使用
    （Python 3.10+ 直接 RuntimeError）。统一 run_coroutine_threadsafe 到同一
    loop 彻底规避，工具调用侧也能从任意线程安全发起。
    """

    def __init__(self):
        self._configs: dict[str, MCPServerConfig] = {}
        self._clients: dict[str, MCPStdioClient] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._loop_thread: Optional[threading.Thread] = None

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop_thread = threading.Thread(
                target=self._loop.run_forever, daemon=True, name="mcp-loop")
            self._loop_thread.start()
        return self._loop

    def run_sync(self, coro, timeout: float = 30.0):
        """在常驻 loop 上跑一个协程并阻塞等结果（线程安全）。"""
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=timeout)

    def load_from_yaml(self, path: str | Path) -> int:
        """从 yaml 加载 server 配置。返回加载数量。"""
        p = Path(path)
        if not p.is_file():
            return 0
        try:
            import yaml
            data = yaml.safe_load(p.read_text("utf-8")) or {}
        except Exception as e:  # noqa: BLE001
            log.warning("加载 MCP 配置失败：%s", e)
            return 0
        servers = data.get("servers", []) or []
        for s in servers:
            if not s.get("enabled", True):
                continue
            cfg = MCPServerConfig(
                name=s["name"],
                command=s["command"],
                args=s.get("args", []) or [],
                env=s.get("env", {}) or {},
                cwd=s.get("cwd"),
                description=s.get("description", ""),
            )
            self._configs[cfg.name] = cfg
        return len(self._configs)

    def register(self, config: MCPServerConfig) -> None:
        self._configs[config.name] = config

    async def start_all(self) -> dict[str, list[dict]]:
        """启动所有 server，返回 {server_name: [tools]}。"""
        results: dict[str, list[dict]] = {}
        for name, cfg in self._configs.items():
            client = MCPStdioClient(cfg)
            try:
                await client.start()
                tools = await client.list_tools()
                self._clients[name] = client
                results[name] = tools
            except Exception as e:  # noqa: BLE001
                log.warning("MCP %s 启动失败：%s", name, e)
                results[name] = []
        return results

    async def stop_all(self) -> None:
        for client in self._clients.values():
            try:
                await client.stop()
            except Exception:  # noqa: BLE001
                pass
        self._clients.clear()

    def get_client(self, name: str) -> Optional[MCPStdioClient]:
        return self._clients.get(name)

    async def call(self, server: str, tool: str,
                   arguments: dict, timeout: float = 30.0) -> str:
        """调一个 server 的一个 tool。"""
        client = self._clients.get(server)
        if client is None:
            raise RuntimeError(f"MCP server '{server}' 未启动")
        return await client.call_tool(tool, arguments, timeout=timeout)

    # -------------------------------------------------- 同步封装（UI / 工具侧用）
    def start_all_sync(self, timeout: float = 20.0) -> dict[str, list[dict]]:
        """启动所有 server（阻塞；内部走常驻 loop）。"""
        return self.run_sync(self.start_all(), timeout=timeout)

    def stop_all_sync(self, timeout: float = 10.0) -> None:
        """停止所有 server 并关闭常驻 loop（阻塞；进程退出前调用）。"""
        try:
            self.run_sync(self.stop_all(), timeout=timeout)
        except Exception:  # noqa: BLE001
            log.debug("MCP stop_all 异常（忽略）", exc_info=True)
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=3)
        self._loop = None
        self._loop_thread = None

    def all_tools(self) -> list[MCPTool]:
        """返回所有 server 注册的工具（启动后才有）。"""
        out: list[MCPTool] = []
        for server_name, client in self._clients.items():
            for t in client._tools:  # noqa: SLF001
                out.append(MCPTool(
                    name=t.get("name", "?"),
                    description=t.get("description", ""),
                    parameters=t.get("inputSchema", {"type": "object",
                                                     "properties": {}}),
                    server=server_name,
                    dangerous=bool(t.get("dangerous", False)),
                ))
        return out

    def bridge_to(self, registry, dangerous_tools: Optional[set[str]] = None) -> int:
        """把 MCP tools 注册到本地 ToolRegistry，返回注册数量。

        每个 MCP tool 在本地对应一个同步包装函数：所有调用统一 run_sync
        到常驻 loop（与 start_all 同一 loop，避免 asyncio.Lock 跨 loop）。
        """
        from app.engine.tools import Tool
        dangerous_tools = dangerous_tools or {"open_app", "open_website"}
        bridged = 0
        for mcp_tool in self.all_tools():
            if not mcp_tool.name:
                continue
            # 命名空间：mcp_<server>_<tool>
            local_name = f"mcp_{mcp_tool.server}_{mcp_tool.name}"
            server_name = mcp_tool.server
            tool_name = mcp_tool.name
            is_dangerous = mcp_tool.dangerous or (
                local_name in dangerous_tools)

            def make_fn(s=server_name, t=tool_name):
                def fn(**kwargs) -> str:
                    try:
                        return self.run_sync(
                            self.call(s, t, kwargs), timeout=60.0)
                    except Exception as e:  # noqa: BLE001
                        return f"错误：MCP 调用失败：{e}"
                return fn

            registry.register(Tool(
                name=local_name,
                description=f"[MCP/{mcp_tool.server}] {mcp_tool.description}",
                parameters=mcp_tool.parameters,
                fn=make_fn(),
            ))
            bridged += 1
        return bridged


async def _await_coro(coro):
    """只是 await 一下用于在线程里跑。"""
    return await coro
