#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Desktop Pet · Live2D Demo 本地服务器
====================================

零依赖（只用 Python 标准库），专为 docs/demo/ 设计：
  - 默认端口 8765（与项目 FastAPI Dashboard 一致，便于记忆）
  - 自动 serve 当前目录的 docs/demo/index.html 作为入口
  - 提供 /models/<path> 代理到 --model-dir 指向的目录（用户的 Live2D 模型）
  - 提供根目录浏览，方便用户复制模型 *.model3.json 的完整 URL
  - 所有响应带 CORS 头，方便直接托管到 GitHub Pages 后仍能从本机拉模型

⚠️ 模型版权说明 / Model Copyright Notice
-----------------------------------------
本服务**不内置任何 Live2D 模型**。请用 --model-dir 指向你自己拥有合法授权的
模型目录。常见来源（如官方 Cubism 样例 Hiyori）是允许在个人项目中本地加载的，
但**禁止二次传播 / 商业使用**，请阅读各模型 EULA。

This server **does NOT bundle any Live2D model**. Use --model-dir to point
at a model directory that you are authorized to use.

用法 / Usage:
    python docs/demo/serve.py                           # 默认配置
    python docs/demo/serve.py --port 9000               # 改端口
    python docs/demo/serve.py --model-dir E:/my-models  # 挂载模型目录
    python docs/demo/serve.py --root .                  # 改静态根

