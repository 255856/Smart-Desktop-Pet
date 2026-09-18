"""Agent 可观测性 Dashboard（FastAPI）。

提供：
    - GET  /             —— HTML 主页（极简）
    - GET  /api/runs     —— 最近 N 条 run（默认 50）
    - GET  /api/runs/{id} —— 单条 run 的完整 trace
    - GET  /api/stats    —— 总体统计
    - GET  /api/memory   —— 当前 memory.json 内容

启动：
    python -m app.web.dashboard --port 8765
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


_HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <title>桌宠 Agent Dashboard</title>
  <style>
    body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
           margin: 24px; background: #fafafa; color: #222; }
    h1 { color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 8px; }
    h2 { color: #34495e; margin-top: 24px; }
    .card { background: white; border-radius: 8px; padding: 16px; margin: 8px 0;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
    .run { cursor: pointer; transition: background 0.2s; }
    .run:hover { background: #f0f8ff; }
    .status-success { color: #27ae60; font-weight: bold; }
    .status-failed { color: #e74c3c; font-weight: bold; }
    .status-cancelled { color: #f39c12; font-weight: bold; }
    pre { background: #f4f4f4; padding: 8px; border-radius: 4px;
          overflow-x: auto; font-size: 12px; }
    .tool { background: #ecf0f1; padding: 2px 8px; border-radius: 3px;
            font-family: monospace; font-size: 12px; }
    .event { margin: 6px 0; padding: 8px; border-left: 3px solid #3498db;
             background: #fdfdfd; }
    .event-tool { border-color: #e67e22; }
    .event-reflection { border-color: #9b59b6; }
    .event-plan { border-color: #16a085; }
    .stat { display: inline-block; margin-right: 24px; }
    .stat-num { font-size: 24px; font-weight: bold; color: #2980b9; }
    .stat-label { font-size: 12px; color: #7f8c8d; }
  </style>
</head>
<body>
  <h1>🐳 桌宠 Agent Dashboard</h1>
  <div id="stats" class="card"></div>
  <h2>最近 Run</h2>
  <div id="runs"></div>
  <div id="detail"></div>

  <script>
    async function loadStats() {
      const s = await fetch('/api/stats').then(r => r.json());
      document.getElementById('stats').innerHTML = `
        <div class="stat"><div class="stat-num">${s.total_runs}</div><div class="stat-label">总 Run</div></div>
        <div class="stat"><div class="stat-num">${(s.success_rate*100).toFixed(0)}%</div><div class="stat-label">成功率</div></div>
        <div class="stat"><div class="stat-num">${s.avg_steps}</div><div class="stat-label">平均步骤</div></div>
        <div class="stat"><div class="stat-num">${Object.keys(s.tool_distribution).length}</div><div class="stat-label">不同工具</div></div>
      `;
    }

    async function loadRuns() {
      const runs = await fetch('/api/runs?limit=50').then(r => r.json());
      const html = runs.map(r => `
        <div class="card run" onclick="loadRun('${r.id}')">
          <div><b>${(r.goal || '').slice(0, 80)}</b></div>
          <div style="font-size:12px;color:#666;margin-top:4px">
            <span class="status-${r.status}">${r.status}</span> ·
            步骤: ${r.total_steps} · 工具调用: ${r.tool_calls} ·
            ${new Date(r.started_at * 1000).toLocaleString()}
          </div>
        </div>
      `).join('');
      document.getElementById('runs').innerHTML = html;
    }

    async function loadRun(id) {
      const r = await fetch('/api/runs/' + id).then(r => r.json());
      const eventsHtml = (r.events || []).map(e => {
        let body = '';
        if (e.kind === 'plan') body = `<pre>${JSON.stringify(e.payload, null, 2)}</pre>`;
        else if (e.kind === 'tool') body = `<span class="tool">${e.payload.name}</span>(${JSON.stringify(e.payload.args || {})})<br>→ ${(e.payload.result || '').slice(0, 200)}`;
        else if (e.kind === 'reflection') body = `<b>${e.payload.verdict}</b>: ${e.payload.comment}`;
        else if (e.kind === 'text') body = `<i>${(e.payload.text || '').slice(0, 200)}</i>`;
        else body = `<pre>${JSON.stringify(e.payload, null, 2)}</pre>`;
        return `<div class="event event-${e.kind}"><b>${e.kind} #${e.seq}</b><br>${body}</div>`;
      }).join('');
      document.getElementById('detail').innerHTML = `
        <h2>Run ${id}</h2>
        <div class="card">
          <div><b>目标：</b>${r.goal}</div>
          <div><b>状态：</b><span class="status-${r.status}">${r.status}</span></div>
          <div><b>模式：</b>${r.mode}</div>
          <div><b>开始：</b>${new Date(r.started_at * 1000).toLocaleString()}</div>
          ${r.finished_at ? `<div><b>结束：</b>${new Date(r.finished_at * 1000).toLocaleString()}</div>` : ''}
          <div><b>最终答案：</b>${r.final_answer || ''}</div>
        </div>
        <h3>事件流</h3>
        ${eventsHtml}
      `;
    }

    loadStats();
    loadRuns();
    setInterval(() => { loadStats(); loadRuns(); }, 5000);
  </script>
</body>
</html>
"""


def create_app(trace_db: str | Path,
               memory_file: Optional[str | Path] = None):
    """构造 FastAPI app。"""
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import HTMLResponse
    except ImportError as e:
        raise RuntimeError("需要 fastapi：pip install fastapi") from e

    app = FastAPI(title="Desktop Pet Agent Dashboard")
    trace_db = str(trace_db)

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return _HTML_PAGE

    @app.get("/api/stats")
    async def stats():
        from app.brain.trace import TraceRecorder
        rec = TraceRecorder(trace_db)
        return rec.stats()

    @app.get("/api/runs")
    async def runs(limit: int = 50):
        from app.brain.trace import TraceRecorder
        rec = TraceRecorder(trace_db)
        return rec.list_runs(limit=limit)

    @app.get("/api/runs/{rid}")
    async def run_detail(rid: str):
        from app.brain.trace import TraceRecorder
        rec = TraceRecorder(trace_db)
        r = rec.get_run(rid)
        if not r:
            raise HTTPException(404, f"Run {rid} not found")
        return r

    @app.get("/api/memory")
    async def memory():
        if memory_file is None or not Path(memory_file).is_file():
            return {"items": [], "note": "memory file not found"}
        try:
            data = json.loads(Path(memory_file).read_text("utf-8"))
        except Exception as e:  # noqa: BLE001
            return {"items": [], "error": str(e)}
        # 排序：importance 高在前
        data.sort(key=lambda x: (-float(x.get("importance", 0.5)),
                                 -float(x.get("created_at", 0))))
        return {"items": data, "count": len(data)}

    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--trace-db", default="data/traces.db")
    parser.add_argument("--memory-file", default="data/memory.json")
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    # 默认在当前目录的 data/ 下找
    trace_db = Path(args.trace_db)
    if not trace_db.is_absolute():
        trace_db = Path.cwd() / trace_db
    memory_file = Path(args.memory_file)
    if not memory_file.is_absolute():
        memory_file = Path.cwd() / memory_file

    app = create_app(trace_db, memory_file)
    try:
        import uvicorn
    except ImportError:
        raise RuntimeError("需要 uvicorn：pip install uvicorn")

    print(f"📊 Dashboard 启动: http://{args.host}:{args.port}")
    print(f"   trace db: {trace_db}")
    print(f"   memory:   {memory_file}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
