"""Agent 可观测性 Dashboard（FastAPI）。"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from app.brain import memory_merge as _memory_merge

log = logging.getLogger(__name__)

# companion_log.txt 的增量解析缓存（只增不减的文件，按已读位置增量解析）
_COMPANION_LOG_CACHE = {
    "offset": 0,        # 已解析到的位置（文本模式的 tell cookie）
    "pending": "",      # 上次没读完的半行
    "decisions": [],
    "outcomes": [],
}


_HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>桌宠智能体控制台</title>
<style>
  :root{
    --bg:#f5f5fb; --card:#ffffff; --border:#e9e6f5; --line:#f0eef9;
    --text:#2c2c38; --sub:#8a8a9c; --faint:#b0b0c0;
    --accent:#7c6cf0; --accent-d:#6a58e0; --accent-bg:#efeaff; --accent-lt:#a99bfa;
    --green:#10b981; --green-bg:#e7f8f1; --red:#ef4444; --red-bg:#fdecec;
    --amber:#f59e0b; --amber-bg:#fef4e3; --blue:#3b82f6; --blue-bg:#eaf1fe;
    --shadow:0 6px 24px rgba(108,92,204,.08);
    --shadow-sm:0 2px 8px rgba(108,92,204,.06);
  }
  *{box-sizing:border-box;}
  html,body{height:100%;}
  body{
    font-family:"Microsoft YaHei UI","Microsoft YaHei","PingFang SC",-apple-system,"Segoe UI",sans-serif;
    margin:0; background:var(--bg); color:var(--text); font-size:14px;
  }
  .wrap{max-width:1240px; margin:0 auto; padding:22px 26px 40px;}

  /* 顶栏 */
  .topbar{display:flex; align-items:center; gap:14px; margin-bottom:20px;}
  .logo{width:44px;height:44px;border-radius:14px;flex:none;
    background:linear-gradient(135deg,#a78bfa,#7c6cf0);
    display:flex;align-items:center;justify-content:center;font-size:22px;
    box-shadow:0 6px 16px rgba(124,108,240,.35);}
  .titles h1{font-size:19px;margin:0;font-weight:700;letter-spacing:.5px;}
  .titles .sub{font-size:12px;color:var(--sub);margin-top:2px;}
  .topbar .spacer{flex:1;}
  .live{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--sub);
    background:var(--card);border:1px solid var(--border);border-radius:999px;
    padding:7px 14px;box-shadow:var(--shadow-sm);}
  .live .dot{width:8px;height:8px;border-radius:50%;background:var(--green);}
  .live.off .dot{background:var(--faint);}
  .live .dot.pulse{animation:pulse 1.4s infinite;}
  @keyframes pulse{0%{box-shadow:0 0 0 0 rgba(16,185,129,.5);}70%{box-shadow:0 0 0 7px rgba(16,185,129,0);}100%{box-shadow:0 0 0 0 rgba(16,185,129,0);}}

  /* 概览卡（8 张） */
  .stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:20px;}
  .stat{background:var(--card);border:1px solid var(--border);border-radius:16px;
    padding:16px 18px;box-shadow:var(--shadow-sm);position:relative;overflow:hidden;}
  .stat .ico{font-size:18px;display:flex;align-items:center;gap:0;}
  .stat .ico .ic{display:block;}
  .ic{display:inline-block;vertical-align:-3px;flex:none;}
  .stat .num{font-size:28px;font-weight:700;margin-top:6px;line-height:1.1;}
  .stat .label{font-size:12px;color:var(--sub);margin-top:4px;}
  .stat .bar{position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--accent);}
  .stat.green .bar{background:var(--green);} .stat.green .num{color:var(--green);}
  .stat.blue .bar{background:var(--blue);} .stat.blue .num{color:var(--blue);}
  .stat.amber .bar{background:var(--amber);} .stat.amber .num{color:var(--amber);}
  .stat.violet .bar{background:var(--accent);} .stat.violet .num{color:var(--accent-d);}
  .est-tag{display:inline-block;background:var(--amber-bg);color:#a36500;border-radius:5px;
    padding:1px 6px;font-size:10.5px;font-weight:700;margin-left:6px;vertical-align:2px;}

  /* 标签页（8 个） */
  .tabs{display:flex;gap:6px;margin-bottom:18px;flex-wrap:wrap;}
  .tab{padding:9px 18px;border-radius:11px;cursor:pointer;font-weight:600;font-size:14px;
    color:var(--sub);background:transparent;border:1px solid transparent;transition:.15s;
    white-space:nowrap;display:inline-flex;align-items:center;gap:6px;}
  .tab:hover{background:var(--accent-bg);color:var(--accent-d);}
  .tab.active{background:var(--accent);color:#fff;box-shadow:0 4px 12px rgba(124,108,240,.3);}
  .view{display:none;} .view.active{display:block;}

  /* 会话视图：左右栏 */
  .sessions{display:grid;grid-template-columns:340px 1fr;gap:16px;align-items:start;}
  .panel{background:var(--card);border:1px solid var(--border);border-radius:16px;
    box-shadow:var(--shadow-sm);overflow:hidden;}
  .list-head{padding:14px 14px 10px;border-bottom:1px solid var(--line);}
  .search{width:100%;border:1.5px solid var(--border);border-radius:10px;padding:9px 12px;
    font-size:13px;outline:none;font-family:inherit;}
  .search:focus{border-color:var(--accent-lt);}
  .filters{display:flex;gap:6px;margin-top:10px;flex-wrap:wrap;}
  .chip{padding:4px 12px;border-radius:999px;font-size:12px;cursor:pointer;border:1px solid var(--border);
    background:#fff;color:var(--sub);user-select:none;}
  .chip.active{background:var(--accent-bg);border-color:var(--accent-lt);color:var(--accent-d);font-weight:600;}
  .run-list{max-height:640px;overflow-y:auto;padding:8px;}
  .run-item{border-radius:12px;padding:11px 12px;cursor:pointer;border:1px solid transparent;margin-bottom:4px;}
  .run-item:hover{background:var(--accent-bg);}
  .run-item.active{background:var(--accent-bg);border-color:var(--accent-lt);}
  .run-item .goal{font-size:13px;font-weight:600;line-height:1.4;display:-webkit-box;
    -webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;word-break:break-word;}
  .run-item .meta{display:flex;align-items:center;gap:8px;margin-top:6px;font-size:11.5px;color:var(--sub);flex-wrap:wrap;}
  .run-item .meta .t{white-space:nowrap;}
  .badge{display:inline-flex;align-items:center;gap:4px;padding:2px 9px;border-radius:999px;
    font-size:11px;font-weight:600;line-height:1.5;}
  .badge.success{background:var(--green-bg);color:var(--green);}
  .badge.failed{background:var(--red-bg);color:var(--red);}
  .badge.running{background:var(--blue-bg);color:var(--blue);}
  .badge.cancelled{background:var(--amber-bg);color:var(--amber);}
  .badge.ghost{background:#f1f0f8;color:var(--sub);}
  .mini-tag{display:inline-flex;align-items:center;gap:3px;background:#f6f5fc;color:#6b63a0;
    border-radius:6px;padding:1px 7px;font-size:11px;}
  .mini-tag.warn{background:var(--red-bg);color:var(--red);}
  .mini-tag.ok{background:var(--green-bg);color:var(--green);}
  .mini-tag.est{background:var(--amber-bg);color:#a36500;}

  /* 详情 */
  .detail{padding:22px 24px;min-height:600px;}
  .empty{min-height:600px;display:flex;flex-direction:column;align-items:center;justify-content:center;
    color:var(--faint);gap:12px;}
  .empty .big{font-size:44px;}
  .d-head{display:flex;align-items:flex-start;gap:12px;flex-wrap:wrap;}
  .d-head h2{margin:0;font-size:18px;line-height:1.4;flex:1;min-width:240px;word-break:break-word;}
  .d-meta{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0 4px;}
  .meta-cell{background:var(--bg);border:1px solid var(--border);border-radius:10px;
    padding:7px 13px;font-size:12px;color:var(--sub);}
  .meta-cell b{color:var(--text);font-weight:600;}
  .section-title{font-size:13px;font-weight:700;color:var(--accent-d);margin:22px 0 10px;
    display:flex;align-items:center;gap:7px;}
  .answer{background:linear-gradient(135deg,#f7f4ff,#fdf6fb);border:1px solid #e6e0fb;
    border-radius:14px;padding:16px 18px;line-height:1.7;font-size:14px;white-space:pre-wrap;
    word-break:break-word;}
  .answer.error{background:var(--red-bg);border-color:#f5c6c6;color:#b42318;}
  .answer strong{color:var(--accent-d);}
  .answer code{background:#eae6fb;color:#5b46e8;border-radius:4px;padding:1px 6px;font-size:13px;}
  .answer pre{background:#232337;color:#e5e7eb;padding:10px 12px;border-radius:9px;overflow-x:auto;
    white-space:pre-wrap;}

  /* 时间线 */
  .timeline{position:relative;padding-left:26px;}
  .timeline::before{content:"";position:absolute;left:9px;top:6px;bottom:6px;width:2px;background:var(--line);}
  .tl-item{position:relative;margin-bottom:12px;}
  .tl-item .node{position:absolute;left:-26px;top:2px;width:20px;height:20px;border-radius:50%;
    display:flex;align-items:center;justify-content:center;font-size:11px;background:#fff;
    border:2px solid var(--accent-lt);z-index:1;}
  .tl-item.tool .node{border-color:var(--amber);background:var(--amber-bg);}
  .tl-item.plan .node{border-color:var(--blue);background:var(--blue-bg);}
  .tl-item.reflection .node{border-color:var(--accent);background:var(--accent-bg);}
  .tl-item.meta .node{border-color:var(--faint);background:#f6f5fc;}
  .tl-item.error .node{border-color:var(--red);background:var(--red-bg);}
  .tl-card{border:1px solid var(--border);border-radius:12px;padding:11px 14px;background:#fff;}
  .tl-card .cap{font-size:12.5px;font-weight:700;margin-bottom:5px;display:flex;align-items:center;gap:8px;}
  .tl-card.tool .cap{color:#b45309;}
  .kv{display:flex;flex-wrap:wrap;gap:6px;margin:4px 0;}
  .kv .pair{background:var(--bg);border:1px solid var(--border);border-radius:7px;padding:3px 9px;
    font-size:12px;}
  .kv .pair .k{color:var(--sub);margin-right:5px;}
  .kv .pair .v{font-family:Consolas,monospace;color:var(--text);word-break:break-all;}
  .result-box{background:var(--bg);border-radius:9px;padding:8px 11px;font-size:12.5px;
    color:#4b4b60;margin-top:6px;white-space:pre-wrap;word-break:break-word;
    border:1px solid var(--line);font-family:Consolas,monospace;max-height:140px;overflow:hidden;}
  .result-box.expanded{max-height:none;}
  .expand-btn{margin-top:6px;background:none;border:none;color:var(--accent);cursor:pointer;
    font-size:12px;padding:0;font-family:inherit;}
  details.raw{margin-top:10px;}
  details.raw summary{cursor:pointer;font-size:12px;color:var(--sub);user-select:none;}
  details.raw pre{background:#f6f5fc;border:1px solid var(--line);border-radius:9px;padding:10px;
    font-size:11.5px;overflow-x:auto;white-space:pre-wrap;word-break:break-word;margin:8px 0 0;}

  /* 通用块 */
  .hint{font-size:12.5px;color:var(--sub);background:var(--bg);border:1px dashed var(--border);
    border-radius:10px;padding:10px 13px;}
  .hint.warn{background:var(--red-bg);border-color:#f5c6c6;color:#b42318;}
  .hint.ok{background:var(--green-bg);border-color:#bfe8d5;color:#0a7c45;}
  .hint.amber{background:var(--amber-bg);border-color:#f6ddb0;color:#a36500;}
  .grid2{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px;}
  .grid3{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px;}
  .kpi{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:13px 15px;
    box-shadow:var(--shadow-sm);}
  .kpi .k{font-size:11.5px;color:var(--sub);}
  .kpi .v{font-size:20px;font-weight:700;margin-top:4px;line-height:1.2;}
  .kpi .x{font-size:11px;color:var(--faint);margin-top:3px;}
  .mem{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:15px 17px;
    box-shadow:var(--shadow-sm);}
  .mem .content{font-size:14px;line-height:1.6;word-break:break-word;white-space:pre-wrap;}
  .mem .foot{display:flex;align-items:center;gap:8px;margin-top:10px;font-size:11.5px;color:var(--sub);flex-wrap:wrap;}
  .cat-tag{background:var(--accent-bg);color:var(--accent-d);border-radius:6px;padding:2px 9px;font-size:11px;font-weight:600;}
  .sub-head{font-size:14px;font-weight:700;margin:22px 0 12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;}
  .bar-row{display:flex;align-items:center;gap:12px;margin-bottom:12px;}
  .bar-row .name{width:170px;flex:none;font-size:13px;font-weight:600;}
  .bar-row .track{flex:1;height:22px;background:var(--bg);border-radius:7px;overflow:hidden;}
  .bar-row .fill{height:100%;border-radius:7px;background:linear-gradient(90deg,#a78bfa,#7c6cf0);
    display:flex;align-items:center;justify-content:flex-end;color:#fff;font-size:11px;font-weight:700;
    padding-right:8px;min-width:26px;transition:width .5s;}
  .error-banner{background:var(--red-bg);border:1px solid #f5c6c6;border-radius:12px;padding:12px 15px;
    color:#b42318;font-size:13px;line-height:1.6;white-space:pre-wrap;word-break:break-word;
    display:flex;gap:10px;align-items:flex-start;}

  /* 表格 */
  table.tbl{width:100%;border-collapse:collapse;font-size:12.5px;}
  table.tbl th{background:var(--bg);color:var(--sub);font-weight:600;text-align:left;
    padding:8px 10px;border-bottom:1px solid var(--border);white-space:nowrap;}
  table.tbl td{padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top;}
  table.tbl tr:hover td{background:#faf9ff;}
  table.tbl td.num{font-family:Consolas,monospace;text-align:right;white-space:nowrap;}
  table.tbl td.name{font-family:Consolas,monospace;font-size:12px;}
  .tbl-wrap{max-height:460px;overflow:auto;border:1px solid var(--line);border-radius:12px;}

  /* 桌宠养成 (v23) */
  .cs-row{display:flex;align-items:center;gap:10px;margin-bottom:9px;}
  .cs-row .name{width:90px;flex:none;font-size:13px;font-weight:600;}
  .cs-row .track{flex:1;height:20px;background:var(--bg);border-radius:6px;overflow:hidden;border:1px solid var(--line);}
  .cs-row .fill{height:100%;border-radius:6px;display:flex;align-items:center;justify-content:flex-end;
    color:#fff;font-size:11px;font-weight:700;padding-right:7px;min-width:34px;transition:width .5s;}
  .cs-row .v{font-family:Consolas,monospace;font-size:12px;color:var(--sub);width:80px;text-align:right;flex:none;}
  .cs-row .fill.satiety{background:linear-gradient(90deg,#fbbf24,#f59e0b);}
  .cs-row .fill.mood{background:linear-gradient(90deg,#a78bfa,#7c6cf0);}
  .cs-row .fill.exp{background:linear-gradient(90deg,#34d399,#10b981);}
  .cs-row .fill.favor{background:linear-gradient(90deg,#f472b6,#ec4899);}
  .cs-row .fill.level{background:linear-gradient(90deg,#60a5fa,#3b82f6);}
  .cs-row .fill.exp_to_next{background:linear-gradient(90deg,#cbd5e1,#94a3b8);}
  .qtag{display:inline-block;padding:2px 8px;border-radius:5px;font-size:11px;font-weight:600;margin-left:6px;}
  .qtag.LOW{background:#fdecec;color:#b42318;}
  .qtag.MID{background:#fef4e3;color:#a36500;}
  .qtag.HIGH{background:#e7f8f1;color:#0a7c45;}
  .trace-row{display:grid;grid-template-columns:62px 1fr auto;gap:10px;align-items:center;
    padding:6px 8px;border-radius:7px;margin-bottom:4px;font-size:12.5px;border-left:3px solid var(--accent);}
  .trace-row.decision{border-left-color:var(--blue);background:#f0f6ff;}
  .trace-row.outcome{border-left-color:var(--green);background:#f0faf3;}
  .trace-row .ts{color:var(--sub);font-family:Consolas,monospace;font-size:11px;}
  .trace-row .body{font-family:Consolas,monospace;}
  .trace-row .arrow{font-size:11px;color:var(--sub);font-family:Consolas,monospace;text-align:right;}
  .config-block{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:8px 11px;
    font-size:12px;line-height:1.7;margin-bottom:8px;word-break:break-all;}
  .config-block b{color:var(--accent-d);}
  .cs-compact{display:flex;flex-direction:column;gap:6px;margin-bottom:8px;}
  .cs-block{background:linear-gradient(135deg,#f7f4ff,#fdf6fb);border:1px solid #e6e0fb;
    border-radius:11px;padding:9px 13px;font-size:13px;line-height:1.7;}
  .cs-block b{color:var(--accent-d);}

  /* 危险操作区 */
  .danger{border:1.5px dashed #f0b4b4;border-radius:14px;padding:15px 17px;background:#fffafa;}
  .danger h3{margin:0 0 6px;font-size:14px;color:#b42318;}
  .danger .ops{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0;}
  button.btn{border:1px solid var(--border);background:#fff;color:var(--text);border-radius:10px;
    padding:8px 14px;font-size:13px;font-family:inherit;cursor:pointer;}
  button.btn:hover{border-color:var(--accent-lt);color:var(--accent-d);}
  button.btn.danger-btn{border-color:#f0b4b4;color:#b42318;background:#fff;}
  button.btn.danger-btn:hover{background:var(--red-bg);}
  button.btn[disabled]{opacity:.45;cursor:not-allowed;}
  .switch{display:inline-flex;align-items:center;gap:8px;font-size:12.5px;color:var(--sub);
    cursor:pointer;user-select:none;}
  .switch input{width:16px;height:16px;accent-color:var(--accent);cursor:pointer;}

  ::-webkit-scrollbar{width:8px;height:8px;}
  ::-webkit-scrollbar-thumb{background:#d8d4ec;border-radius:4px;}
  ::-webkit-scrollbar-thumb:hover{background:var(--accent-lt);}
  @media(max-width:1100px){.stats{grid-template-columns:repeat(2,1fr);}
    .sessions{grid-template-columns:1fr;}.run-list{max-height:360px;}}
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div class="logo">🐳</div>
    <div class="titles">
      <h1>桌宠智能体控制台</h1>
      <div class="sub">项目总览面板 · 文件观测器（只读 data/，独立子进程）</div>
    </div>
    <div class="spacer"></div>
    <div class="live" id="liveBox"><span class="dot pulse"></span><span id="liveText">自动刷新中</span></div>
  </div>

  <div class="stats" id="stats"></div>

  <div class="tabs" id="tabs">
    <div class="tab active" data-tab="runs" data-view="runs" data-icon="chat" title="会话记录：最近会话 / 最近错误 / 工具调用分布 / 耗时分布">运行</div>
    <div class="tab" data-tab="cost" data-view="cost" data-icon="zap">成本</div>
    <div class="tab" data-tab="memory" data-view="memory" data-icon="brain" title="长期记忆：live 库行数 / kind 与 importance 分布 / access_count 命中数 / 逐条内容">记忆</div>
    <div class="tab" data-tab="decision" data-view="decision" data-icon="compass">决策</div>
    <div class="tab" data-tab="tools" data-view="tools" data-icon="wrench" title="工具统计：61 内置 + 7 MCP 清单 / 风险等级 / 解锁等级 / 文档对账差异">工具</div>
    <div class="tab" data-tab="config" data-view="config" data-icon="info">配置</div>
    <div class="tab" data-tab="state" data-view="state" data-icon="heart">状态</div>
    <div class="tab" data-tab="health" data-view="health" data-icon="alert">健康与维护</div>
  </div>

  <!-- ============ Tab 运行 ============ -->
  <div class="view active" id="view-runs">
    <div class="sessions">
      <div class="panel">
        <div class="list-head">
          <input class="search" id="search" placeholder="搜索对话内容…">
          <div class="filters" id="filters">
            <span class="chip active" data-f="all">全部</span>
            <span class="chip" data-f="success">成功</span>
            <span class="chip" data-f="failed">失败</span>
            <span class="chip" data-f="running">进行中</span>
          </div>
        </div>
        <div class="run-list" id="runList"></div>
      </div>
      <div class="panel" id="detailPanel">
        <div class="empty"><div class="big" data-icon="search" data-size="40"></div><div>选择左侧会话，查看完整执行过程</div></div>
      </div>
    </div>
  </div>

  <!-- ============ Tab 成本 ============ -->
  <div class="view" id="view-cost">
    <div class="panel" style="padding:18px;">
      <div class="sub-head" style="margin-top:0;" data-icon="zap">成本总览 <span id="costSource" class="mini-tag"></span></div>
      <div class="grid3" id="costKpis"></div>
      <div id="costErr"></div>
      <div class="sub-head">缓存命中（按时间桶）</div>
      <div id="costSeries"></div>
      <div class="sub-head">按会话 / 模型 / 后端拆分</div>
      <div id="costBreak"></div>
      <div class="sub-head">逐请求明细（最贵的轮次）</div>
      <div class="tbl-wrap"><table class="tbl" id="costTurns"></table></div>
    </div>
  </div>

  <!-- ============ Tab 记忆 ============ -->
  <div class="view" id="view-memory">
    <div class="panel" style="padding:18px;">
      <div class="sub-head" style="margin-top:0;" data-icon="brain">长期记忆库 <span id="memCount" class="mini-tag"></span></div>
      <div id="memStoreHint"></div>
      <div class="grid3" id="memKpis" style="margin-top:12px;"></div>
      <div class="sub-head">live 库分布</div>
      <div id="memDist"></div>
      <div class="sub-head">legacy 库重复 / 冲突提示</div>
      <div id="memDuplicates"></div>
      <div class="sub-head">逐条内容（live 库）</div>
      <div class="grid2" id="memGrid"></div>
      <div class="sub-head">合并 legacy → live（先预览，再执行）</div>
      <div class="danger" style="border-color:#f0d3a0;background:#fffdf7;">
        <h3 style="color:#a36500;">记忆合并（危险操作）</h3>
        <div class="hint">先点「预览合并」，看清楚将保留 / 将并入 / 将丢弃再决定。执行前会自动备份**两个记忆库**（不打包整个 data/），legacy 库只归档（移动到 backups/）不删除。</div>
        <div class="ops">
          <button class="btn" id="btnMergePreview">预览合并</button>
          <button class="btn danger-btn" id="btnMergeRun" disabled>执行合并（需二次确认）</button>
        </div>
        <div id="mergePreview"></div>
      </div>
    </div>
  </div>

  <!-- ============ Tab 决策 ============ -->
  <div class="view" id="view-decision">
    <div class="panel" style="padding:18px;">
      <div class="sub-head" style="margin-top:0;" data-icon="compass">桌宠决策日志 <span id="decisionSource" class="mini-tag"></span></div>
      <div class="grid3" id="decisionKpis"></div>
      <div class="sub-head">最近决策</div>
      <div id="decisionList"></div>
      <div class="sub-head">最近结果</div>
      <div id="outcomeList"></div>
      <div class="sub-head">养成规则（生效配置）</div>
      <div id="decisionConfig"></div>
    </div>
  </div>

  <!-- ============ Tab 工具 ============ -->
  <div class="view" id="view-tools">
    <div class="panel" style="padding:18px;">
      <div class="sub-head" style="margin-top:0;" data-icon="wrench">工具统计 · 清单 <span id="toolCount" class="mini-tag"></span></div>
      <div class="grid3" id="toolKpis"></div>
      <div id="toolErr"></div>
      <div class="sub-head">风险等级分布</div>
      <div id="toolRisk"></div>
      <div class="sub-head">全部工具</div>
      <div class="tbl-wrap"><table class="tbl" id="toolTable"></table></div>
      <div class="sub-head">对账结果（文档 ↔ 注册表）</div>
      <div id="reconBox"></div>
    </div>
  </div>

  <!-- ============ Tab 配置 ============ -->
  <div class="view" id="view-config">
    <div class="panel" style="padding:18px;">
      <div class="sub-head" style="margin-top:0;" data-icon="info">生效配置 <span id="configPath" class="mini-tag"></span></div>
      <div id="configErr"></div>
      <div class="grid2">
        <div>
          <div class="sub-head" style="margin-top:8px;">模型</div>
          <div id="configModel"></div>
        </div>
        <div>
          <div class="sub-head" style="margin-top:8px;">密钥（只显示是否配置与长度）</div>
          <div id="configKey"></div>
        </div>
      </div>
      <div class="sub-head">工具解锁表</div>
      <div id="configUnlock"></div>
    </div>
  </div>

  <!-- ============ Tab 状态 ============ -->
  <div class="view" id="view-state">
    <div class="panel" style="padding:18px;">
      <div class="sub-head" style="margin-top:0;" data-icon="heart">桌宠状态 <span id="statePath" class="mini-tag"></span></div>
      <div id="stateErr"></div>
      <div id="stateBars"></div>
      <div class="sub-head">工具解锁进度</div>
      <div id="stateUnlock"></div>
    </div>
  </div>

  <!-- ============ Tab 健康与维护 ============ -->
  <div class="view" id="view-health">
    <div class="panel" style="padding:18px;">
      <div class="sub-head" style="margin-top:0;" data-icon="alert">进程健康</div>
      <div class="grid3" id="healthKpis"></div>
      <div id="healthErr"></div>
      <div class="sub-head">限流 / 崩溃样本</div>
      <div id="healthSamples"></div>
      <div class="sub-head">data/ 文件体检</div>
      <div class="tbl-wrap"><table class="tbl" id="healthFiles"></table></div>
      <div class="sub-head">向量索引（Chroma + bge-small-zh-v1.5 + bge-reranker-base）</div>
      <div class="grid3" id="healthVector"></div>
      <div class="sub-head">危险操作</div>
      <div class="danger">
        <h3>默认只读 · 要操作得显式打开维护模式</h3>
        <div class="hint warn">
          所有危险操作都是 <b>POST</b>，需要 <b>maintenance=1</b> + 显式 confirm 标志，
          并且<b>动手之前自动备份</b>（备份范围 = 这次操作真正会动的文件；
          全量备份默认<b>不含</b> ~289MB 的 checkpoint 库）。GET 请求不会触发任何写操作。
        </div>
        <div class="ops">
          <label class="switch"><input type="checkbox" id="maintSwitch"> 维护模式</label>
        </div>
        <div class="ops">
          <button class="btn danger-btn" data-op="backup" data-confirm="backup">备份 data/</button>
          <label class="switch" title="默认不打进 checkpoint 库（仓库里 ~289MB，应用自己会 prune 它）"><input type="checkbox" id="maintIncludeCkpt"> 备份时包含 checkpoint 库（~289MB）</label>
          <button class="btn danger-btn" data-op="reset-ledger" data-confirm="reset-ledger">重置 token 账本</button>
          <button class="btn danger-btn" data-op="reconciliation" data-confirm="rerun-reconciliation">重跑工具对账</button>
          <button class="btn danger-btn" data-op="merge-memory" data-confirm="merge-memory">合并记忆库</button>
          <button class="btn danger-btn" data-op="rebuild-vector-index" data-confirm="rebuild-vector-index">重建向量索引</button>
        </div>
        <div id="maintResult"></div>
      </div>
    </div>
  </div>
</div>
<script>
"use strict";
const $ = (s, el=document) => el.querySelector(s);
const $$ = (s, el=document) => [...el.querySelectorAll(s)];

// 统一 SVG 线条图标（Lucide 风格，currentColor；不使用 emoji，唯一例外是 logo 的鲸鱼）
const ICONS = {
  chat:'<path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/>',
  heart:'<path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/>',
  check:'<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>',
  wrench:'<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>',
  zap:'<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>',
  clock:'<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
  brain:'<path d="M9.5 2A2.5 2.5 0 0 1 12 4.5v15a2.5 2.5 0 0 1-4.96.44 2.5 2.5 0 0 1-2.96-3.08 3 3 0 0 1-.34-5.58 2.5 2.5 0 0 1 1.32-4.24 2.5 2.5 0 0 1 1.98-3A2.5 2.5 0 0 1 9.5 2Z"/><path d="M14.5 2A2.5 2.5 0 0 0 12 4.5v15a2.5 2.5 0 0 0 4.96.44 2.5 2.5 0 0 0 2.96-3.08 2.5 2.5 0 0 0-.34-5.58 2.5 2.5 0 0 0-1.32-4.24 2.5 2.5 0 0 0-1.98-3A2.5 2.5 0 0 0 14.5 2Z"/>',
  search:'<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
  compass:'<circle cx="12" cy="12" r="10"/><polygon points="16.24 7.76 14.12 14.12 7.76 16.24 9.88 9.88 16.24 7.76"/>',
  eye:'<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>',
  info:'<circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/>',
  xcircle:'<circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/>',
  alert:'<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
  hourglass:'<path d="M5 22h14M5 2h14M17 22v-4.17a2 2 0 0 0-.59-1.42L12 12l-4.41 4.41A2 2 0 0 0 7 17.83V22M7 2v4.17a2 2 0 0 0 .59 1.42L12 12l4.41-4.41A2 2 0 0 0 17 6.17V2"/>',
  dollar:'<line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>',
  gauge:'<path d="M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z"/><path d="M16.24 7.76a6 6 0 0 1 0 8.49M7.76 16.25a6 6 0 0 1 0-8.49M19.07 4.93a10 10 0 0 1 0 14.14M4.93 19.07a10 10 0 0 1 0-14.14"/>',
  shield:'<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
  layers:'<polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/>',
};
function icon(name, size){
  return '<svg class="ic" width="'+(size||17)+'" height="'+(size||17)+'" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+(ICONS[name]||"")+'</svg>';
}

const TOOL_NAME = {
  open_app:"打开应用", open_website:"打开网址", date_info:"查询日期",
  list_desktop_files:"查看桌面文件",
};
const STATUS_CN = {success:"成功", failed:"失败", running:"进行中", cancelled:"已取消"};
const MODE_CN = {react:"智能体", single:"单轮对话"};

function esc(s){return String(s==null?"":s)
  .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
  .replace(/"/g,"&quot;").replace(/'/g,"&#39;");}

function miniMd(text){
  let h = esc(text);
  h = h.replace(/(?:\x60){3}[\s\S]*?(?:\x60){3}/g, m => "<pre>"+esc(m.replace(/(?:\x60){3}[^\n]*\n?|(?:\x60){3}/g,""))+"</pre>");
  h = h.replace(/\x60([^\x60]+)\x60/g, "<code>$1</code>");
  h = h.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  h = h.replace(/\n/g, "<br>");
  return h;
}

function timeAgo(ts){
  if(!ts) return "—";
  const d = Date.now()/1000 - ts;
  if(d<60) return "刚刚";
  if(d<3600) return Math.floor(d/60)+" 分钟前";
  if(d<86400) return Math.floor(d/3600)+" 小时前";
  if(d<86400*7) return Math.floor(d/86400)+" 天前";
  const dt=new Date(ts*1000);
  return (dt.getMonth()+1)+"月"+dt.getDate()+"日";
}
function fmtTime(ts){
  if(!ts) return "—";
  const d=new Date(ts*1000);
  const p=n=>String(n).padStart(2,"0");
  return d.getFullYear()+"-"+p(d.getMonth()+1)+"-"+p(d.getDate())+" "+p(d.getHours())+":"+p(d.getMinutes())+":"+p(d.getSeconds());
}
function fmtDur(s){
  if(s==null||isNaN(s)) return "—";
  if(s<1) return Math.round(s*1000)+" ms";
  if(s<60) return s.toFixed(1)+" 秒";
  return Math.floor(s/60)+" 分 "+Math.round(s%60)+" 秒";
}
function fmtNum(n){
  const v=Number(n||0);
  return v.toLocaleString("zh-CN");
}
function fmtBytes(n){
  const v=Number(n||0);
  if(v<1024) return v+" B";
  if(v<1048576) return (v/1024).toFixed(1)+" KB";
  if(v<1073741824) return (v/1048576).toFixed(1)+" MB";
  return (v/1073741824).toFixed(2)+" GB";
}
function pct(x){ return (Math.round(Number(x||0)*10000)/100)+"%"; }
function badge(status){
  return '<span class="badge '+status+'">'+(STATUS_CN[status]||esc(status))+'</span>';
}
function kpi(k,v,x,cls){
  return '<div class="kpi"><div class="k">'+esc(k)+'</div><div class="v '+(cls||"")+'">'+v+'</div>'
    + '<div class="x">'+(x||"")+'</div></div>';
}
function barRow(name, val, total, color){
  const p=total? Math.round(val/total*100) : 0;
  return '<div class="bar-row">'
    + '<div class="name">'+name+'</div>'
    + '<div class="track"><div class="fill" style="width:'+Math.max(p,val?6:0)+'%;'+(color?('background:'+color+';'):"")+'">'+val+'</div></div>'
    + '</div>';
}
function emptyBox(text, ico){
  return '<div class="empty" style="min-height:180px;grid-column:1/-1;"><div class="big">'+icon(ico||"info",40)+'</div><div>'+esc(text)+'</div></div>';
}

const state = {
  stats:null, runs:[], selectedId:null, filter:"all", kw:"",
  detail:null, auto:true, companion:null, tab:"runs",
  memPreview:null, maintOn:false,
};

async function api(path){
  const r = await fetch(path);
  if(!r.ok) throw new Error(path+" "+r.status);
  return r.json();
}
async function apiRaw(path, opts){
  const r = await fetch(path, opts);
  let body=null;
  try{ body = await r.json(); }catch(_){ body=null; }
  if(!r.ok){
    const msg = (body && (body.detail||body.message)) || (r.status+" "+r.statusText);
    throw new Error(String(msg));
  }
  return body;
}

/* ------------------------------------------------------------------
 * 顶部卡片（8 张）：/api/overview 一次拿全
 * ------------------------------------------------------------------ */
function fmtCardValue(label, value, est){
  if(label.indexOf("成功率")>=0 || label.indexOf("命中率")>=0) return String(value);
  if(label.indexOf("耗时")>=0) return fmtDur(value);
  if(label.indexOf("花费")>=0) return "$"+Number(value||0).toFixed(6)+(est?'<span class="est-tag">估算</span>':"");
  if(typeof value==="number") return fmtNum(value);
  return esc(value);
}
function renderCards(ov){
  if(!ov || !ov.cards) return;
  const cls=["violet","green","blue","amber","violet","blue","amber","green"];
  const ico=["chat","check","wrench","zap","clock","layers","dollar","gauge"];
  $("#stats").innerHTML = ov.cards.map(function(c,i){
    return '<div class="stat '+cls[i%cls.length]+'"><div class="bar"></div>'
    + '<div class="ico">'+icon(ico[i%ico.length])+'</div>'
    + '<div class="num">'+fmtCardValue(c.label,c.value,c.estimated)+'</div>'
    + '<div class="label">'+esc(c.label)+(c.estimated?' <span class="est-tag">estimated</span>':"")+'</div>'
    + '</div>';
  }).join("");
}

/* ------------------------------------------------------------------
 * Tab 运行：/api/stats /api/runs /api/runs/{rid}
 * ------------------------------------------------------------------ */
function renderRunList(){
  const box=$("#runList");
  let list=state.runs;
  if(state.filter!=="all") list=list.filter(r=>r.status===state.filter);
  if(state.kw) list=list.filter(r=>(r.goal||"").toLowerCase().includes(state.kw));
  if(!list.length){
    box.innerHTML='<div class="empty" style="min-height:200px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;">'+icon("search",42)+'<div>没有符合条件的会话</div></div>';
    return;
  }
  const scrollTop=box.scrollTop;
  box.innerHTML=list.map(function(r){
    const tags=[];
    if(r.tool_calls>0) tags.push('<span class="mini-tag">'+icon("wrench",12)+' '+r.tool_calls+'</span>');
    if(r.total_steps>0) tags.push('<span class="mini-tag">'+icon("zap",12)+' '+r.total_steps+'</span>');
    return '<div class="run-item '+(r.id===state.selectedId?"active":"")+'" data-id="'+r.id+'">'
      + '<div class="goal">'+esc(r.goal||"(无内容)")+'</div>'
      + '<div class="meta">'+badge(r.status)+'<span class="t">'+timeAgo(r.started_at)+'</span>'+tags.join("")+'</div>'
      + '</div>';
  }).join("");
  box.scrollTop=scrollTop;
}

function pickStreamText(events){
  let acc="";
  for(const e of events){
    if(e.kind==="text" && e.payload && e.payload.accumulated!=null)
      acc=e.payload.accumulated;
  }
  return acc;
}

function renderToolCard(p){
  const name=p.name||p.tool_name||"?";
  const cn=TOOL_NAME[name]||name;
  const args=p.args||p.arguments||{};
  let kvHtml="";
  try{
    const keys=Object.keys(args);
    if(keys.length){
      kvHtml='<div class="kv">'+keys.map(function(k){
        return '<span class="pair"><span class="k">'+esc(k)+'</span><span class="v">'+esc(typeof args[k]==="object"?JSON.stringify(args[k]):args[k])+'</span></span>';
      }).join("")+'</div>';
    }
  }catch(_){}
  let result=p.result;
  if(result!=null && typeof result==="object") result=JSON.stringify(result,null,2);
  let resultHtml="";
  if(result!=null && result!==""){
    const rid="res"+Math.random().toString(36).slice(2,8);
    resultHtml='<div class="result-box" id="'+rid+'">'+esc(String(result))+'</div>'
      + '<button class="expand-btn" onclick="toggleResult(&#39;'+rid+'&#39;,this)">展开结果 ▾</button>';
  }
  return '<div class="tl-card tool">'
    + '<div class="cap">'+icon("wrench")+' '+esc(cn)+'<span class="mini-tag">'+esc(name)+'</span></div>'
    + kvHtml+resultHtml
    + '</div>';
}

function renderPlanCard(p){
  const steps=p.steps||[];
  return '<div class="tl-card">'
    + '<div class="cap" style="color:#1d4ed8;">'+icon("compass")+' 执行计划</div>'
    + '<ol style="margin:4px 0 0;padding-left:20px;">'
    + steps.map(function(s){
        return '<li style="margin-bottom:6px;font-size:12.5px;line-height:1.6;">'
        + '<div>'+esc(s.thought||"")+'</div>'
        + (s.tool_name?'<span class="mini-tag" style="margin-top:3px;">'+icon("wrench",12)+' '+esc(TOOL_NAME[s.tool_name]||s.tool_name)+'</span>':"")
        + '</li>';
      }).join("")
    + '</ol></div>';
}

function renderDetail(r){
  const ev=r.events||[];
  const errEv=ev.find(e=>e.kind==="error");
  const isFail=r.status==="failed" || !!errEv;
  const answer=(r.final_answer||"").replace(/^\[错误\]\s*/,"");
  const errMsg=errEv ? (errEv.payload&&errEv.payload.message || JSON.stringify(errEv.payload)) : "";
  const a=answer.trim(), m=errMsg.trim();
  const answerDupError=!!m && !!a && (a===m || a.includes(m) || m.includes(a));
  const dur=r.finished_at? r.finished_at-r.started_at:null;
  const model=r.meta&&r.meta.model;

  const tl=ev.filter(e=>["plan","tool","reflection","meta"].includes(e.kind));
  let tlHtml;
  if(tl.length){
    tlHtml='<div class="timeline">'+tl.map(function(e){
      const p=e.payload||{};
      if(e.kind==="tool")
        return '<div class="tl-item tool"><div class="node">'+icon("wrench")+'</div>'+renderToolCard(p)+'</div>';
      if(e.kind==="plan")
        return '<div class="tl-item plan"><div class="node">'+icon("compass")+'</div>'+renderPlanCard(p)+'</div>';
      if(e.kind==="reflection")
        return '<div class="tl-item reflection"><div class="node">'+icon("eye")+'</div>'
          + '<div class="tl-card"><div class="cap" style="color:var(--accent-d);">'+icon("eye")+' 反思评估</div>'
          + '<div style="font-size:12.5px;">'+esc(p.comment||"")+' '
          + (p.verdict?'<span class="mini-tag" style="margin-left:6px;">'+esc(p.verdict)+'</span>':"")+'</div></div></div>';
      if(e.kind==="meta"){
        const label=p.event==="force_retry"?"自动重试":p.event||"内部事件";
        const note=p.reason||p.intent||"";
        return '<div class="tl-item meta"><div class="node">'+icon("info")+'</div>'
          + '<div class="tl-card"><div class="cap" style="color:var(--sub);">'+icon("info")+' '+esc(label)+'</div>'
          + '<div style="font-size:12.5px;color:var(--sub);">'+esc(note)+'</div></div></div>';
      }
      return "";
    }).join("")+'</div>';
  } else {
    tlHtml='<div class="hint">'+icon("chat")+' 这是一条普通对话，没有调用工具或额外步骤。</div>';
  }

  const stream=pickStreamText(ev);
  const streamHtml = stream && stream!==answer
    ? '<details class="raw"><summary>查看生成过程（流式文本）</summary><pre>'+esc(stream)+'</pre></details>' : "";
  const rawHtml='<details class="raw"><summary>原始事件（'+ev.length+' 条 · 开发者）</summary>'
    + '<pre>'+esc(ev.map(function(e){return '#'+e.seq+' ['+e.kind+'] '+JSON.stringify(e.payload,null,2);}).join("\n\n"))+'</pre></details>';

  const finish=ev.find(e=>e.kind==="finish");
  const emo=finish&&finish.payload&&finish.payload.emotion;

  $("#detailPanel").innerHTML='<div class="detail">'
    + '<div class="d-head"><h2>'+esc(r.goal||"(无内容)")+'</h2>'+badge(r.status)+'</div>'
    + '<div class="d-meta">'
      + '<span class="meta-cell">'+icon("clock")+' '+fmtTime(r.started_at)+'</span>'
      + '<span class="meta-cell">'+icon("clock")+' 耗时 <b>'+fmtDur(dur)+'</b></span>'
      + '<span class="meta-cell">模式 <b>'+(MODE_CN[r.mode]||esc(r.mode))+'</b></span>'
      + '<span class="meta-cell">'+icon("zap")+' '+r.total_steps+' 步 · '+icon("wrench")+' '+r.tool_calls+' 次</span>'
      + (model?'<span class="meta-cell">模型 <b>'+esc(model)+'</b></span>':"")
      + (emo?'<span class="meta-cell">情绪 <b>'+esc(emo)+'</b></span>':"")
    + '</div>'
    + '<div id="runCompanionBlock">'+renderCompanionForRun(r)+'</div>'
    + (isFail&&errMsg?'<div class="section-title">'+icon("xcircle")+' 错误信息</div>'
        + '<div class="error-banner"><span>'+icon("alert")+'</span><div>'+esc(errMsg)+'</div></div>':"")
    + '<div class="section-title">'+icon("chat")+' 最终回答</div>'
    + (answerDupError
        ? '<div class="hint">'+icon("alert")+' 本次生成在出错前未产生有效回答，原因见上方错误信息。</div>'
        : '<div class="answer '+(r.status==="failed"?"error":"")+'">'+miniMd(answer)||"(空)"+'</div>')
    + '<div class="section-title">'+icon("compass")+' 执行过程</div>'
    + tlHtml+streamHtml+rawHtml
    + '</div>';
}

async function selectRun(id){
  state.selectedId=id;
  renderRunList();
  $("#detailPanel").innerHTML='<div class="empty"><div class="big">'+icon("hourglass",40)+'</div><div>加载中…</div></div>';
  try{
    const r=await api("/api/runs/"+id);
    if(state.selectedId===id){ state.detail=r; renderDetail(r); }
  }catch(e){
    $("#detailPanel").innerHTML='<div class="empty"><div class="big">'+icon("alert",40)+'</div><div>加载失败：'+esc(e.message)+'</div></div>';
  }
}

/* ------------------------------------------------------------------
 * Tab 成本：/api/tokens/summary /api/tokens/series /api/tokens/turns
 * ------------------------------------------------------------------ */
function renderCost(summary, series, turns){
  const t=summary||{};
  const cost=t.cost||{};
  $("#costSource").textContent=t.source||"";
  $("#costErr").innerHTML = t.error
    ? '<div class="hint amber" style="margin-top:12px;">'+icon("alert")+' '+esc(t.error)+'</div>' : "";
  const hit=Number(t.cache_hit_tokens||0), miss=Number(t.cache_miss_tokens||0);
  $("#costKpis").innerHTML=[
    kpi("累计 token", fmtNum(t.total_tokens), "prompt "+fmtNum(t.prompt_tokens)+" / completion "+fmtNum(t.completion_tokens)),
    kpi("缓存命中率", pct(t.cache_hit_rate), "命中 "+fmtNum(hit)+" / 未命中 "+fmtNum(miss), "green"),
    kpi("请求数", fmtNum(t.requests), "会话数 "+fmtNum(t.sessions)),
    kpi("每轮平均 token", fmtNum(t.avg_tokens_per_turn), "按请求平均"),
    kpi("累计花费（估算）", "$"+Number(cost.amount||0).toFixed(6),
        (cost.price_source||"placeholder")+" · estimated="+(cost.estimated!==false), "amber"),
    kpi("缓存写入 token", fmtNum(t.cache_write_tokens), "cache_write_tokens"),
  ].join("");

  const items=(series&&series.items)||[];
  if(!items.length){
    $("#costSeries").innerHTML='<div class="hint">还没有 token 账本数据。</div>';
  }else{
    const maxTotal=Math.max.apply(null,items.map(i=>Number(i.total_tokens||0)))||1;
    $("#costSeries").innerHTML=items.slice(-40).map(function(i){
      const rate=Math.round(Number(i.cache_hit_rate||0)*100);
      return '<div class="bar-row">'
        + '<div class="name" style="font-family:Consolas;font-size:11.5px;">'+esc(fmtTime(i.ts/1000))+'</div>'
        + '<div class="track"><div class="fill" style="width:'+Math.max(Math.round(i.total_tokens/maxTotal*100),4)+'%">'+fmtNum(i.total_tokens)+'</div></div>'
        + '<div style="width:130px;text-align:right;font-family:Consolas;font-size:11.5px;color:var(--sub);">命中 '+rate+'% · '+fmtNum(i.requests)+' 次</div>'
        + '</div>';
    }).join("");
  }

  const brk=[];
  const bySession=t.by_session||{}, byModel=t.by_model||{}, byBackend=t.by_backend||{};
  [["会话",bySession],["模型",byModel],["后端",byBackend]].forEach(function(pair){
    const label=pair[0], table=pair[1]||{};
    const rows=Object.keys(table).map(function(k){return [k,table[k]];})
      .sort(function(a,b){return (b[1].total_tokens||0)-(a[1].total_tokens||0);});
    if(!rows.length) return;
    brk.push('<div class="sub-head" style="margin-top:14px;font-size:13px;">按'+label+'</div>'
      + '<div class="tbl-wrap"><table class="tbl"><thead><tr><th>'+label+'</th><th class="num">请求</th>'
      + '<th class="num">token</th><th class="num">命中</th><th class="num">未命中</th><th class="num">命中率</th>'
      + '<th class="num">花费(估)</th></tr></thead><tbody>'
      + rows.map(function(row){
          const k=row[0], v=row[1];
          return '<tr><td class="name">'+esc(k||"(无)")+'</td>'
            + '<td class="num">'+fmtNum(v.requests)+'</td><td class="num">'+fmtNum(v.total_tokens)+'</td>'
            + '<td class="num">'+fmtNum(v.cache_hit_tokens)+'</td><td class="num">'+fmtNum(v.cache_miss_tokens)+'</td>'
            + '<td class="num">'+pct(v.cache_hit_rate)+'</td>'
            + '<td class="num">$'+Number((v.cost||{}).amount||0).toFixed(6)+'</td></tr>';
        }).join("")
      + '</tbody></table></div>');
  });
  $("#costBreak").innerHTML = brk.join("") || '<div class="hint">台账里还没有会话/模型拆分。</div>';

  const turnsItems=(turns&&turns.items)||[];
  if(!turnsItems.length){
    $("#costTurns").innerHTML='<tbody><tr><td style="color:var(--faint);">暂无逐请求明细</td></tr></tbody>';
  }else{
    const rows=turnsItems.slice().sort(function(a,b){return (b.total_tokens||0)-(a.total_tokens||0);}).slice(0,100);
    $("#costTurns").innerHTML='<thead><tr><th>时间</th><th>session</th><th>#</th><th>后端</th><th>模型</th>'
      + '<th class="num">prompt</th><th class="num">completion</th><th class="num">total</th>'
      + '<th class="num">命中</th><th class="num">未命中</th><th class="num">命中率</th><th class="num">花费(估)</th></tr></thead>'
      + '<tbody>'+rows.map(function(r){
          return '<tr>'
            + '<td class="num">'+esc(fmtTime(r.ts))+'</td>'
            + '<td class="name">'+esc(r.session_id||"")+'</td>'
            + '<td class="num">'+r.request_index+'</td>'
            + '<td>'+esc(r.backend||"")+'</td><td>'+esc(r.model||"")+'</td>'
            + '<td class="num">'+fmtNum(r.prompt_tokens)+'</td>'
            + '<td class="num">'+fmtNum(r.completion_tokens)+'</td>'
            + '<td class="num">'+fmtNum(r.total_tokens)+'</td>'
            + '<td class="num">'+fmtNum(r.cache_hit_tokens)+'</td>'
            + '<td class="num">'+fmtNum(r.cache_miss_tokens)+'</td>'
            + '<td class="num">'+pct(r.cache_hit_rate)+'</td>'
            + '<td class="num">$'+Number((r.cost||{}).amount||0).toFixed(6)+'</td></tr>';
        }).join("")+'</tbody>';
  }
}

/* ------------------------------------------------------------------
 * Tab 记忆：/api/memory（live 库）+ /api/memory/merge-preview
 * ------------------------------------------------------------------ */
function renderMemory(data){
  const items=(data&&data.items)||[];
  const live=(data&&data.live)||{};
  const legacy=(data&&data.legacy)||{};
  const isLive = (data&&data.store)==="live";
  $("#memCount").textContent=(isLive?"live 库 ":"")+items.length+" 条";
  $("#memStoreHint").innerHTML =
    '<div class="hint '+(isLive?"ok":"warn")+'">'
    + icon(isLive?"check":"alert")
    + ' 当前展示的是 <b>'+(isLive?"live 库（桌宠真正在用的那份）":"legacy 库（live 库不存在时的回落）")+'</b>：'
    + '<span class="name">'+esc(live.path||"")+'</span></div>'
    + '<div class="hint" style="margin-top:8px;">'+icon("info")+' '+esc((data&&data.note)||"")+'</div>';

  $("#memKpis").innerHTML=[
    kpi("live 库行数", fmtNum(live.count||0), esc(live.path||"")),
    kpi("legacy 库行数", fmtNum(legacy.count||0), esc(legacy.path||"")),
    kpi("access_count 命中数", fmtNum(live.access_hits||0), "被召回过的记忆条数", "green"),
    kpi("重复 / 冲突", fmtNum((data&&data.duplicate_count)||0), "冲突 "+fmtNum((data&&data.conflict_count)||0), "amber"),
  ].join("");

  const byKind=live.by_kind||{}, byImp=live.by_importance||{};
  const kTotal=Object.keys(byKind).reduce(function(a,k){return a+byKind[k];},0);
  const iTotal=Object.keys(byImp).reduce(function(a,k){return a+byImp[k];},0);
  let dist="";
  if(!kTotal && !iTotal){
    dist='<div class="hint">live 库里还没有记忆。</div>';
  }else{
    dist = '<div class="sub-head" style="margin-top:6px;font-size:13px;">按 kind / category</div>'
      + (Object.keys(byKind).length
        ? Object.keys(byKind).map(function(k){return barRow(esc(k||"other"),byKind[k],kTotal);}).join("")
        : '<div class="hint">没有 kind 分布。</div>')
      + '<div class="sub-head" style="margin-top:14px;font-size:13px;">按 importance</div>'
      + (Object.keys(byImp).length
        ? Object.keys(byImp).sort(function(a,b){return Number(b)-Number(a);}).map(function(k){return barRow("重要性 "+esc(k),byImp[k],iTotal);}).join("")
        : '<div class="hint">没有 importance 分布。</div>');
  }
  $("#memDist").innerHTML=dist;

  const dup=(data&&data.duplicates)||[];
  if(!dup.length){
    $("#memDuplicates").innerHTML='<div class="hint ok">'+icon("check")+' 没有发现 legacy 重复/冲突。</div>';
  }else{
    $("#memDuplicates").innerHTML='<div class="tbl-wrap"><table class="tbl">'
      + '<thead><tr><th>分组</th><th>关系</th><th>相似度</th><th>legacy 内容</th><th>live 内容</th><th>处理</th></tr></thead>'
      + '<tbody>'+dup.map(function(d){
          return '<tr>'
            + '<td>'+esc(d.group||"—")+'</td>'
            + '<td><span class="mini-tag '+(d.relation==="conflict"?"warn":(d.relation==="duplicate"?"ok":""))+'">'+esc(d.relation)+'</span></td>'
            + '<td class="num">'+Number(d.similarity||0).toFixed(2)+'</td>'
            + '<td>'+esc(d.legacy_content||"")+'</td>'
            + '<td>'+esc(d.live_content||"—")+'</td>'
            + '<td>'+esc(d.note||"")+'</td></tr>';
        }).join("")+'</tbody></table></div>';
  }

  if(!items.length){
    $("#memGrid").innerHTML=emptyBox("还没有记住任何事","brain");
    return;
  }
  $("#memGrid").innerHTML=items.map(function(m){
    return '<div class="mem">'
      + '<div class="content">'+esc(m.content)+'</div>'
      + '<div class="foot">'
      + '<span class="cat-tag">'+esc(m.category||m.kind||"未分类")+'</span>'
      + '<span>重要性 '+Number(m.importance||0).toFixed(2)+'</span>'
      + '<span>'+fmtTime(m.created_at)+'</span>'
      + '<span class="mini-tag '+(Number(m.access_count||0)>0?"ok":"")+'">命中 '+(m.access_count||0)+'</span>'
      + '</div></div>';
  }).join("");
}

function renderMergePreview(pv){
  state.memPreview=pv||null;
  const btn=$("#btnMergeRun");
  if(!pv){
    $("#mergePreview").innerHTML='<div class="hint">还没有预览。</div>';
    btn.disabled=true;
    return;
  }
  function sec(title,arr,cls){
    const list=arr||[];
    return '<div class="sub-head" style="margin-top:10px;font-size:13px;">'+title
      + ' <span class="mini-tag '+(cls||"")+'">'+list.length+'</span></div>'
      + (list.length ? list.map(function(x){
          return '<div class="config-block">'+esc(x.legacy_content||x.content||"")
            + (x.note?'<br><span style="color:var(--sub);">'+esc(x.note)+'</span>':"")+'</div>';
        }).join("") : '<div class="hint">无</div>');
  }
  $("#mergePreview").innerHTML=
      '<div class="hint">live '+pv.live_count+' 条 · legacy '+pv.legacy_count+' 条</div>'
    + sec("将并入 live",pv.will_insert,"ok")
    + sec("保留 live（legacy 丢弃）",pv.will_keep_live)
    + sec("两条都留（冲突）",pv.will_keep_both,"warn")
    + sec("直接丢弃（占位符）",pv.will_discard,"warn");
  btn.disabled = !((pv.will_insert||[]).length || (pv.will_keep_both||[]).length);
}

/* ------------------------------------------------------------------
 * Tab 决策：/api/companion
 * ------------------------------------------------------------------ */
function renderDecision(c){
  if(!c){ $("#decisionList").innerHTML='<div class="hint warn">决策日志读取失败。</div>'; return; }
  $("#decisionSource").textContent=c.persist_path||"";
  const st=c.state||{};
  const q=st.quantize||{};
  $("#decisionKpis").innerHTML=[
    kpi("等级", "Lv."+(st.level||1), "EXP "+Number(st.exp||0).toFixed(1)),
    kpi("心情", Number(st.feeling||0).toFixed(1), q.mood||""),
    kpi("饱食", Number(st.strength_food||0).toFixed(1), q.satiety||""),
    kpi("好感", Number(st.likability||0).toFixed(1), q.favor||""),
    kpi("最近决策数", fmtNum((c.decisions||[]).length), "companion_log.txt"),
    kpi("最近结果数", fmtNum((c.outcomes||[]).length), "companion_log.txt"),
  ].join("");

  const decs=c.decisions||[];
  $("#decisionList").innerHTML = decs.length ? decs.map(function(d){
    return '<div class="trace-row decision">'
      + '<div class="ts">'+esc(fmtTime(d.ts))+'</div>'
      + '<div class="body">'+esc(d.tool||d.action||"")+' · '+esc(d.decision||d.result||"")+' '
      + (d.rule?'<span class="mini-tag">'+esc(d.rule)+'</span>':"")
      + (d.thread_id?'<span class="mini-tag">'+esc(d.thread_id)+'</span>':"")+'</div>'
      + '<div class="arrow">'+esc(d.reason||"")+'</div></div>';
  }).join("") : '<div class="hint">还没有决策记录。</div>';

  const outs=c.outcomes||[];
  $("#outcomeList").innerHTML = outs.length ? outs.map(function(o){
    return '<div class="trace-row outcome">'
      + '<div class="ts">'+esc(fmtTime(o.ts))+'</div>'
      + '<div class="body">'+esc(o.tool||"")+' · Δ心情 '+Number(o.feeling_delta||0).toFixed(1)
      + ' · Δ饱食 '+Number(o.food_delta||0).toFixed(1)
      + ' · Δ好感 '+Number(o.favor_delta||0).toFixed(1)
      + ' · ΔEXP '+Number(o.exp_delta||0).toFixed(1)+'</div>'
      + '<div class="arrow">Lv.'+esc(o.level_before)+'→'+esc(o.level_after)+'</div></div>';
  }).join("") : '<div class="hint">还没有结果记录。</div>';

  const cfg=c.config||{};
  $("#decisionConfig").innerHTML=Object.keys(cfg).map(function(k){
    const v=cfg[k];
    return '<div class="config-block"><b>'+esc(k)+'</b>：'+esc(typeof v==="object"?JSON.stringify(v):v)+'</div>';
  }).join("");
}

/* ------------------------------------------------------------------
 * Tab 工具：/api/tools + /api/reconciliation
 * ------------------------------------------------------------------ */
function renderTools(inv, recon){
  const tools=(inv&&inv.tools)||[];
  const servers=(inv&&inv.mcp_by_server)||{};
  $("#toolCount").textContent=((inv&&inv.count)||0)+" 个";
  $("#toolErr").innerHTML = (inv && inv.available===false)
    ? '<div class="hint warn" style="margin-top:12px;">'+icon("alert")+' 工具注册表读取失败：对账脚本不可用。</div>' : "";

  const mcpNames=Object.keys(servers);
  $("#toolKpis").innerHTML=[
    kpi("工具总数", fmtNum((inv&&inv.count)||0), "61 内置 + 7 MCP", "violet"),
    kpi("内置工具", fmtNum((inv&&inv.builtin_count)||0), "app/engine/tools/*.py"),
    kpi("MCP 工具", fmtNum((inv&&inv.mcp_count)||0),
        mcpNames.map(function(s){return s+" "+(servers[s]||[]).length;}).join(" · "), "blue"),
    kpi("已登记解锁表", fmtNum((inv&&inv.unlock_total)||0),
        "MCP 默认解锁等级 "+((inv&&inv.mcp_default_level)!=null?(inv.mcp_default_level):8)),
    kpi("调用过的工具", fmtNum(tools.filter(function(t){return t.calls>0;}).length), "来自 traces.db"),
    kpi("对账差异", fmtNum((recon&&recon.error_count)||0),
        (recon&&recon.available===false)?"对账不可用":"阻断类差异", "amber"),
  ].join("");

  const riskCount={};
  for(const t of tools){
    const k=t.risk_label||"UNKNOWN";
    riskCount[k]=(riskCount[k]||0)+1;
  }
  const rTotal=tools.length||1;
  $("#toolRisk").innerHTML = Object.keys(riskCount).length
    ? Object.keys(riskCount).sort().map(function(k){return barRow(esc(k),riskCount[k],rTotal);}).join("")
    : '<div class="hint">没有工具数据。</div>';

  if(!tools.length){
    $("#toolTable").innerHTML='<tbody><tr><td style="color:var(--faint);">没有工具数据</td></tr></tbody>';
  }else{
    $("#toolTable").innerHTML='<thead><tr><th>工具</th><th>来源</th><th>MCP server</th><th>风险等级</th>'
      + '<th>解锁等级</th><th class="num">调用</th><th class="num">失败</th><th class="num">失败率</th></tr></thead>'
      + '<tbody>'+tools.map(function(t){
          return '<tr>'
            + '<td class="name">'+esc(t.name)+'</td>'
            + '<td>'+(t.source==="mcp"?'<span class="mini-tag">MCP</span>':"内置")+'</td>'
            + '<td>'+esc(t.server||"—")+'</td>'
            + '<td>'+esc(t.risk_label||"—")+(t.risk_level!=null?' <span class="mini-tag">'+t.risk_level+'</span>':"")+'</td>'
            + '<td>'+(t.unlock_level==null?"—":esc(t.unlock_level))+'</td>'
            + '<td class="num">'+fmtNum(t.calls)+'</td>'
            + '<td class="num">'+fmtNum(t.failures)+'</td>'
            + '<td class="num">'+pct(t.failure_rate)+'</td></tr>';
        }).join("")+'</tbody>';
  }

  if(!recon || recon.available===false){
    $("#reconBox").innerHTML='<div class="hint warn">'+icon("alert")+' 对账结果不可用：'+esc((recon&&recon.error)||"未知原因")+'</div>';
    return;
  }
  const srcs=recon.sources||{};
  let html='<div class="hint">对账脚本：scripts/check_tool_docs.py · 注册表 '+fmtNum(recon.live_count)+' 个 · 阻断类差异 '+fmtNum(recon.error_count)+' 条</div>';
  Object.keys(srcs).forEach(function(name){
    const src=srcs[name]||{};
    const onlySrc=src.only_in_source||[], onlyLive=src.only_in_live||[];
    html+='<div class="sub-head" style="margin-top:12px;font-size:13px;">'+esc(name)
      + ' <span class="mini-tag">共有 '+fmtNum(src.both)+'</span>'
      + ' <span class="mini-tag warn">文档多 '+onlySrc.length+'</span>'
      + ' <span class="mini-tag ok">注册表多 '+onlyLive.length+'</span></div>';
    if(!onlySrc.length && !onlyLive.length){
      html+='<div class="hint ok">'+icon("check")+' 两边一致。</div>';
    }else{
      html+='<div class="config-block"><b>只在文档里：</b>'+esc(onlySrc.join(", ")||"—")+'</div>'
        + '<div class="config-block"><b>只在注册表里：</b>'+esc(onlyLive.join(", ")||"—")+'</div>';
    }
  });
  const ph=(recon.phantoms)||[];
  if(ph.length){
    html+='<div class="sub-head" style="margin-top:12px;font-size:13px;">幽灵工具（被引用但注册表里没有）</div>'
      + ph.map(function(p){return '<div class="config-block"><b>'+esc(p.name)+'</b>：'+esc((p.refs||[]).join(", "))+'</div>';}).join("");
  }
  const errs=(recon.errors)||[];
  if(errs.length){
    html+='<div class="sub-head" style="margin-top:12px;font-size:13px;">阻断类差异</div>'
      + errs.map(function(e){return '<div class="error-banner"><span>'+icon("alert")+'</span><div>'+esc(typeof e==="string"?e:JSON.stringify(e))+'</div></div>';}).join("");
  }
  $("#reconBox").innerHTML=html;
}

/* ------------------------------------------------------------------
 * Tab 配置：/api/config（API key 只显示 configured / length / valid）
 * ------------------------------------------------------------------ */
function renderConfig(cfg){
  if(!cfg){ $("#configModel").innerHTML='<div class="hint warn">配置读取失败。</div>'; return; }
  $("#configPath").textContent=cfg.config_path||"";
  $("#configErr").innerHTML = cfg.error
    ? '<div class="hint amber" style="margin-bottom:10px;">'+icon("alert")+' '+esc(cfg.error)+'</div>' : "";
  $("#configModel").innerHTML=
      '<div class="config-block"><b>模型</b>：'+esc(cfg.model||"—")+'</div>'
    + '<div class="config-block"><b>base_url</b>：'+esc(cfg.base_url||"—")+'</div>'
    + '<div class="config-block"><b>backend</b>：'+esc(cfg.backend||"—")+'</div>'
    + '<div class="config-block"><b>temperature</b>：'+esc(cfg.temperature==null?"—":cfg.temperature)
    + ' · <b>max_tokens</b>：'+esc(cfg.max_tokens==null?"—":cfg.max_tokens)
    + ' · <b>stream</b>：'+esc(cfg.stream==null?"—":cfg.stream)
    + ' · <b>timeout</b>：'+esc(cfg.timeout==null?"—":cfg.timeout)+'</div>';

  const k=cfg.api_key||{};
  $("#configKey").innerHTML=
      '<div class="config-block"><b>已配置</b>：'+(k.configured?"是":"否")+'</div>'
    + '<div class="config-block"><b>长度</b>：'+esc(k.length||0)+' 字符</div>'
    + '<div class="config-block"><b>看起来是有效 key</b>：'+(k.valid?"是":"否")+'</div>'
    + '<div class="hint ok" style="margin-top:8px;">'+icon("shield")+' 面板永远不返回 key 内容，只报「配没配 / 多长 / 是不是占位符」。</div>';

  const unlock=cfg.unlock||{};
  const levels=unlock.levels||{};
  const keys=Object.keys(levels);
  $("#configUnlock").innerHTML = keys.length
    ? keys.map(function(lv){
        return '<div class="sub-head" style="margin-top:8px;font-size:13px;">等级 '+esc(lv)
          + ' <span class="mini-tag">'+((levels[lv]||[]).length)+' 个</span></div>'
          + '<div class="config-block">'+esc((levels[lv]||[]).join(", "))+'</div>';
      }).join("")
    : '<div class="hint">没有解锁表。</div>';
}

/* ------------------------------------------------------------------
 * Tab 状态：/api/state
 * ------------------------------------------------------------------ */
function renderState(st){
  if(!st){ $("#stateBars").innerHTML='<div class="hint warn">状态读取失败。</div>'; return; }
  $("#statePath").textContent=st.path||"";
  $("#stateErr").innerHTML = st.error
    ? '<div class="hint amber" style="margin-bottom:10px;">'+icon("alert")+' '+esc(st.error)+'</div>'
    : (!st.exists ? '<div class="hint amber" style="margin-bottom:10px;">'+icon("alert")+' 还没有 pet_state.json（主程序可能没在跑）。</div>' : "");
  const rows=[
    ["饱食", st.strength_food, "satiety", 100],
    ["心情", st.feeling, "mood", 100],
    ["好感", st.likability, "favor", 100],
    ["等级", st.level, "level", Math.max(Number(st.level||1),1)],
    ["EXP", st.exp, "exp", 100],
  ];
  $("#stateBars").innerHTML=rows.map(function(row){
    const name=row[0], v=row[1], cls=row[2], max=row[3];
    const p=Math.max(0,Math.min(100,Number(v||0)/max*100));
    return '<div class="cs-row"><div class="name">'+name+'</div>'
      + '<div class="track"><div class="fill '+cls+'" style="width:'+p+'%">'+Number(v||0).toFixed(1)+'</div></div>'
      + '<div class="v">'+Number(v||0).toFixed(1)+'</div></div>';
  }).join("");

  const up=st.unlock_progress||{};
  $("#stateUnlock").innerHTML=
      '<div class="cs-row"><div class="name">解锁进度</div>'
    + '<div class="track"><div class="fill level" style="width:'+Number(up.percent||0)+'%">'+Number(up.percent||0).toFixed(1)+'%</div></div>'
    + '<div class="v">'+(up.unlocked_tools||0)+'/'+(up.total_tools||0)+'</div></div>'
    + '<div class="hint">当前等级 '+esc(up.level||1)+' · 已解锁 '+esc(up.unlocked_tools||0)+' / '+esc(up.total_tools||0)+' 个工具</div>';
}

/* ------------------------------------------------------------------
 * Tab 健康与维护：/api/health + /api/maintenance/status
 * ------------------------------------------------------------------ */
/* 向量索引状态的中文标签（口径见 app/brain/vector_index.py 的 derive_state） */
var VSTATE={not_built:"还没建",built:"已建好",drifted:"已漂移",unavailable:"不可用",disabled:"已关闭"};

function renderHealth(h){
  if(!h){ $("#healthKpis").innerHTML='<div class="hint warn">健康数据读取失败。</div>'; return; }
  const rl=h.rate_limited||{}, cr=h.crashes||{}, lg=h.ledger||{};
  $("#healthKpis").innerHTML=[
    kpi("429 限流", fmtNum(rl.count||0), rl.last_ts?("最近 "+timeAgo(rl.last_ts)):"没有记录", rl.count?"amber":""),
    kpi("崩溃", fmtNum(cr.count||0), cr.last_ts?("最近 "+timeAgo(cr.last_ts)):"没有记录", cr.count?"warn":""),
    kpi("账本污染", lg.polluted?"是":"否", lg.note||"session_id 看起来正常", lg.polluted?"warn":"green"),
    kpi("data/ 文件数", fmtNum((h.files||[]).length), "文件观测器只看这里"),
  ].join("");
  $("#healthErr").innerHTML = h.error ? '<div class="hint warn" style="margin-top:10px;">'+icon("alert")+' '+esc(h.error)+'</div>' : "";

  const samples=(rl.samples||[]).map(function(s){return ["429 限流",s];})
    .concat((cr.samples||[]).map(function(s){return ["崩溃",s];}));
  $("#healthSamples").innerHTML = samples.length
    ? samples.map(function(x){return '<div class="config-block"><b>'+esc(x[0])+'</b>：'+esc(x[1])+'</div>';}).join("")
    : '<div class="hint ok">'+icon("check")+' 没有限流 / 崩溃记录。</div>';

  const files=h.files||[];
  $("#healthFiles").innerHTML='<thead><tr><th>文件</th><th class="num">大小</th></tr></thead>'
    + '<tbody>'+files.map(function(f){return '<tr><td class="name">'+esc(f.name)+'</td><td class="num">'+fmtBytes(f.bytes)+'</td></tr>';}).join("")+'</tbody>';

  const vi=h.vector_index||{};
  const missing=(vi.models_missing||[]);
  const modelOk=!missing.length;
  $("#healthVector").innerHTML=[
    kpi("向量索引", VSTATE[vi.state]||vi.state||"—", vi.built?("已索引 "+(vi.n_indexed||0)+" 行"):"还没建过（用下面的按钮重建）", vi.drift?"warn":(vi.built?"green":"")),
    kpi("索引模型", vi.embed_model||"—", vi.rerank_model||"", ""),
    kpi("向量模型", modelOk?"已就绪":"没下载", modelOk?"两个模型都在本地缓存里":("缺 "+missing.length+" 个（合计 ≈1.2 GB）"), modelOk?"green":"warn"),
    kpi("与记忆库一致", vi.drift?"否":"是", vi.drift?("源库 "+(vi.n_source||0)+" 行 / 索引 "+(vi.n_indexed||0)+" 行"):("源库 "+(vi.n_source||0)+" 行"), vi.drift?"warn":"green"),
  ].join("")+(vi.notice?('<div class="hint warn" style="margin-top:10px;">'+icon("alert")+' '+esc(vi.notice).replace(/\n/g,"<br>")+'</div>'):"")+(vi.error?('<div class="hint warn" style="margin-top:10px;">'+icon("alert")+' '+esc(vi.error)+'</div>'):"");
}

function backupLine(r){
  const b=(r&&r.backup)||null;
  if(!b) return "";
  if(b.noop) return '<br>'+icon("info")+' 本次无需备份：'+esc(b.reason||"（不写 data/）");
  if(!b.archive) return b.dir?('<br>备份目录：<span class="name">'+esc(b.dir)+'</span>'):"";
  const files=b.files||[];
  let s='<br>'+icon("check")+' 归档：<span class="name">'+esc(b.archive)+'</span>'
    +'（'+esc(fmtBytes(b.bytes||0))+'，'+files.length+' 个文件）';
  s+='<br>包含：'+esc(files.join("、")||"—");
  if(b.checkpoints&&b.checkpoints.included===false){
    s+='<br>'+icon("alert")+' '+esc(b.checkpoints.reason||"");
  }
  return s;
}

async function runMaintenance(op, confirmToken, label){
  if(!state.maintOn){
    $("#maintResult").innerHTML='<div class="hint warn">'+icon("alert")+' 维护模式没打开 —— 面板默认只读。</div>';
    return;
  }
  const askMsg = op==="reconciliation"
    ? "确认执行「"+label+"」？\n\n对账只读注册表与文档，不写 data/，本次无需备份。"
    : "确认执行「"+label+"」？\n\n执行前会自动备份这次操作真正会动的文件。";
  if(!window.confirm(askMsg)) return;
  $("#maintResult").innerHTML='<div class="hint">执行中…</div>';
  try{
    let url="/api/maintenance/"+op+"?maintenance=1&confirm="+encodeURIComponent(confirmToken);
    if(op==="backup"){
      const ck=$("#maintIncludeCkpt");
      if(ck&&ck.checked) url+="&include_checkpoints=1";
    }
    const r=await apiRaw(url,{method:"POST"});
    $("#maintResult").innerHTML='<div class="hint ok">'+icon("check")+' '+esc(label)+' 完成。'
      + backupLine(r)
      + (r&&r.note?'<br>'+esc(r.note):"")+'</div>';
  }catch(e){
    $("#maintResult").innerHTML='<div class="hint warn">'+icon("alert")+' '+esc(label)+' 失败：'+esc(e.message)+'</div>';
  }
}

function toggleResult(id,btn){
  const box=$("#"+id);
  const open=box.classList.toggle("expanded");
  btn.textContent=open?"收起 ▴":"展开结果 ▾";
}

/* ------------------------------------------------------------------
 * 决策缓存（会话详情里嵌的桌宠养成反馈）
 * ------------------------------------------------------------------ */
async function loadCompanionCache(){
  try{
    state.companion = await api("/api/companion?decision_limit=200&outcome_limit=200");
  }catch(_){
    state.companion = null;
  }
}

function renderCompanionForRun(run){
  if(!state.companion){
    return '<div class="hint" style="margin:10px 0;">桌宠养成 trace 加载失败 (主程序可能没在跑 / pet_state.json 没生成).</div>';
  }
  const c = state.companion;
  const t0 = run.started_at || 0;
  const t1 = run.finished_at || (Date.now()/1000);
  const inWindow = (r) => r.ts >= t0 - 5 && r.ts <= t1 + 5;
  const threadId = run.meta && run.meta.thread_id;
  const decs = (c.decisions||[]).filter(d => inWindow(d) && (!threadId || d.thread_id === threadId || d.thread_id === "default"));
  const outs = (c.outcomes||[]).filter(o => inWindow(o) && (!threadId || o.thread_id === threadId || o.thread_id === "default"));

  const lastQ = decs.length > 0 ? decs[decs.length-1] : null;
  const qtag = (k) => lastQ ? '<span class="qtag '+(lastQ[k]||"MID")+'">'+k+'='+esc(lastQ[k]||"?")+'</span>' : "";

  let dFeeling=0, dFood=0, dFavor=0, dExp=0;
  let lv0=null, lv1=null;
  outs.forEach(o => {
    dFeeling += o.feeling_delta||0;
    dFood    += o.food_delta||0;
    dFavor   += o.favor_delta||0;
    dExp     += o.exp_delta||0;
    if (lv0===null) lv0 = o.level_before;
    lv1 = o.level_after;
  });
  const arr = (n) => n > 0 ? "+"+n.toFixed(1) : n < 0 ? n.toFixed(1) : "0";
  const lvChg = (lv0!==null && lv1!==null && lv0!==lv1) ? " Lv."+lv0+"→"+lv1 : "";
  const ruleHits = decs.map(d => '<span class="mini-tag" style="margin:2px 4px 2px 0;">'+esc(d.tool)+" "+esc(d.decision)+'</span>').join("");

  if (decs.length === 0 && outs.length === 0){
    return '<div class="hint" style="margin:10px 0;">本会话没有桌宠 trace 记录 (调的工具都 SAFE / 时间窗外 / 主程序没跑).</div>';
  }

  return '<div class="section-title" data-icon="heart">桌宠养成反馈 (本次 run)</div>'
    + '<div class="cs-compact">'
    + '<div class="cs-block"><b>调前 S = 量化</b> '+qtag("sat")+' '+qtag("mood")+' '+qtag("level")+' '+qtag("favor")+'</div>'
    + '<div class="cs-block"><b>调后 Δ</b> · 心情 '+arr(dFeeling)+' · 饱食 '+arr(dFood)+' · 好感 '+arr(dFavor)+' · EXP '+arr(dExp)+esc(lvChg)+'</div>'
    + (ruleHits ? '<div class="cs-block"><b>命中规则</b> · '+ruleHits+'</div>' : "")
    + '</div>';
}

/* ------------------------------------------------------------------
 * 各 Tab 的加载器
 * ------------------------------------------------------------------ */
const TAB_LOADERS = {
  async cost(){
    const res=await Promise.all([
      api("/api/tokens/summary"),
      api("/api/tokens/series?bucket_ms=60000"),
      api("/api/tokens/turns?limit=200"),
    ]);
    renderCost(res[0],res[1],res[2]);
  },
  async memory(){
    renderMemory(await api("/api/memory"));
  },
  async decision(){
    state.companion = await api("/api/companion?decision_limit=200&outcome_limit=200");
    renderDecision(state.companion);
  },
  async tools(){
    const res=await Promise.all([api("/api/tools"),api("/api/reconciliation")]);
    renderTools(res[0],res[1]);
  },
  async config(){
    renderConfig(await api("/api/config"));
  },
  async state(){
    renderState(await api("/api/state"));
  },
  async health(){
    const h=await api("/api/health");
    renderHealth(h);
    try{
      const st=await api("/api/maintenance/status");
      const last=(st.last_results||[]).slice(-1)[0];
      if(last){
        $("#maintResult").innerHTML='<div class="hint">'+icon("info")+' 最近一次危险操作：'+esc(last.op||"")
          +' · '+esc(new Date((last.at||0)*1000).toLocaleString())
          + backupLine(last)+'</div>';
      }
    }catch(_){}
  },
  async runs(){},
};

async function loadTab(name){
  const loader=TAB_LOADERS[name];
  if(!loader) return;
  try{
    await loader();
  }catch(e){
    const box=$("#view-"+name);
    if(box){
      const err=document.createElement("div");
      err.className="hint warn";
      err.style.margin="12px 0";
      err.textContent="加载失败："+e.message;
      box.prepend(err);
    }
  }
}

/* ------------------------------------------------------------------
 * 刷新循环
 * ------------------------------------------------------------------ */
async function refresh(){
  try{
    const res=await Promise.all([
      api("/api/overview"),
      api("/api/stats"),
      api("/api/runs?limit=100"),
    ]);
    state.stats=res[1]; state.runs=res[2];
    renderCards(res[0]);
    renderRunList();
    $("#liveText").textContent="更新于 "+new Date().toLocaleTimeString();
    const cur=state.runs.find(r=>r.id===state.selectedId);
    if(cur && cur.status==="running") selectRun(state.selectedId);
    if(state.detail) renderDetail(state.detail);
  }catch(e){
    $("#liveBox").classList.add("off");
    $("#liveText").textContent="连接失败";
  }
}

async function refreshCompanion(){
  await loadCompanionCache();
  if(state.detail) renderDetail(state.detail);
}

// 事件绑定
$$(".tab").forEach(function(t){
  t.onclick=function(){
    $$(".tab").forEach(function(x){x.classList.remove("active");});
    t.classList.add("active");
    $$(".view").forEach(function(v){v.classList.remove("active");});
    $("#view-"+t.dataset.view).classList.add("active");
    state.tab=t.dataset.view;
    loadTab(state.tab);
  };
});
$("#runList").addEventListener("click",function(e){
  const item=e.target.closest(".run-item");
  if(item) selectRun(item.dataset.id);
});
$("#filters").addEventListener("click",function(e){
  const c=e.target.closest(".chip"); if(!c)return;
  $$("#filters .chip").forEach(function(x){x.classList.remove("active");});
  c.classList.add("active"); state.filter=c.dataset.f; renderRunList();
});
$("#search").addEventListener("input",function(e){state.kw=e.target.value.trim().toLowerCase();renderRunList();});
$("#liveBox").onclick=function(){
  state.auto=!state.auto;
  $("#liveBox").classList.toggle("off",!state.auto);
  $(".dot",$("#liveBox")).classList.toggle("pulse",state.auto);
  $("#liveText").textContent=state.auto?("自动刷新中"):"已暂停";
};
$("#maintSwitch").addEventListener("change",function(e){
  state.maintOn=!!e.target.checked;
  $$(".danger .danger-btn").forEach(function(b){b.disabled=!state.maintOn;});
});
$$(".danger .danger-btn").forEach(function(b){
  b.disabled=true;
  b.onclick=function(){runMaintenance(b.dataset.op,b.dataset.confirm,b.textContent.trim());};
});
$("#btnMergePreview").onclick=async function(){
  $("#mergePreview").innerHTML='<div class="hint">预览中…</div>';
  try{
    renderMergePreview(await api("/api/memory/merge-preview"));
  }catch(e){
    $("#mergePreview").innerHTML='<div class="hint warn">'+icon("alert")+' 预览失败：'+esc(e.message)+'</div>';
  }
};
$("#btnMergeRun").onclick=async function(){
  if(!state.memPreview) return;
  if(!window.confirm("确认把 legacy 记忆并进 live 库？\n\n执行前会自动备份两个记忆库，legacy 库只归档（移动到 backups/）不删除。")) return;
  $("#mergePreview").innerHTML='<div class="hint">合并中…</div>';
  try{
    const r=await apiRaw("/api/maintenance/merge-memory?maintenance=1&confirm=merge-memory",{method:"POST"});
    var ins=(r.inserted||[]).length, mk=(r.marked||[]).length;
    $("#mergePreview").innerHTML='<div class="hint ok">'+icon("check")+' 合并完成：并入 '+fmtNum(ins)+' 条；'
      +'冲突标记 '+fmtNum(mk)+' 对；'
      +'legacy 归档到 <span class="name">'+esc(r.legacy_archived_to||"—")+'</span>（移动，不是删除）。</div>';
    renderMemory(await api("/api/memory"));
  }catch(e){
    $("#mergePreview").innerHTML='<div class="hint warn">'+icon("alert")+' 合并失败：'+esc(e.message)+'</div>';
  }
};

// 填充静态 HTML 中的 data-icon 占位（统一从 ICONS 取 SVG）
$$("[data-icon]").forEach(function(el){
  const sz = el.dataset.size ? parseInt(el.dataset.size, 10) : 15;
  el.insertAdjacentHTML("afterbegin", icon(el.dataset.icon, sz));
});

refresh();
loadTab("runs");
setInterval(function(){ if(state.auto) refresh(); },5000);
setInterval(function(){ if(state.auto) refreshCompanion(); },15000);
</script>
</body>
</html>
"""


