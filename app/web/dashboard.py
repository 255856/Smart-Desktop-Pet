"""Agent 可观测性 Dashboard（FastAPI）。"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


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

  /* 概览卡 */
  .stats{display:grid;grid-template-columns:repeat(5,1fr);gap:14px;margin-bottom:20px;}
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

  /* 标签页 */
  .tabs{display:flex;gap:6px;margin-bottom:18px;flex-wrap:nowrap;}
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
  .hint{font-size:12.5px;color:var(--sub);background:var(--bg);border:1px dashed var(--border);
    border-radius:10px;padding:10px 13px;}

  /* 记忆 / 工具视图 */
  .grid2{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px;}
  .mem{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:15px 17px;
    box-shadow:var(--shadow-sm);}
  .mem .content{font-size:14px;line-height:1.6;word-break:break-word;white-space:pre-wrap;}
  .mem .foot{display:flex;align-items:center;gap:8px;margin-top:10px;font-size:11.5px;color:var(--sub);}
  .cat-tag{background:var(--accent-bg);color:var(--accent-d);border-radius:6px;padding:2px 9px;font-size:11px;font-weight:600;}
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
    font-size:12px;line-height:1.7;margin-bottom:8px;}
  .config-block b{color:var(--accent-d);}
  .cs-compact{display:flex;flex-direction:column;gap:6px;margin-bottom:8px;}
  .cs-block{background:linear-gradient(135deg,#f7f4ff,#fdf6fb);border:1px solid #e6e0fb;
    border-radius:11px;padding:9px 13px;font-size:13px;line-height:1.7;}
  .cs-block b{color:var(--accent-d);}

  .bar-row{display:flex;align-items:center;gap:12px;margin-bottom:12px;}
  .bar-row .name{width:150px;flex:none;font-size:13px;font-weight:600;}
  .bar-row .track{flex:1;height:22px;background:var(--bg);border-radius:7px;overflow:hidden;}
  .bar-row .fill{height:100%;border-radius:7px;background:linear-gradient(90deg,#a78bfa,#7c6cf0);
    display:flex;align-items:center;justify-content:flex-end;color:#fff;font-size:11px;font-weight:700;
    padding-right:8px;min-width:26px;transition:width .5s;}
  .sub-head{font-size:14px;font-weight:700;margin:22px 0 12px;}
  .error-banner{background:var(--red-bg);border:1px solid #f5c6c6;border-radius:12px;padding:12px 15px;
    color:#b42318;font-size:13px;line-height:1.6;white-space:pre-wrap;word-break:break-word;
    display:flex;gap:10px;align-items:flex-start;}
  ::-webkit-scrollbar{width:8px;height:8px;}
  ::-webkit-scrollbar-thumb{background:#d8d4ec;border-radius:4px;}
  ::-webkit-scrollbar-thumb:hover{background:var(--accent-lt);}
  @media(max-width:900px){.stats{grid-template-columns:repeat(2,1fr);}
    .sessions{grid-template-columns:1fr;}.run-list{max-height:360px;}}
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div class="logo">🐳</div>
    <div class="titles">
      <h1>桌宠智能体控制台</h1>
      <div class="sub">本地调试面板 · 记录每一次对话与工具调用过程</div>
    </div>
    <div class="spacer"></div>
    <div class="live" id="liveBox"><span class="dot pulse"></span><span id="liveText">自动刷新中</span></div>
  </div>

  <div class="stats" id="stats"></div>

  <div class="tabs">
    <div class="tab active" data-view="sessions" data-icon="chat">会话记录</div>
    <div class="tab" data-view="memory" data-icon="brain">长期记忆</div>
    <div class="tab" data-view="tools" data-icon="wrench">工具统计</div>
  </div>

  <!-- 会话 -->
  <div class="view active" id="view-sessions">
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

  <!-- 记忆 -->
  <div class="view" id="view-memory">
    <div class="panel" style="padding:18px;">
      <div class="sub-head" style="margin-top:0;" data-icon="brain">桌宠记住的事 <span id="memCount" class="mini-tag"></span></div>
      <div class="grid2" id="memGrid"></div>
    </div>
  </div>

  <!-- 工具 -->
  <div class="view" id="view-tools">
    <div class="panel" style="padding:20px;">
      <div class="sub-head" style="margin-top:0;">工具调用分布</div>
      <div id="toolBars"></div>
      <div class="sub-head">状态分布</div>
      <div id="statusBars"></div>
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
  brain:'<path d="M9.5 2A2.5 2.5 0 0 1 12 4.5v15a2.5 2.5 0 0 1-4.96.44 2.5 2.5 0 0 1-2.96-3.08 3 3 0 0 1-.34-5.58 2.5 2.5 0 0 1 1.32-4.24 2.5 2.5 0 0 1 1.98-3A2.5 2.5 0 0 1 9.5 2Z"/><path d="M14.5 2A2.5 2.5 0 0 0 12 4.5v15a2.5 2.5 0 0 0 4.96.44 2.5 2.5 0 0 0 2.96-3.08 3 3 0 0 0 .34-5.58 2.5 2.5 0 0 0-1.32-4.24 2.5 2.5 0 0 0-1.98-3A2.5 2.5 0 0 0 14.5 2Z"/>',
  search:'<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
  compass:'<circle cx="12" cy="12" r="10"/><polygon points="16.24 7.76 14.12 14.12 7.76 16.24 9.88 9.88 16.24 7.76"/>',
  eye:'<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>',
  info:'<circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/>',
  xcircle:'<circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/>',
  alert:'<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
  hourglass:'<path d="M5 22h14M5 2h14M17 22v-4.17a2 2 0 0 0-.59-1.42L12 12l-4.41 4.41A2 2 0 0 0 7 17.83V22M7 2v4.17a2 2 0 0 0 .59 1.42L12 12l4.41-4.41A2 2 0 0 0 17 6.17V2"/>',
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
  h = h.replace(/```[\s\S]*?```/g, m => "<pre>"+esc(m.replace(/```[^\n]*\n?|```/g,""))+"</pre>");
  h = h.replace(/`([^`]+)`/g, "<code>$1</code>");
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
  return `${dt.getMonth()+1}月${dt.getDate()}日`;
}
function fmtTime(ts){
  if(!ts) return "—";
  const d=new Date(ts*1000);
  const p=n=>String(n).padStart(2,"0");
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function fmtDur(s){
  if(s==null||isNaN(s)) return "—";
  if(s<1) return Math.round(s*1000)+" ms";
  if(s<60) return s.toFixed(1)+" 秒";
  return Math.floor(s/60)+" 分 "+Math.round(s%60)+" 秒";
}
function badge(status){
  return `<span class="badge ${status}">${STATUS_CN[status]||esc(status)}</span>`;
}

const state = {
  stats:null, runs:[], selectedId:null, filter:"all", kw:"",
  detail:null, auto:true,
};

async function api(path){
  const r = await fetch(path);
  if(!r.ok) throw new Error(path+" "+r.status);
  return r.json();
}

function renderStats(){
  const s = state.stats; if(!s) return;
  const rate = Math.round((s.success_rate||0)*100);
  const cards=[
    {cls:"violet", ico:icon("chat"), num:s.total_runs??0, label:"总会话数"},
    {cls:"green", ico:icon("check"), num:rate+"%", label:`成功率（${s.success_runs||0}/${s.total_runs||0}）`},
    {cls:"blue", ico:icon("wrench"), num:s.total_tool_calls??0, label:"工具调用次数"},
    {cls:"amber", ico:icon("zap"), num:s.avg_steps??0, label:"平均步骤"},
    {cls:"violet", ico:icon("clock"), num:fmtDur(s.avg_duration), label:"平均耗时"},
  ];
  $("#stats").innerHTML = cards.map(c=>`
    <div class="stat ${c.cls}"><div class="bar"></div>
      <div class="ico">${c.ico}</div>
      <div class="num">${c.num}</div>
      <div class="label">${c.label}</div>
    </div>`).join("");
}

function renderRunList(){
  const box=$("#runList");
  let list=state.runs;
  if(state.filter!=="all") list=list.filter(r=>r.status===state.filter);
  if(state.kw) list=list.filter(r=>(r.goal||"").toLowerCase().includes(state.kw));
  if(!list.length){
    box.innerHTML=`<div class="empty" style="min-height:200px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;"><svg width="42" height="42" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" style="opacity:.30;"><rect x="4" y="5" width="16" height="14" rx="2.5"/><path d="M4 9h16"/></svg><div>没有符合条件的会话</div></div>`;
    return;
  }
  const scrollTop=box.scrollTop;
  box.innerHTML=list.map(r=>{
    const tags=[];
    if(r.tool_calls>0) tags.push(`<span class="mini-tag">${icon("wrench", 12)} ${r.tool_calls}</span>`);
    if(r.total_steps>0) tags.push(`<span class="mini-tag">${icon("zap", 12)} ${r.total_steps}</span>`);
    return `<div class="run-item ${r.id===state.selectedId?"active":""}" data-id="${r.id}">
      <div class="goal">${esc(r.goal||"(无内容)")}</div>
      <div class="meta">${badge(r.status)}<span class="t">${timeAgo(r.started_at)}</span>${tags.join("")}</div>
    </div>`;
  }).join("");
  box.scrollTop=scrollTop;
}

// ---------- 详情：把事件流整理成可读结构 ----------
function pickStreamText(events){
  // text 事件是 token 流，最终文本取最后一个 accumulated
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
      kvHtml=`<div class="kv">${keys.map(k=>
        `<span class="pair"><span class="k">${esc(k)}</span><span class="v">${esc(typeof args[k]==="object"?JSON.stringify(args[k]):args[k])}</span></span>`
      ).join("")}</div>`;
    }
  }catch(_){}
  let result=p.result;
  if(result!=null && typeof result==="object") result=JSON.stringify(result,null,2);
  let resultHtml="";
  if(result!=null && result!==""){
    const rid="res"+Math.random().toString(36).slice(2,8);
    resultHtml=`<div class="result-box" id="${rid}">${esc(String(result))}</div>
      <button class="expand-btn" onclick="toggleResult('${rid}',this)">展开结果 ▾</button>`;
  }
  return `<div class="tl-card tool">
    <div class="cap">${icon("wrench")} ${esc(cn)}<span class="mini-tag">${esc(name)}</span></div>
    ${kvHtml}${resultHtml}
  </div>`;
}

function renderPlanCard(p){
  const steps=p.steps||[];
  return `<div class="tl-card">
    <div class="cap" style="color:#1d4ed8;">${icon("compass")} 执行计划</div>
    <ol style="margin:4px 0 0;padding-left:20px;">
      ${steps.map(s=>`<li style="margin-bottom:6px;font-size:12.5px;line-height:1.6;">
        <div>${esc(s.thought||"")}</div>
        ${s.tool_name?`<span class="mini-tag" style="margin-top:3px;">${icon("wrench", 12)} ${esc(TOOL_NAME[s.tool_name]||s.tool_name)}</span>`:""}
      </li>`).join("")}
    </ol>
  </div>`;
}

function renderDetail(r){
  const ev=r.events||[];
  const errEv=ev.find(e=>e.kind==="error");
  const isFail=r.status==="failed" || !!errEv;
  const answer=(r.final_answer||"").replace(/^\[错误\]\s*/,"");
  const errMsg=errEv ? (errEv.payload&&errEv.payload.message || JSON.stringify(errEv.payload)) : "";
  // 失败时 final_answer 往往就是错误原文，避免与顶部错误横幅重复
  const a=answer.trim(), m=errMsg.trim();
  const answerDupError=!!m && !!a && (a===m || a.includes(m) || m.includes(a));
  const dur=r.finished_at? r.finished_at-r.started_at:null;
  const model=r.meta&&r.meta.model;

  // 时间线（只保留计划/工具/反思/提示；error 统一在顶部横幅，text 碎片剔除）
  const tl=ev.filter(e=>["plan","tool","reflection","meta"].includes(e.kind));
  let tlHtml;
  if(tl.length){
    tlHtml=`<div class="timeline">${tl.map(e=>{
      const p=e.payload||{};
      if(e.kind==="tool")
        return `<div class="tl-item tool"><div class="node">${icon("wrench")}</div>${renderToolCard(p)}</div>`;
      if(e.kind==="plan")
        return `<div class="tl-item plan"><div class="node">${icon("compass")}</div>${renderPlanCard(p)}</div>`;
      if(e.kind==="reflection")
        return `<div class="tl-item reflection"><div class="node">${icon("eye")}</div>
          <div class="tl-card"><div class="cap" style="color:var(--accent-d);">${icon("eye")} 反思评估</div>
          <div style="font-size:12.5px;">${esc(p.comment||"")}
          ${p.verdict?`<span class="mini-tag" style="margin-left:6px;">${esc(p.verdict)}</span>`:""}</div></div></div>`;
      if(e.kind==="meta"){
        const label=p.event==="force_retry"?"自动重试":p.event||"内部事件";
        const note=p.reason||p.intent||"";
        return `<div class="tl-item meta"><div class="node">${icon("info")}</div>
          <div class="tl-card"><div class="cap" style="color:var(--sub);">${icon("info")} ${esc(label)}</div>
          <div style="font-size:12.5px;color:var(--sub);">${esc(note)}</div></div></div>`;
      }
      return "";
    }).join("")}</div>`;
  } else {
    tlHtml=`<div class="hint">${icon("chat")} 这是一条普通对话，没有调用工具或额外步骤。</div>`;
  }

  // 折叠：生成过程文本 + 原始事件
  const stream=pickStreamText(ev);
  const streamHtml = stream && stream!==answer
    ? `<details class="raw"><summary>查看生成过程（流式文本）</summary><pre>${esc(stream)}</pre></details>` : "";
  const rawHtml=`<details class="raw"><summary>原始事件（${ev.length} 条 · 开发者）</summary>
    <pre>${esc(ev.map(e=>`#${e.seq} [${e.kind}] ${JSON.stringify(e.payload,null,2)}`).join("\n\n"))}</pre></details>`;

  const finish=ev.find(e=>e.kind==="finish");
  const emo=finish&&finish.payload&&finish.payload.emotion;

  $("#detailPanel").innerHTML=`<div class="detail">
    <div class="d-head">
      <h2>${esc(r.goal||"(无内容)")}</h2>
      ${badge(r.status)}
    </div>
    <div class="d-meta">
      <span class="meta-cell">${icon("clock")} ${fmtTime(r.started_at)}</span>
      <span class="meta-cell">${icon("clock")} 耗时 <b>${fmtDur(dur)}</b></span>
      <span class="meta-cell">模式 <b>${MODE_CN[r.mode]||esc(r.mode)}</b></span>
      <span class="meta-cell">${icon("zap")} ${r.total_steps} 步 · ${icon("wrench")} ${r.tool_calls} 次</span>
      ${model?`<span class="meta-cell">模型 <b>${esc(model)}</b></span>`:""}
      ${emo?`<span class="meta-cell">情绪 <b>${esc(emo)}</b></span>`:""}
    </div>

    <div id="runCompanionBlock">${renderCompanionForRun(r)}</div>

    ${isFail&&errMsg?`<div class="section-title">${icon("xcircle")} 错误信息</div>
      <div class="error-banner"><span>${icon("alert")}</span><div>${esc(errMsg)}</div></div>`:""}

    <div class="section-title">${icon("chat")} 最终回答</div>
    ${answerDupError
      ? `<div class="hint">${icon("alert")} 本次生成在出错前未产生有效回答，原因见上方错误信息。</div>`
      : `<div class="answer ${r.status==="failed"?"error":""}">${miniMd(answer)||"(空)"}</div>`}

    <div class="section-title">${icon("compass")} 执行过程</div>
    ${tlHtml}
    ${streamHtml}
    ${rawHtml}
  </div>`;
}

async function selectRun(id){
  state.selectedId=id;
  renderRunList();
  $("#detailPanel").innerHTML=`<div class="empty"><div class="big">${icon("hourglass", 40)}</div><div>加载中…</div></div>`;
  try{
    const r=await api("/api/runs/"+id);
    if(state.selectedId===id){ state.detail=r; renderDetail(r); }
  }catch(e){
    $("#detailPanel").innerHTML=`<div class="empty"><div class="big">${icon("alert", 40)}</div><div>加载失败：${esc(e.message)}</div></div>`;
  }
}

function renderMemory(data){
  const items=(data&&data.items)||[];
  $("#memCount").textContent=items.length+" 条";
  if(!items.length){
    $("#memGrid").innerHTML=`<div class="empty" style="grid-column:1/-1;min-height:220px;"><div class="big">${icon("brain", 40)}</div><div>还没有记住任何事</div></div>`;
    return;
  }
  $("#memGrid").innerHTML=items.map(m=>`
    <div class="mem">
      <div class="content">${esc(m.content)}</div>
      <div class="foot">
        <span class="cat-tag">${esc(m.category||"未分类")}</span>
        <span>${fmtTime(m.created_at)}</span>
      </div>
    </div>`).join("");
}

function renderTools(){
  const s=state.stats; if(!s) return;
  const dist=s.tool_distribution||{};
  const entries=Object.entries(dist);
  const total=entries.reduce((a,[,n])=>a+n,0)||1;
  if(!entries.length){
    $("#toolBars").innerHTML=`<div class="hint">还没有调用过工具。</div>`;
  }else{
    $("#toolBars").innerHTML=entries.map(([k,v])=>{
      const pct=Math.round(v/total*100);
      return `<div class="bar-row">
        <div class="name">${TOOL_NAME[k]?esc(TOOL_NAME[k])+" · ":""}<span style="font-family:Consolas;font-size:11px;color:var(--sub);">${esc(k)}</span></div>
        <div class="track"><div class="fill" style="width:${Math.max(pct,6)}%">${v}</div></div>
      </div>`;
    }).join("");
  }
  const st=[
    ["成功",s.success_runs||0,"var(--green)"],
    ["失败",s.failed_runs||0,"var(--red)"],
    ["进行中",s.running_runs||0,"var(--blue)"],
    ["已取消",s.cancelled_runs||0,"var(--amber)"],
  ];
  const stTotal=s.total_runs||1;
  $("#statusBars").innerHTML=st.map(([n,v,col])=>{
    const pct=Math.round(v/stTotal*100);
    return `<div class="bar-row"><div class="name">${n}</div>
      <div class="track"><div class="fill" style="width:${Math.max(pct,v?6:0)}%;background:${col};">${v}</div></div></div>`;
  }).join("");
}

function toggleResult(id,btn){
  const box=$("#"+id);
  const open=box.classList.toggle("expanded");
  btn.textContent=open?"收起 ▴":"展开结果 ▾";
}


// ---------- v23: 桌宠养成 trace 反馈 (嵌到会话详情顶部, 不是独立 tab) ----------
const RISK_CN = {0:"纯读", 1:"低", 2:"中", 3:"高", 4:"不可逆"};
const RISK_CLS = {0:"LOW", 1:"LOW", 2:"MID", 3:"HIGH", 4:"HIGH"};
const DEC_CLS = {ALLOW:"HIGH", CONFIRM:"MID", REJECT:"LOW", REFUSE:"LOW"};

// 缓存 /api/companion 拉的 decisions + outcomes + state, renderDetail 时按时间窗筛
state.companion = null;

async function loadCompanionCache(){
  try{
    state.companion = await api("/api/companion?decision_limit=200&outcome_limit=200");
  }catch(_){
    state.companion = null;
  }
}

function renderCompanionForRun(run){
  // 嵌到会话详情顶部: 该 run 期间的 trace_decision + trace_outcome + 命中规则
  if(!state.companion){
    return `<div class="hint" style="margin:10px 0;">桌宠养成 trace 加载失败 (主程序可能没在跑 / pet_state.json 没生成).</div>`;
  }
  const c = state.companion;
  const t0 = run.started_at || 0;
  const t1 = run.finished_at || (Date.now()/1000);
  // 时间窗 + thread_id 匹配 (多窗口重叠 → 取最近)
  const inWindow = (r) => r.ts >= t0 - 5 && r.ts <= t1 + 5;
  const threadId = run.meta && run.meta.thread_id;
  const decs = (c.decisions||[]).filter(d => inWindow(d) && (!threadId || d.thread_id === threadId || d.thread_id === "default"));
  const outs = (c.outcomes||[]).filter(o => inWindow(o) && (!threadId || o.thread_id === threadId || o.thread_id === "default"));

  // 1) 调前 S=量化 (取该 run 期间最后一次 decision 的 sat/mood/level/favor)
  const lastQ = decs.length > 0 ? decs[decs.length-1] : null;
  const qtag = (k) => lastQ ? `<span class="qtag ${lastQ[k]||"MID"}">${k}=${esc(lastQ[k]||"?")}</span>` : "";

  // 2) 调后 Δ (把 run 期间所有 outcome 累加)
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
  const arr = (n) => n > 0 ? `+${n.toFixed(1)}` : n < 0 ? n.toFixed(1) : "0";
  const lvChg = (lv0!==null && lv1!==null && lv0!==lv1) ? ` Lv.${lv0}→${lv1}` : "";

  // 3) 命中规则 (decision 的 note + decision 名, 主人 yaml 那 27+6 条)
  // companion_log.txt 当前没存 note, 留个 hint
  const ruleHits = decs.map(d => `<span class="mini-tag" style="margin:2px 4px 2px 0;">${esc(d.tool)} ${esc(d.decision)}</span>`).join("");

  if (decs.length === 0 && outs.length === 0){
    return `<div class="hint" style="margin:10px 0;">本会话没有桌宠 trace 记录 (调的工具都 SAFE / 时间窗外 / 主程序没跑).</div>`;
  }

  return `
    <div class="section-title" data-icon="heart">桌宠养成反馈 (本次 run)</div>
    <div class="cs-compact">
      <div class="cs-block">
        <b>调前 S = 量化</b> ${qtag("sat")} ${qtag("mood")} ${qtag("level")} ${qtag("favor")}
      </div>
      <div class="cs-block">
        <b>调后 Δ</b> · 心情 ${arr(dFeeling)} · 饱食 ${arr(dFood)} · 好感 ${arr(dFavor)} · EXP ${arr(dExp)}${esc(lvChg)}
      </div>
      ${ruleHits ? `<div class="cs-block"><b>命中规则</b> · ${ruleHits}</div>` : ""}
    </div>
  `;
}

async function refresh(){
  try{
    const [s,runs]=await Promise.all([api("/api/stats"),api("/api/runs?limit=100")]);
    state.stats=s; state.runs=runs;
    await loadCompanionCache();    // v23-9: 每 5s 跟 /api/companion 同步
    renderStats(); renderRunList(); renderTools();
    // 重渲染当前选中的 run (桌宠养成反馈可能时间窗内新数据)
    if(state.detail) renderDetail(state.detail);
    $("#liveText").textContent="更新于 "+new Date().toLocaleTimeString();
    // 正在进行中的会话：实时刷新详情
    const cur=runs.find(r=>r.id===state.selectedId);
    if(cur && cur.status==="running") selectRun(state.selectedId);
    if($("#view-memory").classList.contains("active") && !state._memLoaded){
      state._memLoaded=true;
      renderMemory(await api("/api/memory"));
    }
  }catch(e){
    $("#liveBox").classList.add("off");
    $("#liveText").textContent="连接失败";
  }
}

// 事件绑定
$$(".tab").forEach(t=>t.onclick=()=>{
  $$(".tab").forEach(x=>x.classList.remove("active"));
  t.classList.add("active");
  $$(".view").forEach(v=>v.classList.remove("active"));
  $("#view-"+t.dataset.view).classList.add("active");
  if(t.dataset.view==="memory") api("/api/memory").then(renderMemory);
});
$("#runList").addEventListener("click",e=>{
  const item=e.target.closest(".run-item");
  if(item) selectRun(item.dataset.id);
});
$("#filters").addEventListener("click",e=>{
  const c=e.target.closest(".chip"); if(!c)return;
  $$("#filters .chip").forEach(x=>x.classList.remove("active"));
  c.classList.add("active"); state.filter=c.dataset.f; renderRunList();
});
$("#search").addEventListener("input",e=>{state.kw=e.target.value.trim().toLowerCase();renderRunList();});
$("#liveBox").onclick=()=>{
  state.auto=!state.auto;
  $("#liveBox").classList.toggle("off",!state.auto);
  $(".dot",$("#liveBox")).classList.toggle("pulse",state.auto);
  $("#liveText").textContent=state.auto?("自动刷新中"):"已暂停";
};

// 填充静态 HTML 中的 data-icon 占位（统一从 ICONS 取 SVG）
$$("[data-icon]").forEach(el=>{
  const sz = el.dataset.size ? parseInt(el.dataset.size, 10) : 15;
  el.insertAdjacentHTML("afterbegin", icon(el.dataset.icon, sz));
});

refresh();
setInterval(()=>{ if(state.auto) refresh(); },5000);
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
        """v23-10: 长期记忆读 sqlite memory.db (不是 memory.json).

        桌宠实际存的路径是 data/memory.db (MemoryStore v22 改用 sqlite),
        旧 memory.json 已废弃. memory_file 参数是兼容旧 dashboard 配置.
        """
        import sqlite3
        # 优先读 sqlite memory.db (MemoryStore 实际路径)
        # 1) 默认 cwd/data/memory.db
        # 2) 兼容 memory_file 路径 (用 .db 后缀替换 .json)
        candidates = []
        if memory_file is not None:
            mp = Path(memory_file)
            candidates.append(mp.with_suffix(".db") if mp.suffix == ".json" else mp)
            candidates.append(mp)  # 原路径也试
        candidates.append(Path.cwd() / "data" / "memory.db")

        for path in candidates:
            if path.is_file():
                try:
                    conn = sqlite3.connect(str(path))
                    try:
                        cur = conn.execute(
                            "SELECT id, content, category, importance, created_at "
                            "FROM memories ORDER BY importance DESC, created_at DESC"
                        )
                        items = [
                            {
                                "id": row[0],
                                "content": row[1],
                                "category": row[2],
                                "importance": row[3],
                                "created_at": row[4],
                            }
                            for row in cur.fetchall()
                        ]
                    finally:
                        conn.close()
                    return {"items": items, "count": len(items), "source": str(path)}
                except Exception as e:  # noqa: BLE001
                    return {"items": [], "error": str(e), "tried": str(path)}
        # 都没找到, fallback 到 JSON (兼容老 dashboard 配)
        if memory_file is not None and Path(memory_file).is_file():
            try:
                data = json.loads(Path(memory_file).read_text("utf-8"))
            except Exception as e:  # noqa: BLE001
                return {"items": [], "error": str(e)}
            data.sort(key=lambda x: (-float(x.get("importance", 0.5)),
                                     -float(x.get("created_at", 0))))
            return {"items": data, "count": len(data), "source": str(memory_file)}
        return {"items": [], "note": "memory.db not found", "tried": [str(p) for p in candidates]}

    @app.get("/api/companion")
    async def companion(decision_limit: int = 30, outcome_limit: int = 30):
        """v23: 桌宠养成面板 - 当前 PetState + yaml 配置 + trace 日志.

        数据源:
          - state: data/pet_state.json (yaml persistence.file 配置)
          - trace: data/companion_log.txt (v23-1 接好的 JSON-line)
          - config: app/core/companion_config.yaml (实时读)
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

        # 2) 读 companion_log.txt
        log_path = Path(cfg.trace.log_file)
        if not log_path.is_absolute():
            log_path = Path.cwd() / log_path
        decisions = []
        outcomes = []
        if log_path.is_file():
            try:
                for line in log_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line: continue
                    rec = json.loads(line)
                    if rec.get("kind") == "outcome":
                        outcomes.append(rec)
                    elif "decision" in rec and "kind" not in rec:
                        decisions.append(rec)
            except Exception:  # noqa: BLE001
                pass
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
