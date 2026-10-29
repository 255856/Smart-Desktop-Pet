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


log = logging.getLogger(__name__)

#: 记忆神经网络里最多画多少个节点（前端 JS 的 MAX_NODES 用这个值填进去）
MEM_NET_MAX_NODES = 220

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
    /* 基础面 */
    --bg:#f7f8fb; --card:#ffffff; --surface:#fff;
    --surface-sunken:#f4f6fa; --surface-hover:#f5f6fa;
    --card-border:#e4e9f1; --border:#dfe4ed; --line:#edf0f5;
    --text:#202635; --text-strong:#293044; --sub:#5f6a7d; --faint:#8995af;
    /* 品牌强调（蓝） */
    --accent:#4d6bfe; --accent-d:#405de5; --accent-bg:#edf1ff; --accent-lt:#c8d4ff;
    /* 语义状态 */
    --green:#168467; --green-bg:#eaf7f1;
    --red:#bd3b3b; --red-bg:#fff0ed;
    --amber:#b47720; --amber-bg:#fff8ee;
    --blue:#4d6bfe; --blue-bg:#edf1ff;
    --shadow:0 1px 2px rgba(23,45,81,.04),0 10px 28px rgba(23,45,81,.05);
    --shadow-sm:0 1px 2px rgba(23,45,81,.05);
  }
  *{box-sizing:border-box;}
  html,body{height:100%;}
  body{
    font-family:"Microsoft YaHei UI","Microsoft YaHei","PingFang SC",-apple-system,"Segoe UI",sans-serif;
    margin:0; background:var(--bg); color:var(--text); font-size:13.5px;
    -webkit-font-smoothing:antialiased;
  }

  /* ============ 侧边栏 ============ */
  .sidebar{position:fixed;inset:0 auto 0 0;width:232px;background:var(--surface);
    border-right:1px solid var(--card-border);padding:24px 14px 16px;
    display:flex;flex-direction:column;z-index:30;}
  .brand{display:flex;align-items:center;gap:11px;padding:0 8px 20px;}
  .brand-mark{width:38px;height:38px;border-radius:11px;background:var(--accent-bg);
    display:flex;align-items:center;justify-content:center;font-size:19px;flex:none;}
  .brand-text{font-size:14.5px;font-weight:700;color:var(--text-strong);line-height:1.25;}
  .brand-text small{display:block;font-size:10.5px;font-weight:500;color:var(--faint);margin-top:2px;}
  .nav-label{font-size:10.5px;font-weight:700;letter-spacing:1.1px;color:var(--faint);
    text-transform:uppercase;padding:15px 10px 7px;}
  .nav{display:flex;flex-direction:column;gap:2px;}
  .sidebar-bottom{margin-top:auto;display:flex;align-items:center;gap:8px;
    padding:13px 10px 2px;border-top:1px solid var(--line);}
  .local-dot{width:7px;height:7px;border-radius:50%;background:var(--green);flex:none;}
  .sb-text{font-size:11.5px;color:var(--sub);}

  /* ============ 工作区 ============ */
  .workspace{margin-left:232px;min-height:100vh;display:flex;flex-direction:column;min-width:0;}
  .topbar{height:62px;border-bottom:1px solid var(--card-border);
    background:rgba(255,255,255,.82);backdrop-filter:saturate(180%) blur(8px);
    display:flex;align-items:center;justify-content:space-between;
    padding:0 32px;position:sticky;top:0;z-index:20;}
  .breadcrumb{font-size:13px;display:flex;align-items:center;gap:8px;color:var(--faint);}
  .breadcrumb .sep{color:var(--border);}
  #crumbPage{color:var(--text-strong);font-weight:600;}
  .topbar-actions{display:flex;align-items:center;gap:14px;}
  main{padding:30px 34px 54px;max-width:1560px;width:100%;margin:0 auto;}
  .page-heading{margin-bottom:22px;}
  .eyebrow{font-size:10.5px;font-weight:700;letter-spacing:1.6px;color:var(--accent);}
  .page-heading h1{font-size:23px;font-weight:700;margin:8px 0 7px;color:var(--text-strong);letter-spacing:.3px;}
  .page-heading p{font-size:13.5px;color:var(--sub);margin:0;}

  /* 顶栏 live 状态 */
  .live{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--sub);
    background:var(--card);border:1px solid var(--border);border-radius:999px;
    padding:7px 14px;box-shadow:var(--shadow-sm);}
  .live .dot{width:8px;height:8px;border-radius:50%;background:var(--green);}
  .live.off .dot{background:var(--faint);}
  .live .dot.pulse{animation:pulse 1.4s infinite;}
  @keyframes pulse{0%{box-shadow:0 0 0 0 rgba(22,132,103,.45);}70%{box-shadow:0 0 0 7px rgba(22,132,103,0);}100%{box-shadow:0 0 0 0 rgba(22,132,103,0);}}

  /* 概览卡（8 张） */
  .stats{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:22px;}
  /* 概览卡只在首页（运行）显示；其余视图直接聚焦该视图内容（数据在其专属 KPI 中） */
  body[data-view]:not([data-view="runs"]) .stats{display:none;}
  .stat{background:var(--card);border:1px solid var(--card-border);border-radius:14px;
    padding:15px 17px;box-shadow:var(--shadow-sm);position:relative;overflow:hidden;
    transition:box-shadow .18s ease,transform .18s ease;}
  .stat:hover{box-shadow:var(--shadow);transform:translateY(-1px);}
  .stat .ico{font-size:16px;display:flex;align-items:center;}
  .stat .ico .ic{display:block;}
  .ic{display:inline-block;vertical-align:-3px;flex:none;}
  .stat .num{font-size:26px;font-weight:700;margin-top:7px;line-height:1.1;letter-spacing:.3px;}
  .stat .label{font-size:12px;color:var(--sub);margin-top:4px;}
  .stat .bar{position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--accent);}
  .stat.green .bar{background:var(--green);} .stat.green .num{color:var(--green);}
  .stat.blue .bar{background:var(--blue);} .stat.blue .num{color:var(--blue);}
  .stat.amber .bar{background:var(--amber);} .stat.amber .num{color:var(--amber);}
  .stat.violet .bar{background:var(--accent);} .stat.violet .num{color:var(--accent-d);}
  .est-tag{display:inline-block;background:var(--amber-bg);color:var(--amber);border-radius:5px;
    padding:1px 6px;font-size:10.5px;font-weight:700;margin-left:6px;vertical-align:2px;}

  /* 侧边栏导航（.tab） */
  .tab{display:flex;align-items:center;gap:10px;width:100%;text-align:left;
    padding:9px 11px;border-radius:10px;border:1px solid transparent;background:transparent;
    font-family:inherit;font-size:13.5px;font-weight:600;color:var(--sub);
    cursor:pointer;transition:background .15s,color .15s;white-space:nowrap;}
  .tab .ic{flex:none;color:var(--faint);}
  .tab:hover{background:var(--surface-hover);color:var(--text-strong);}
  .tab:hover .ic{color:var(--sub);}
  .tab.active{background:var(--accent-bg);color:var(--accent-d);border-color:var(--accent-lt);}
  .tab.active .ic{color:var(--accent);}
  .view{display:none;} .view.active{display:block;animation:fade .28s ease;}
  @keyframes fade{from{opacity:0;transform:translateY(5px);}to{opacity:1;transform:none;}}

  /* 会话视图：左右栏 */
  .sessions{display:grid;grid-template-columns:340px 1fr;gap:16px;align-items:start;}
  .panel{background:var(--card);border:1px solid var(--card-border);border-radius:14px;
    box-shadow:var(--shadow-sm);overflow:hidden;}
  .list-head{padding:14px 14px 10px;border-bottom:1px solid var(--line);}
  .search{width:100%;border:1px solid var(--border);border-radius:10px;padding:9px 12px;
    font-size:13px;outline:none;font-family:inherit;background:var(--surface);color:var(--text);}
  .search::placeholder{color:var(--faint);}
  .search:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(77,107,254,.12);}
  .filters{display:flex;gap:6px;margin-top:10px;flex-wrap:wrap;}
  .chip{padding:4px 12px;border-radius:999px;font-size:12px;cursor:pointer;border:1px solid var(--border);
    background:#fff;color:var(--sub);user-select:none;transition:.15s;}
  .chip:hover{border-color:var(--accent-lt);color:var(--accent-d);}
  .chip.active{background:var(--accent-bg);border-color:var(--accent-lt);color:var(--accent-d);font-weight:600;}
  .run-list{max-height:calc(100vh - 236px);overflow-y:auto;padding:8px;}
  .run-item{border-radius:11px;padding:11px 12px;cursor:pointer;border:1px solid transparent;margin-bottom:4px;transition:.15s;}
  .run-item:hover{background:var(--surface-hover);}
  .run-item.active{background:var(--accent-bg);border-color:var(--accent-lt);}
  .run-item .goal{font-size:13px;font-weight:600;line-height:1.4;display:-webkit-box;
    -webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;word-break:break-word;}
  .run-item .meta{display:flex;align-items:center;gap:8px;margin-top:6px;font-size:11.5px;color:var(--sub);flex-wrap:wrap;}
  .run-item .meta .t{white-space:nowrap;}
  .badge{display:inline-flex;align-items:center;gap:4px;padding:2px 9px;border-radius:999px;
    font-size:11px;font-weight:600;line-height:1.5;}
  .badge.success{background:var(--green-bg);color:var(--green);}
  .badge.failed{background:var(--red-bg);color:var(--red);}
  .badge.running{background:var(--blue-bg);color:var(--accent-d);}
  .badge.cancelled{background:var(--amber-bg);color:var(--amber);}
  .badge.ghost{background:var(--surface-sunken);color:var(--sub);}
  .mini-tag{display:inline-flex;align-items:center;gap:3px;background:var(--surface-sunken);color:var(--sub);
    border:1px solid var(--line);border-radius:6px;padding:1px 7px;font-size:11px;}
  .mini-tag.warn{background:var(--red-bg);color:var(--red);border-color:#f5c6c6;}
  .mini-tag.ok{background:var(--green-bg);color:var(--green);border-color:#bfe8d5;}
  .mini-tag.est{background:var(--amber-bg);color:var(--amber);border-color:#f6ddb0;}

  /* 详情 */
  .detail{padding:22px 24px;min-height:600px;}
  .empty{min-height:600px;display:flex;flex-direction:column;align-items:center;justify-content:center;
    color:var(--faint);gap:12px;}
  .empty .big{font-size:44px;}
  .d-head{display:flex;align-items:flex-start;gap:12px;flex-wrap:wrap;}
  .d-head h2{margin:0;font-size:18px;line-height:1.4;flex:1;min-width:240px;word-break:break-word;}
  .d-meta{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0 4px;}
  .meta-cell{background:var(--surface-sunken);border:1px solid var(--line);border-radius:10px;
    padding:7px 13px;font-size:12px;color:var(--sub);}
  .meta-cell b{color:var(--text);font-weight:600;}
  .section-title{font-size:13px;font-weight:700;color:var(--accent-d);margin:22px 0 10px;
    display:flex;align-items:center;gap:7px;}
  .answer{background:var(--surface-sunken);border:1px solid var(--line);
    border-radius:13px;padding:16px 18px;line-height:1.7;font-size:13.5px;white-space:pre-wrap;
    word-break:break-word;}
  .answer.error{background:var(--red-bg);border-color:#f5c6c6;color:#b42318;}
  .answer strong{color:var(--text-strong);}
  .answer code{background:#e9ecf3;color:#40465a;border-radius:4px;padding:1px 6px;font-size:12.5px;}
  .answer pre{background:#232a3d;color:#e5e7eb;padding:10px 12px;border-radius:9px;overflow-x:auto;
    white-space:pre-wrap;}

  /* 时间线 */
  .timeline{position:relative;padding-left:26px;}
  .timeline::before{content:"";position:absolute;left:9px;top:6px;bottom:6px;width:2px;background:var(--line);}
  .tl-item{position:relative;margin-bottom:12px;}
  .tl-item .node{position:absolute;left:-26px;top:2px;width:20px;height:20px;border-radius:50%;
    display:flex;align-items:center;justify-content:center;font-size:11px;background:#fff;
    border:2px solid var(--accent-lt);z-index:1;}
  .tl-item.tool .node{border-color:var(--amber);background:var(--amber-bg);}
  .tl-item.plan .node{border-color:var(--accent);background:var(--blue-bg);}
  .tl-item.reflection .node{border-color:var(--accent);background:var(--accent-bg);}
  .tl-item.meta .node{border-color:var(--faint);background:var(--surface-sunken);}
  .tl-item.error .node{border-color:var(--red);background:var(--red-bg);}
  .tl-card{border:1px solid var(--card-border);border-radius:11px;padding:11px 14px;background:#fff;}
  .tl-card .cap{font-size:12.5px;font-weight:700;margin-bottom:5px;display:flex;align-items:center;gap:8px;}
  .tl-card.tool .cap{color:#9a6418;}
  .kv{display:flex;flex-wrap:wrap;gap:6px;margin:4px 0;}
  .kv .pair{background:var(--surface-sunken);border:1px solid var(--line);border-radius:7px;padding:3px 9px;
    font-size:12px;}
  .kv .pair .k{color:var(--sub);margin-right:5px;}
  .kv .pair .v{font-family:Consolas,monospace;color:var(--text);word-break:break-all;}
  .result-box{background:var(--surface-sunken);border-radius:9px;padding:8px 11px;font-size:12.5px;
    color:var(--sub);margin-top:6px;white-space:pre-wrap;word-break:break-word;
    border:1px solid var(--line);font-family:Consolas,monospace;max-height:140px;overflow:hidden;}
  .result-box.expanded{max-height:none;}
  .expand-btn{margin-top:6px;background:none;border:none;color:var(--accent);cursor:pointer;
    font-size:12px;padding:0;font-family:inherit;}
  details.raw{margin-top:10px;}
  details.raw summary{cursor:pointer;font-size:12px;color:var(--sub);user-select:none;}
  details.raw pre{background:var(--surface-sunken);border:1px solid var(--line);border-radius:9px;padding:10px;
    font-size:11.5px;overflow-x:auto;white-space:pre-wrap;word-break:break-word;margin:8px 0 0;}

  /* 通用块 */
  .hint{font-size:12.5px;color:var(--sub);background:var(--surface-sunken);border:1px dashed var(--border);
    border-radius:10px;padding:10px 13px;}
  .hint.warn{background:var(--red-bg);border-color:#f5c6c6;color:#b42318;}
  .hint.ok{background:var(--green-bg);border-color:#bfe8d5;color:#0a7c45;}
  .hint.amber{background:var(--amber-bg);border-color:#f6ddb0;color:#9a6418;}
  .grid2{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px;}
  .grid3{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px;}
  .kpi{background:var(--card);border:1px solid var(--card-border);border-radius:13px;padding:13px 15px;
    box-shadow:var(--shadow-sm);}
  .kpi .k{font-size:11.5px;color:var(--sub);}
  .kpi .v{font-size:20px;font-weight:700;margin-top:4px;line-height:1.2;}
  .kpi .x{font-size:11px;color:var(--faint);margin-top:3px;}
  .mem{background:var(--card);border:1px solid var(--card-border);border-radius:13px;padding:15px 17px;
    box-shadow:var(--shadow-sm);}
  .mem .content{font-size:13.5px;line-height:1.6;word-break:break-word;white-space:pre-wrap;}
  .mem .foot{display:flex;align-items:center;gap:8px;margin-top:10px;font-size:11.5px;color:var(--sub);flex-wrap:wrap;}
  .cat-tag{background:var(--accent-bg);color:var(--accent-d);border-radius:6px;padding:2px 9px;font-size:11px;font-weight:600;}

  /* 记忆视图：神经网络 / 列表 切换 */
  .mem-mode{display:inline-flex;background:var(--surface-sunken);border:1px solid var(--line);
    border-radius:10px;padding:3px;gap:2px;margin:18px 0 14px;}
  .mem-mode-btn{border:0;background:transparent;color:var(--sub);font-size:12.5px;font-weight:600;
    padding:6px 16px;border-radius:8px;cursor:pointer;font-family:inherit;transition:.15s;}
  .mem-mode-btn:hover{color:var(--text);}
  .mem-mode-btn.active{background:#fff;color:var(--accent-d);box-shadow:var(--shadow-sm);}

  /* 深色神经网络画布卡片 */
  .mem-net{position:relative;height:560px;border-radius:14px;overflow:hidden;
    background:radial-gradient(120% 120% at 50% 20%,#0e1830 0%,#0a1122 55%,#070c18 100%);
    border:1px solid #1c2942;}
  .mem-net canvas{position:absolute;inset:0;width:100%;height:100%;display:block;cursor:grab;}
  .mem-net canvas.dragging{cursor:grabbing;}
  .mem-net-legend{position:absolute;left:14px;top:14px;display:flex;flex-direction:column;gap:7px;
    background:rgba(10,17,34,.55);border:1px solid rgba(120,150,210,.18);border-radius:11px;
    padding:11px 13px;backdrop-filter:blur(6px);}
  .mem-net-legend .lg-row{display:flex;align-items:center;gap:8px;font-size:11.5px;color:#c3cde4;}
  .mem-net-legend .lg-dot{width:9px;height:9px;border-radius:50%;flex:none;}
  .mem-net-legend .lg-note{margin-top:3px;padding-top:8px;font-size:10.5px;line-height:1.7;
    color:#8fa0c4;border-top:1px solid rgba(120,150,210,.16);}
  .k-thought{background:#6bb8ff;}
  .k-topic{background:#61e0d1;}
  .k-event{background:#8cd17a;}
  .k-fact{background:#f5c766;}
  .k-impression{background:#f58fc2;}
  .k-other{background:#b3c2eb;}
  .mem-net-hint{position:absolute;right:14px;top:14px;font-size:11px;color:#8fa0c4;
    background:rgba(10,17,34,.55);border:1px solid rgba(120,150,210,.18);border-radius:9px;
    padding:7px 11px;backdrop-filter:blur(6px);white-space:nowrap;display:none;}
  .mem-net-badge{position:absolute;left:14px;bottom:14px;font-size:11px;color:#a9badb;
    background:rgba(10,17,34,.55);border:1px solid rgba(120,150,210,.18);border-radius:9px;
    padding:5px 10px;backdrop-filter:blur(6px);font-variant-numeric:tabular-nums;}
  .mem-net-reset{position:absolute;right:14px;top:52px;font-size:11px;color:#c3cde4;
    background:rgba(10,17,34,.55);border:1px solid rgba(120,150,210,.22);border-radius:9px;
    padding:6px 11px;cursor:pointer;font-family:inherit;backdrop-filter:blur(6px);}
  .mem-net-reset:hover{color:#fff;border-color:rgba(140,170,235,.4);}
  .mem-tip{position:absolute;z-index:5;max-width:280px;pointer-events:none;opacity:0;
    background:rgba(14,22,42,.92);border:1px solid rgba(140,170,235,.3);border-radius:10px;
    padding:10px 12px;font-size:12px;line-height:1.55;color:#e6ecf8;
    box-shadow:0 10px 30px rgba(0,0,0,.4);transition:opacity .12s;}
  .mem-tip.show{opacity:1;}
  .mem-tip .tip-kind{display:inline-block;font-size:10.5px;font-weight:700;border-radius:5px;
    padding:1px 7px;margin-bottom:6px;}
  .mem-tip .tip-text{color:#dfe6f5;word-break:break-word;}
  .mem-tip .tip-meta{margin-top:6px;font-size:10.5px;color:#8fa0c4;}
  .mem-net-empty{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;
    justify-content:center;gap:10px;color:#8fa0c4;font-size:13px;}
  .mem-net-empty .ic{opacity:.7;}
  .mem-node-detail{margin-top:14px;background:var(--surface-sunken);border:1px solid var(--line);
    border-radius:13px;padding:14px 16px;font-size:13px;line-height:1.65;}
  .mem-node-detail .nd-head{display:flex;align-items:center;gap:10px;margin-bottom:8px;flex-wrap:wrap;}
  .mem-node-detail .nd-close{margin-left:auto;border:1px solid var(--border);background:#fff;
    border-radius:8px;font-size:11px;padding:4px 11px;cursor:pointer;font-family:inherit;color:var(--sub);}
  .mem-node-detail .nd-content{word-break:break-word;white-space:pre-wrap;}
  .mem-node-detail .nd-meta{margin-top:10px;font-size:11.5px;color:var(--sub);display:flex;gap:14px;flex-wrap:wrap;}
  .mem-node-detail .nd-section{margin-top:12px;padding-top:10px;border-top:1px solid var(--line);}
  .mem-node-detail .nd-section-t{font-size:11px;font-weight:700;color:var(--faint);
    letter-spacing:.6px;margin-bottom:7px;}
  .mem-node-detail .nd-near{display:flex;align-items:center;gap:9px;width:100%;
    text-align:left;background:#fff;border:1px solid var(--line);border-radius:9px;
    padding:6px 10px;margin-bottom:5px;cursor:pointer;font-family:inherit;font-size:12.5px;color:var(--text);}
  .mem-node-detail .nd-near:hover{border-color:var(--accent-lt);background:var(--accent-bg);}
  .mem-node-detail .nd-near-k{width:8px;height:8px;border-radius:50%;flex:none;}
  .mem-node-detail .nd-near-t{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .mem-node-detail .nd-near-w{font-size:11px;color:var(--faint);flex:none;font-variant-numeric:tabular-nums;}
  .mem-node-detail .nd-empty{font-size:12px;color:var(--faint);}

  /* 睡觉面板 */
  .mem-sleep-bar{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:12px 0 4px;}
  .mem-sleep-time{border:1px solid var(--border);border-radius:9px;padding:6px 10px;
    font-size:13px;font-family:inherit;color:var(--text);background:#fff;}
  .mem-sleep-out{margin-top:10px;font-size:12.5px;color:var(--sub);line-height:1.75;}
  .mem-sleep-out .sl-line{display:flex;gap:8px;align-items:baseline;padding:3px 0;}
  .mem-sleep-out .sl-sum{padding-left:22px;color:var(--text);}
  .sub-head{font-size:14px;font-weight:700;margin:22px 0 12px;display:flex;align-items:center;gap:8px;flex-wrap:wrap;}
  .bar-row{display:flex;align-items:center;gap:12px;margin-bottom:12px;}
  .bar-row .name{width:170px;flex:none;font-size:13px;font-weight:600;}
  .bar-row .track{flex:1;height:22px;background:var(--surface-sunken);border:1px solid var(--line);border-radius:7px;overflow:hidden;}
  .bar-row .fill{height:100%;border-radius:6px;background:linear-gradient(90deg,#6aa0ff,#4d6bfe);
    display:flex;align-items:center;justify-content:flex-end;color:#fff;font-size:11px;font-weight:700;
    padding-right:8px;min-width:26px;transition:width .5s;}
  .error-banner{background:var(--red-bg);border:1px solid #f5c6c6;border-radius:12px;padding:12px 15px;
    color:#b42318;font-size:13px;line-height:1.6;white-space:pre-wrap;word-break:break-word;
    display:flex;gap:10px;align-items:flex-start;}

  /* 表格 */
  table.tbl{width:100%;border-collapse:collapse;font-size:12.5px;}
  table.tbl th{background:var(--surface-sunken);color:var(--sub);font-weight:600;text-align:left;
    padding:8px 10px;border-bottom:1px solid var(--border);white-space:nowrap;}
  table.tbl td{padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top;}
  table.tbl tr:hover td{background:var(--surface-hover);}
  table.tbl td.num{font-family:Consolas,monospace;text-align:right;white-space:nowrap;}
  table.tbl td.name{font-family:Consolas,monospace;font-size:12px;}
  .tbl-wrap{max-height:460px;overflow:auto;border:1px solid var(--line);border-radius:12px;}

  /* 桌宠养成 (v23) */
  .cs-row{display:flex;align-items:center;gap:10px;margin-bottom:9px;}
  .cs-row .name{width:90px;flex:none;font-size:13px;font-weight:600;}
  .cs-row .track{flex:1;height:20px;background:var(--bg);border-radius:6px;overflow:hidden;border:1px solid var(--line);
    position:relative;}
  .cs-row .track::after{content:"";position:absolute;inset:0;border-radius:6px;pointer-events:none;
    background:linear-gradient(90deg,transparent,rgba(110,150,245,.20),transparent);
    background-size:42% 100%;background-repeat:no-repeat;
    animation:trackShine 9s linear infinite;animation-delay:var(--d,0s);}
  .cs-row .fill{height:100%;border-radius:6px;display:flex;align-items:center;justify-content:flex-end;
    color:#fff;font-size:11px;font-weight:700;padding-right:7px;min-width:34px;transition:width .5s;
    position:relative;overflow:hidden;animation:barBreath 7s ease-in-out infinite;
    animation-delay:var(--d,0s);}
  /* 填充里一道来回扫的高光 */
  .cs-row .fill::after{content:"";position:absolute;top:0;bottom:0;left:0;width:34%;
    background:linear-gradient(90deg,rgba(255,255,255,0),rgba(255,255,255,.78),rgba(255,255,255,0));
    transform:translateX(-130%);animation:barShine 6.5s ease-in-out infinite;
    animation-delay:calc(var(--d,0s) + 1.2s);pointer-events:none;}
  @keyframes barBreath{0%,100%{filter:brightness(1);}50%{filter:brightness(1.12);}}
  @keyframes barShine{0%{transform:translateX(-130%);}55%,100%{transform:translateX(330%);}}
  @keyframes trackShine{0%{background-position:-42% 0;}100%{background-position:142% 0;}}
  @media (prefers-reduced-motion:reduce){
    .cs-row .fill,.cs-row .fill::after,.cs-row .track::after{animation:none;}
  }
  .cs-row .v{font-family:Consolas,monospace;font-size:12px;color:var(--sub);width:80px;text-align:right;flex:none;}
  .cs-row .fill.satiety{background:linear-gradient(90deg,#fbbf24,#f59e0b);}
  .cs-row .fill.mood{background:linear-gradient(90deg,#6aa0ff,#4d6bfe);}
  .cs-row .fill.exp{background:linear-gradient(90deg,#34d399,#10b981);}
  .cs-row .fill.favor{background:linear-gradient(90deg,#f472b6,#ec4899);}
  .cs-row .fill.level{background:linear-gradient(90deg,#60a5fa,#3b82f6);}
  .cs-row .fill.exp_to_next{background:linear-gradient(90deg,#cbd5e1,#94a3b8);}
  .qtag{display:inline-block;padding:2px 8px;border-radius:5px;font-size:11px;font-weight:600;margin-left:6px;}
  .qtag.LOW{background:var(--red-bg);color:#b42318;}
  .qtag.MID{background:var(--amber-bg);color:#9a6418;}
  .qtag.HIGH{background:var(--green-bg);color:#0a7c45;}
  .trace-row{display:grid;grid-template-columns:62px 1fr auto;gap:10px;align-items:center;
    padding:6px 8px;border-radius:7px;margin-bottom:4px;font-size:12.5px;border-left:3px solid var(--accent);}
  .trace-row.decision{border-left-color:var(--blue);background:var(--blue-bg);}
  .trace-row.outcome{border-left-color:var(--green);background:var(--green-bg);}
  .trace-row .ts{color:var(--sub);font-family:Consolas,monospace;font-size:11px;}
  .trace-row .body{font-family:Consolas,monospace;}
  .trace-row .arrow{font-size:11px;color:var(--sub);font-family:Consolas,monospace;text-align:right;}
  .config-block{background:var(--surface-sunken);border:1px solid var(--line);border-radius:8px;padding:8px 11px;
    font-size:12px;line-height:1.7;margin-bottom:8px;word-break:break-all;}
  .config-block b{color:var(--accent-d);}
  .cs-compact{display:flex;flex-direction:column;gap:6px;margin-bottom:8px;}
  .cs-block{background:var(--surface-sunken);border:1px solid var(--line);
    border-radius:11px;padding:9px 13px;font-size:13px;line-height:1.7;}
  .cs-block b{color:var(--accent-d);}

  /* 危险操作区 */
  .danger{border:1.5px dashed #f0b4b4;border-radius:14px;padding:15px 17px;background:#fffafa;}
  .danger h3{margin:0 0 6px;font-size:14px;color:#b42318;}
  .danger .ops{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0;}
  button.btn{border:1px solid var(--border);background:#fff;color:var(--text);border-radius:10px;
    padding:8px 14px;font-size:13px;font-family:inherit;cursor:pointer;transition:.15s;}
  button.btn:hover{border-color:var(--accent-lt);color:var(--accent-d);background:var(--accent-bg);}
  button.btn.danger-btn{border-color:#f0b4b4;color:#b42318;background:#fff;}
  button.btn.danger-btn:hover{background:var(--red-bg);}
  button.btn[disabled]{opacity:.45;cursor:not-allowed;}
  .switch{display:inline-flex;align-items:center;gap:8px;font-size:12.5px;color:var(--sub);
    cursor:pointer;user-select:none;}
  .switch input{width:16px;height:16px;accent-color:var(--accent);cursor:pointer;}

  /* 设置 Tab */
  .set-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px;}
  .set-card{background:var(--surface-sunken);border:1px solid var(--line);border-radius:12px;padding:12px 14px;}
  .set-card h4{margin:0 0 9px;font-size:13px;font-weight:700;color:var(--accent-d);}
  .set-row{display:flex;align-items:center;gap:10px;margin-bottom:8px;}
  .set-row .lbl{flex:1;font-size:12.5px;color:var(--sub);}
  .set-row .lbl .restart{display:inline-block;margin-left:5px;font-size:10px;color:#b45309;}
  .set-row input[type=text],.set-row input[type=password],.set-row input[type=number],
  .set-row select{border:1px solid var(--border);border-radius:8px;padding:5px 9px;font-size:12.5px;
    font-family:inherit;color:var(--text);background:#fff;min-width:110px;}
  .set-row input[type=range]{width:130px;accent-color:var(--accent);}
  .set-row .num{font-family:Consolas,monospace;font-size:12px;color:var(--text);
    min-width:52px;text-align:right;font-variant-numeric:tabular-nums;}
  .set-chips{display:flex;flex-wrap:wrap;gap:7px;}
  .set-chip{border:1px solid var(--border);background:#fff;border-radius:8px;padding:4px 10px;
    font-size:12px;font-family:inherit;color:var(--text);cursor:pointer;transition:.15s;}
  .set-chip:hover{border-color:var(--accent-lt);color:var(--accent-d);background:var(--accent-bg);}
  .set-chip.on{border-color:var(--accent);background:var(--accent-bg);color:var(--accent-d);font-weight:600;}
  .set-chip small{display:block;font-size:10px;color:var(--faint);}
  .set-avatars{display:flex;flex-wrap:wrap;gap:9px;}
  .set-avatar{width:62px;height:62px;border-radius:10px;border:2px solid var(--line);
    background:#fff center/cover no-repeat;cursor:pointer;padding:0;}
  .set-avatar.on{border-color:var(--accent);}
  .set-bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:14px;}

  ::-webkit-scrollbar{width:9px;height:9px;}
  ::-webkit-scrollbar-track{background:transparent;}
  ::-webkit-scrollbar-thumb{background:#cdd4e0;border-radius:6px;border:2px solid var(--bg);}
  ::-webkit-scrollbar-thumb:hover{background:#b3bccb;}
  @media(max-width:1100px){
    .stats{grid-template-columns:repeat(2,1fr);}
    .sessions{grid-template-columns:1fr;}
    .run-list{max-height:360px;}
  }
</style>
</head>
<body data-view="runs">
<aside class="sidebar" aria-label="控制台导航">
  <div class="brand">
    <span class="brand-mark">🐳</span>
    <span class="brand-text">桌宠智能体控制台<small>项目总览 · 文件观测器</small></span>
  </div>
  <div class="nav-label">数据总览</div>
  <nav class="nav">
    <button type="button" class="tab active" data-tab="runs" data-view="runs" data-icon="chat" title="会话记录：最近会话 / 最近错误 / 工具调用分布 / 耗时分布">运行</button>
    <button type="button" class="tab" data-tab="cost" data-view="cost" data-icon="zap">成本</button>
    <button type="button" class="tab" data-tab="memory" data-view="memory" data-icon="brain" title="长期记忆：live 库行数 / kind 与 importance 分布 / access_count 命中数 / 逐条内容">记忆</button>
    <button type="button" class="tab" data-tab="decision" data-view="decision" data-icon="compass">决策</button>
  </nav>
  <div class="nav-label">系统</div>
  <nav class="nav">
    <button type="button" class="tab" data-tab="tools" data-view="tools" data-icon="wrench" title="工具统计：61 内置 + 7 MCP 清单 / 风险等级 / 解锁等级 / 文档对账差异">工具</button>
    <button type="button" class="tab" data-tab="settings" data-view="settings" data-icon="zap" title="桌宠设置：视觉 / 行为 / 语音 / 模型（改完立刻生效）">设置</button>
    <button type="button" class="tab" data-tab="config" data-view="config" data-icon="info">配置</button>
    <button type="button" class="tab" data-tab="state" data-view="state" data-icon="heart">状态</button>
    <button type="button" class="tab" data-tab="health" data-view="health" data-icon="alert">健康与维护</button>
  </nav>
  <div class="sidebar-bottom">
    <span class="local-dot"></span>
    <span class="sb-text">只读 data/ · 独立子进程</span>
  </div>
</aside>

<div class="workspace">
  <header class="topbar">
    <div class="breadcrumb" id="crumb"><span id="crumbGroup">数据总览</span> <span class="sep">/</span> <span id="crumbPage">运行</span></div>
    <div class="topbar-actions">
      <div class="live" id="liveBox"><span class="dot pulse"></span><span id="liveText">自动刷新中</span></div>
    </div>
  </header>
  <main id="mainContent">
    <div class="page-heading">
      <div class="eyebrow">SMART DESKTOP PET / CONSOLE</div>
      <h1 id="pageTitle">桌宠智能体控制台</h1>
      <p id="pageDesc">项目总览面板 · 文件观测器（只读 data/，独立子进程）</p>
    </div>

    <div class="stats" id="stats"></div>

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
      <div class="sub-head" style="margin-top:0;" data-icon="brain">长期记忆（sidecar） <span id="memCount" class="mini-tag"></span></div>
      <div id="memStoreHint"></div>
      <div class="grid3" id="memKpis" style="margin-top:12px;"></div>

      <div class="mem-mode" id="memMode" role="tablist">
        <button type="button" class="mem-mode-btn active" data-mode="net">网络</button>
        <button type="button" class="mem-mode-btn" data-mode="list">列表</button>
      </div>

      <!-- 3D 神经网络（深色画布卡片） -->
      <div class="mem-net" id="memNet">
        <canvas id="memCanvas"></canvas>
        <div class="mem-net-legend" id="memLegend"></div>
        <span class="mem-net-badge" id="memGraphBadge" style="display:none;"></span>
        <button type="button" class="mem-net-reset" id="memReset">重置视角</button>
        <div class="mem-tip" id="memTip"></div>
        <div class="mem-net-empty" id="memNetEmpty" style="display:none;"></div>
      </div>
      <div class="mem-node-detail" id="memNodeDetail" style="display:none;"></div>

      <!-- 睡觉：把记忆整理一遍 -->
      <div class="mem-sleep">
        <div class="sub-head" style="margin-top:20px;" data-icon="hourglass">睡觉 · 整理记忆 <span id="memSleepTag" class="mini-tag">没开定时</span></div>
        <div class="hint">睡觉做三件事：意思相近的几条合并成一条；不重要又久没碰的变模糊；不重要而且久到没救的直接忘掉。</div>
        <div class="mem-sleep-bar">
          <label class="switch"><input type="checkbox" id="memSleepOn">每天定时睡觉</label>
          <input type="time" id="memSleepAt" class="mem-sleep-time" value="03:00">
          <button type="button" class="btn" id="memSleepSave">保存定时</button>
          <button type="button" class="btn" id="memSleepRun">现在睡一觉</button>
        </div>
        <div id="memSleepOut" class="mem-sleep-out"></div>
      </div>

      <!-- 列表 / 分布 -->
      <div class="mem-list-wrap" id="memListWrap" style="display:none;">
        <div class="sub-head">按 kind / importance 分布</div>
        <div id="memDist"></div>
        <div class="sub-head">逐条内容（JSON 记忆卡）</div>
        <div class="grid2" id="memGrid"></div>
      </div>

      <div class="danger" style="border-color:#f0d3a0;background:#fffdf7;">
        <h3 style="color:#a36500;">索引重建（危险操作）</h3>
        <div class="hint">真值永远是 sidecar 的 JSON 卡片；重建只是让它把 Chroma 向量索引和 BM25 索引按卡片重算一遍，卡片本身不会被改动。要执行请到「健康与维护」Tab 打开维护模式。</div>
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

  <!-- ============ Tab 设置 ============ -->
  <div class="view" id="view-settings">
    <div class="panel" style="padding:18px;">
      <div class="sub-head" style="margin-top:0;" data-icon="zap">桌宠设置 <span id="settingsTag" class="mini-tag"></span></div>
      <div id="settingsErr"></div>
      <div class="set-bar">
        <button type="button" class="btn" id="settingsSave">保存全部</button>
        <button type="button" class="btn" id="settingsReset">恢复默认</button>
        <button type="button" class="btn" id="settingsReload">重新读取</button>
        <span class="hint" style="margin:0;">改动点「保存」后立刻在桌宠上生效。</span>
      </div>
      <div id="settingsGroups" class="set-grid"></div>
      <div class="sub-head">Live2D 外观</div>
      <div id="settingsAppearance"></div>
      <div class="sub-head">桌宠头像</div>
      <div id="settingsAvatars"></div>
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
      <div class="sub-head">记忆 sidecar 的索引（Chroma 向量 + BM25 + 精排 cross-encoder）</div>
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
          <button class="btn danger-btn" data-op="rebuild-vector-index" data-confirm="rebuild-vector-index">重建记忆索引</button>
        </div>
        <div id="maintResult"></div>
      </div>
    </div>
  </div>
  </main>
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

// 把绝对路径转成不含盘符/用户目录的短路径
function shortPath(p){
  if(!p) return "";
  p = String(p).replace(/\\/g,"/").replace(/^[a-zA-Z]:\/+/,"");
  var parts = p.split("/").filter(Boolean);
  var file = parts.length ? parts[parts.length-1] : p;
  var dir = parts.length>=2 ? parts[parts.length-2] : "";
  // 已知数据目录：保留「目录/文件名」（如 data/token-usage.jsonl）；其余只显示文件名
  if(/^(data|config|configs|logs|backups|assets|memory|memories|tmp)$/i.test(dir)){
    return dir.toLowerCase()+"/"+file;
  }
  return file;
}

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
  maintOn:false, memory:null,
};
let memMode="net"; // 记忆视图：net 神经网络 / list 列表

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
    + '<div class="label">'+esc(c.label)+'</div>'
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
  $("#costSource").textContent=shortPath(t.source);
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
/* 记忆 sidecar（独立进程：python -m memory_sidecar serve --port 8788）的状态行 */
function sidecarLine(sc){
  const ok=!!sc.reachable;
  const models=(sc&&sc.models)||{};
  const detail = ok
    ? ('在线 · '+(sc.cards==null?"?":sc.cards)+' 张卡 · 向量 '+esc(models.embed||"—")+' · 精排 '+esc(models.rerank||"—")
       +(sc.degraded?' · <b>已降级</b>：'+esc((sc.degradation||[]).join("；")):''))
    : ('没连上（'+esc(sc.error||"记忆服务没在跑")+'）—— 桌宠照常聊天，只是没有长期记忆');
  return '<div class="hint '+(ok?"ok":"warn")+'" style="margin-top:8px;">'
    + icon(ok?"check":"alert")+' 记忆 sidecar：<span class="name">'+esc(sc.url||"")+'</span> · '+detail+'</div>';
}

function renderMemory(data){
  const items=(data&&data.items)||[];
  const sc=(data&&data.sidecar)||{};
  const ok=!!(data&&data.ok);
  $("#memCount").textContent=(data&&data.count!=null?data.count:items.length)+" 条";

  const byKind=(data&&data.by_kind)||{}, byImp=(data&&data.by_importance)||{};
  $("#memStoreHint").innerHTML = sidecarLine(sc)
    + '<div class="hint" style="margin-top:8px;">'+icon("info")+' '+esc((data&&data.note)||"")+'</div>';

  $("#memKpis").innerHTML=[
    kpi("活跃卡片", fmtNum(items.length), "JSON 卡片（唯一真值）", ok?"green":"warn"),
    kpi("被召回过的卡片", fmtNum((data&&data.access_hits)||0), "hitCount > 0", "green"),
    kpi("向量索引", fmtNum(((data&&data.stats||{}).vector||{}).count||0), "Chroma 行数（派生）"),
    kpi("BM25 文档", fmtNum(((data&&data.stats||{}).bm25||{}).docs||0), "关键词腿（派生）"),
  ].join("");

  const kTotal=Object.keys(byKind).reduce(function(a,k){return a+byKind[k];},0);
  const iTotal=Object.keys(byImp).reduce(function(a,k){return a+byImp[k];},0);
  let dist="";
  if(!kTotal && !iTotal){
    dist = ok ? '<div class="hint">卡片库里还没有记忆。</div>'
              : '<div class="hint warn">'+icon("alert")+' 记忆 sidecar 没连上，没有可展示的卡片。</div>';
  }else{
    dist = '<div class="sub-head" style="margin-top:6px;font-size:13px;">按 kind</div>'
      + Object.keys(byKind).map(function(k){return barRow(esc(k||"fact"),byKind[k],kTotal);}).join("")
      + '<div class="sub-head" style="margin-top:14px;font-size:13px;">按 importance</div>'
      + Object.keys(byImp).sort(function(a,b){return Number(b)-Number(a);})
          .map(function(k){return barRow("重要性 "+esc(k),byImp[k],iTotal);}).join("");
  }
  $("#memDist").innerHTML=dist;

  if(!items.length){
    $("#memGrid").innerHTML=emptyBox(ok?"还没有记住任何事":"记忆 sidecar 没在跑","brain");
    return;
  }
  $("#memGrid").innerHTML=items.map(function(m){
    return '<div class="mem">'
      + '<div class="content">'+esc(m.content)+'</div>'
      + '<div class="foot">'
      + '<span class="cat-tag">'+esc(m.category||m.kind||"未分类")+'</span>'
      + '<span>重要性 '+Number(m.importance||0).toFixed(2)+'</span>'
      + '<span>'+fmtTime(m.created_at)+'</span>'
      + '<span class="mini-tag '+(Number(m.hit_count||0)>0?"ok":"")+'">命中 '+(m.hit_count||0)+'</span>'
      + '</div></div>';
  }).join("");
}

/* 睡觉：定时设置 + 这一觉的计划 + 上次的结果 */
function renderSleep(d){
  const st=(d&&d.state)||{};
  const plan=(d&&d.plan)||{};
  const counts=plan.counts||{};
  const tag=$("#memSleepTag");
  tag.textContent=st.enabled?("每天 "+st.time):"没开定时";
  tag.className="mini-tag"+(st.enabled?"":" warn");
  const on=$("#memSleepOn");
  if(on)on.checked=!!st.enabled;
  const at=$("#memSleepAt");
  if(at&&st.time)at.value=st.time;

  const out=[];
  if(plan.ok===false){
    out.push('<div class="sl-line">'+icon("alert")+' '
      +esc(plan.reason||"记忆 sidecar 没连上，算不出这一觉的计划。")+'</div>');
  }else{
    out.push('<div class="sl-line">'+icon("info")+' 这一觉会：合并 <b>'+(counts.clusters||0)
      +'</b> 组相似记忆，模糊 <b>'+(counts.blur||0)+'</b> 条，忘掉 <b>'+(counts.forget||0)+'</b> 条。</div>');
  }
  const last=st.lastReport||{};
  if(last.at){
    out.push('<div class="sl-line">'+icon("check")+' 上次睡觉（'+fmtTime(last.at/1000)
      +'）：合并 '+(last.merged||0)+' 组，模糊 '+(last.blurred||0)+' 条，忘掉 '
      +(last.forgotten||0)+' 条。</div>');
    (last.summaries||[]).forEach(function(s){
      out.push('<div class="sl-line sl-sum">· '+esc(s)+'</div>');
    });
  }
  $("#memSleepOut").innerHTML=out.join("");
}
async function loadSleep(note){
  let d=null;
  try{ d=await api("/api/memory/sleep"); }
  catch(e){ d={ok:false,plan:{ok:false,reason:e.message}}; }
  renderSleep(d);
  if(note){
    $("#memSleepOut").insertAdjacentHTML("afterbegin",
      '<div class="sl-line">'+icon("check")+' '+esc(note)+'</div>');
  }
}

/* ------------------------------------------------------------------
 * 记忆 · 3D 神经网络（原生 WebGL，自包含 / 离线 / 零依赖）
 * 每条记忆 = 一个发光神经元；连线 = 余弦近邻；点节点看它和谁挨着
 * ------------------------------------------------------------------ */
const MemNet = (function(){
  const KIND_ORDER=["thought","topic","event","fact","impression"];
  const KIND={
    thought:   {c:[0.42,0.72,1.00],t:"心里话"},
    topic:     {c:[0.38,0.88,0.82],t:"话题"},
    event:     {c:[0.55,0.82,0.48],t:"事件"},
    fact:      {c:[0.96,0.78,0.40],t:"事实/偏好"},
    impression:{c:[0.96,0.56,0.76],t:"印象"},
    other:     {c:[0.70,0.76,0.92],t:"其他"}};
  const MAX_NODES=__MEM_MAX_NODES__;

  /* ---- 最小 mat4（列主序） ---- */
  function ident(){return new Float32Array([1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1]);}
  function persp(fovy,asp,n,f){const t=1/Math.tan(fovy/2),nf=1/(n-f);
    return new Float32Array([t/asp,0,0,0,0,t,0,0,0,0,(f+n)*nf,-1,0,0,2*f*n*nf,0]);}
  function mul(a,b){const o=new Float32Array(16);
    for(let c=0;c<4;c++)for(let r=0;r<4;r++)
      o[c*4+r]=a[r]*b[c*4]+a[4+r]*b[c*4+1]+a[8+r]*b[c*4+2]+a[12+r]*b[c*4+3];
    return o;}
  function rotY(m,r){const c=Math.cos(r),s=Math.sin(r),R=ident();
    R[0]=c;R[2]=s;R[8]=-s;R[10]=c;return mul(m,R);}
  function rotX(m,r){const c=Math.cos(r),s=Math.sin(r),R=ident();
    R[5]=c;R[6]=s;R[9]=-s;R[10]=c;return mul(m,R);}
  function tr(m,v){const x=v[0],y=v[1],z=v[2];
    return [m[0]*x+m[4]*y+m[8]*z+m[12],m[1]*x+m[5]*y+m[9]*z+m[13],
            m[2]*x+m[6]*y+m[10]*z+m[14],m[3]*x+m[7]*y+m[11]*z+m[15]];}

  /* ---- 着色器 ---- */
  const VS=`attribute vec3 aPos;attribute vec4 aColor;attribute float aGlow;
    uniform mat4 uMVP;uniform float uScale;
    varying vec4 vColor;varying float vGlow;
    void main(){vec4 p=uMVP*vec4(aPos,1.0);gl_Position=p;
      gl_PointSize=aColor.w*uScale*(16.0/max(p.w,0.1));
      vColor=vec4(aColor.rgb,1.0);vGlow=aGlow;}`;
  const FS=`precision mediump float;varying vec4 vColor;varying float vGlow;
    void main(){vec2 uv=gl_PointCoord*2.0-1.0;float d=length(uv);
      if(d>1.0)discard;
      float core=smoothstep(0.24,0.0,d);
      float mid=smoothstep(0.78,0.0,d);
      float halo=exp(-d*2.4)*0.7;
      vec3 bright=min(vColor.rgb*1.35+0.22,vec3(1.0));
      vec3 col=mix(vColor.rgb,bright,core);
      float a=clamp(core+mid*0.5+halo*0.4,0.0,1.0)*vGlow;
      gl_FragColor=vec4(col,a);}`;
  const LVS=`attribute vec3 aStart;attribute vec3 aEnd;attribute float aFlag;attribute vec4 aColor;
    uniform mat4 uMVP;uniform vec2 uRes;uniform float uWidth;
    varying vec4 vColor;varying float vSide;
    void main(){
      /* aFlag: 0/1 = 起点朝下/朝上，2/3 = 终点朝下/朝上 */
      vColor=aColor;
      float which=step(1.5,aFlag);
      float side=(mod(aFlag,2.0)<0.5)?-1.0:1.0;
      vSide=side;
      vec4 cs=uMVP*vec4(aStart,1.0),ce=uMVP*vec4(aEnd,1.0);
      if(cs.w<=0.0||ce.w<=0.0){gl_Position=vec4(0.0,0.0,0.0,0.0);return;}
      vec2 ps=cs.xy/cs.w,pe=ce.xy/ce.w;
      vec2 dv=(pe-ps)*uRes*0.5;
      float len=length(dv);
      vec2 dir=vec2(1.0,0.0);
      if(len>0.001)dir=dv/len;
      vec2 nrm=vec2(-dir.y,dir.x);
      vec2 p=mix(ps,pe,which)+nrm*side*(uWidth*0.5)/(uRes*0.5);
      float w=mix(cs.w,ce.w,which);
      gl_Position=vec4(p*w,mix(cs.z,ce.z,which),w);
    }`;
  const LFS=`precision mediump float;varying vec4 vColor;varying float vSide;
    uniform float uAlpha;uniform float uCore;uniform float uEdge0;uniform float uEdge1;
    void main(){
      float d=abs(vSide);
      float body=1.0-smoothstep(uEdge0,uEdge1,d);
      float core=uCore*(1.0-smoothstep(0.0,0.4,d));
      vec3 col=mix(vColor.rgb,min(vColor.rgb*1.45+0.28,vec3(1.0)),core);
      gl_FragColor=vec4(col,vColor.a*body*uAlpha);
    }`;

  /* ---- 状态 ---- */
  let gl,canvas,prog,lprog;
  let nodes=[],segs=[],edgeLinks=[];
  let layoutKind="kind";
  let graphMode="neighbors";
  let rotXv=0.25,rotYv=0,dist=16,dragging=false,last=[0,0],hover=-1,selected=-1,hovering=false;
  let raf=0,running=false,pxr=1,mvp=ident();
  let nbPos,nbCol,nbGlow,ebSt,ebEn,ebSf,ebCol,nodeCount=0,edgeVCount=0,ready=false;

  function shader(t,s){const o=gl.createShader(t);gl.shaderSource(o,s);gl.compileShader(o);
    if(!gl.getShaderParameter(o,gl.COMPILE_STATUS))throw new Error(gl.getShaderInfoLog(o));return o;}
  function program(vs,fs){const p=gl.createProgram();
    gl.attachShader(p,shader(gl.VERTEX_SHADER,vs));
    gl.attachShader(p,shader(gl.FRAGMENT_SHADER,fs));gl.linkProgram(p);
    if(!gl.getProgramParameter(p,gl.LINK_STATUS))throw new Error(gl.getProgramInfoLog(p));return p;}

  function norm(v){const l=Math.hypot(v[0],v[1],v[2])||1;return [v[0]/l,v[1]/l,v[2]/l];}
  function fiboDir(i,n){const k=i+0.5,g=Math.PI*(3-Math.sqrt(5));
    const phi=Math.acos(1-2*k/n),theta=g*i;
    return [Math.sin(phi)*Math.cos(theta),Math.cos(phi),Math.sin(phi)*Math.sin(theta)];}

  function build(data){
    const all=(data&&data.items)||[];
    const graph=(data&&data.graph)||null;
    const byId={};
    all.forEach(m=>{byId[m.id]=m;});

    const real=!!(graph&&graph.ok&&Array.isArray(graph.nodes)&&graph.nodes.length>1);
    let list=all.slice().sort((a,b)=>(b.importance||0)-(a.importance||0));
    if(list.length>MAX_NODES)list=list.slice(0,MAX_NODES);
    const keep=new Set(list.map(x=>x.id));

    nodes=[];
    edgeLinks=[];
    segs=[];

    if(real){
      /* 位置由向量主成分给出，意思相近的卡自然挨在一起 */
      const index={};
      graph.nodes.forEach(function(g){
        const m=byId[g.id];
        if(!m||!keep.has(g.id))return;
        const kd=m.kind||"fact";
        const k=KIND[kd]?kd:"other";
        const imp=Math.max(0,Math.min(1,Number(m.importance)||0));
        const pos=[Number(g.x)||0,Number(g.y)||0,Number(g.z)||0];
        index[g.id]=nodes.length;
        nodes.push({id:g.id,kind:k,pos:pos,size:11+imp*17,glow:0.85+imp*0.15,m:m});
      });
      (graph.edges||[]).forEach(function(e){
        const ia=index[e.a],ib=index[e.b];
        if(ia===undefined||ib===undefined)return;
        const w=Math.max(0,Math.min(1,Number(e.w)||0));
        const link={a:e.a,b:e.b,w:w,ia:ia,ib:ib};
        edgeLinks.push(link);
        const c=KIND[nodes[ia].kind].c;
        segs.push({a:nodes[ia].pos,b:nodes[ib].pos,
          col:[c[0],c[1],c[2],0.26+w*0.6],link:link});
      });
    }else{
      /* 没有向量时的退路：按 kind 摆球面，位置只表示分类 */
      const wanted=list.filter(m=>keep.has(m.id));
      const kinds=KIND_ORDER.filter(k=>wanted.some(m=>(m.kind||"fact")===k));
      const dir={};
      kinds.forEach((k,i)=>{dir[k]=fiboDir(i,Math.max(kinds.length,1));});
      const R=8.5,counters={};
      wanted.forEach(function(m){
        let kd=m.kind||"fact";
        if(!KIND[kd]){
          if(!dir[kd])dir[kd]=fiboDir(Object.keys(dir).length%5,5);
        }
        const k=KIND[kd]?kd:"other";
        const d0=dir[kd];
        counters[kd]=(counters[kd]||0)+1;
        const j=counters[kd]-1,a=j*2.399,spread=0.62;
        const off=[Math.cos(a)*spread,Math.sin(a*1.6)*spread,Math.sin(a)*spread];
        const v=norm([d0[0]+off[0],d0[1]+off[1],d0[2]+off[2]]);
        const imp=Math.max(0,Math.min(1,Number(m.importance)||0));
        const r=R-imp*2.2+(j%4)*0.3;
        nodes.push({id:m.id,kind:k,pos:[v[0]*r,v[1]*r,v[2]*r],
          size:11+imp*17,glow:0.85+imp*0.15,m:m});
      });
      /* 中心光球：各分类的汇合点 */
      nodes.forEach(function(n){
        const c=KIND[n.kind].c;
        segs.push({a:n.pos,b:[0,0,0],col:[c[0],c[1],c[2],0.16],link:null});
      });
      /* 同类的连一颗最近邻，只表达「同类」不算语义 */
      const group={};
      nodes.forEach(n=>{(group[n.kind]=group[n.kind]||[]).push(n);});
      nodes.forEach(function(n){
        let best=null,bd=1e9;
        for(const p of group[n.kind]){if(p===n)continue;
          const d=(p.pos[0]-n.pos[0])**2+(p.pos[1]-n.pos[1])**2+(p.pos[2]-n.pos[2])**2;
          if(d<bd){bd=d;best=p;}}
        if(best&&String(n.id)<String(best.id)){
          const link={a:n.id,b:best.id,w:0,ia:nodes.indexOf(n),ib:nodes.indexOf(best)};
          edgeLinks.push(link);
          const c=KIND[n.kind].c;
          segs.push({a:n.pos,b:best.pos,col:[c[0],c[1],c[2],0.3],link:link});
        }
      });
    }
    layoutKind=real?"semantic":"kind";
    graphMode=real?((graph&&graph.mode)||"neighbors"):"kind";
  }

  function setBuf(b,d){if(!b)b=gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER,b);gl.bufferData(gl.ARRAY_BUFFER,d,gl.DYNAMIC_DRAW);return b;}

  /* 选中节点时把它和它的近邻点亮，其余节点保持原样 */
  function uploadNodes(){
    const hub=layoutKind!=="semantic";
    const total=nodes.length+(hub?1:0);
    const pos=new Float32Array(total*3),col=new Float32Array(total*4),glow=new Float32Array(total);
    const sel=selected>=0&&nodes[selected]?nodes[selected].id:null;
    const near={};
    if(sel){
      edgeLinks.forEach(function(e){
        if(e.a===sel)near[e.b]=1;
        else if(e.b===sel)near[e.a]=1;
      });
    }
    nodes.forEach((n,i)=>{const c=KIND[n.kind].c;
      pos[i*3]=n.pos[0];pos[i*3+1]=n.pos[1];pos[i*3+2]=n.pos[2];
      col[i*4]=c[0];col[i*4+1]=c[1];col[i*4+2]=c[2];
      col[i*4+3]=n.size*(i===selected?1.5:1);
      let g=n.glow*(i===selected?1.6:1);
      if(sel&&i!==selected&&near[n.id])g*=1.25;
      glow[i]=g;});
    if(hub){
      const ci=nodes.length;
      col[ci*4]=0.72;col[ci*4+1]=0.84;col[ci*4+2]=1.0;col[ci*4+3]=34;glow[ci]=1;
    }
    nbPos=setBuf(nbPos,pos);nbCol=setBuf(nbCol,col);nbGlow=setBuf(nbGlow,glow);
    nodeCount=total;
  }

  /* 每段连线摊成 6 个顶点、屏幕空间定宽的窄带，深色底上也看得清 */
  function uploadEdges(){
    const vc=segs.length*6;
    const st=new Float32Array(vc*3),en=new Float32Array(vc*3),sf=new Float32Array(vc),cl=new Float32Array(vc*4);
    const sel=selected>=0&&nodes[selected]?nodes[selected].id:null;
    const flags=[0,2,3,0,3,1];
    segs.forEach(function(s,i){
      let r=s.col[0],g=s.col[1],b=s.col[2],a=s.col[3];
      if(s.link&&sel){
        if(s.link.a===sel||s.link.b===sel){
          a=Math.min(1,a*2.4+0.25);
          r=Math.min(1,r+0.22);g=Math.min(1,g+0.22);b=Math.min(1,b+0.22);
        }
        /* 选中之后，其它线保持原样（只是不额外加亮） */
      }
      for(let k=0;k<6;k++){
        const vi=i*6+k;
        st[vi*3]=s.a[0];st[vi*3+1]=s.a[1];st[vi*3+2]=s.a[2];
        en[vi*3]=s.b[0];en[vi*3+1]=s.b[1];en[vi*3+2]=s.b[2];
        sf[vi]=flags[k];
        cl[vi*4]=r;cl[vi*4+1]=g;cl[vi*4+2]=b;cl[vi*4+3]=a;
      }
    });
    ebSt=setBuf(ebSt,st);ebEn=setBuf(ebEn,en);ebSf=setBuf(ebSf,sf);ebCol=setBuf(ebCol,cl);
    edgeVCount=vc;
  }

  function uploadGL(){uploadNodes();uploadEdges();}

  function resize(){
    const dpr=Math.min(window.devicePixelRatio||1,1.5),w=canvas.clientWidth,h=canvas.clientHeight;
    if(canvas.width!==Math.round(w*dpr)||canvas.height!==Math.round(h*dpr)){
      canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr);
      gl.viewport(0,0,canvas.width,canvas.height);}
    pxr=dpr;
  }
  function attrib(p,name,b,sz){const loc=gl.getAttribLocation(p,name);
    gl.bindBuffer(gl.ARRAY_BUFFER,b);gl.enableVertexAttribArray(loc);
    gl.vertexAttribPointer(loc,sz,gl.FLOAT,false,0,0);}
  function frame(){
    rotYv+=hovering?0:0.0016;resize();
    gl.clearColor(0,0,0,0);gl.clear(gl.COLOR_BUFFER_BIT);
    gl.enable(gl.BLEND);gl.disable(gl.DEPTH_TEST);
    let m=ident();m=rotY(m,rotYv);m=rotX(m,rotXv);
    const view=ident();view[14]=-dist;
    mvp=mul(persp(50*Math.PI/180,canvas.width/canvas.height,.1,100),mul(view,m));
    gl.useProgram(lprog);
    gl.uniformMatrix4fv(gl.getUniformLocation(lprog,"uMVP"),false,mvp);
    gl.uniform2f(gl.getUniformLocation(lprog,"uRes"),canvas.width,canvas.height);
    const lw=gl.getUniformLocation(lprog,"uWidth"),la=gl.getUniformLocation(lprog,"uAlpha");
    const lc=gl.getUniformLocation(lprog,"uCore"),le0=gl.getUniformLocation(lprog,"uEdge0"),
          le1=gl.getUniformLocation(lprog,"uEdge1");
    attrib(lprog,"aStart",ebSt,3);attrib(lprog,"aEnd",ebEn,3);
    attrib(lprog,"aFlag",ebSf,1);attrib(lprog,"aColor",ebCol,4);
    /* 外圈辉光 */
    gl.uniform1f(lw,10.0*pxr);gl.uniform1f(la,0.22);gl.uniform1f(lc,0);
    gl.uniform1f(le0,0.0);gl.uniform1f(le1,1.0);
    gl.blendFunc(gl.SRC_ALPHA,gl.ONE);
    gl.drawArrays(gl.TRIANGLES,0,edgeVCount);
    /* 亮线本体 */
    gl.uniform1f(lw,2.6*pxr);gl.uniform1f(la,1.0);gl.uniform1f(lc,1);
    gl.uniform1f(le0,0.45);gl.uniform1f(le1,1.0);
    gl.blendFunc(gl.SRC_ALPHA,gl.ONE_MINUS_SRC_ALPHA);
    gl.drawArrays(gl.TRIANGLES,0,edgeVCount);

    gl.useProgram(prog);
    gl.blendFunc(gl.SRC_ALPHA,gl.ONE);
    gl.uniformMatrix4fv(gl.getUniformLocation(prog,"uMVP"),false,mvp);
    gl.uniform1f(gl.getUniformLocation(prog,"uScale"),pxr);
    attrib(prog,"aPos",nbPos,3);attrib(prog,"aColor",nbCol,4);attrib(prog,"aGlow",nbGlow,1);
    gl.drawArrays(gl.POINTS,0,nodeCount);
  }
  function loop(){if(!running)return;frame();raf=requestAnimationFrame(loop);}
  function start(){if(running||!ready)return;running=true;loop();}
  function stop(){running=false;if(raf)cancelAnimationFrame(raf);raf=0;}

  function pickAt(x,y){
    const w=canvas.clientWidth,h=canvas.clientHeight;let best=-1,bd=1e9;
    for(let i=0;i<nodes.length;i++){
      const p=tr(mvp,nodes[i].pos);if(p[3]<=0)continue;
      const sx=(p[0]/p[3]*0.5+0.5)*w,sy=(1-(p[1]/p[3]*0.5+0.5))*h;
      const approx=nodes[i].size*pxr*(16/p[3]);
      const d=Math.hypot(x-sx,y-sy),hitR=Math.max(approx*0.65,13);
      if(d<hitR&&d<bd){bd=d;best=i;}}
    return best;
  }
  function neighborsOf(id){
    const out=[];
    edgeLinks.forEach(function(e){
      let other=null,w=e.w;
      if(e.a===id){other=e.b;}else if(e.b===id){other=e.a;}
      if(other===null)return;
      const t=nodes[e.a===id?e.ib:e.ia];
      if(t)out.push({node:t,w:w});
    });
    out.sort(function(a,b){return b.w-a.w;});
    return out;
  }
  function clearSelection(){
    if(selected<0)return;
    selected=-1;
    uploadNodes();uploadEdges();
    const el=document.getElementById("memNodeDetail");
    if(el)el.style.display="none";
  }
  function select(i){
    const n=nodes[i],m=n.m,c=KIND[n.kind];
    const rgb=c.c.map(v=>Math.round(v*255)).join(",");
    selected=i;uploadNodes();uploadEdges();
    const el=document.getElementById("memNodeDetail");
    const near=neighborsOf(n.id);
    const semantic=layoutKind==="semantic";
    const nearTitle=semantic?"语义近邻":"同类邻居";
    const nearHtml=near.length
      ? '<div class="nd-section"><div class="nd-section-t">'+nearTitle+'</div>'+
        near.slice(0,6).map(function(x){
          return '<button type="button" class="nd-near" data-id="'+esc(x.node.id)+'">'+
            '<span class="nd-near-k k-'+x.node.kind+'"></span>'+
            '<span class="nd-near-t">'+esc(x.node.m.content)+'</span>'+
            (semantic
              ? '<span class="nd-near-w">'+Math.round(x.w*100)+'%</span>' : '')+
            '</button>';}).join("")+'</div>'
      : (semantic
          ? '<div class="nd-section"><div class="nd-section-t">'+nearTitle+'</div>'
            +'<div class="nd-empty">没有语义上够近的其它记忆。</div></div>' : '');
    el.style.display="block";
    el.innerHTML='<div class="nd-head"><span class="cat-tag" style="background:rgba('+rgb+',.2);color:rgb('+rgb+')">'+c.t+'</span><b>记忆详情</b>'
      +'<button class="nd-close" id="ndClose">关闭</button></div>'
      +'<div class="nd-content">'+esc(m.content)+'</div>'
      +'<div class="nd-meta"><span>重要性 '+Number(m.importance||0).toFixed(2)+'</span>'
      +'<span>命中 '+(m.hit_count||0)+'</span><span>创建 '+fmtTime(m.created_at)+'</span>'
      +(m.category?'<span>分类 '+esc(m.category)+'</span>':"")+'</div>'
      +nearHtml;
    el.querySelectorAll(".nd-near").forEach(function(btn){
      btn.onclick=function(){
        const k=nodes.findIndex(function(x){return x.id===btn.dataset.id;});
        if(k>=0)select(k);
      };
    });
    document.getElementById("ndClose").onclick=function(){
      el.style.display="none";clearSelection();};
  }
  function bind(){
    const tip=document.getElementById("memTip");
    function showTip(i,x,y){const n=nodes[i],m=n.m,c=KIND[n.kind];
      const rgb=c.c.map(v=>Math.round(v*255)).join(",");
      tip.innerHTML='<span class="tip-kind" style="background:rgba('+rgb+',.22);color:rgb('+rgb+')">'+c.t+'</span>'
        +'<div class="tip-text">'+esc(m.content)+'</div>'
        +'<div class="tip-meta">重要性 '+Number(m.importance||0).toFixed(2)+' · 命中 '+(m.hit_count||0)+' · '+fmtTime(m.created_at)+'</div>';
      tip.classList.add("show");
      let tx=x+14,ty=y+14;
      if(tx+280>canvas.clientWidth)tx=x-280;
      if(ty+120>canvas.clientHeight)ty=canvas.clientHeight-120;
      tip.style.left=tx+"px";tip.style.top=ty+"px";}
    function hideTip(){tip.classList.remove("show");hover=-1;}
    let moved=0;
    canvas.addEventListener("mousedown",function(e){
      const r=canvas.getBoundingClientRect();
      dragging=true;moved=0;last=[e.clientX-r.left,e.clientY-r.top];
      canvas.classList.add("dragging");});
    window.addEventListener("mousemove",function(e){
      const r=canvas.getBoundingClientRect(),x=e.clientX-r.left,y=e.clientY-r.top;
      if(dragging){moved+=Math.abs(x-last[0])+Math.abs(y-last[1]);
        rotYv+=(x-last[0])*0.01;rotXv+=(y-last[1])*0.01;
        rotXv=Math.max(-1.4,Math.min(1.4,rotXv));last=[x,y];hideTip();
      }else if(x>=0&&y>=0&&x<=r.width&&y<=r.height){
        const i=pickAt(x,y);hover=i;if(i>=0)showTip(i,x,y);else hideTip();}});
    window.addEventListener("mouseup",function(e){
      if(!dragging)return;
      dragging=false;canvas.classList.remove("dragging");
      if(moved<6){
        const r=canvas.getBoundingClientRect();
        const x=e.clientX-r.left,y=e.clientY-r.top;
        if(x>=0&&y>=0&&x<=r.width&&y<=r.height){
          const i=pickAt(x,y);
          if(i>=0){select(i);hideTip();}
          else clearSelection();      /* 点空白 → 恢复所有连线 */
        }}});
    canvas.addEventListener("mouseenter",function(){hovering=true;});
    canvas.addEventListener("mouseleave",function(){hovering=false;hideTip();});
    canvas.addEventListener("wheel",function(e){e.preventDefault();
      dist=Math.max(8,Math.min(42,dist*(1+Math.sign(e.deltaY)*0.08)));},{passive:false});
    document.getElementById("memReset").addEventListener("click",function(){
      rotXv=0.25;rotYv=0;dist=16;});
  }
  function legend(){
    const rows=KIND_ORDER.map(function(k){
      return '<div class="lg-row"><span class="lg-dot k-'+k+'"></span>'+KIND[k].t+'</div>';}).join("");
    const note=layoutKind==="semantic"
      ? (graphMode==="all"
          ? '位置 = 向量主成分 · 连线 = 全部相似关系'
          : '位置 = 向量主成分 · 连线 = 余弦近邻')
      : '按类别摆位，没有向量可用';
    document.getElementById("memLegend").innerHTML=rows
      +'<div class="lg-note">'+note+'<br>拖拽旋转 · 滚轮缩放 · 点击节点看近邻'
      +'<br>选中节点时它的连线会更亮'
      +'<br>指针停在画布上时暂停自转</div>';
  }
  function screenPoints(){
    const w=canvas.clientWidth,h=canvas.clientHeight;
    return nodes.map(function(n){const p=tr(mvp,n.pos);
      if(p[3]<=0)return null;
      return {x:(p[0]/p[3]*0.5+0.5)*w,
        y:(1-(p[1]/p[3]*0.5+0.5))*h,id:n.id};}).filter(Boolean);
  }
  function init(){
    canvas=document.getElementById("memCanvas");
    try{gl=canvas.getContext("webgl",{alpha:true,antialias:true,premultipliedAlpha:false});}
    catch(e){gl=null;}
    if(!gl)return false;
    prog=program(VS,FS);lprog=program(LVS,LFS);
    uploadNodes();uploadEdges();
    legend();bind();ready=true;return true;
  }
  function setData(data){
    if(!ready)return;
    const keepId=(selected>=0&&nodes[selected])?nodes[selected].id:null;
    build(data);
    selected=-1;
    const empty=document.getElementById("memNetEmpty");
    if(!nodes.length){
      empty.style.display="flex";
      empty.innerHTML='<div class="ic">'+icon("brain",40)+'</div><div>'+
        (data&&data.ok?"还没有记住任何事":"记忆 sidecar 没在跑，暂无可展示的记忆")+'</div>';
    }else empty.style.display="none";
    legend();
    const badge=document.getElementById("memGraphBadge");
    if(badge){
      badge.textContent=layoutKind==="semantic"
        ? (nodes.length+" 个节点 · "+edgeLinks.length
           +(graphMode==="all"?" 条连线（全部）":" 条语义关联"))
        : "分类布局";
      badge.style.display=layoutKind==="semantic"?"":"none";
    }
    uploadGL();
    if(keepId){
      const k=nodes.findIndex(function(x){return x.id===keepId;});
      if(k>=0)select(k);
      else document.getElementById("memNodeDetail").style.display="none";
    }
  }
  return {init:init,setData:setData,start:start,stop:stop,screenPoints:screenPoints,
    get isReady(){return ready;}};
})();


/* ------------------------------------------------------------------
 * Tab 决策：/api/companion
 * ------------------------------------------------------------------ */
function renderDecision(c){
  if(!c){ $("#decisionList").innerHTML='<div class="hint warn">决策日志读取失败。</div>'; return; }
  $("#decisionSource").textContent=shortPath(c.persist_path);
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
 * Tab 设置：/api/settings（走桌宠进程的控制端口，改完立刻生效）
 * ------------------------------------------------------------------ */
const SETTINGS = {schema:[], values:{}, appearance:{}, avatars:[], dirty:{}};

function _setRowControl(item, value){
  const id="set_"+item.key;
  if(item.type==="bool"){
    return '<label class="switch"><input type="checkbox" id="'+id+'" data-set="'+esc(item.key)+'"'+(value?" checked":"")+'></label>';
  }
  if(item.type==="int"||item.type==="float"){
    const step=item.step||1;
    return '<input type="range" id="'+id+'" data-set="'+esc(item.key)+'"'+
      ' min="'+(item.min!=null?item.min:0)+'" max="'+(item.max!=null?item.max:100)+'"'+
      ' step="'+step+'" value="'+esc(value)+'">'+
      '<span class="num" id="'+id+'_n">'+esc(value)+'</span>';
  }
  if(item.type==="secret"){
    return '<input type="password" id="'+id+'" data-set="'+esc(item.key)+'"'+
      ' placeholder="留空 = 不改" value="">';
  }
  if(item.options&&item.options.length){
    return '<select id="'+id+'" data-set="'+esc(item.key)+'">'+
      item.options.map(function(o){
        return '<option value="'+esc(o)+'"'+(String(value)===String(o)?" selected":"")+'>'+esc(o)+'</option>';
      }).join("")+'</select>';
  }
  return '<input type="text" id="'+id+'" data-set="'+esc(item.key)+'" value="'+esc(value)+'">';
}

function renderSettings(){
  const groups={};
  SETTINGS.schema.forEach(function(item){
    (groups[item.group]=groups[item.group]||[]).push(item);
  });
  const order=[];
  SETTINGS.schema.forEach(function(item){ if(order.indexOf(item.group)<0)order.push(item.group); });
  $("#settingsGroups").innerHTML=order.map(function(g){
    const rows=(groups[g]||[]).map(function(item){
      const v=SETTINGS.values[item.key];
      return '<div class="set-row"><span class="lbl">'+esc(item.label)+
        (item.restart?'<span class="restart">重启生效</span>':'')+'</span>'+
        _setRowControl(item,v)+'</div>';
    }).join("");
    return '<div class="set-card"><h4>'+esc(g)+'</h4>'+rows+'</div>';
  }).join("");

  // 外观（Live2D 分类 chip）
  const ap=SETTINGS.appearance||{};
  if(!ap.available){
    $("#settingsAppearance").innerHTML='<div class="hint">当前不是 Live2D 渲染器，'+
      '外观分类由桌宠右键菜单切换。</div>';
  }else{
    const active=ap.active||[];
    $("#settingsAppearance").innerHTML=(ap.groups||[]).map(function(g){
      const chips=(g.items||[]).map(function(it){
        const on=active.indexOf(it[0])>=0;
        return '<button type="button" class="set-chip'+(on?" on":"")+'" data-l2d="'+esc(g.id)+'" data-l2d-item="'+esc(it[0])+'">'+
          esc(it[1])+'</button>';
      }).join("");
      return '<div class="set-card" style="margin-bottom:12px;"><h4>'+esc(g.label)+'</h4>'+
        '<div class="set-chips">'+chips+'</div></div>';
    }).join("")+
      '<button type="button" class="btn" data-l2d-reset="1">复位全部外观</button>';
  }

  // 头像
  const av=SETTINGS.avatars||[];
  $("#settingsAvatars").innerHTML=av.length
    ? '<div class="set-avatars">'+av.map(function(a){
        return '<button type="button" class="set-avatar" data-avatar="'+esc(a.path)+'"'+
          ' title="'+esc(a.group+" · "+a.name)+'"'+
          ' style="background-image:url(\'/api/avatar?path='+encodeURIComponent(a.path)+'\')"></button>';
      }).join("")+'</div>'
    : '<div class="hint">没有可用头像。</div>';
}

function _collectSettings(){
  const out={};
  $$("[data-set]").forEach(function(el){
    const key=el.dataset.set;
    // 开关要一直发（包括 false）—— 只发 true 的话，关掉的开关永远改不掉
    if(el.type==="checkbox"){ out[key]=el.checked; return; }
    if(el.type==="range"){ out[key]=Number(el.value); return; }
    if(el.type==="password"){ if(el.value)out[key]=el.value; return; }
    out[key]=el.value;
  });
  return out;
}

async function loadSettings(note){
  const tag=$("#settingsTag");
  try{
    const data=await api("/api/settings");
    SETTINGS.schema=data.schema||[];
    SETTINGS.values=data.values||{};
    SETTINGS.appearance=data.appearance||{};
    SETTINGS.avatars=data.avatars||[];
    tag.textContent=data.connected?"桌宠已连上":"桌宠没连上";
    tag.className="mini-tag"+(data.connected?"":" warn");
    if(!data.connected){
      $("#settingsErr").innerHTML='<div class="hint warn" style="margin-bottom:10px;">'+icon("alert")+
        ' 桌宠进程没连上（控制端口 '+(data.controlUrl||"未知")+'）—— 设置改了不会生效。'+
        '启动桌宠后点「重新读取」。</div>';
    }else{
      $("#settingsErr").innerHTML=note?'<div class="hint ok" style="margin-bottom:10px;">'+icon("check")+' '+esc(note)+'</div>':"";
    }
    renderSettings();
  }catch(e){
    tag.textContent="读取失败";
    tag.className="mini-tag warn";
    $("#settingsErr").innerHTML='<div class="hint warn">'+icon("alert")+' '+esc(e.message)+'</div>';
  }
}

async function saveSettings(){
  const btn=$("#settingsSave");
  btn.disabled=true;
  try{
    const r=await apiRaw("/api/settings",{method:"POST",
      headers:{"content-type":"application/json"},
      body:JSON.stringify({changes:_collectSettings()})});
    if(r&&r.ok){
      const restart=(r.restart||[]).length;
      await loadSettings("已生效"+(restart?("（"+restart+" 项要重启桌宠才生效）"):""));
    }else{
      $("#settingsErr").innerHTML='<div class="hint warn">'+icon("alert")+' '+
        esc((r&&(r.error||"保存失败"))||"保存失败")+'</div>';
    }
  }catch(e){
    $("#settingsErr").innerHTML='<div class="hint warn">'+icon("alert")+' '+esc(e.message)+'</div>';
  }finally{
    btn.disabled=false;
  }
}

async function settingsAction(action, params){
  try{
    const r=await apiRaw("/api/settings/action",{method:"POST",
      headers:{"content-type":"application/json"},
      body:JSON.stringify({action:action,params:params||{}})});
    if(r&&r.ok)await loadSettings();
    else $("#settingsErr").innerHTML='<div class="hint warn">'+icon("alert")+' '+
      esc((r&&r.error)||"操作失败")+'</div>';
  }catch(e){
    $("#settingsErr").innerHTML='<div class="hint warn">'+icon("alert")+' '+esc(e.message)+'</div>';
  }
}

/* ------------------------------------------------------------------
 * Tab 配置：/api/config（API key 只显示 configured / length / valid）
 * ------------------------------------------------------------------ */
function renderConfig(cfg){
  if(!cfg){ $("#configModel").innerHTML='<div class="hint warn">配置读取失败。</div>'; return; }
  $("#configPath").textContent=shortPath(cfg.config_path);
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
  $("#statePath").textContent=shortPath(st.path);
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
  $("#stateBars").innerHTML=rows.map(function(row,i){
    const name=row[0], v=row[1], cls=row[2], max=row[3];
    const p=Math.max(0,Math.min(100,Number(v||0)/max*100));
    return '<div class="cs-row" style="--d:-'+(i*1.1)+'s"><div class="name">'+name+'</div>'
      + '<div class="track"><div class="fill '+cls+'" style="width:'+p+'%">'
      + Number(v||0).toFixed(1)+'</div></div>'
      + '<div class="v">'+Number(v||0).toFixed(1)+'</div></div>';
  }).join("");

  const up=st.unlock_progress||{};
  $("#stateUnlock").innerHTML=
      '<div class="cs-row" style="--d:-5.5s"><div class="name">解锁进度</div>'
    + '<div class="track"><div class="fill level" style="width:'+Number(up.percent||0)+'%">'
    + Number(up.percent||0).toFixed(1)+'%</div></div>'
    + '<div class="v">'+(up.unlocked_tools||0)+'/'+(up.total_tools||0)+'</div></div>'
    + '<div class="hint">当前等级 '+esc(up.level||1)+' · 已解锁 '+esc(up.unlocked_tools||0)+' / '+esc(up.total_tools||0)+' 个工具</div>';
}

/* ------------------------------------------------------------------
 * Tab 健康与维护：/api/health + /api/maintenance/status
 * ------------------------------------------------------------------ */
/* 记忆 sidecar 的模型状态词（口径见 memory_sidecar/store.py 的 _model_state） */
var MSTATE={idle:"还没加载",absent:"模型没下载",loading:"加载中",ready:"就绪",failed:"加载失败",disabled:"已关闭"};

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

  const ms=h.memory_stats||{}, sc=h.memory_sidecar||{};
  const vec=ms.vector||{}, bm=ms.bm25||{}, rr=ms.rerank||{};
  $("#healthVector").innerHTML=[
    kpi("卡片（真值）", fmtNum(ms.cards||0), "活跃 "+fmtNum(ms.active||0)+" · 过时 "+fmtNum(ms.superseded||0), ms.cards?"green":"warn"),
    kpi("向量索引", vec.enabled===false?"已关闭":(MSTATE[vec.state]||vec.state||"—"), "已索引 "+fmtNum(vec.count||0)+" 行 · "+esc(vec.model||"—"), vec.error?"warn":"green"),
    kpi("BM25 索引", MSTATE[bm.state]||bm.state||"—", "文档 "+fmtNum(bm.docs||0)+" 篇", bm.error?"warn":"green"),
    kpi("精排", rr.enabled===false?"已关闭":(MSTATE[rr.state]||rr.state||"—"), esc(rr.model||"—"), rr.error?"warn":"green"),
  ].join("")+sidecarLine(sc)
    +((ms.degradation||[]).length?('<div class="hint warn" style="margin-top:10px;">'+icon("alert")+' '+esc((ms.degradation||[]).join("；"))+'</div>'):"");
}

function backupLine(r){
  const b=(r&&r.backup)||null;
  if(!b) return "";
  if(b.noop) return '<br>'+icon("info")+' 本次无需备份：'+esc(b.reason||"（不写 data/）");
  if(!b.archive) return b.dir?('<br>备份目录：<span class="name">'+esc(shortPath(b.dir))+'</span>'):"";
  const files=b.files||[];
  let s='<br>'+icon("check")+' 归档：<span class="name">'+esc(shortPath(b.archive))+'</span>'
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
    const data=await api("/api/memory");
    state.memory=data;
    renderMemory(data);
    MemNet.setData(data);
    if(memMode==="net")MemNet.start();
    await loadSleep();
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
  async settings(){
    await loadSettings();
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
    // 同步顶栏面包屑（分组 + 页面名）
    var _groups = {runs:"数据总览",cost:"数据总览",memory:"数据总览",decision:"数据总览",
                   tools:"系统",config:"系统",state:"系统",health:"系统"};
    var _names = {runs:"运行",cost:"成本",memory:"记忆",decision:"决策",
                  tools:"工具",config:"配置",state:"状态",health:"健康与维护"};
    var _titles = {runs:"运行总览",cost:"成本分析",memory:"长期记忆",decision:"决策日志",
                   tools:"工具清单",config:"生效配置",state:"桌宠状态",health:"健康与维护"};
    var _descs = {
      runs:"会话记录、成功率与工具调用概览",
      cost:"Token 用量、缓存命中与花费估算",
      memory:"记忆卡片、向量索引与召回情况",
      decision:"工具决策、风险拦截与养成结果",
      tools:"内置与 MCP 工具、风险等级与解锁",
      config:"模型、密钥与工具解锁配置",
      state:"养成数值、等级与解锁进度",
      health:"进程健康、文件体检与维护操作"};
    $("#crumbGroup").textContent = _groups[t.dataset.view];
    $("#crumbPage").textContent = _names[t.dataset.view];
    $("#pageTitle").textContent = _titles[t.dataset.view];
    $("#pageDesc").textContent = _descs[t.dataset.view];
    document.body.setAttribute("data-view", t.dataset.view);
    // 神经网络只在记忆视图 + net 模式时渲染，其余一律停止
    if(t.dataset.view==="memory"){ if(memMode==="net")MemNet.start(); }
    else MemNet.stop();
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

/* 设置 Tab：保存 / 重读 / 恢复默认 / 滑块实时数值 */
$("#settingsSave").addEventListener("click",saveSettings);
$("#settingsReload").addEventListener("click",function(){loadSettings();});
$("#settingsReset").addEventListener("click",function(){
  if(!window.confirm("把所有设置恢复成默认？\n\n（模型配置不动 —— 那是特意填的）"))return;
  settingsAction("reset_defaults");
});
document.addEventListener("input",function(e){
  const el=e.target;
  if(!el||!el.dataset||!el.dataset.set)return;
  if(el.type==="range"){
    const n=document.getElementById(el.id+"_n");
    if(n)n.textContent=el.value;
  }
});
document.addEventListener("click",function(e){
  const el=e.target.closest?e.target.closest("[data-l2d],[data-l2d-reset],[data-avatar]"):null;
  if(!el)return;
  if(el.dataset.l2dReset){
    settingsAction("live2d_reset");
    return;
  }
  if(el.dataset.l2d){
    settingsAction("live2d_activate",{group_id:el.dataset.l2d,item_id:el.dataset.l2dItem});
    return;
  }
  if(el.dataset.avatar){
    settingsAction("set_avatar",{path:el.dataset.avatar});
  }
});

/* 记忆：神经网络 / 列表 切换 */
$$("#memMode .mem-mode-btn").forEach(function(b){
  b.addEventListener("click",function(){
    if(b.classList.contains("active"))return;
    $$("#memMode .mem-mode-btn").forEach(function(x){x.classList.remove("active");});
    b.classList.add("active");
    memMode=b.dataset.mode;
    if(memMode==="net"){
      $("#memNet").style.display="block";
      $("#memListWrap").style.display="none";
      MemNet.start();
    }else{
      MemNet.stop();
      $("#memNet").style.display="none";
      $("#memNodeDetail").style.display="none";
      $("#memListWrap").style.display="block";
    }
  });
});

/* 记忆：睡觉（定时 + 立刻睡） */
$("#memSleepSave").addEventListener("click",async function(){
  const btn=this;
  btn.disabled=true;
  try{
    await apiRaw("/api/memory/sleep/schedule",{method:"POST",
      headers:{"content-type":"application/json"},
      body:JSON.stringify({enabled:$("#memSleepOn").checked,time:$("#memSleepAt").value})});
    await loadSleep("定时已保存。");
  }catch(e){
    $("#memSleepOut").innerHTML='<div class="sl-line">'+icon("alert")+' 保存失败：'
      +esc(e.message)+'</div>';
  }finally{
    btn.disabled=false;
  }
});
$("#memSleepRun").addEventListener("click",async function(){
  const btn=this;
  btn.disabled=true;
  btn.textContent="睡梦中…";
  $("#memSleepOut").innerHTML='<div class="sl-line">'+icon("hourglass")
    +' 正在整理记忆，模型在写摘要…</div>';
  try{
    const r=await apiRaw("/api/memory/sleep/run",{method:"POST",
      headers:{"content-type":"application/json"},body:JSON.stringify({})});
    if(r&&r.ok){
      await loadSleep();
      $("#memSleepOut").insertAdjacentHTML("afterbegin",'<div class="sl-line">'+icon("check")
        +' 睡好了：合并 '+(r.merged||0)+' 组，模糊 '+(r.blurred||0)+' 条，忘掉 '
        +(r.forgotten||0)+' 条。</div>');
      const data=await api("/api/memory");
      state.memory=data;
      renderMemory(data);
      MemNet.setData(data);
    }else{
      $("#memSleepOut").innerHTML='<div class="sl-line">'+icon("alert")+' 没睡成：'
        +esc((r&&r.error)||"未知原因")+'</div>';
    }
  }catch(e){
    $("#memSleepOut").innerHTML='<div class="sl-line">'+icon("alert")+' 没睡成：'
      +esc(e.message)+'</div>';
  }finally{
    btn.disabled=false;
    btn.textContent="现在睡一觉";
  }
});

$$(".danger .danger-btn").forEach(function(b){
  b.disabled=true;
  b.onclick=function(){runMaintenance(b.dataset.op,b.dataset.confirm,b.textContent.trim());};
});
// 填充静态 HTML 中的 data-icon 占位（统一从 ICONS 取 SVG）
$$("[data-icon]").forEach(function(el){
  const sz = el.dataset.size ? parseInt(el.dataset.size, 10) : 15;
  el.insertAdjacentHTML("afterbegin", icon(el.dataset.icon, sz));
});

refresh();
loadTab("runs");
setInterval(function(){ if(state.auto) refresh(); },5000);
setInterval(function(){ if(state.auto) refreshCompanion(); },15000);

// 初始化 3D 神经网络（WebGL 不可用则静默降级，不影响其它功能）
MemNet.init();
// 页面隐藏时暂停渲染，回到前台且在记忆视图 + net 模式时恢复
document.addEventListener("visibilitychange",function(){
  if(document.hidden)MemNet.stop();
  else if(state.tab==="memory"&&memMode==="net")MemNet.start();
});
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


def _sidecar_status(base_url: Optional[str] = None, timeout: float = 0.4) -> dict:
    """记忆 sidecar（独立进程）的实时状态 —— 面板只读展示，**不写任何东西**。

    面板刷新不能被 sidecar 卡住：0.4s 超时 + 连不上就算「没连上」。
    （读的是 GET /health，纯只读，不碰 data/。）
    """
    from app.brain.memory_client import MemoryClient

    client = MemoryClient(base_url or "", timeout=timeout)
    health = {}
    try:
        health = client.health() or {}
    except Exception as e:  # noqa: BLE001 - 面板永远不许因为记忆服务挂掉
        health = {}
    reachable = bool(health.get("ok"))
    return {
        "reachable": reachable,
        "url": client.base_url,
        "port": health.get("port"),
        "cards": health.get("cards"),
        "cards_total": health.get("cardsTotal"),
        "models": health.get("models") or {},
        "bm25": health.get("bm25"),
        "degraded": bool(health.get("degraded")),
        "degradation": health.get("degradation") or [],
        "data_dir": health.get("dataDir") or "",
        "error": "" if reachable else (client.last_error or "没连上（sidecar 没在跑）"),
    }


def _memory_sidecar(base_url: Optional[str] = None, timeout: float = 2.0):
    """指向记忆 sidecar 的客户端（面板是独立进程 —— 只能走 HTTP）。"""
    from app.brain.memory_client import MemoryClient
    return MemoryClient(base_url or "", timeout=timeout)


def _pet_control(base_url: Optional[str] = None, timeout: float = 8.0):
    """指向桌宠进程控制端口的客户端（设置都走它）。"""
    from app.web.pet_control_client import PetControlClient
    return PetControlClient(base_url or "", timeout=timeout)


def _memory_payload(base_url: Optional[str] = None) -> dict:
    """记忆 Tab 的响应体：**全部来自 sidecar**（JSON 卡片是唯一真值）。

    只读 sidecar 的端点：GET /health（活着没 / 多少张卡 / 模型状态 / 降级原因）、
    GET /stats（活跃 / 过时 / 按 kind 分布 / 两个索引的状态）、
    卡片列表（GET /list；还没实现就读 sidecar 自己报的 cards.json）。

    进程内的老记忆库（data/memory*.db + data/vectors/）已经删掉了，
    这里**不再**读任何本地记忆库文件；sidecar 不在就老实说没连上，
    依然返回 200（面板显示「没连上」，绝不是 500）。
    """
    client = _memory_sidecar(base_url)
    try:
        health = client.health(timeout=0.6) or {}
    except Exception:  # noqa: BLE001 - 面板永远不许因为记忆服务挂掉
        health = {}
    reachable = bool(health.get("ok"))
    stats = (client.stats(timeout=1.0) or {}) if reachable else {}

    items: list = []
    by_kind: dict = {}
    by_importance: dict = {}
    access_hits = 0
    if reachable:
        try:
            for view in client.all(limit=500):
                items.append({
                    "id": view.id,
                    "content": view.content,
                    "category": view.category,
                    "kind": view.kind,
                    "importance": round(view.importance, 2),
                    "created_at": view.created_at,
                    "updated_at": view.updated_at,
                    "hit_count": view.hit_count,
                })
                by_kind[view.kind] = by_kind.get(view.kind, 0) + 1
                bucket = "%.1f" % round(view.importance, 1)
                by_importance[bucket] = by_importance.get(bucket, 0) + 1
                if view.hit_count > 0:
                    access_hits += 1
        except Exception:  # noqa: BLE001
            items = []

    note = ("记忆只有 sidecar 一份真值（JSON 卡片）：data/memory_sidecar/cards.json；"
            "向量索引 / BM25 都是它派生出来的，随时可以重建。"
            if reachable else
            "记忆 sidecar 没在跑（python -m memory_sidecar serve --port 8788）——"
            "桌宠照常聊天，只是没有长期记忆。")

    graph = {"ok": False, "nodes": [], "edges": []}
    if reachable:
        try:
            # 默认就把所有够格的连线都画出来；选中节点时前端只留它那几条
            graph = client.graph(limit=MEM_NET_MAX_NODES, top_k=4,
                                 mode="all", timeout=15.0)
        except Exception:  # noqa: BLE001
            graph = {"ok": False, "nodes": [], "edges": []}

    return {
        "ok": reachable,
        "source": "sidecar",
        "url": client.base_url,
        "count": len(items),
        "items": items,
        "by_kind": by_kind,
        "by_importance": by_importance,
        "access_hits": access_hits,
        "stats": stats,
        "graph": graph,
        "sidecar": _sidecar_status(base_url),
        "note": note,
    }


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


def create_app(trace_db: str | Path,
               sidecar_url: Optional[str | Path] = None,
               pet_control_url: Optional[str | Path] = None):
    """构造 FastAPI app。

    sidecar_url：记忆 sidecar 的地址（面板是独立进程，记忆只走 HTTP 读它）。
    传 None / 空串就用默认地址（http://127.0.0.1:8788，端口可被环境变量覆盖）。

    pet_control_url：桌宠进程的控制端口（设置都搬进控制台了，改设置要靠它
    实时驱动运行中的桌宠）。传 None / 空串就是「桌宠没连上」。
    """
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
            _sleep_service.start_scheduler()
            yield
        finally:
            try:
                _sleep_service.stop_scheduler()
            except Exception:  # noqa: BLE001
                pass
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

    # 记忆「睡觉」服务：定时整理记忆（归组 / 模糊 / 遗忘）。
    from app.web.memory_sleep import MemorySleepService
    _sleep_service = MemorySleepService(
        _data_dir, sidecar_url=str(sidecar_url or ""), project_root=_root,
    )

    # 桌宠进程的控制端口：设置 Tab 靠它实时驱动运行中的桌宠。
    _control_url = str(pet_control_url or "")

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
        return _HTML_PAGE.replace("__MEM_MAX_NODES__", str(MEM_NET_MAX_NODES))

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
        """长期记忆：**全部读自 sidecar**（JSON 卡片是唯一真值）。

        进程内的老记忆库（data/memory*.db / data/vectors/）已经删掉了：
        面板跟桌宠一样，只是一个 HTTP 客户端。
        """
        return _memory_payload(sidecar_url)

    @app.get("/api/memory/graph")
    def memory_graph(mode: str = "all", limit: int = MEM_NET_MAX_NODES, top_k: int = 4):
        """语义图。默认把所有超过相似度下限的连线都算出来。"""
        client = _memory_sidecar(sidecar_url, timeout=20.0)
        try:
            return client.graph(limit=limit, top_k=top_k, mode=mode, timeout=15.0)
        except Exception:  # noqa: BLE001
            return {"ok": False, "nodes": [], "edges": []}

    @app.get("/api/memory/sleep")
    def memory_sleep():
        """睡觉面板：定时设置 + 这一觉的计划 + 上次的结果。"""
        try:
            plan = _sleep_service.plan()
        except Exception:  # noqa: BLE001
            plan = {"ok": False}
        return {"ok": True, "state": _sleep_service.state(), "plan": plan}

    @app.post("/api/memory/sleep/schedule")
    def memory_sleep_schedule(payload: Optional[dict] = None):
        """改定时睡觉的设置（enabled / time）。"""
        payload = payload or {}
        state = _sleep_service.set_schedule(
            enabled=payload.get("enabled"), time=payload.get("time"))
        return {"ok": True, "state": state}

    @app.post("/api/memory/sleep/run")
    def memory_sleep_run(payload: Optional[dict] = None):
        """现在睡一觉：模型写摘要 → sidecar 归组 / 模糊 / 遗忘。"""
        payload = payload or {}
        return _sleep_service.run(dry_run=bool(payload.get("dry_run")),
                                  trigger="manual")

    @app.get("/api/tools")
    def tools():
        """工具 Tab：68 个工具（61 内置 + 7 MCP）+ 风险/解锁等级 + 调用统计 + 对账。"""
        return _tool_inventory(trace_db)

    @app.get("/api/reconciliation")
    def reconciliation():
        """工具 Tab：文档 <-> 注册表的对账差异（复用 check_tool_docs.py）。"""
        return _reconciliation_summary(_reconciliation_result())

    # -- 设置（走桌宠进程的控制端口） ------------------------------------
    @app.get("/api/settings")
    def settings_overview():
        """设置 Tab：设置项清单 + 当前值 + 桌宠是否连上。"""
        client = _pet_control(_control_url)
        return {
            "ok": True,
            "connected": bool(client.base_url) and bool(client.health().get("ok")),
            "controlUrl": client.base_url,
            "schema": client.schema().get("schema") or [],
            "values": client.settings().get("values") or {},
            "appearance": client.appearance().get("appearance") or {},
            "avatars": client.avatars().get("avatars") or [],
        }

    @app.post("/api/settings")
    def settings_update(payload: Optional[dict] = None):
        """设置 Tab：改设置（桌宠上立即生效）。"""
        payload = payload or {}
        changes = payload.get("changes")
        if not isinstance(changes, dict):
            changes = {k: v for k, v in payload.items() if k != "ok"}
        return _pet_control(_control_url).update_settings(changes)

    @app.post("/api/settings/action")
    def settings_action(payload: Optional[dict] = None):
        """设置 Tab：运行时动作（换 Live2D 外观 / 试听语音 / 换头像）。"""
        payload = payload or {}
        action = str(payload.get("action") or "")
        params = payload.get("params")
        if not isinstance(params, dict):
            params = {k: v for k, v in payload.items() if k != "action"}
        return _pet_control(_control_url).action(action, **params)

    @app.get("/api/avatar")
    def avatar_preview(path: str = ""):
        """头像缩略图（只给 assets/avatars 目录里的图片）。"""
        from fastapi.responses import FileResponse
        base = (_root / "assets" / "avatars").resolve()
        try:
            target = Path(path).resolve()
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="路径不对")
        if base not in target.parents or target.suffix.lower() not in (".png", ".jpg", ".jpeg"):
            raise HTTPException(status_code=400, detail="只能看 assets/avatars 里的图片")
        if not target.is_file():
            raise HTTPException(status_code=404, detail="头像文件没了")
        return FileResponse(str(target))

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
        # 记忆 sidecar 的两个索引（向量 / BM25 + 精排）现在归 sidecar 自己管，
        # 面板只读它 /health + /stats 报出来的状态 ——
        # 不再有「本进程的向量索引」这回事（那个模块已经删了）。
        mem = _memory_payload(sidecar_url)
        payload["memory_sidecar"] = mem.get("sidecar") or {}
        payload["memory_stats"] = mem.get("stats") or {}
        return payload

    @app.get("/api/overview")
    def overview():
        """顶部卡片数据（8 张卡片一次拿全，前端不用发 8 个请求）。"""
        stats = _recorder.stats()
        tokens = _token_summary(_data_dir)
        mem = _memory_payload(sidecar_url)
        inv = _tool_inventory(trace_db)
        return {
            "stats": stats,
            "tokens": tokens,
            "memory": {
                "count": mem.get("count", 0),
                "url": mem.get("url", ""),
                "reachable": bool(mem.get("ok")),
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

    @app.post("/api/maintenance/rebuild-vector-index")
    def maintenance_rebuild_index(maintenance: int = 0, confirm: str = ""):
        """让**记忆 sidecar 自己**重建两个索引（POST /rebuild）。

        真值永远是 JSON 卡片（sidecar 的 cards.json）—— 重建只是把 Chroma
        向量索引和 BM25 索引按卡片重新算一遍，**一张卡片都不会动**，
        所以这里不需要备份（以前要备份的是本地 sqlite + data/vectors/，
        那两个东西已经没了）。

        三道闸门不变：POST + maintenance=1 + confirm=rebuild-vector-index。
        """
        _require_maintenance(maintenance, confirm, "rebuild-vector-index")
        client = _memory_sidecar(sidecar_url, timeout=60.0)
        rebuilt = client.rebuild() or {}
        ok = bool(rebuilt) and not (rebuilt.get("errors") or [])
        result = {
            "op": "rebuild-vector-index",
            "ok": ok,
            "url": client.base_url,
            "rebuilt": rebuilt,
            "error": "" if rebuilt else (client.last_error or "sidecar 没在跑"),
            "note": ("索引由 sidecar 从 JSON 卡片重建（向量腿 + BM25 腿）；"
                     "卡片本身没动过，所以这次没有备份步骤"),
        }
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
    parser.add_argument("--sidecar-url", default="",
                        help="记忆 sidecar 地址（默认 http://127.0.0.1:8788）")
    parser.add_argument("--pet-control-url", default="",
                        help="桌宠进程的控制端口（设置 Tab 靠它实时驱动桌宠）")
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    # 默认在当前目录的 data/ 下找
    trace_db = Path(args.trace_db)
    if not trace_db.is_absolute():
        trace_db = Path.cwd() / trace_db
    app = create_app(trace_db, args.sidecar_url or None,
                     args.pet_control_url or None)
    try:
        import uvicorn
    except ImportError:
        raise RuntimeError("需要 uvicorn：pip install uvicorn")

    print(f"Dashboard 启动: http://{args.host}:{args.port}")
    print(f"   trace db: {trace_db}")
    print(f"   memory:   sidecar {args.sidecar_url or '（默认 127.0.0.1:8788）'}")
    print(f"   桌宠控制: {args.pet_control_url or '（没连上，设置改不了）'}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