def _read_companion_log(log_path: Path) -> tuple[list, list]:
    """读 companion_log.txt（只增不减），**只解析新增的部分**。

    原来每次请求都把整个文件读进来逐行 json.loads —— 文件 2.87 MB / 12,872 行时
    实测 69.5 ms/次，而前端每 5 秒轮询一次，代价随文件增长线性恶化。
    这里记下已解析到的位置，只解析新追加的行（末尾半行留到下次）。
    """
    cache = _COMPANION_LOG_CACHE
    try:
        size = log_path.stat().st_size
    except OSError:
        return [], []
    if size < cache["offset"]:
        # 文件被截断 / 轮转 → 全量重来
        cache["offset"] = 0
        cache["pending"] = ""
        cache["decisions"] = []
        cache["outcomes"] = []
    if size > cache["offset"]:
        try:
            with log_path.open("r", encoding="utf-8") as f:
                f.seek(cache["offset"])
                chunk = f.read()
                cache["offset"] = f.tell()
        except OSError:
            return cache["decisions"], cache["outcomes"]
        text = cache["pending"] + chunk
        lines = text.split("\n")
        cache["pending"] = lines.pop()      # 最后一段可能是半行，留到下次
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if rec.get("kind") == "outcome":
                cache["outcomes"].append(rec)
            elif "decision" in rec and "kind" not in rec:
                cache["decisions"].append(rec)
    return cache["decisions"], cache["outcomes"]


