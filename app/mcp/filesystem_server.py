"""示例 MCP server：filesystem 沙箱访问。

实现 MCP 协议的极简子集：
    - initialize
    - tools/list
    - tools/call

提供工具：
    - fs_read_file(path: str) -> str
    - fs_list_dir(path: str) -> str
    - fs_search_files(pattern: str) -> str

约束：
    - 只能访问 config["allowed_roots"] 下的路径
    - 写操作（fs_write_file）默认禁用

启动：
    python -m app.mcp.filesystem_server --root ./data --root ./tmp
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)


# MCP server 注册的工具列表
TOOLS = [
    {
        "name": "fs_read_file",
        "description": "读取一个文本文件（最多 2000 字符）。仅允许沙箱目录。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "fs_list_dir",
        "description": "列出一个目录的文件。仅允许沙箱目录。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "目录路径"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "fs_search_files",
        "description": "在沙箱内按 glob 模式搜索文件。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "glob 模式，如 *.txt"},
                "root": {"type": "string", "description": "搜索根目录（沙箱内）"},
            },
            "required": ["pattern"],
        },
    },
]


def is_in_allowed(path: str, allowed_roots: list[Path]) -> bool:
    """检查 path 是否在沙箱里。"""
    try:
        p = Path(path).resolve()
    except Exception:
        return False
    for root in allowed_roots:
        try:
            p.relative_to(root.resolve())
            return True
        except ValueError:
            continue
    return False


def handle_call(name: str, arguments: dict,
                allowed_roots: list[Path]) -> dict:
    """处理 tools/call，返回 MCP 格式 result。"""
    if name == "fs_read_file":
        path = arguments.get("path", "")
        if not is_in_allowed(path, allowed_roots):
            return {"content": [{"type": "text",
                                 "text": f"错误：路径不在沙箱内：{path}"}]}
        p = Path(path)
        if not p.is_file():
            return {"content": [{"type": "text",
                                 "text": f"错误：不是文件：{path}"}]}
        try:
            text = p.read_text(encoding="utf-8", errors="replace")[:2000]
            return {"content": [{"type": "text", "text": text}]}
        except Exception as e:  # noqa: BLE001
            return {"content": [{"type": "text",
                                 "text": f"读取失败：{e}"}]}

    if name == "fs_list_dir":
        path = arguments.get("path", "")
        if not is_in_allowed(path, allowed_roots):
            return {"content": [{"type": "text",
                                 "text": f"错误：路径不在沙箱内：{path}"}]}
        p = Path(path)
        if not p.is_dir():
            return {"content": [{"type": "text",
                                 "text": f"错误：不是目录：{path}"}]}
        try:
            entries = sorted(p.iterdir(), key=lambda x: x.name)
            lines = [f"{'📁' if e.is_dir() else '📄'} {e.name}" for e in entries[:50]]
            return {"content": [{
                "type": "text",
                "text": f"{path} 共 {len(entries)} 项：\n" + "\n".join(lines),
            }]}
        except Exception as e:  # noqa: BLE001
            return {"content": [{"type": "text", "text": f"列目录失败：{e}"}]}

    if name == "fs_search_files":
        pattern = arguments.get("pattern", "")
        root = arguments.get("root", "")
        if not root:
            # 默认搜第一个沙箱根
            root = str(allowed_roots[0]) if allowed_roots else ""
        if not is_in_allowed(root, allowed_roots):
            return {"content": [{"type": "text",
                                 "text": f"错误：根目录不在沙箱内：{root}"}]}
        try:
            matches = []
            for p in Path(root).rglob(pattern):
                if is_in_allowed(str(p), allowed_roots):
                    matches.append(str(p))
            matches = matches[:50]
            return {"content": [{
                "type": "text",
                "text": f"找到 {len(matches)} 个匹配：\n" + "\n".join(matches),
            }]}
        except Exception as e:  # noqa: BLE001
            return {"content": [{"type": "text", "text": f"搜索失败：{e}"}]}

    return {"content": [{"type": "text", "text": f"未知工具：{name}"}]}


def run_server(allowed_roots: list[Path]) -> None:
    """主循环：stdin 一行行读 JSON-RPC，stdout 输出。"""
    log.info("MCP filesystem_server 启动，roots=%s",
             [str(r) for r in allowed_roots])
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        req_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params", {}) or {}

        # initialize
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "filesystem_server",
                               "version": "1.0.0"},
            }
            send(req_id, result)
            continue
        if method == "notifications/initialized":
            continue
        if method == "tools/list":
            send(req_id, {"tools": TOOLS})
            continue
        if method == "tools/call":
            name = params.get("name", "")
            args = params.get("arguments", {}) or {}
            result = handle_call(name, args, allowed_roots)
            send(req_id, result)
            continue
        if req_id is not None:
            send(req_id, None, error={"code": -32601,
                                        "message": f"未知方法：{method}"})


def send(req_id, result=None, error=None) -> None:
    msg = {"jsonrpc": "2.0", "id": req_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--root", action="append", default=[],
                   help="沙箱根目录（可多个）")
    args = p.parse_args()
    if not args.root:
        args.root = ["./data"]
    allowed = [Path(r).resolve() for r in args.root]
    run_server(allowed)


if __name__ == "__main__":
    main()