启动后访问 http://127.0.0.1:8765/ 即可。
"""

from __future__ import annotations

import argparse
import http.server
import socketserver
import sys
import urllib.parse
from pathlib import Path


# ---------------------------------------------------------------------------
#  路径安全：拒绝 ../ 穿越；只允许在允许的根目录内解析
# ---------------------------------------------------------------------------

def safe_resolve(root: Path, url_path: str) -> Path | None:
    """把 URL 路径解析为 root 下的真实路径，跨出 root 返回 None。"""
    try:
        decoded = urllib.parse.unquote(url_path)
        rel = decoded.lstrip("/")
        candidate = (root / rel).resolve()
        root_resolved = root.resolve()
        # 必须在 root 之下
        candidate.relative_to(root_resolved)
        return candidate
    except (ValueError, OSError):
        return None


# ---------------------------------------------------------------------------
#  请求处理器
# ---------------------------------------------------------------------------

class DemoHandler(http.server.SimpleHTTPRequestHandler):
    """扩展默认 handler：
      - 注入 CORS 头（方便 GitHub Pages 托管页面拉本机模型）
      - 默认根指向 --root
      - /models/<path> 重写到 --model-dir/<path>
      - 根路径自动跳到 /index.html
      - 目录请求返回简易 HTML 索引（方便复制 *.model3.json URL）
    """

    server_root: Path = Path("docs/demo").resolve()
    model_dir: Path | None = None

    # ---------- CORS ----------
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        # PIXI 模型贴图跨域
        self.send_header("Cross-Origin-Resource-Policy", "cross-origin")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    # ---------- 路由 ----------
    def translate_path(self, path: str) -> str:
        """默认走 --root；/models/* 改走 --model-dir。"""
        # 剥离 query string / fragment，避免 index.html?lang=en 被当成文件名
        path = path.split("?", 1)[0].split("#", 1)[0]
        if self.model_dir and (path == "/models" or path.startswith("/models/")):
            rel = path[len("/models"):].lstrip("/")
            target = (self.model_dir / rel).resolve()
            # 安全：必须在 model_dir 下
            try:
                target.relative_to(self.model_dir.resolve())
            except ValueError:
                return str(self.model_dir)  # 越界 → 回退到 model_dir 本身
            return str(target)
        return str(safe_resolve(self.server_root, path) or self.server_root)

    def do_GET(self):
        # 根路径（允许带 query）
        path_only = self.path.split("?", 1)[0].split("#", 1)[0]
        if path_only in ("/", ""):
            if (self.server_root / "index.html").is_file():
                # 静态根本身就是 demo 目录（默认启动）
                self.path = "/index.html"
                return super().do_GET()
            # 静态根为项目根（--root .）时，根路径没有 index.html，重定向到内置 demo
            if (self.server_root / "docs" / "demo" / "index.html").is_file():
                target = "/docs/demo/index.html"
                if "?" in self.path:
                    target += "?" + self.path.split("?", 1)[1]
                self.send_response(302)
                self.send_header("Location", target)
                self.end_headers()
                return
            self.path = "/index.html"
            return super().do_GET()
        # 目录 → 渲染简易索引
        fs_path = Path(self.translate_path(self.path))
        if fs_path.is_dir():
            return self._render_dir_index(fs_path)
        return super().do_GET()

    # ---------- 目录索引 ----------
    def _render_dir_index(self, dir_path: Path):
        try:
            entries = sorted(dir_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as e:
            self.send_error(500, str(e))
            return

        # 计算相对路径前缀，方便用户复制 URL
        rel_prefix = self.path.rstrip("/")

        rows = []
        for entry in entries:
            if entry.name.startswith("."):
                continue
            href = f"{rel_prefix}/{urllib.parse.quote(entry.name)}"
            if entry.is_dir():
                rows.append(
                    f'<li>📁 <a href="{href}/">{entry.name}/</a></li>'
                )
            elif entry.suffix.lower() == ".model3.json":
                # 重点：复制按钮（兼容所有浏览器）
                full_url = f"http://{self.headers.get('host', '127.0.0.1:8765')}{href}"
                rows.append(
                    f'<li>🎭 <a href="{href}">{entry.name}</a> '
                    f'<button class="copy" data-url="{full_url}">复制 URL</button></li>'
                )
            else:
                rows.append(
                    f'<li>📄 <a href="{href}">{entry.name}</a></li>'
                )

        # 当前路径面包屑
        path_parts = [p for p in rel_prefix.split("/") if p]
        crumbs = ['<a href="/">~</a>']
        for i, part in enumerate(path_parts):
            href = "/" + "/".join(path_parts[: i + 1])
            crumbs.append(f'<a href="{href}">{urllib.parse.quote(part)}</a>')
        breadcrumb = " / ".join(crumbs)

        html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Index of {rel_prefix}</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", "PingFang SC", sans-serif;
         background:#1a1c2c; color:#e8e8f0; padding:24px; }}
  a {{ color:#74b9ff; text-decoration:none; }}
  a:hover {{ text-decoration:underline; }}
  h1 {{ font-size:18px; }}
  ul {{ list-style:none; padding:0; }}
  li {{ padding:6px 12px; margin:2px 0; background:rgba(255,255,255,.04);
        border-radius:6px; }}
  button.copy {{
    margin-left:8px; padding:2px 10px; font-size:11px;
    background:#6c5ce7; color:white; border:0; border-radius:4px;
    cursor:pointer;
  }}
  button.copy:hover {{ background:#5d4dd6; }}
  button.copy.ok {{ background:#00b894; }}
  .breadcrumb {{ color:#a8b0c0; margin-bottom:16px; font-size:13px; }}
  .note {{ background:rgba(255,118,117,.15); border-left:3px solid #ff7675;
           padding:8px 12px; border-radius:4px; font-size:12px;
           margin-bottom:16px; color:#ffce5c; }}
</style></head><body>
<h1>📂 Index of {rel_prefix}</h1>
<div class="breadcrumb">{breadcrumb}</div>
<div class="note">
  ⚠️ 本服务器仅 serve 你指定的本地目录，<strong>不内置任何 Live2D 模型</strong>。
  模型版权属原作者，请使用合法授权的模型。
</div>
<ul>
  {''.join(rows) if rows else '<li>（空目录）</li>'}
</ul>
<script>
document.querySelectorAll('button.copy').forEach(btn => {{
  btn.addEventListener('click', async () => {{
    try {{
      await navigator.clipboard.writeText(btn.dataset.url);
      btn.textContent = '已复制 ✓';
      btn.classList.add('ok');
      setTimeout(() => {{ btn.textContent = '复制 URL'; btn.classList.remove('ok'); }}, 1500);
    }} catch (e) {{
      btn.textContent = '复制失败';
    }}
  }});
}});
</script>
</body></html>"""

        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------- 静音默认日志（demo 用） ----------
    def log_message(self, format, *args):
        sys.stderr.write(f"[demo] {self.address_string()} {format % args}\n")


# ---------------------------------------------------------------------------
#  入口
# ---------------------------------------------------------------------------

class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """每请求一线程，足够 demo 用。"""
    daemon_threads = True
    allow_reuse_address = True


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    p.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    here = Path(__file__).resolve().parent
    p.add_argument("--root", type=Path, default=here,
                   help=f"静态根目录（默认 {here}）")
    p.add_argument("--model-dir", type=Path, default=None,
                   help="可选：你的 Live2D 模型目录，浏览器可通过 /models/<file> 访问")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    if not (root / "index.html").exists():
        print(f"⚠️  警告：{root}/index.html 不存在，浏览器可能 404", file=sys.stderr)

    DemoHandler.server_root = root
    DemoHandler.model_dir = args.model_dir.resolve() if args.model_dir else None

    print(f"🐳 Desktop Pet · Live2D Demo")
    print(f"   http://{args.host}:{args.port}/")
    print(f"   static root : {root}")
    if DemoHandler.model_dir:
        print(f"   model dir   : {DemoHandler.model_dir}   （挂载到 /models/*）")
    else:
        print(f"   model dir   : （未设置）用 --model-dir 指向你的模型目录")
    print(f"   Ctrl+C 停止")
    print()

    with ThreadedServer((args.host, args.port), DemoHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n👋 已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