import json
import re
import sqlite3
from pathlib import Path
from typing import Optional

# --------------------------------------------------------------------------
# Stage 5 - 项目总览面板：数据源读取层
#
# 面板是**文件观测器**：独立子进程，只读磁盘上的文件，不去碰运行中的进程。
# 所以凡是"要展示的"都必须先落到文件里（Stage 0.2 的 token 账本就是为此而做的）。
#
# 这一层负责：路径解析 + 读文件 + 聚合。端点只负责拼装。
# 硬约束：任何数据源缺失/为空/损坏，端点都必须返回 200 + 空结构，不许 500。
# --------------------------------------------------------------------------

# dashboard.py 自己所在仓库（app/web/dashboard.py -> 上两级）。
# 工具注册表与对账脚本永远以"代码所在仓库"为准，跟面板的工作目录无关。
_REPO_ROOT = Path(__file__).resolve().parents[2]

# 工具清单/对账结果比较重（要真的建一遍注册表），缓存 60s。
_TOOLS_TTL = 60.0
_tools_cache: dict = {}

# 占位价目表（每百万 token，USD）。
# MiniMax 的真实价目表两个仓库里都没有（docs/baseline-before-alignment.md 第 8 节），
# Stage 4.1 的 app/brain/model_prices.py 还没落地 —— 所以面板算出来的金额
# 一律标 estimated=true，并把用到的单价原样吐出来，方便人纠正。
_PLACEHOLDER_PRICE_USD_PER_MILLION = {
    "input": 0.30,
    "output": 0.60,
    "cache_hit": 0.03,
    "cache_write": 0.30,
}


