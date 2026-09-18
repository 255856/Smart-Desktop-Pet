"""MCP 客户端 + filesystem_server 集成测试。"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest


class TestFilesystemServerSandbox:
    """测试沙箱逻辑（不启子进程）。"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        Path(self.tmpdir, "data").mkdir()
        Path(self.tmpdir, "data", "test.txt").write_text("hello world", encoding="utf-8")
        Path(self.tmpdir, "data", "sub").mkdir()
        Path(self.tmpdir, "data", "sub", "a.py").write_text("print(1)", encoding="utf-8")
        Path(self.tmpdir, "outside.txt").write_text("secret", encoding="utf-8")

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_is_in_allowed_true(self):
        from app.mcp.filesystem_server import is_in_allowed
        roots = [Path(self.tmpdir, "data").resolve()]
        assert is_in_allowed(str(Path(self.tmpdir, "data", "test.txt")), roots)

    def test_is_in_allowed_false(self):
        from app.mcp.filesystem_server import is_in_allowed
        roots = [Path(self.tmpdir, "data").resolve()]
        assert not is_in_allowed(str(Path(self.tmpdir, "outside.txt")), roots)

    def test_handle_fs_read_file(self):
        from app.mcp.filesystem_server import handle_call
        roots = [Path(self.tmpdir, "data").resolve()]
        result = handle_call("fs_read_file",
                             {"path": str(Path(self.tmpdir, "data", "test.txt"))},
                             roots)
        assert "hello world" in result["content"][0]["text"]

    def test_handle_fs_read_blocked(self):
        from app.mcp.filesystem_server import handle_call
        roots = [Path(self.tmpdir, "data").resolve()]
        result = handle_call("fs_read_file",
                             {"path": str(Path(self.tmpdir, "outside.txt"))},
                             roots)
        assert "不在沙箱" in result["content"][0]["text"]

    def test_handle_fs_list_dir(self):
        from app.mcp.filesystem_server import handle_call
        roots = [Path(self.tmpdir, "data").resolve()]
        result = handle_call("fs_list_dir",
                             {"path": str(Path(self.tmpdir, "data"))}, roots)
        text = result["content"][0]["text"]
        assert "test.txt" in text

    def test_handle_fs_search(self):
        from app.mcp.filesystem_server import handle_call
        roots = [Path(self.tmpdir, "data").resolve()]
        result = handle_call("fs_search_files",
                             {"pattern": "*.py",
                              "root": str(Path(self.tmpdir, "data"))}, roots)
        text = result["content"][0]["text"]
        assert "a.py" in text


class TestMCPClientBridge:
    """测试客户端通过子进程启动 server + 桥接到 ToolRegistry。"""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        Path(self.tmpdir, "data").mkdir()
        Path(self.tmpdir, "data", "hello.txt").write_text("hello mcp", encoding="utf-8")

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_full_mcp_roundtrip(self):
        """启动 filesystem_server 子进程，走完整 JSON-RPC，调用工具。"""
        from app.mcp.protocol import MCPClientRegistry, MCPServerConfig

        async def drive():
            reg = MCPClientRegistry()
            reg.register(MCPServerConfig(
                name="fs",
                command=sys.executable,
                args=["-m", "app.mcp.filesystem_server",
                      "--root", str(Path(self.tmpdir, "data"))],
                description="test fs server",
            ))
            results = await reg.start_all()
            try:
                assert "fs" in results
                tool_names = [t["name"] for t in results["fs"]]
                assert "fs_read_file" in tool_names
                # 真实调用
                result = await reg.call("fs", "fs_read_file",
                                        {"path": str(Path(self.tmpdir, "data", "hello.txt"))})
                return result
            finally:
                await reg.stop_all()

        result = asyncio.run(drive())
        assert "hello mcp" in result

    def test_bridge_to_tool_registry(self):
        """MCP 工具桥接到本地 ToolRegistry。"""
        from app.mcp.protocol import MCPClientRegistry, MCPServerConfig
        from app.engine.tools import ToolRegistry

        async def drive():
            reg = MCPClientRegistry()
            reg.register(MCPServerConfig(
                name="fs",
                command=sys.executable,
                args=["-m", "app.mcp.filesystem_server",
                      "--root", str(Path(self.tmpdir, "data"))],
            ))
            local_reg = ToolRegistry()
            await reg.start_all()
            try:
                bridged = reg.bridge_to(local_reg)
                names = local_reg.names()
                # 调一下试试
                result = local_reg.execute(
                    "mcp_fs_fs_read_file",
                    json.dumps({"path": str(Path(self.tmpdir, "data", "hello.txt"))})
                )
                return bridged, names, result
            finally:
                await reg.stop_all()

        bridged, names, result = asyncio.run(drive())
        assert bridged >= 3
        assert any(n.startswith("mcp_fs_") for n in names)
        assert "hello mcp" in result

    def test_bridge_handles_unstarted_servers(self):
        """没启 server 时 bridge 返回 0。"""
        from app.mcp.protocol import MCPClientRegistry
        from app.engine.tools import ToolRegistry
        reg = MCPClientRegistry()
        local = ToolRegistry()
        bridged = reg.bridge_to(local)
        assert bridged == 0