def _project_root() -> Path:
    """配置文件所在仓库根：优先当前工作目录，回落到 dashboard.py 所在仓库。"""
    cwd = Path.cwd()
    if (cwd / "config.yaml").is_file():
        return cwd
    if (_REPO_ROOT / "config.yaml").is_file():
        return _REPO_ROOT
    return cwd


def _resolve_data_dir(trace_db) -> Path:
    """data/ 目录：优先跟着 trace db 走（面板就是被 --trace-db 指到 data/ 的）。"""
    parent = Path(trace_db).parent
    if str(parent) not in ("", "."):
        return parent
    return Path("data")


def _yaml_load(path: Path) -> dict:
    """读 yaml，坏文件/缺文件一律当空 dict（面板不许因为配置坏了就 500）。"""
    try:
        import yaml
    except ImportError:
        return {}
    try:
        raw = path.read_text("utf-8-sig")
    except OSError:
        return {}
    try:
        data = yaml.safe_load(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _secret_values(root: Path) -> set:
    """配置里的敏感串（API key 等）—— 给所有响应做兜底脱敏。

    API key 绝不能出现在任何响应体里（PLAN.md 5.2）：
    这里把 config.yaml 里所有 *_api_key 的值收集起来，
    中间件逐个替换掉（即使某个端点不小心写进去了也拦得住）。
    """
    out = set()
    raw = _yaml_load(root / "config.yaml")

    def _walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(v, str) and "api_key" in str(k).lower():
                    v = v.strip()
                    if len(v) >= 8:
                        out.add(v)
                else:
                    _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(raw)
    return out


def _character_name(root: Path) -> str:
    """角色名（小汐）—— 决定 live 记忆库叫 data/memory_<角色名>.db。"""
    cfg = _yaml_load(root / "config.yaml")
    ch = cfg.get("character") or {}
    name = str(ch.get("name") or "").strip()
    rel = str(ch.get("character_file") or "").strip()
    if not name and rel:
        p = Path(rel)
        if not p.is_absolute():
            p = root / p
        name = str((_yaml_load(p).get("character") or {}).get("name") or "").strip()
    return name


def _same_path(a: Path, b: Path) -> bool:
    """两个路径是不是同一个文件。"""
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return str(a).lower() == str(b).lower()


def _memory_stores(data_dir: Path, root: Path, memory_file: Optional[Path] = None):
    """(live 库路径, legacy 库路径)。

    live 库 = data/memory_<角色名>.db（MemoryStore 的 per-character 隔离，见
    app/brain/memory.py:301-322）；legacy 库 = data/memory.db。

    解析顺序：
      1. 调用方显式点名了一个 .db（--memory-file 指到 sqlite 记忆库）
         → 那就是"当前在用的这份"；
      2. --memory-file 指到 json（生产用法 data/memory.json）
         → 找同名的 memory_<角色名>.db；
      3. data/memory_<角色名>.db 存在 → 用它；
      4. data/memory_*.db 里有别的 → 用那个；
      5. 都没有 → 回落到 legacy 库（端点会把 store 标成 legacy 并写明原因）。
    """
    legacy = data_dir / "memory.db"
    char = _character_name(root)
    live: Optional[Path] = None
    if memory_file is not None:
        mf = Path(memory_file)
        if mf.suffix.lower() == ".db":
            # 调用方点名了一个 sqlite 记忆库 → 当前在用的就是它
            live = mf
        else:
            # 生产用法：--memory-file data/memory.json → 同名的 memory_<角色名>.db
            sibling = mf.with_name(f"{mf.stem}_{char}.db" if char else "")
            if sibling.is_file():
                live = sibling
    if live is None:
        cand = data_dir / f"memory_{char}.db" if char else None
        if cand is not None and cand.is_file():
            live = cand
        else:
            cands = sorted(p for p in data_dir.glob("memory_*.db")
                           if p.name != legacy.name)
            live = cands[0] if cands else (cand or legacy)
    if live is None:
        live = legacy
    elif not live.is_file() and legacy.is_file():
        # live 库不存在 → 回落到 legacy（端点会明确标 store=legacy）
        live = legacy
    return live, legacy


def _memory_payload(data_dir: Path, root: Path,
                    memory_file: Optional[Path]) -> dict:
    """记忆 Tab 的响应体：**live 库**（桌宠真正在用的那份）+ legacy 对照。

    PLAN.md 5.2：/api/memory 必须读 live 库 data/memory_<角色名>.db ——
    旧版本读的是 legacy 库 data/memory.db，看的不是桌宠真正在用的那份。
    现在同一个响应里再带上 legacy 行数 + 两库之间的重复/冲突提示（5.1）。
    """
    live_path, legacy_path = _memory_stores(data_dir, root, memory_file)
    live_items = _read_memory_db(live_path)
    legacy_items = _read_memory_db(legacy_path)
    pairs = _classify_legacy(live_items, legacy_items)
    fallback = _same_path(live_path, legacy_path)

    by_kind: dict = {}
    by_importance: dict = {}
    access_hits = 0
    for item in live_items:
        kind = str(item.get("kind") or item.get("category") or "other")
        by_kind[kind] = by_kind.get(kind, 0) + 1
        bucket = "%.1f" % round(float(item.get("importance") or 0.0), 1)
        by_importance[bucket] = by_importance.get(bucket, 0) + 1
        if _int(item.get("access_count")) > 0:
            access_hits += 1

    if fallback:
        note = ("live 库 data/memory_<角色名>.db 不存在，这里回落到 legacy 库 "
                "data/memory.db（桌宠还没在这台机器上写过记忆）")
    else:
        note = ("桌宠实际在用的库是 data/memory_<角色名>.db；"
                "data/memory.db 是 legacy 库，见 duplicates 里的重复/冲突提示")

    return {
        "items": live_items,
        "count": len(live_items),
        "source": str(live_path),
        "store": "legacy" if fallback else "live",
        "live": {
            "path": str(live_path),
            "exists": live_path.is_file(),
            "count": len(live_items),
            "by_kind": by_kind,
            "by_importance": by_importance,
            "access_hits": access_hits,
        },
        "legacy": {
            "path": str(legacy_path),
            "exists": legacy_path.is_file(),
            "count": len(legacy_items),
            "items": legacy_items,
        },
        "duplicates": pairs,
        "duplicate_count": sum(
            1 for p in pairs if p.get("relation") in ("duplicate", "conflict")),
        "conflict_count": sum(
            1 for p in pairs if p.get("relation") == "conflict"),
        "known_groups": sorted({
            p.get("group") for p in pairs if p.get("group")}),
        "note": note,
    }



def _vector_index_status(data_dir: Path, root: Path,
                         memory_file: Optional[Path] = None) -> dict:
    """向量索引的状态（**只读**：不建库、不加载模型、不写任何东西）。

    面板要回答三个问题：索引在不在？建了多少行？跟记忆库对得上吗？
    （口径跟 MemoryStore.vector_index_status() **完全一致**：
      状态用同一套词 —— not_built / built / drifted / unavailable ——
      并且**绝不跟 n_indexed 打架**。）
    """
    out = {
        "available": False, "built": False, "state": "not_built",
        "state_reason": "", "path": None, "db": None,
        "embed_model": None, "rerank_model": None,
        "n_indexed": 0, "n_source": 0, "drift": False, "in_sync": False,
        "updated_at": None, "error": None,
        # —— 向量模型在不在（**只读磁盘**：不加载模型、不联网）+ 降级说明 ——
        "models": None, "models_missing": [], "models_ready": False,
        "degraded": False, "degraded_reason": "", "notice": "",
    }
    try:
        from app.brain.vector_index import (derive_state, digest_rows,
                                            index_dir_for, missing_models_notice,
                                            models_status)
    except Exception as exc:  # noqa: BLE001
        out["error"] = "向量索引模块不可用：%s" % exc
        out["state"] = "unavailable"
        out["state_reason"] = out["error"]
        return out
    # 模型在不在：**先算**（索引还没建的时候主人更需要知道这个）
    try:
        mstat = models_status()
        out["models"] = mstat
        missing = list(mstat.get("missing") or [])
        out["models_missing"] = missing
        out["models_ready"] = bool(mstat.get("complete"))
        if missing:
            out["degraded"] = True
            out["degraded_reason"] = "models_missing"
            out["notice"] = missing_models_notice(missing)
    except Exception:  # noqa: BLE001 - 探测失败不许影响面板
        pass
    try:
        live_path, _legacy = _memory_stores(Path(data_dir), Path(root),
                                             memory_file)
        out["db"] = str(live_path)
        src_rows = _read_memory_db(live_path)
        out["n_source"] = len(src_rows)
        idx_dir = index_dir_for(live_path)
        out["path"] = str(idx_dir)
        if not idx_dir.is_dir():
            out["state"], out["state_reason"] = derive_state(
                enabled=True, built=False, n_indexed=0,
                n_source=out["n_source"], in_sync=False, drift=False,
                error=None)
            return out
        state = {}
        state_file = idx_dir / "_index_state.json"
        if state_file.is_file():
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                state = {}
        n_indexed = int(state.get("count") or 0)
        built = bool(state.get("digest")) or n_indexed > 0
        out["built"] = built
        out["available"] = built
        out["n_indexed"] = n_indexed
        out["embed_model"] = state.get("embed_model")
        out["rerank_model"] = state.get("rerank_model")
        out["updated_at"] = state.get("updated_at")
        # 漂移判定跟 store 同一个口径（**整库摘要**比对）：
        # 只比行数会漏掉「内容变了、行数没变」的情况。
        src_digest = digest_rows([
            {"id": r.get("id"), "content": r.get("content"),
             "category": r.get("category"), "scope": r.get("scope"),
             "kind": r.get("kind"), "topic": r.get("topic"),
             "status": r.get("status")} for r in src_rows])
        idx_digest = str(state.get("digest") or "")
        in_sync = bool(idx_digest) and idx_digest == src_digest
        out["in_sync"] = in_sync
        out["drift"] = bool(built) and not in_sync
        out["state"], out["state_reason"] = derive_state(
            enabled=True, built=built, n_indexed=n_indexed,
            n_source=out["n_source"], in_sync=in_sync, drift=out["drift"],
            error=None)
    except Exception as exc:  # noqa: BLE001
        out["error"] = "读向量索引状态失败：%s" % exc
    return out


def _read_memory_db(path: Path) -> list:
    """读 memories 表（只读打开）。表结构变了也不炸：按实际存在的列取。"""
    if not path.is_file():
        return []
    try:
        conn = sqlite3.connect("file:" + path.as_posix() + "?mode=ro", uri=True)
    except Exception:
        return []
    out = []
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)")}
        if not cols:
            return []
        want = [c for c in ("id", "content", "category", "kind", "scope",
                             "importance", "created_at", "last_access_ts",
                             "access_count", "topic", "status") if c in cols]
        if "content" not in want:
            return []
        rows = conn.execute("SELECT " + ", ".join(want) + " FROM memories "
                            "ORDER BY importance DESC, created_at DESC")
        for row in rows.fetchall():
            rec = dict(zip(want, row))
            try:
                rec["importance"] = float(rec.get("importance") or 0.0)
            except (TypeError, ValueError):
                rec["importance"] = 0.0
            try:
                rec["access_count"] = int(rec.get("access_count") or 0)
            except (TypeError, ValueError):
                rec["access_count"] = 0
            out.append(rec)
    except Exception:
        out = []
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out


def _is_placeholder(content: str) -> bool:
    """占位符内容（内容无效，应当丢弃）—— 判定在共享模块里（同一套口径）。

    面板原来自己写死「共享记忆:」这一句，跟 CLI 的通用占位符判定不是一回事；
    现在两边都走 §app/brain/memory_merge.py§ 的 §is_placeholder()§。
    """
    return _memory_merge.is_placeholder(content)


def _classify_groups(live_items: list, legacy_items: list) -> list:
    """判定（**唯一实现**：§app/brain/memory_merge.py§）→ Group 列表。

    CLI 脚本（§scripts/merge_memory_libraries.py§）调的是同一个模块，
    所以面板预览看到的四类判定跟 CLI 报告**逐行一致**。
    """
    return _memory_merge.classify_items(live_items, legacy_items)


def _classify_legacy(live_items: list, legacy_items: list) -> list:
    """legacy 每一行 vs live 库：判成 duplicate / conflict / unique / invalid。

    口径（PLAN.md 2.6）：
      duplicate = 语义重复      -> 保留 live 那条，legacy 丢弃
      conflict  = 主题像但值不同 -> 两条都保留，打 conflict 标记
      unique    = live 没有     -> 并进 live
      invalid   = 占位符/内容无效 -> 丢弃

    **判定只有一份实现**：§app/brain/memory_merge.py§。面板原来自己抄了一份
    分词（§_sim_tokens§）+ 阈值（0.35 / 0.15），跟 CLI 用的
    MemoryStore.detect_conflicts() 口径不一致 —— 现在两边完全一致。
    """
    return [_memory_merge.group_to_dict(g)
            for g in _classify_groups(live_items, legacy_items)]


def _known_group(a: str, b: str) -> str:
    """已知的 3 组语义重复 —— 共享实现（§app/brain/memory_merge.py§）。

    只影响显示名，不参与任何判定。
    """
    return _memory_merge.known_group(a, b)


# token 台账的 6 个桶字段（app/brain/token_ledger.py 的 BUCKET_FIELDS）。
# 台账行可能多出别的字段（比如 step），这里只认这 6 个，多余字段一律忽略。
_LEDGER_BUCKETS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cache_hit_tokens",
    "cache_miss_tokens",
    "cache_write_tokens",
)


def _ledger_rows(path: Path) -> list:
    """读 token 台账 JSONL。

    自己解析而不是 import 台账模块：面板是文件观测器，而且台账模块正在被别的
    agent 改（key 加了 step），面板不能被那次改动带崩 —— 这里只认 6 个桶字段，
    多余字段一律忽略。
    """
    rows = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
    except OSError:
        return []
    return rows


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float(value) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _hit_rate(hit: int, miss: int) -> float:
    """缓存命中率 = 命中 / (命中 + 未命中)。跟台账模块同一口径。"""
    denom = hit + miss
    return round(hit / denom, 4) if denom else 0.0


def _load_model_prices():
    """Stage 4.1 的挂载点：app/brain/model_prices.py。

    那个模块现在还不存在（Stage 4.1 才做），所以这里返回 None，
    面板继续用占位价目表 —— 金额一律标 estimated=true。
    落地之后：只要它提供 estimate_cost(buckets) -> dict，
    面板会自动改用它的结果（这里不用再改）。
    """
    try:
        from app.brain import model_prices
    except Exception:  # noqa: BLE001
        return None
    fn = getattr(model_prices, "estimate_cost", None)
    return fn if callable(fn) else None


def _estimate_cost(buckets: dict) -> dict:
    """按价目表算钱。金额一律标 estimated=true。

    价目表来源：Stage 4.1 的 app/brain/model_prices.py（还没落地）。
    在那之前用占位价目表算，并把用到的单价原样吐出来，方便人纠正。
    """
    price = _PLACEHOLDER_PRICE_USD_PER_MILLION
    hit = _int(buckets.get("cache_hit_tokens"))
    miss = _int(buckets.get("cache_miss_tokens"))
    write = _int(buckets.get("cache_write_tokens"))
    prompt = _int(buckets.get("prompt_tokens"))
    completion = _int(buckets.get("completion_tokens"))
    uncached = max(0, prompt - hit - miss)
    usd = (hit * price["cache_hit"] + miss * price["input"]
           + uncached * price["input"] + write * price["cache_write"]
           + completion * price["output"]) / 1000000.0
    out = {
        "amount": round(usd, 6),
        "currency": "USD",
        "estimated": True,
        "price_source": "placeholder",
        "rates_per_million": dict(price),
        "note": ("MiniMax 真实价目表未落地（Stage 4.1 / app/brain/model_prices.py），"
                 "这里的金额是占位价目表算出的估算值"),
    }
    real = _load_model_prices()
    if real is not None:
        try:
            got = real(dict(buckets))
        except Exception:  # noqa: BLE001
            got = None
        if isinstance(got, dict) and got.get("amount") is not None:
            out.update({k: v for k, v in got.items() if k != "note"})
            out["amount"] = round(float(got.get("amount") or 0.0), 6)
            out.setdefault("estimated", True)
            out["price_source"] = got.get("price_source", "model_prices")
            out["note"] = "金额来自 app/brain/model_prices.py"
    return out


def _aggregate_ledger(rows: list) -> dict:
    """把台账行聚合成总量 + 按 model/backend/session 的拆分。

    台账可能含 kind=aggregate 的聚合行（压实后的老样本），这种行自带 n 字段，
    代表 n 条请求；bucket 数字已经是这些请求的合计，所以不再乘 n。
    """
    totals = {b: 0 for b in _LEDGER_BUCKETS}
    requests = 0
    sessions = set()
    by_model: dict = {}
    by_backend: dict = {}
    by_session: dict = {}
    for row in rows:
        kind = str(row.get("kind") or "request")
        n = _int(row.get("n")) if kind == "aggregate" else 1
        requests += n
        sid = str(row.get("session_id") or "")
        if sid:
            sessions.add(sid)
        model = str(row.get("model") or "unknown")
        backend = str(row.get("backend") or "unknown")
        buckets = {b: _int(row.get(b)) for b in _LEDGER_BUCKETS}
        for b, v in buckets.items():
            totals[b] += v
        for table, key in ((by_model, model), (by_backend, backend),
                           (by_session, sid)):
            rec = table.setdefault(key, {
                "requests": 0, "total_tokens": 0, "prompt_tokens": 0,
                "completion_tokens": 0, "cache_hit_tokens": 0,
                "cache_miss_tokens": 0, "cache_hit_rate": 0.0, "last_ts": 0.0,
            })
            rec["requests"] += n
            for b, v in buckets.items():
                rec[b] = rec.get(b, 0) + v
            rec["last_ts"] = max(_float(rec.get("last_ts")),
                                 _float(row.get("ts")))
    for rec in (list(by_model.values()) + list(by_backend.values())
                + list(by_session.values())):
        rec["cache_hit_rate"] = _hit_rate(_int(rec.get("cache_hit_tokens")),
                                          _int(rec.get("cache_miss_tokens")))
        rec["cost"] = _estimate_cost(rec)
    totals["cache_hit_rate"] = _hit_rate(totals["cache_hit_tokens"],
                                         totals["cache_miss_tokens"])
    totals["requests"] = requests
    totals["sessions"] = len(sessions)
    totals["avg_tokens_per_turn"] = (round(totals["total_tokens"] / requests, 2)
                                      if requests else 0.0)
    totals["cost"] = _estimate_cost(totals)
    totals["by_model"] = by_model
    totals["by_backend"] = by_backend
    totals["by_session"] = by_session
    return totals


def _token_summary(data_dir: Path) -> dict:
    """成本 Tab 的汇总：累计 token / 缓存命中率 / 每轮平均 / 按 session 拆分。"""
    path = data_dir / "token-usage.jsonl"
    out = {
        "source": str(path), "available": False,
        "requests": 0, "sessions": 0, "prompt_tokens": 0,
        "completion_tokens": 0, "total_tokens": 0, "cache_hit_tokens": 0,
        "cache_miss_tokens": 0, "cache_write_tokens": 0,
        "cache_hit_rate": 0.0, "avg_tokens_per_turn": 0.0,
        "by_model": {}, "by_backend": {}, "by_session": {},
        "cost": _estimate_cost({}), "error": None,
    }
    if not path.is_file():
        out["error"] = "token-usage.jsonl 不存在"
        return out
    rows = _ledger_rows(path)
    if not rows:
        out["error"] = "token-usage.jsonl 是空的"
        return out
    out["available"] = True
    out.update(_aggregate_ledger(rows))
    return out


def _token_series(data_dir: Path, bucket_ms: int) -> dict:
    """按时间桶聚合的 token 曲线（成本 Tab 的折线）。"""
    path = data_dir / "token-usage.jsonl"
    out = {"source": str(path), "bucket_ms": bucket_ms, "items": [],
           "error": None}
    if not path.is_file():
        out["error"] = "token-usage.jsonl 不存在"
        return out
    ms = max(1000, _int(bucket_ms) or 60000)
    buckets: dict = {}
    for row in _ledger_rows(path):
        ts = _float(row.get("ts"))
        key = int(ts // ms) * ms
        slot = buckets.setdefault(key, {
            "ts": key, "requests": 0, "prompt_tokens": 0,
            "completion_tokens": 0, "total_tokens": 0,
            "cache_hit_tokens": 0, "cache_miss_tokens": 0,
            "cache_write_tokens": 0,
        })
        slot["requests"] += 1
        for b in _LEDGER_BUCKETS:
            slot[b] += _int(row.get(b))
    items = []
    for key in sorted(buckets):
        slot = buckets[key]
        slot["cache_hit_rate"] = _hit_rate(slot["cache_hit_tokens"],
                                           slot["cache_miss_tokens"])
        slot["cost"] = _estimate_cost(slot)
        items.append(slot)
    out["items"] = items
    return out


def _token_turns(data_dir: Path, session_id: Optional[str], limit: int) -> dict:
    """逐请求明细（哪一轮最贵就靠它）。"""
    path = data_dir / "token-usage.jsonl"
    out = {"source": str(path), "session_id": session_id, "items": [],
           "error": None}
    if not path.is_file():
        out["error"] = "token-usage.jsonl 不存在"
        return out
    rows = _ledger_rows(path)
    if session_id:
        rows = [r for r in rows if str(r.get("session_id") or "") == session_id]
    rows.sort(key=lambda r: _float(r.get("ts")))
    cap = max(1, min(_int(limit) or 200, 2000))
    rows = rows[-cap:]
    items = []
    for row in rows:
        item = {b: _int(row.get(b)) for b in _LEDGER_BUCKETS}
        item["ts"] = _float(row.get("ts"))
        item["session_id"] = str(row.get("session_id") or "")
        item["request_index"] = _int(row.get("request_index"))
        item["backend"] = str(row.get("backend") or "")
        item["model"] = str(row.get("model") or "")
        item["retry"] = _int(row.get("retry"))
        item["cache_hit_rate"] = _hit_rate(item["cache_hit_tokens"],
                                           item["cache_miss_tokens"])
        item["cost"] = _estimate_cost(item)
        items.append(item)
    out["items"] = items
    return out

def _load_check_tool_docs():
    """加载 scripts/check_tool_docs.py（复用它的清单逻辑，不重写一遍）。"""
    import importlib.util
    path = _REPO_ROOT / "scripts" / "check_tool_docs.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(
        "dashboard_check_tool_docs", path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception:
        return None
    return mod


def _reconciliation_result(force: bool = False) -> dict:
    """工具清单对账结果（scripts/check_tool_docs.py 的 collect()）。"""
    import time
    now = time.time()
    if not force:
        cached = _tools_cache.get("reconciliation")
        if cached and now - _tools_cache.get("ts", 0) < _TOOLS_TTL:
            return cached
    mod = _load_check_tool_docs()
    if mod is None:
        return {"available": False,
                "error": "scripts/check_tool_docs.py 找不到或加载失败",
                "live_count": 0, "errors": []}
    try:
        result = mod.collect(strict_docs=False)
    except Exception as e:
        return {"available": False, "error": f"对账失败：{e}",
                "live_count": 0, "errors": []}
    result["available"] = True
    result["error"] = None
    _tools_cache["reconciliation"] = result
    _tools_cache["ts"] = now
    return result


def _reconciliation_summary(recon: dict) -> dict:
    """对账结果的人看的摘要（阻断类差异 + 覆盖类差异）。"""
    sources = {}
    for name, src in (recon.get("sources") or {}).items():
        diff = src.get("diff") or {}
        sources[name] = {
            "only_in_source": list(diff.get("only_in_source") or []),
            "only_in_live": list(diff.get("only_in_live") or []),
            "both": len(diff.get("both") or []),
            "allowlisted": list(diff.get("allowlisted") or []),
            "note": src.get("note"),
        }
    errors = list(recon.get("errors") or [])
    return {
        "available": bool(recon.get("available")),
        "error": recon.get("error"),
        "live_count": _int(recon.get("live_count")),
        "error_count": len(errors),
        "errors": errors,
        "sources": sources,
        "phantoms": recon.get("phantoms") or [],
    }


def _trace_tool_stats(trace_db) -> dict:
    """工具调用次数 / 失败次数（从 traces.db 的 tool 事件里数）。

    失败的判定用工具契约的前缀（docs/tools-reference.md 定的 错误：前缀），
    跟 app/brain/agent.py 的判定同源，但不 import brain 包。
    """
    stats: dict = {}
    if not trace_db or not Path(trace_db).is_file():
        return stats
    try:
        conn = sqlite3.connect("file:" + Path(trace_db).as_posix() + "?mode=ro",
                               uri=True)
    except Exception:
        return stats
    try:
        for (payload,) in conn.execute(
                "SELECT payload_json FROM events WHERE kind='tool'"):
            try:
                obj = json.loads(payload or "{}")
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            name = str(obj.get("name") or obj.get("tool_name") or "")
            if not name:
                continue
            rec = stats.setdefault(name, {"calls": 0, "failures": 0})
            rec["calls"] += 1
            result = obj.get("result")
            text = result if isinstance(result, str) else json.dumps(
                result, ensure_ascii=False, default=str)
            if text.lstrip().startswith(("错误", "Error:")):
                rec["failures"] += 1
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return stats


def _tool_inventory(trace_db, force: bool = False) -> dict:
    """68 个工具（61 内置 + 7 MCP）+ 风险等级 + 解锁等级 + 调用统计 + 对账结果。"""
    import time
    now = time.time()
    if not force:
        cached = _tools_cache.get("inventory")
        if cached and now - _tools_cache.get("ts", 0) < _TOOLS_TTL:
            return cached

    recon = _reconciliation_result(force=force)
    builtin = list(recon.get("live_builtin") or [])
    mcp = list(recon.get("live_mcp") or [])
    mcp_by_server = dict(recon.get("mcp_by_server") or {})

    risk: dict = {}
    try:
        from app.engine.tools._risk import TOOL_RISK_LEVEL
        risk = {k: _int(v) for k, v in TOOL_RISK_LEVEL.items()}
    except Exception:
        risk = {}

    unlock: dict = {}
    mcp_default_level = 8
    try:
        from app.core.companion_config import companion_config
        levels = companion_config.tool_unlock.levels or {}
        for lvl, names in levels.items():
            for name in names or []:
                unlock[str(name)] = _int(lvl)
        mcp_default_level = _int(companion_config.tool_unlock.mcp_default_level)
    except Exception:
        unlock = {}

    server_of: dict = {}
    for server, names in mcp_by_server.items():
        for name in names or []:
            server_of[str(name)] = str(server)

    rows = []
    for name in builtin:
        rows.append({"name": name, "source": "builtin", "server": None})
    for name in mcp:
        rows.append({"name": name, "source": "mcp",
                     "server": server_of.get(str(name))})

    stats = _trace_tool_stats(trace_db)
    for row in rows:
        name = row["name"]
        row["risk_level"] = risk.get(name)
        row["risk_label"] = {0: "SAFE", 1: "LOW", 2: "MEDIUM", 3: "HIGH",
                             4: "IRREVERSIBLE"}.get(row["risk_level"])
        row["unlock_level"] = (unlock.get(name) if row["source"] == "builtin"
                               else mcp_default_level)
        st = stats.get(name) or {"calls": 0, "failures": 0}
        row["calls"] = _int(st.get("calls"))
        row["failures"] = _int(st.get("failures"))
        row["failure_rate"] = (round(row["failures"] / row["calls"], 4)
                               if row["calls"] else 0.0)

    out = {
        "available": bool(recon.get("available")),
        "count": len(rows),
        "builtin_count": len(builtin),
        "mcp_count": len(mcp),
        "mcp_by_server": mcp_by_server,
        "unlock_total": len(unlock),
        "mcp_default_level": mcp_default_level,
        "tools": rows,
        "reconciliation": _reconciliation_summary(recon),
    }
    _tools_cache["inventory"] = out
    _tools_cache["ts"] = now
    return out


def _effective_config(root: Path) -> dict:
    """生效配置：模型 / base_url / 温度 / max_tokens / 解锁表。

    API key 永远不返回原值 —— 只报"配没配 + 多长 + 是不是占位符"。
    """
    out = {
        "model": None, "base_url": None, "temperature": None,
        "max_tokens": None, "stream": None, "timeout": None, "backend": None,
        "api_key": {"configured": False, "length": 0, "valid": False},
        "unlock": {"levels": {}, "mcp_default_level": 8, "count": 0},
        "config_path": str(root / "config.yaml"),
        "error": None,
    }
    cfg = None
    try:
        from app.core.config import load_config
        cfg = load_config(root / "config.yaml")
    except Exception as e:
        out["error"] = f"读 config 失败：{e}"
    if cfg is not None:
        try:
            key = str(cfg.llm.api_key or "")
            out["model"] = cfg.llm.model
            out["base_url"] = cfg.llm.base_url
            out["temperature"] = cfg.llm.temperature
            out["max_tokens"] = cfg.llm.max_tokens
            out["stream"] = cfg.llm.stream
            out["timeout"] = cfg.llm.timeout
            out["backend"] = getattr(getattr(cfg, "brain", None), "backend", None)
            out["api_key"] = {
                "configured": bool(key),
                "length": len(key),
                "valid": bool(cfg.has_api_key()),
            }
        except Exception as e:
            out["error"] = f"读 config 失败：{e}"
    else:
        raw = _yaml_load(root / "config.yaml")
        llm = raw.get("llm") or {}
        out["model"] = llm.get("model")
        out["base_url"] = llm.get("base_url")
        out["temperature"] = llm.get("temperature")
        out["max_tokens"] = llm.get("max_tokens")
        out["stream"] = llm.get("stream")
        out["timeout"] = llm.get("timeout")
        out["backend"] = (raw.get("brain") or {}).get("backend")
    try:
        from app.core.companion_config import companion_config
        levels = companion_config.tool_unlock.levels or {}
        out["unlock"] = {
            "levels": {str(k): list(v or []) for k, v in sorted(levels.items())},
            "mcp_default_level": _int(companion_config.tool_unlock.mcp_default_level),
            "count": sum(len(v or []) for v in levels.values()),
        }
    except Exception:
        pass
    return out


def _pet_status(data_dir: Path) -> dict:
    """桌宠状态：心情 / 精力 / 饱食 / 好感 / 等级 / 工具解锁进度。"""
    path = data_dir / "pet_state.json"
    out = {
        "path": str(path), "exists": path.is_file(),
        "strength_food": 0.0, "feeling": 0.0, "level": 1, "exp": 0.0,
        "likability": 0.0, "mode": None, "money": 0.0,
        "unlock_progress": {"level": 1, "unlocked_tools": 0, "total_tools": 0,
                            "percent": 0.0},
        "error": None,
    }
    state = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text("utf-8"))
            if isinstance(loaded, dict):
                state = loaded
        except Exception as e:
            out["error"] = f"读 pet_state 失败：{e}"
    for key in ("strength_food", "feeling", "exp", "likability", "money"):
        out[key] = _float(state.get(key))
    out["level"] = _int(state.get("level")) or 1
    out["mode"] = state.get("mode")

    total = 0
    unlocked = 0
    try:
        from app.core.companion_config import companion_config
        levels = companion_config.tool_unlock.levels or {}
        total = sum(len(v or []) for v in levels.values())
        unlocked = sum(len(v or []) for lvl, v in levels.items()
                       if _int(lvl) <= out["level"])
    except Exception:
        pass
    out["unlock_progress"] = {
        "level": out["level"],
        "unlocked_tools": unlocked,
        "total_tools": total,
        "percent": round(unlocked / total * 100, 1) if total else 0.0,
    }
    return out


_CRASH_SIGNATURES = ("access violation", "fatal exception", "segfault",
                     "Windows fatal exception", "崩溃")


def _health_report(data_dir: Path, trace_db) -> dict:
    """进程健康：429 限流 / 崩溃 / 账本污染 / data/ 文件体检。"""
    out = {
        "rate_limited": {"count": 0, "last_ts": 0.0, "samples": []},
        "crashes": {"count": 0, "last_ts": 0.0, "samples": []},
        "ledger": {"available": False, "polluted": False, "sessions": [],
                   "note": None},
        "files": [],
        "error": None,
    }
    if trace_db and Path(trace_db).is_file():
        try:
            conn = sqlite3.connect("file:" + Path(trace_db).as_posix()
                                   + "?mode=ro", uri=True)
        except Exception:
            conn = None
        if conn is not None:
            try:
                for ts, payload in conn.execute(
                        "SELECT ts, payload_json FROM events WHERE kind='error'"):
                    try:
                        obj = json.loads(payload or "{}")
                    except Exception:
                        obj = {}
                    msg = str((obj or {}).get("message") or payload or "")
                    low = msg.lower()
                    if "429" in msg or "rate_limit" in low or "tpm/rpm" in low:
                        out["rate_limited"]["count"] += 1
                        out["rate_limited"]["last_ts"] = max(
                            out["rate_limited"]["last_ts"], _float(ts))
                        if len(out["rate_limited"]["samples"]) < 5:
                            out["rate_limited"]["samples"].append(msg[:200])
                    if any(sig.lower() in low for sig in _CRASH_SIGNATURES):
                        out["crashes"]["count"] += 1
                        out["crashes"]["last_ts"] = max(
                            out["crashes"]["last_ts"], _float(ts))
                        if len(out["crashes"]["samples"]) < 5:
                            out["crashes"]["samples"].append(msg[:200])
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    summary = _token_summary(data_dir)
    out["ledger"] = {
        "available": bool(summary.get("available")),
        "polluted": False,
        "sessions": summary.get("by_session") or [],
        "note": None,
    }
    # by_session 是 {session_id: 聚合} —— 迭代出来是 session_id 字符串，
    # 之前当成 dict 取 .get 会 AttributeError（/api/health 直接 500）。
    foreign = [str(sid) for sid in (summary.get("by_session") or {})
               if not str(sid).endswith(":main")]
    if foreign:
        out["ledger"]["polluted"] = True
        out["ledger"]["note"] = (
            f"{len(foreign)} 个会话不是正式会话（session_id 不以 :main 结尾），"
            "疑似基线/测试流量")

    try:
        for child in sorted(data_dir.iterdir()):
            if not child.is_file():
                continue
            try:
                size = child.stat().st_size
            except OSError:
                size = 0
            out["files"].append({"name": child.name, "bytes": size})
    except Exception as e:
        out["error"] = f"列 data/ 失败：{e}"
    return out


# --------------------------------------------------------------------------
# 危险操作前的备份：**只打包这次操作真正会动的文件**。
#
# 原来三个维护端点（backup / reset-ledger / reconciliation）都调
# _backup_data_dir() 把**整个** data/ 打成 zip —— 里面混着 ~289MB 的
# langchain_checkpoints.db（应用自己都按 512KB 上限 prune 它，见
# §app/brain/langchain_agent.py§ 的 _prune_bloated_checkpoint()）。
# 于是「重置一下 token 账本」也要先付 289MB 的代价。
#
# 现在按 PLAN.md 5.3「打包 data/*.db + *.jsonl + *.txt」的口径，但**按需限定范围**：
#   * reset-ledger   → 只备份它要清空的那一个账本文件；
#   * reconciliation → 只读注册表与文档，一个字节都不写 → **不用备份**；
#   * backup         → 全量备份，但默认**排除** checkpoint 库
#                      （要打包得显式 ?include_checkpoints=1）。
# --------------------------------------------------------------------------

# 运行时 checkpoint 库：应用自己会 prune（上限 512KB），默认不进备份包。
CHECKPOINT_DB_NAME = "langchain_checkpoints.db"

_CHECKPOINT_EXCLUDE_REASON = (
    "langchain_checkpoints.db 是运行时 checkpoint 状态（仓库里 ~289MB），"
    "应用自己会按 512KB 上限 prune 它，默认**不进**备份包；"
    "要把它一起打包请显式加 ?include_checkpoints=1")
_CHECKPOINT_INCLUDE_REASON = (
    "已按 ?include_checkpoints=1 显式 opt-in，checkpoint 库一起打进包里")


def _pack_files(srcs, dest_dir: Path, archive_name: str) -> dict:
    """把给定的一批文件打成 zip（只复制，不删、不改原文件）。

    返回统一的形状：archive（归档路径）/ bytes（归档大小）/ files（文件名单）/
    packed（每个文件的字节数）/ skipped（打不进去的）/ error。
    """
    import zipfile
    dest_dir = Path(dest_dir)
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return {"ok": False, "dir": str(dest_dir), "archive": None,
                "bytes": 0, "packed": [], "files": [], "skipped": [],
                "noop": False, "error": str(e)}
    archive = dest_dir / archive_name
    packed = []
    skipped = []
    try:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for src in srcs:
                src = Path(src)
                if not src.is_file():
                    continue
                try:
                    zf.write(src, arcname=src.name)
                    packed.append({"name": src.name,
                                   "bytes": src.stat().st_size})
                except OSError as e:
                    skipped.append({"name": src.name, "error": str(e)})
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "dir": str(dest_dir), "archive": str(archive),
                "bytes": 0, "packed": packed,
                "files": [p["name"] for p in packed],
                "skipped": skipped, "noop": False, "error": str(e)}
    return {
        "ok": True,
        "dir": str(dest_dir),
        "archive": str(archive),
        "bytes": archive.stat().st_size if archive.exists() else 0,
        "packed": packed,
        "files": [p["name"] for p in packed],
        "skipped": skipped,
        "noop": False,
        "error": None,
    }


def _backup_files(data_dir: Path, stamp: str, names, label: str) -> dict:
    """只备份**指定的几个文件**（这次操作真正会动的那些）。"""
    data_dir = Path(data_dir)
    names = list(names)
    result = _pack_files([data_dir / n for n in names],
                         data_dir / "backups" / stamp,
                         f"{label}-{stamp}.zip")
    result["reason"] = "只备份这次操作会动的文件：" + "、".join(names)
    return result


def _backup_vector_index(data_dir: Path, idx_dir: Path, stamp: str) -> dict:
    """备份**向量索引目录**（这次操作真正会动的文件）。

    索引是**派生**的（真相在 SQLite），但重建会覆盖它 —— 按同样的纪律
    先留一份副本。目录还不存在 → 本次操作不写任何已有文件 → 无需备份。
    """
    data_dir = Path(data_dir)
    idx_dir = Path(idx_dir)
    srcs = []
    if idx_dir.is_dir():
        try:
            srcs = [p for p in sorted(idx_dir.rglob("*")) if p.is_file()]
        except OSError:
            srcs = []
    if not srcs:
        return _backup_noop("向量索引还没建过（本次操作不写任何已有文件）")
    result = _pack_files(srcs, data_dir / "backups" / stamp,
                         f"vector-index-{stamp}.zip")
    result["reason"] = "只备份这次操作会动的文件：向量索引目录 %s" % idx_dir
    return result


def _backup_noop(reason: str) -> dict:
    """「这次不需要备份」—— 也要给前端一个统一形状的返回。"""
    return {"ok": True, "dir": None, "archive": None, "bytes": 0,
            "packed": [], "files": [], "skipped": [],
            "noop": True, "reason": reason, "error": None}


def _backup_data_dir(data_dir: Path, stamp: str,
                     include_checkpoints: bool = False) -> dict:
    """全量备份 data/：打包 data/*.db + *.jsonl + *.txt 到 data/backups/<时间戳>/。

    默认**排除** langchain_checkpoints.db（~289MB 运行时状态，应用自己会 prune），
    需要时用 ?include_checkpoints=1 显式 opt-in。
    """
    data_dir = Path(data_dir)
    srcs = []
    for pattern in ("*.db", "*.jsonl", "*.txt"):
        for src in sorted(data_dir.glob(pattern)):
            if src.is_file():
                srcs.append(src)
    excluded = []
    if not include_checkpoints:
        kept = []
        for src in srcs:
            if src.name == CHECKPOINT_DB_NAME:
                excluded.append(src.name)
            else:
                kept.append(src)
        srcs = kept
    result = _pack_files(srcs, data_dir / "backups" / stamp,
                         f"data-{stamp}.zip")
    checkpoints = {
        "name": CHECKPOINT_DB_NAME,
        "requested": bool(include_checkpoints),
        "included": CHECKPOINT_DB_NAME in result.get("files", []),
        "excluded": excluded,
        "reason": (_CHECKPOINT_INCLUDE_REASON if include_checkpoints
                   else _CHECKPOINT_EXCLUDE_REASON),
    }
    result["checkpoints"] = checkpoints
    result["reason"] = ("全量备份 data/ 的 *.db + *.jsonl + *.txt（PLAN.md 5.3）；"
                        + checkpoints["reason"])
    return result

# 危险操作的结果日志（面板用 /api/maintenance/status 显示"刚才做了什么"）。
_DANGEROUS_OPS_LOG: dict = {}


def _remember_dangerous_op(name: str, result: dict) -> None:
    """记一次危险操作的结果（只留最近 20 条，不落盘）。"""
    result = dict(result)
    result["at"] = time.time()
    _DANGEROUS_OPS_LOG[name] = result
    if len(_DANGEROUS_OPS_LOG) > 20:
        for key in list(_DANGEROUS_OPS_LOG)[:-20]:
            _DANGEROUS_OPS_LOG.pop(key, None)


def _backup_stamp() -> str:
    import datetime
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def _merge_into_live(live_path: Path, legacy_path: Path, groups: list,
                     archive_dir: Path, backup: Optional[dict] = None) -> dict:
    """把 legacy 并进 live 库 —— 落库走**共享实现**（§app/brain/memory_merge.py§）。

    共享实现负责四件事（跟 CLI 一模一样）：
      1. 备份（面板那边已经把 zip 备份打好传进来了，备份失败就不许动手）；
      2. 把 unique / **conflict** 的 legacy 行**插进 live 库**
         —— 面板原来只插 unique，冲突行**从来没进过 live 库**，
            那条记忆于是永远召不回来；
      3. 给 conflict 的两条打标记：§memories.topic='conflict'§ +
         §memory_conflicts§ 配对表（Stage 2.4 的 supersede() 靠它工作）；
      4. 把 legacy 库**移动**到 backups/ 归档（**不是删除**，字节不变、仍可打开）。

    live 库正在被桌宠主进程开着 —— 共享实现用事务 + 出错回滚，绝不写坏。
    """
    return _memory_merge.apply_merge(live_path, legacy_path, groups,
                                     archive_dir, archive_dir, backup)


def create_app(trace_db: str | Path,
               memory_file: Optional[str | Path] = None):
    """构造 FastAPI app。"""
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import HTMLResponse
    except ImportError as e:
        raise RuntimeError("需要 fastapi：pip install fastapi") from e

    # TraceRecorder 复用一条长连接（见 app/brain/trace.py），
    # 以前每个请求新建一个 recorder（等于每请求开一条连接 + 跑一次建表 DDL），
    # 现在整个 app 共用一个。
    from app.brain.trace import TraceRecorder
    _recorder = TraceRecorder(trace_db)

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        try:
            yield
        finally:
            try:
                _recorder.close()
            except Exception:  # noqa: BLE001
                pass

    app = FastAPI(title="Desktop Pet Agent Dashboard", lifespan=_lifespan)

    # Stage 5 · 项目总览面板的路径解析：
    #   data/ 跟着 trace db 走（面板就是被 --trace-db 指到 data/ 的），
    #   配置文件跟着仓库根走（config.yaml + app/core/companion_config.yaml）。
    _data_dir = _resolve_data_dir(trace_db)
    _root = _project_root()

    # API key 兜底脱敏：即使某个端点不小心把 key 写进了响应体，
    # 这个中间件也会把它替换掉（PLAN.md 5.2：配置 Tab 里看不到 API key 内容）。
    _secrets = _secret_values(_root)

    @app.middleware("http")
    async def _redact_secrets(request, call_next):
        response = await call_next(request)
        body = getattr(response, "body", None)
        if not body or not _secrets:
            return response
        try:
            text = body.decode("utf-8")
        except (UnicodeDecodeError, AttributeError):
            return response
        redacted = text
        for secret in _secrets:
            if secret in redacted:
                redacted = redacted.replace(secret, "***REDACTED***")
        if redacted == text:
            return response
        from fastapi.responses import Response
        headers = dict(response.headers)
        headers.pop("content-length", None)
        return Response(content=redacted, status_code=response.status_code,
                        headers=headers, media_type=response.media_type)

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return _HTML_PAGE

    @app.get("/api/stats")
    async def stats():
        return _recorder.stats()

    @app.get("/api/runs")
    async def runs(limit: int = 50):
        return _recorder.list_runs(limit=limit)

    @app.get("/api/runs/{rid}")
    async def run_detail(rid: str):
        r = _recorder.get_run(rid)
        if not r:
            raise HTTPException(404, f"Run {rid} not found")
        return r

    @app.get("/api/companion")
    def companion(decision_limit: int = 30, outcome_limit: int = 30):
        """桌宠养成面板：当前 PetState + yaml 配置 + trace 日志。

        注意这里是**同步 def**（不是 async def）：FastAPI 会把同步端点丢到线程池，
        不会占住 uvicorn 的事件循环。原来写成 async def 却在里面做同步的全量
        文件解析 —— 实测单请求 69.5 ms，把整个 Dashboard 服务堵死
        （两个并发请求耗时比 2.10x = 完全串行）。

        数据源：
          - state: data/pet_state.json（yaml persistence.file 配置）
          - trace: data/companion_log.txt（JSON-line，增量解析）
          - config: app/core/companion_config.yaml（实时读）
        """
        from app.core.companion_config import companion_config
        cfg = companion_config

        # 1) 读 pet_state.json
        ps_path = Path(cfg.persistence.file)
        if not ps_path.is_absolute():
            ps_path = Path.cwd() / ps_path
        state_data = {}
        if ps_path.is_file():
            try:
                state_data = json.loads(ps_path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                state_data = {}
        # 计算量化 4 维 (用 yaml 阈值)
        qb = cfg.quantize
        def q(v, lo, hi):
            if v < lo: return "LOW"
            if v > hi: return "HIGH"
            return "MID"
        quantize = {
            "satiety": q(state_data.get("strength_food", 0), qb.low_below, qb.mid_below),
            "mood":    q(state_data.get("feeling", 0),      qb.low_below, qb.mid_below),
            "level":   "HIGH" if state_data.get("level", 1) >= 15 else ("LOW" if state_data.get("level", 1) < 5 else "MID"),
            "favor":   q(state_data.get("likability", 0),  qb.low_below, qb.mid_below),
        }
        # 加 favor 进 state_data 让前端渲染
        state_for_ui = {
            "strength_food": state_data.get("strength_food", 0),
            "feeling":       state_data.get("feeling", 0),
            "level":         state_data.get("level", 1),
            "exp":           state_data.get("exp", 0),
            "likability":    state_data.get("likability", 0),
            "quantize":      quantize,
        }

        # 2) 读 companion_log.txt（增量解析：只解析新增的行）
        log_path = Path(cfg.trace.log_file)
        if not log_path.is_absolute():
            log_path = Path.cwd() / log_path
        decisions, outcomes = _read_companion_log(log_path)
        # 倒序 (新→旧), 前端取前 N
        decisions.sort(key=lambda r: r.get("ts", 0), reverse=True)
        outcomes.sort(key=lambda r: r.get("ts", 0), reverse=True)

        # 3) 抽 yaml 配置关键字段
        ev = cfg.event_hooks
        cfg_dump = {
            "range_satiety":   f"{cfg.satiety.minimum:.0f}..{cfg.satiety.maximum:.0f} (init {cfg.satiety.initial:.0f})",
            "range_mood":      f"{cfg.mood.minimum:.0f}..{cfg.mood.maximum:.0f} (init {cfg.mood.initial:.0f})",
            "range_favor":     f"{cfg.favor.minimum:.0f}..{cfg.favor.maximum:.0f} (init {cfg.favor.initial:.0f})",
            "level_max":       cfg.leveling.max_level,
            "exp_base":        cfg.leveling.base,
            "exp_pow":         cfg.leveling.exp,
            "exp_to_next_lv2": cfg.leveling.base * (2 ** cfg.leveling.exp),
            "satiety_per_hour":       cfg.time_decay.satiety_per_hour,
            "mood_no_interact_48h":   cfg.time_decay.mood_no_interact_48h,
            "check_interval_seconds": cfg.time_decay.check_interval_seconds,
            "favor_high": cfg.favor_rules.high_threshold,
            "favor_low":  cfg.favor_rules.low_threshold,
            "event_hooks": {
                "task_success": {"mood": ev.task_success.mood, "favor": ev.task_success.favor, "exp": ev.task_success.exp, "satiety": ev.task_success.satiety},
                "task_failure": {"mood": ev.task_failure.mood, "favor": ev.task_failure.favor, "exp": ev.task_failure.exp, "satiety": ev.task_failure.satiety},
                "user_rejected":{"mood": ev.user_rejected.mood, "favor": ev.user_rejected.favor, "exp": ev.user_rejected.exp, "satiety": ev.user_rejected.satiety},
                "user_accepted":{"mood": ev.user_accepted.mood, "favor": ev.user_accepted.favor, "exp": ev.user_accepted.exp, "satiety": ev.user_accepted.satiety},
                "user_feed":    {"mood": ev.user_feed.mood,    "favor": ev.user_feed.favor,    "exp": ev.user_feed.exp,    "satiety": ev.user_feed.satiety},
                "user_interact": {"mood": ev.user_interact.mood, "favor": ev.user_interact.favor, "exp": ev.user_interact.exp, "satiety": ev.user_interact.satiety},
            },
        }

        return {
            "state": state_for_ui,
            "config": cfg_dump,
            "decisions": decisions[:decision_limit],
            "outcomes":  outcomes[:outcome_limit],
            "persist_path": str(ps_path),
        }

    # ------------------------------------------------------------------
    # Stage 5 · 项目总览面板的端点
    #
    # 面板是文件观测器：这里只读磁盘上的文件。data/ 的路径由 trace db 推导
    # （面板就是被 --trace-db 指到 data/ 的），配置文件由仓库根推导。
    # ------------------------------------------------------------------
    _data_dir = _resolve_data_dir(trace_db)
    _root = _project_root()

    @app.get("/api/tokens/summary")
    def tokens_summary():
        """成本 Tab：累计 token / 缓存命中率 / 每轮平均 / 按 session 拆分。"""
        return _token_summary(_data_dir)

    @app.get("/api/tokens/series")
    def tokens_series(bucket_ms: int = 60000):
        """成本 Tab：按时间桶聚合的 token 曲线。"""
        return _token_series(_data_dir, bucket_ms)

    @app.get("/api/tokens/turns")
    def tokens_turns(session_id: Optional[str] = None, limit: int = 200):
        """成本 Tab：逐请求明细（哪一轮最贵）。"""
        return _token_turns(_data_dir, session_id, limit)

    @app.get("/api/memory")
    def memory():
        """长期记忆：读 **live 库** data/memory_<角色名>.db（桌宠真正在用的那份）。

        旧版本读的是 legacy 库 data/memory.db —— 看的不是桌宠在用的数据。
        现在同一个响应里再带上 legacy 行数 + 两库之间的已知重复/冲突（PLAN.md 5.1）。
        """
        return _memory_payload(_data_dir, _root,
                               Path(memory_file) if memory_file else None)

    @app.get("/api/memory/merge-preview")
    def memory_merge_preview():
        """合并 legacy/live 的预览（只读，不动任何文件）。"""
        live_path, legacy_path = _memory_stores(
            _data_dir, _root,
            Path(memory_file) if memory_file else None)
        live_items = _read_memory_db(live_path)
        legacy_items = _read_memory_db(legacy_path)
        pairs = _classify_legacy(live_items, legacy_items)
        return {
            "live_path": str(live_path),
            "legacy_path": str(legacy_path),
            "store": "legacy" if _same_path(live_path, legacy_path) else "live",
            "live_count": len(live_items),
            "legacy_count": len(legacy_items),
            "will_insert": [p for p in pairs if p.get("action") == "insert"],
            "will_keep_live": [p for p in pairs if p.get("action") == "keep_live"],
            "will_keep_both": [p for p in pairs if p.get("action") == "keep_both"],
            "will_discard": [p for p in pairs if p.get("action") == "discard"],
            "pairs": pairs,
        }

    @app.get("/api/tools")
    def tools():
        """工具 Tab：68 个工具（61 内置 + 7 MCP）+ 风险/解锁等级 + 调用统计 + 对账。"""
        return _tool_inventory(trace_db)

    @app.get("/api/reconciliation")
    def reconciliation():
        """工具 Tab：文档 <-> 注册表的对账差异（复用 check_tool_docs.py）。"""
        return _reconciliation_summary(_reconciliation_result())

    @app.get("/api/config")
    def config_overview():
        """配置 Tab：生效的模型/base_url/温度/max_tokens + 解锁表。

        API key 永远不返回原值 —— 只有 configured / length / valid。
        """
        return _effective_config(_root)

    @app.get("/api/state")
    def state():
        """状态 Tab：桌宠状态 + 工具解锁进度。"""
        return _pet_status(_data_dir)

    @app.get("/api/health")
    def health():
        """健康与维护 Tab：429 限流 / 崩溃 / 账本污染 / data/ 文件体检。"""
        payload = _health_report(_data_dir, trace_db)
        # 向量索引状态（只读）：索引在不在 / 建了多少 / 漂移没漂移
        payload["vector_index"] = _vector_index_status(
            _data_dir, _root, Path(memory_file) if memory_file else None)
        return payload

    @app.get("/api/overview")
    def overview():
        """顶部卡片数据（8 张卡片一次拿全，前端不用发 8 个请求）。"""
        stats = _recorder.stats()
        tokens = _token_summary(_data_dir)
        live_path, legacy_path = _memory_stores(
            _data_dir, _root,
            Path(memory_file) if memory_file else None)
        live_items = _read_memory_db(live_path)
        legacy_items = _read_memory_db(legacy_path)
        inv = _tool_inventory(trace_db)
        return {
            "stats": stats,
            "tokens": tokens,
            "memory": {
                "live_count": len(live_items),
                "legacy_count": len(legacy_items),
                "live_path": str(live_path),
                "legacy_path": str(legacy_path),
            },
            "tools": {
                "count": inv.get("count", 0),
                "builtin_count": inv.get("builtin_count", 0),
                "mcp_count": inv.get("mcp_count", 0),
                "reconciliation_errors":
                    inv.get("reconciliation", {}).get("error_count", 0),
            },
            "cards": [
                {"label": "总会话数", "value": stats.get("total_runs", 0)},
                {"label": "成功率",
                 "value": str(round(float(stats.get("success_rate") or 0) * 100))
                          + "%"},
                {"label": "工具调用次数",
                 "value": stats.get("total_tool_calls", 0)},
                {"label": "平均步骤", "value": stats.get("avg_steps", 0)},
                {"label": "平均耗时",
                 "value": round(float(stats.get("avg_duration") or 0), 1)},
                {"label": "累计 token", "value": tokens.get("total_tokens", 0)},
                {"label": "累计花费",
                 "value": tokens.get("cost", {}).get("amount", 0.0),
                 "estimated": True},
                {"label": "缓存命中率",
                 "value": str(round(float(tokens.get("cache_hit_rate") or 0) * 100))
                          + "%"},
            ],
        }

    # ------------------------------------------------------------------
    # 危险操作：POST + 维护模式开关 + 显式确认 + 每次先自动备份 data/
    #
    # 三道闸门（缺一不可）：
    #   1. maintenance=1 —— 面板默认是只读的，这个开关要显式打开；
    #   2. confirm=<操作名> —— 前端二次确认，防误触；
    #   3. 每个操作动手之前先自动备份 data/。
    # 没有备份就不许动 data/ 里的任何东西；备份只复制，不删。
    # ------------------------------------------------------------------
    def _require_maintenance(maintenance: int, confirm: str, token: str):
        if not maintenance:
            raise HTTPException(
                403, "维护模式未开启（加 ?maintenance=1）；面板默认是只读的")
        if confirm != token:
            raise HTTPException(
                400, f"需要二次确认：?confirm={token}")

    def _do_backup(include_checkpoints: bool = False) -> dict:
        return _backup_data_dir(_data_dir, _backup_stamp(),
                                include_checkpoints=include_checkpoints)

    @app.post("/api/maintenance/backup")
    def maintenance_backup(maintenance: int = 0, confirm: str = "",
                          include_checkpoints: bool = False):
        """全量备份 data/（打包 *.db + *.jsonl + *.txt 到 data/backups/<时间戳>/）。

        默认**不含** langchain_checkpoints.db（~289MB 运行时状态，应用自己会 prune）——
        要把它一起打包，显式加 ?include_checkpoints=1。
        响应里报归档路径、大小、文件名单，以及 checkpoint 库到底进没进包。
        """
        _require_maintenance(maintenance, confirm, "backup")
        result = _do_backup(include_checkpoints=bool(include_checkpoints))
        if not result.get("ok"):
            raise HTTPException(500, f"备份失败：{result.get('error')}")
        _remember_dangerous_op("backup", {"op": "backup", "backup": result})
        return {"op": "backup", "backup": result,
                "backup_archive": result.get("archive"),
                "backup_bytes": result.get("bytes"),
                "backup_files": result.get("files"),
                "checkpoints": result.get("checkpoints"),
                "note": "只复制，不删 data/ 里的任何东西"}

    @app.post("/api/maintenance/reset-ledger")
    def maintenance_reset_ledger(maintenance: int = 0, confirm: str = ""):
        """重置 token 账本（先备份；旧账本内容保留在备份目录里，不删）。

        备份**只打包它要清空的那一个账本文件** —— 原来是把整个 data/
        打成 zip（里面混着 289MB 的 checkpoint 库），这里收窄到操作真正动的文件。
        """
        _require_maintenance(maintenance, confirm, "reset-ledger")
        ledger = _data_dir / "token-usage.jsonl"
        backup = _backup_files(_data_dir, _backup_stamp(),
                               [ledger.name], "ledger")
        if not backup.get("ok"):
            raise HTTPException(500, f"备份失败，拒绝继续：{backup.get('error')}")
        before = 0
        archived = None
        if ledger.is_file():
            try:
                before = len(_ledger_rows(ledger))
            except Exception:
                before = 0
            archived = str(Path(backup["dir"]) / "token-usage.jsonl")
            try:
                shutil.copy2(ledger, archived)
            except OSError as e:
                raise HTTPException(500, f"归档旧账本失败：{e}")
            try:
                ledger.write_text("", encoding="utf-8")
            except OSError as e:
                raise HTTPException(500, f"清空账本失败：{e}")
        result = {"op": "reset-ledger", "backup": backup,
                  "backup_archive": backup.get("archive"),
                  "backup_bytes": backup.get("bytes"),
                  "backup_files": backup.get("files"),
                  "rows_before": before, "rows_after": 0,
                  "archived_to": archived,
                  "note": ("只备份了 %s（这次操作真正会动的文件）；"
                           "旧账本的完整内容保留在备份目录里（没有删除）"
                           % ledger.name)}
        _remember_dangerous_op("reset-ledger", result)
        return result

    @app.post("/api/maintenance/reconciliation")
    def maintenance_reconciliation(maintenance: int = 0, confirm: str = ""):
        """重跑工具对账（复用 scripts/check_tool_docs.py），结果贴回面板。

        对账**只读**注册表与文档，一个字节都不写进 data/ → **不需要备份**。
        原来这里照样把整个 data/ 打成 zip（289MB），纯属白干。
        """
        _require_maintenance(maintenance, confirm, "rerun-reconciliation")
        backup = _backup_noop("对账只读注册表与文档，不写 data/ 里的任何文件"
                              " → 本次操作无需备份（没有产生任何归档）")
        summary = _reconciliation_summary(_reconciliation_result(force=True))
        _tools_cache.pop("inventory", None)
        result = {"op": "rerun-reconciliation", "backup": backup,
                  "backup_archive": backup.get("archive"),
                  "backup_bytes": backup.get("bytes"),
                  "backup_files": backup.get("files"),
                  "reconciliation": summary,
                  "note": "对账只读注册表与文档，不改任何数据文件"
                          "（本次无需备份，也没有产生归档）"}
        _remember_dangerous_op("rerun-reconciliation", result)
        return result

    @app.post("/api/maintenance/merge-memory")
    def maintenance_merge_memory(maintenance: int = 0, confirm: str = ""):
        """合并 legacy/live 记忆库（先备份；legacy **归档**而不是删除）。

        判定与落库都走**共享实现** §app/brain/memory_merge.py§
        （CLI 脚本 §scripts/merge_memory_libraries.py§ 也用它）——
        面板预览 = CLI 报告，逐行一致。

        三道闸门不变：POST + maintenance=1 + confirm=merge-memory，
        而且**动手之前一定先备份**（备份失败就不许动）。
        """
        _require_maintenance(maintenance, confirm, "merge-memory")
        live_path, legacy_path = _memory_stores(
            _data_dir, _root,
            Path(memory_file) if memory_file else None)
        live_items = _read_memory_db(live_path)
        legacy_items = _read_memory_db(legacy_path)
        groups = _classify_groups(live_items, legacy_items)
        pairs = [_memory_merge.group_to_dict(g) for g in groups]

        # 备份：**只备份它会动的两个文件**（两个记忆库）。
        # 原来是把整个 data/ 打成 zip —— 里面混着 289MB 的
        # langchain_checkpoints.db，每次维护操作都又慢又占地方。
        backup = _memory_merge.backup_databases_zip(
            live_path, legacy_path,
            _data_dir / "backups" / _backup_stamp())
        if not backup.get("ok"):
            raise HTTPException(500, f"备份失败，拒绝继续：{backup.get('error')}")

        if _same_path(live_path, legacy_path):
            # 没有独立的 live 库（live 库不存在，已回落到 legacy）→ 没有可合并的差异
            result = {"op": "merge-memory", "backup": backup,
                      "live_path": str(live_path),
                      "legacy_path": str(legacy_path),
                      "inserted": [], "inserted_count": 0,
                      "marked": [], "marked_count": 0,
                      "kept_live": 0, "kept_both": 0,
                      "discarded": 0, "legacy_archived_to": None,
                      "pairs": pairs,
                      "note": ("live 库不存在（已回落到 legacy 库），没有可合并的差异；"
                               "没有改动任何记忆")}
            _remember_dangerous_op("merge-memory", result)
            return result

        # 落库（共享实现）：插入 unique **和 conflict** 的行 + 打 conflict
        # 标记 + 把 legacy 库**移动**到 backups/ 归档（不是删除）。
        merged = _merge_into_live(live_path, legacy_path, groups,
                                  _data_dir / "backups", backup)
        if not merged.get("ok"):
            raise HTTPException(500, merged.get("error") or "合并失败")
        archived = (merged.get("archived") or {}).get("to")
        result = {"op": "merge-memory", "backup": backup,
                  "live_path": str(live_path),
                  "legacy_path": str(legacy_path),
                  "inserted": merged.get("inserted", []),
                  "inserted_count": len(merged.get("inserted", [])),
                  "marked": merged.get("marked", []),
                  "marked_count": len(merged.get("marked", [])),
                  "kept_live": len([p for p in pairs
                                    if p.get("action") == "keep_live"]),
                  "kept_both": len([p for p in pairs
                                    if p.get("action") == "keep_both"]),
                  "discarded": len([p for p in pairs
                                    if p.get("action") == "discard"]),
                  "legacy_archived_to": archived,
                  "pairs": pairs,
                  "note": ("legacy 库已**归档**（移动到 backups/，字节不变、仍可打开），"
                           "没有删除；冲突的两条都保留并打了 conflict 标记；"
                           "live 库里原有的行一条没动")}
        _remember_dangerous_op("merge-memory", result)
        return result


    @app.post("/api/maintenance/rebuild-vector-index")
    def maintenance_rebuild_vector_index(maintenance: int = 0, confirm: str = ""):
        """重建记忆库的**向量索引**（先备份；只写 data/vectors/<角色>/）。

        三道闸门不变：POST + maintenance=1 + confirm=rebuild-vector-index，
        而且**动手之前一定先备份**（备份失败就不许动）。

        重建在**后台线程**里跑：第一次可能要下载模型（≈1.2 GB），
        不能把 HTTP 请求挂在那儿 —— 面板立刻返回，完成后刷新看状态。
        """
        _require_maintenance(maintenance, confirm, "rebuild-vector-index")
        live_path, _legacy = _memory_stores(
            _data_dir, _root, Path(memory_file) if memory_file else None)
        from app.brain.vector_index import index_dir_for
        idx_dir = index_dir_for(live_path)
        # 备份：这次操作真正会动的文件 = 向量索引目录里的文件
        backup = _backup_vector_index(_data_dir, idx_dir, _backup_stamp())
        if not backup.get("ok"):
            raise HTTPException(500, f"备份失败，拒绝继续：{backup.get('error')}")
        state = {"running": True, "ok": None, "error": None, "count": 0}

        def _work() -> None:
            try:
                from app.brain.memory import MemoryStore
                store = MemoryStore(live_path)
                report = store.rebuild_vector_index(blocking=True)
                state.update(running=False, ok=bool(report.get("ok")),
                             error=report.get("error"),
                             count=report.get("count") or 0)
            except Exception as exc:  # noqa: BLE001
                state.update(running=False, ok=False, error=str(exc))

        thread = threading.Thread(target=_work, daemon=True,
                                  name="VectorIndexRebuild")
        thread.start()
        result = {"op": "rebuild-vector-index", "backup": backup,
                  "backup_archive": backup.get("archive"),
                  "backup_bytes": backup.get("bytes"),
                  "backup_files": backup.get("files"),
                  "db": str(live_path), "index_dir": str(idx_dir),
                  "started": True, "state": dict(state),
                  "note": ("重建在后台线程里跑（第一次可能要下载模型 ≈1.2 GB，"
                           "不会阻塞面板）；完成后刷新本页看状态")}
        _remember_dangerous_op("rebuild-vector-index", result)
        return result

    @app.get("/api/maintenance/status")
    def maintenance_status():
        """最近一次危险操作的结果（面板用它显示"刚才做了什么"）。"""
        return {"maintenance_required": True,
                "operations": [
                    {"name": "backup", "confirm": "backup",
                     "method": "POST", "endpoint": "/api/maintenance/backup"},
                    {"name": "reset-ledger", "confirm": "reset-ledger",
                     "method": "POST",
                     "endpoint": "/api/maintenance/reset-ledger"},
                    {"name": "rerun-reconciliation",
                     "confirm": "rerun-reconciliation", "method": "POST",
                     "endpoint": "/api/maintenance/reconciliation"},
                    {"name": "merge-memory", "confirm": "merge-memory",
                     "method": "POST",
                     "endpoint": "/api/maintenance/merge-memory"},
                    {"name": "rebuild-vector-index",
                     "confirm": "rebuild-vector-index", "method": "POST",
                     "endpoint": "/api/maintenance/rebuild-vector-index"},
                ],
                "last_results": list(_DANGEROUS_OPS_LOG.values()),
                "backups_dir": str(_data_dir / "backups")}

    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
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

    print(f"Dashboard 启动: http://{args.host}:{args.port}")
    print(f"   trace db: {trace_db}")
    print(f"   memory:   {memory_file}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
