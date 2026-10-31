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
    --topbar-bg:rgba(255,255,255,.82);
    --skeleton-hi:#e9edf4;
  }
  :root[data-theme="dark"]{
    --bg:#0f1218; --card:#171b24; --surface:#1a1f2a;
    --surface-sunken:#131722; --surface-hover:#1e2431;
    --card-border:#262c3a; --border:#2a3140; --line:#232a37;
    --text:#dfe4ee; --text-strong:#eef2f9; --sub:#9aa5b8; --faint:#6b7689;
    --accent:#7c93ff; --accent-d:#8fa4ff; --accent-bg:#232c4d; --accent-lt:#3a4a7a;
    --green:#3dd68c; --green-bg:#15352a;
    --red:#f27b6a; --red-bg:#3a2020;
    --amber:#e0a94e; --amber-bg:#3a2f1c;
    --blue:#7c93ff; --blue-bg:#232c4d;
    --shadow:0 1px 2px rgba(0,0,0,.35),0 10px 28px rgba(0,0,0,.4);
    --shadow-sm:0 1px 2px rgba(0,0,0,.35);
    --topbar-bg:rgba(17,20,28,.8);
    --skeleton-hi:#2a3140;
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
  .brand-mark{position:relative;width:38px;height:38px;border-radius:11px;background:var(--accent-bg);
    display:flex;align-items:center;justify-content:center;font-size:19px;flex:none;}
  .brand-mark::after{content:"";position:absolute;inset:-4px;border-radius:15px;border:1.5px solid var(--accent-lt);
    opacity:0;animation:brandBreath 3.4s ease-in-out infinite;pointer-events:none;}
  @keyframes brandBreath{0%,100%{opacity:0;transform:scale(.96);}50%{opacity:.65;transform:scale(1.04);}}
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
    background:var(--topbar-bg);backdrop-filter:saturate(180%) blur(8px);
    display:flex;align-items:center;justify-content:space-between;
    padding:0 32px;position:sticky;top:0;z-index:20;}
  .breadcrumb{font-size:13px;display:flex;align-items:center;gap:8px;color:var(--faint);}
  .breadcrumb .sep{color:var(--border);display:inline-block;transition:color .2s ease,transform .2s ease;}
  .breadcrumb .sep:hover{color:var(--accent);transform:translateY(-1px);}
  #crumbPage{color:var(--text-strong);font-weight:600;}
  .topbar-actions{display:flex;align-items:center;gap:14px;}
  main{padding:30px 34px 54px;width:100%;max-width:2200px;margin:0 auto;}
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
  .stat .num{font-size:26px;font-weight:700;margin-top:7px;line-height:1.1;letter-spacing:.3px;font-variant-numeric:tabular-nums;}
  .stat .label{font-size:12px;color:var(--sub);margin-top:4px;font-weight:500;}
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
    background:var(--surface);color:var(--sub);user-select:none;transition:.15s;}
  .chip:hover{border-color:var(--accent-lt);color:var(--accent-d);}
  .chip.active{background:var(--accent-bg);border-color:var(--accent-lt);color:var(--accent-d);font-weight:600;}
  .run-list{max-height:calc(100vh - 236px);overflow-y:auto;padding:8px;}

  /* 通用分页条 */
  .pager{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;
    padding:11px 14px;border-top:1px solid var(--line);background:var(--card);}
  .pager.standalone{border:1px solid var(--card-border);border-radius:12px;margin-top:12px;}
  .pg-info{font-size:12px;color:var(--sub);font-variant-numeric:tabular-nums;}
  .pg-btns{display:flex;align-items:center;gap:5px;flex-wrap:wrap;}
  .pg-btns button{font-family:inherit;font-size:12px;font-weight:600;cursor:pointer;
    border:1px solid var(--border);background:var(--surface);color:var(--sub);border-radius:8px;
    min-width:32px;height:30px;padding:0 9px;transition:.15s;}
  .pg-btns button:hover:not(:disabled):not(.active){border-color:var(--accent-lt);color:var(--accent-d);background:var(--accent-bg);}
  .pg-btns button.active{background:var(--accent);border-color:var(--accent);color:#fff;cursor:default;}
  .pg-btns button:disabled{opacity:.45;cursor:not-allowed;}
  .pg-btns .pg-gap{color:var(--faint);padding:0 3px;font-size:12px;}
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
  .empty .big{font-size:26px;width:64px;height:64px;border-radius:50%;
    background:var(--accent-bg);color:var(--accent);display:flex;align-items:center;justify-content:center;}

  .run-list:focus{outline:none;}
  .skeleton{background:linear-gradient(90deg,var(--surface-sunken) 25%,var(--skeleton-hi) 37%,var(--surface-sunken) 63%);
    background-size:400% 100%;animation:sk 1.4s ease infinite;border-radius:8px;}
  @keyframes sk{0%{background-position:100% 0;}100%{background-position:0 0;}}
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
    display:flex;align-items:center;justify-content:center;font-size:11px;background:var(--surface);
    border:2px solid var(--accent-lt);z-index:1;}
  .tl-item.tool .node{border-color:var(--amber);background:var(--amber-bg);}
  .tl-item.plan .node{border-color:var(--accent);background:var(--blue-bg);}
  .tl-item.reflection .node{border-color:var(--accent);background:var(--accent-bg);}
  .tl-item.meta .node{border-color:var(--faint);background:var(--surface-sunken);}
  .tl-item.error .node{border-color:var(--red);background:var(--red-bg);}
  .tl-item.live .node{animation:nodePulse 1.8s ease-out infinite;}
  @keyframes nodePulse{0%{box-shadow:0 0 0 0 rgba(77,107,254,.45);}
    70%{box-shadow:0 0 0 9px rgba(77,107,254,0);}100%{box-shadow:0 0 0 0 rgba(77,107,254,0);}}
  .tl-card{border:1px solid var(--card-border);border-radius:11px;padding:11px 14px;background:var(--surface);}
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
  details.raw pre{background:var(--surface-sunken);border:1px solid var(--line);border-radius:9px;padding:11px 12px;
    font-size:12.5px;line-height:1.65;color:var(--text);overflow-x:auto;white-space:pre-wrap;
    word-break:break-word;margin:8px 0 0;font-family:Consolas,"Courier New",monospace;}

  /* 原始事件：按事件分块的深色代码块 */
  .raw-event{background:#1b2233;border:1px solid #2a3349;border-radius:10px;margin-bottom:10px;overflow:hidden;}
  .raw-event-head{display:flex;align-items:center;gap:9px;padding:8px 12px;
    background:#222b42;border-bottom:1px solid #2a3349;}
  .raw-event-seq{color:#8b97b3;font-family:Consolas,monospace;font-size:12px;font-weight:700;}
  .raw-kind{padding:1px 8px;border-radius:5px;font-size:10.5px;font-weight:700;letter-spacing:.3px;
    border:1px solid transparent;}
  details.raw .raw-event pre{margin:0;padding:11px 12px;font-size:12.5px;line-height:1.65;background:transparent;
    border:none;color:#d6dce8;white-space:pre-wrap;word-break:break-word;overflow-x:auto;
    font-family:Consolas,"Courier New",monospace;}
  .rk-err{background:rgba(248,113,113,.15);color:#fda4a4;border-color:rgba(248,113,113,.3);}
  .rk-tool{background:rgba(96,165,250,.15);color:#93c5fd;border-color:rgba(96,165,250,.3);}
  .rk-plan{background:rgba(129,140,248,.15);color:#a5b4fc;border-color:rgba(129,140,248,.3);}
  .rk-text{background:rgba(52,211,153,.15);color:#6ee7b7;border-color:rgba(52,211,153,.3);}
  .rk-fin{background:rgba(192,132,252,.15);color:#d8b4fe;border-color:rgba(192,132,252,.3);}
  .rk-refl{background:rgba(45,212,191,.15);color:#5eead4;border-color:rgba(45,212,191,.3);}
  .rk-meta,.rk-usage{background:rgba(148,163,184,.15);color:#cbd5e1;border-color:rgba(148,163,184,.3);}
  .rk-other{background:rgba(255,255,255,.07);color:#cdd6ea;border-color:rgba(255,255,255,.13);}
  .json-key{color:#7fb0ff;}
  .json-str{color:#9ed8a6;}
  .json-num{color:#f0b878;}
  .json-bool{color:#e08fb0;}
  .json-null{color:#8b97b3;}

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

  /* 记忆视图：网络 / 列表 切换（统一使用 .view-mode / .vm-btn） */

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
  .mem-node-detail .nd-close{margin-left:auto;border:1px solid var(--border);background:var(--surface);
    border-radius:8px;font-size:11px;padding:4px 11px;cursor:pointer;font-family:inherit;color:var(--sub);}
  .mem-node-detail .nd-content{word-break:break-word;white-space:pre-wrap;}
  .mem-node-detail .nd-meta{margin-top:10px;font-size:11.5px;color:var(--sub);display:flex;gap:14px;flex-wrap:wrap;}
  .mem-node-detail .nd-section{margin-top:12px;padding-top:10px;border-top:1px solid var(--line);}
  .mem-node-detail .nd-section-t{font-size:11px;font-weight:700;color:var(--faint);
    letter-spacing:.6px;margin-bottom:7px;}
  .mem-node-detail .nd-near{display:flex;align-items:center;gap:9px;width:100%;
    text-align:left;background:var(--surface);border:1px solid var(--line);border-radius:9px;
    padding:6px 10px;margin-bottom:5px;cursor:pointer;font-family:inherit;font-size:12.5px;color:var(--text);}
  .mem-node-detail .nd-near:hover{border-color:var(--accent-lt);background:var(--accent-bg);}
  .mem-node-detail .nd-near-k{width:8px;height:8px;border-radius:50%;flex:none;}
  .mem-node-detail .nd-near-t{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .mem-node-detail .nd-near-w{font-size:11px;color:var(--faint);flex:none;font-variant-numeric:tabular-nums;}
  .mem-node-detail .nd-empty{font-size:12px;color:var(--faint);}

  /* 记忆分区（身份 / 价值 / 程序性）：常量块，不在 sidecar，但每轮进 system prompt */
  .mem-partitions{margin-top:14px;background:var(--surface-sunken);border:1px solid var(--line);
    border-radius:13px;padding:14px 16px;}
  .mem-partitions .mp-sub{display:flex;align-items:center;gap:10px;flex-wrap:wrap;
    font-size:12px;font-weight:700;color:var(--text);margin-bottom:10px;}
  .mem-partitions .mp-hint{font-weight:400;font-size:11.5px;color:var(--faint);}
  .mem-partitions .mp-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));
    gap:12px;}
  .mem-partitions .mp-card{background:var(--surface);border:1px solid var(--line);
    border-radius:11px;padding:11px 13px;}
  .mem-partitions .mp-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;
    margin-bottom:7px;}
  .mem-partitions .mp-title{font-size:12.5px;font-weight:700;color:var(--text);}
  .mem-partitions .mp-path{font-size:10.5px;color:var(--faint);font-family:Consolas,Menlo,monospace;}
  .mem-partitions .mp-item{display:flex;gap:7px;align-items:baseline;padding:3px 0;
    font-size:12.5px;line-height:1.6;color:var(--sub);}
  .mem-partitions .mp-text{flex:1;word-break:break-word;}
  .mem-partitions .mp-lv{flex:none;font-size:10.5px;color:var(--accent);border:1px solid var(--line);
    border-radius:5px;padding:0 6px;}
  .mem-partitions .mp-rate{flex:none;font-size:10.5px;color:var(--green);}
  .mem-partitions .mp-empty{font-size:12px;color:var(--faint);}
  /* 睡觉面板 */
  .mem-sleep-bar{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:12px 0 4px;}
  .mem-sleep-time{border:1px solid var(--border);border-radius:9px;padding:6px 10px;
    font-size:13px;font-family:inherit;color:var(--text);background:var(--surface);}
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
  table.tbl td.num{font-family:Consolas,monospace;text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums;}
  table.tbl th.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums;}
  table.tbl td.name{font-family:Consolas,monospace;font-size:12px;}
  table.tbl.cost-split{table-layout:fixed;}
  table.tbl.cost-split td.name{overflow-wrap:anywhere;word-break:break-all;}
  .tbl-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:12px;}
  .tbl-wrap thead th{position:sticky;top:0;z-index:2;}

  /* 成本统计表格：明细表不折行靠横滚；命中率分档着色；花费加粗 */
  #costTurns th, #costTurns td{white-space:nowrap;}
  #costTurns td:nth-child(1){font-variant-numeric:tabular-nums;color:var(--sub);}
  #costTurns td:nth-child(5){font-family:Consolas,monospace;font-size:11.5px;color:var(--text-strong);}
  #costTurns td.cost-money{font-weight:600;color:var(--text-strong);}
  .hitrate{font-variant-numeric:tabular-nums;font-weight:600;}
  .hitrate.hi{color:var(--green);}
  .hitrate.mid{color:var(--amber);}
  .hitrate.lo{color:#d64545;}
  .cost-split td.cost-money{font-weight:600;color:var(--text-strong);}

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
  .danger{border:1.5px dashed #f0b4b4;border-radius:14px;padding:15px 17px;background:var(--surface)afa;}
  .danger h3{margin:0 0 6px;font-size:14px;color:#b42318;}
  .danger .ops{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0;}
  button.btn{border:1px solid var(--border);background:var(--surface);color:var(--text);border-radius:10px;
    padding:8px 14px;font-size:13px;font-family:inherit;cursor:pointer;transition:.15s;
    white-space:nowrap;flex:none;}
  button.btn:hover{border-color:var(--accent-lt);color:var(--accent-d);background:var(--accent-bg);}
  button.btn.danger-btn{border-color:#f0b4b4;color:#b42318;background:var(--surface);}
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
    font-family:inherit;color:var(--text);background:var(--surface);min-width:110px;
    transition:border-color .15s,box-shadow .15s;}
  .set-row input[type=text]:focus,.set-row input[type=password]:focus,.set-row input[type=number]:focus,
  .set-row select:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-bg);}
  .set-row input[type=range]{width:130px;accent-color:var(--accent);}
  .set-row .num{font-family:Consolas,monospace;font-size:12px;color:var(--text);
    min-width:52px;text-align:right;font-variant-numeric:tabular-nums;}
  .set-chips{display:flex;flex-wrap:wrap;gap:7px;}
  .set-chip{border:1px solid var(--border);background:var(--surface);border-radius:8px;padding:4px 10px;
    font-size:12px;font-family:inherit;color:var(--text);cursor:pointer;transition:.15s;}
  .set-chip:hover{border-color:var(--accent-lt);color:var(--accent-d);background:var(--accent-bg);}
  .set-chip.on{border-color:var(--accent);background:var(--accent-bg);color:var(--accent-d);font-weight:600;}
  .set-chip small{display:block;font-size:10px;color:var(--faint);}
  .set-avatars{display:flex;flex-wrap:wrap;gap:9px;}
  .set-avatar{width:62px;height:62px;border-radius:10px;border:2px solid var(--line);
    background:var(--surface) center/cover no-repeat;cursor:pointer;padding:0;}
  .set-avatar.on{border-color:var(--accent);}
  .set-bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:14px;}

  /* Live2D 模型 / 场景 / 动作 */
  .set-models{display:flex;gap:9px;flex-wrap:wrap;}
  .set-model{border:1px solid var(--border);background:var(--surface);border-radius:10px;padding:7px 13px;
    font-size:12.5px;font-family:inherit;color:var(--text);cursor:pointer;transition:.15s;}
  .set-model:hover{border-color:var(--accent-lt);background:var(--accent-bg);}
  .set-model.on{border-color:var(--accent);background:var(--accent-bg);color:var(--accent-d);font-weight:600;}
  .set-model small{display:block;font-size:10px;color:var(--faint);font-weight:400;}
  .set-scene{display:flex;align-items:center;gap:6px 10px;flex-wrap:wrap;padding:8px 0;
    border-bottom:1px solid var(--line);font-size:12.5px;color:var(--text);}
  .set-scene:last-child{border-bottom:0;}
  .set-scene .lft{flex:1 1 150px;min-width:0;display:flex;flex-direction:column;gap:2px;}
  .set-scene .nm{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .set-scene .sum{color:var(--sub);font-size:11.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  .set-scene .rgt{flex:0 0 auto;display:flex;align-items:center;gap:6px;flex-wrap:wrap;}
  .set-scene .tag{flex:none;font-size:10.5px;color:var(--faint);}
  .set-scene .tag.over{color:var(--accent-d);font-weight:600;}
  .set-mini{border:1px solid var(--border);background:var(--surface);border-radius:7px;padding:3px 10px;
    font-size:11.5px;font-family:inherit;color:var(--sub);cursor:pointer;flex:none;}
  .set-mini:hover{border-color:var(--accent-lt);color:var(--accent-d);background:var(--accent-bg);}
  .set-mini.danger:hover{border-color:#f0b4b4;color:#b42318;background:var(--red-bg);}
  .set-editor{margin:8px 0 14px;padding:14px 16px;background:var(--surface-sunken);
    border:1px solid var(--line);border-radius:12px;}
  .set-editor.plain{margin:4px 0 0;padding:10px 12px;background:var(--surface);}
  .set-editor h5{margin:0 0 10px;font-size:13px;font-weight:700;color:var(--accent-d);}
  .set-field{display:flex;align-items:center;gap:10px;margin-bottom:8px;flex-wrap:wrap;}
  .set-field > .cap{width:70px;flex:none;font-size:12.5px;color:var(--sub);font-weight:600;}
  .set-field select,.set-field input[type=text],.set-field input[type=number]{
    border:1px solid var(--border);border-radius:8px;padding:5px 9px;font-size:12.5px;
    font-family:inherit;color:var(--text);background:var(--surface);min-width:150px;}
  .set-toggle-grid{display:flex;flex-wrap:wrap;gap:7px;margin-bottom:10px;}
  .set-toggle-grid .set-chip.on{background:var(--accent);border-color:var(--accent);color:#fff;}
  .set-editor .row{display:flex;gap:9px;margin-top:12px;flex-wrap:wrap;}

  ::-webkit-scrollbar{width:9px;height:9px;}
  ::-webkit-scrollbar-track{background:transparent;}
  ::-webkit-scrollbar-thumb{background:#cdd4e0;border-radius:6px;border:2px solid var(--bg);}
  ::-webkit-scrollbar-thumb:hover{background:#b3bccb;}
  @media(max-width:1100px){
    .stats{grid-template-columns:repeat(2,1fr);}
    .sessions{grid-template-columns:1fr;}
    .run-list{max-height:360px;}
  }
  /* ============ 美化：细节打磨 ============ */
  /* 侧边栏激活指示条 */
  .tab{position:relative;}
  .tab::before{content:"";position:absolute;left:-14px;top:50%;transform:translateY(-50%);
    width:3px;height:16px;border-radius:3px;background:var(--accent);opacity:0;transition:opacity .18s;}
  .tab:hover::before,.tab.active::before{opacity:1;}
  .tab.active{box-shadow:inset 0 0 0 1px var(--accent-lt);}

  /* 统计卡：图标色块 + 数字更大 */
  .stat .ico{width:34px;height:34px;border-radius:9px;background:var(--accent-bg);color:var(--accent);
    display:flex;align-items:center;justify-content:center;}
  .stat .num{font-size:28px;}
  .stat .label{letter-spacing:.2px;}

  /* 运行列表项：hover 滑动 */
  .run-item{position:relative;transition:background .15s,transform .15s,border-color .15s;}
  .run-item:hover{transform:translateX(2px);}

  /* 详情：回答卡片左边框 + 章节标题 accent 条 */
  .answer{border-left:3px solid var(--accent-lt);}
  .section-title{position:relative;}
  .section-title::before{content:"";width:3px;height:14px;border-radius:3px;background:var(--accent);}

  /* 表格：斑马纹 + hover */
  table.tbl tbody tr:nth-child(even) td{background:var(--surface-sunken);}
  table.tbl tbody tr:hover td{background:var(--surface-hover);}
  table.tbl tbody tr td:first-child{box-shadow:inset 0 0 0 0 transparent;transition:box-shadow .18s ease;}
  table.tbl tbody tr:hover td:first-child{box-shadow:inset 3px 0 0 var(--accent);}

  /* 焦点环 */
  button.btn:focus-visible,.chip:focus-visible,.theme-btn:focus-visible,.tab:focus-visible,
  .search:focus-visible{outline:none;box-shadow:0 0 0 3px rgba(77,107,254,.18);}

  /* 主题切换按钮 */
  .appearance-row{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 4px;}
  .theme-btn{border:1px solid var(--border);background:var(--surface);color:var(--sub);
    border-radius:10px;padding:8px 14px;font-size:13px;font-family:inherit;cursor:pointer;transition:.15s;flex:none;}
  .theme-btn:hover{border-color:var(--accent-lt);color:var(--accent-d);}
  .theme-btn.active{background:var(--accent-bg);border-color:var(--accent-lt);color:var(--accent-d);font-weight:600;}

  /* 面板微效 */
  .panel{transition:box-shadow .18s ease;}

  /* 深色下修正硬编码语义色 */
  :root[data-theme="dark"] .answer.error{color:#f2b8a0;border-color:#5a2e2e;}
  :root[data-theme="dark"] .error-banner{color:#f2b8a0;border-color:#5a2e2e;}
  :root[data-theme="dark"] button.btn.danger-btn{color:#f2b8a0;border-color:#5a2e2e;}
  :root[data-theme="dark"] .answer code{background:#2a3140;color:#cbd5e6;}
  :root[data-theme="dark"] .tl-card.tool .cap{color:#e0a94e;}
  :root[data-theme="dark"] .tl-item.plan .tl-card .cap{color:#60a5fa!important;}
  :root[data-theme="dark"] .bar-row .fill{background:linear-gradient(90deg,#7aa5ff,#5b7bff);}
  :root[data-theme="dark"] .cs-row .fill.satiety{background:linear-gradient(90deg,#fbbf24,#f59e0b);}
  :root[data-theme="dark"] .cs-row .fill.mood{background:linear-gradient(90deg,#7aa5ff,#5b7bff);}
  :root[data-theme="dark"] .cs-row .fill.exp{background:linear-gradient(90deg,#3ddba2,#12b989);}
  :root[data-theme="dark"] .cs-row .fill.favor{background:linear-gradient(90deg,#f47bb6,#ec4899);}
  :root[data-theme="dark"] .cs-row .fill.level{background:linear-gradient(90deg,#6ea8ff,#4b8af6);}
  :root[data-theme="dark"] .cs-row .fill.exp_to_next{background:linear-gradient(90deg,#94a3b8,#64748b);}
  /* ============ 顶部分栏切换器（统一所有 Tab） ============ */
  .view-mode{position:relative;display:flex;gap:6px;padding:4px;background:var(--surface-sunken);
    border:1px solid var(--line);border-radius:12px;margin:0 0 16px;width:fit-content;
    max-width:100%;flex-wrap:nowrap;box-shadow:inset 0 1px 2px rgba(0,0,0,.03);}
  .vm-indicator{position:absolute;top:4px;bottom:4px;left:0;width:0;border-radius:9px;
    background:var(--card);border:1px solid var(--accent-lt);box-shadow:var(--shadow-sm);
    pointer-events:none;z-index:0;
    transition:left .28s cubic-bezier(.34,1.3,.5,1),width .28s cubic-bezier(.34,1.3,.5,1);}
  .vm-btn{position:relative;z-index:1;flex:none;text-align:left;
    border:1px solid transparent;background:transparent;color:var(--sub);
    border-radius:9px;padding:7px 16px;font-size:13px;font-weight:600;font-family:inherit;
    cursor:pointer;transition:color .18s ease,transform .12s ease;white-space:nowrap;}
  .vm-btn:hover{color:var(--text-strong);}
  .vm-btn:active{transform:scale(.94);}
  .vm-btn.active{background:transparent;border-color:transparent;color:var(--accent-d);}
  .vm-pane{display:none;animation:vmIn .22s ease;}
  .vm-pane.active{display:block;}
  @keyframes vmIn{from{opacity:0;transform:translateY(6px);}to{opacity:1;transform:none;}}

  /* 成本总览二级统计切换器 */
  .cost-tabs{display:inline-flex;gap:4px;padding:3px;background:var(--surface-sunken);
    border:1px solid var(--line);border-radius:10px;margin:0 0 12px;}
  .cost-btn{border:0;background:transparent;color:var(--sub);border-radius:7px;
    padding:5px 14px;font-size:12.5px;font-weight:600;font-family:inherit;cursor:pointer;
    transition:color .15s ease,background .15s ease,transform .12s ease;}
  .cost-btn:hover{color:var(--text-strong);}
  .cost-btn:active{transform:scale(.95);}
  .cost-btn.active{background:var(--card);color:var(--accent-d);box-shadow:var(--shadow-sm);}
  /* 堆叠条形统计图（时间 × 模型） */
  .cost-bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:0 0 12px;}
  .cc-head{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin:2px 0 12px;flex-wrap:wrap;}
  .cc-head-l{display:flex;align-items:baseline;gap:8px;}
  .cc-head-t{font-size:14px;font-weight:700;color:var(--text-strong);}
  .cc-head-s{font-size:11px;color:var(--faint);}
  .cc-head-r{font-size:12px;color:var(--sub);display:flex;align-items:baseline;gap:10px;}
  .cc-head-r b{color:var(--text-strong);font-weight:700;font-variant-numeric:tabular-nums;}
  .cc-head-r .div{color:var(--line);}
  .cc-legend{display:flex;flex-wrap:wrap;gap:12px;margin:0 0 12px;}
  .cc-legend .cc-lg{display:inline-flex;align-items:center;gap:6px;font-size:11.5px;color:var(--sub);}
  .cc-legend .cc-lg i{width:10px;height:10px;border-radius:3px;flex:none;}
  .cc-chart{display:flex;gap:8px;}
  .cc-yaxis{position:relative;width:58px;height:240px;flex:none;}
  .cc-yaxis span{position:absolute;right:8px;transform:translateY(50%);font-size:10.5px;color:var(--faint);font-variant-numeric:tabular-nums;}
  .cc-body{position:relative;flex:1;height:240px;}
  .cc-grid{position:absolute;inset:0;pointer-events:none;}
  .cc-grid i{position:absolute;left:0;right:0;border-top:1px dashed var(--line);}
  .cc-plot{position:absolute;inset:0;display:flex;align-items:flex-end;gap:8px;padding:0 4px;}
  .cc-plot .cc-col{position:relative;display:flex;flex-direction:column;justify-content:flex-end;
    flex:1;min-width:18px;max-width:52px;gap:0;border-radius:5px 5px 0 0;overflow:visible;}
  .cc-plot .cc-seg{width:100%;min-height:0;}
  .cc-plot .cc-seg:first-child{border-radius:5px 5px 0 0;}
  .cc-col .cc-tip{display:none;position:absolute;bottom:calc(100% + 8px);left:50%;transform:translateX(-50%);
    background:var(--card);border:1px solid var(--line);border-radius:10px;padding:9px 12px;
    font-size:11.5px;z-index:20;box-shadow:0 6px 20px rgba(0,0,0,.12);white-space:nowrap;min-width:150px;}
  .cc-col:hover .cc-tip{display:block;}
  .cc-tip .cc-tip-t{font-weight:700;color:var(--text-strong);margin-bottom:6px;font-size:12px;}
  .cc-tip .cc-tip-r{display:flex;align-items:center;gap:7px;color:var(--sub);margin-top:4px;}
  .cc-tip .cc-tip-r i{width:9px;height:9px;border-radius:3px;flex:none;}
  .cc-tip .cc-tip-r b{color:var(--text-strong);font-weight:700;margin-left:auto;font-variant-numeric:tabular-nums;}
  .cc-xaxis{display:flex;gap:8px;margin:8px 0 0 66px;}
  .cc-xaxis .cc-x{flex:1;min-width:18px;text-align:center;font-size:10.5px;color:var(--faint);
    white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}

  /* 按钮微动效（面板入场已移除，避免 Tab 切换卡顿） */
  button.btn:active,.chip:active,.tab:active,.theme-btn:active{transform:scale(.97);}
  .run-item:active{transform:scale(.995);}
  /* ============ 继续美化：静态光晕 / 面板悬停（无卡顿动画） ============ */
  .stat::after{content:"";position:absolute;right:-28px;top:-28px;width:110px;height:110px;border-radius:50%;
    background:radial-gradient(circle,var(--accent-bg) 0%,transparent 70%);opacity:.55;pointer-events:none;}
  .stat.green::after{background:radial-gradient(circle,var(--green-bg) 0%,transparent 70%);}
  .stat.blue::after{background:radial-gradient(circle,var(--blue-bg) 0%,transparent 70%);}
  .stat.amber::after{background:radial-gradient(circle,var(--amber-bg) 0%,transparent 70%);}
  .stat.violet::after{background:radial-gradient(circle,var(--accent-bg) 0%,transparent 70%);}
  .panel:hover{box-shadow:var(--shadow);}
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
    <button type="button" class="tab" data-tab="memory" data-view="memory" data-icon="brain" title="长期记忆：sidecar 的 JSON 卡片 / kind 与 importance 分布 / 命中数 / 逐条内容">记忆</button>
    <button type="button" class="tab" data-tab="decision" data-view="decision" data-icon="compass">决策</button>
  </nav>
  <div class="nav-label">系统</div>
  <nav class="nav">
    <button type="button" class="tab" data-tab="tools" data-view="tools" data-icon="wrench" title="工具统计：内置 + MCP 清单 / 风险等级 / 解锁等级 / 执行期白名单 / 文档对账差异">工具</button>
    <button type="button" class="tab" data-tab="voice" data-view="voice" data-icon="heart" title="本机语音：音色权重 / 情绪参考库 / TTS 与 RVC 服务状态">语音</button>
    <button type="button" class="tab" data-tab="settings" data-view="settings" data-icon="zap" title="Live2D 设置：视觉 / 行为 / 语音 / 模型（改完立刻生效）">Live2D 设置</button>
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
        <div class="run-list" id="runList" tabindex="0"></div>
        <div id="runPager"></div>
      </div>
      <div class="panel" id="detailPanel">
        <div class="empty"><div class="big" data-icon="search" data-size="40"></div><div>选择左侧会话，查看完整执行过程</div></div>
      </div>
    </div>
  </div>

  <!-- ============ Tab 成本 ============ -->
  <div class="view" id="view-cost">
    <div class="panel" style="padding:18px;">
      <div class="view-mode">
        <span class="vm-indicator"></span>
        <button type="button" class="vm-btn active" data-pane="overview">总览</button>
        <button type="button" class="vm-btn" data-pane="detail">明细</button>
      </div>
      <div class="vm-pane active" data-pane="overview">
        <div class="sub-head" style="margin-top:0;" data-icon="zap">成本总览 <span id="costSource" class="mini-tag"></span></div>
        <div class="grid3" id="costKpis"></div>
        <div id="costErr"></div>
        <div id="costPriceNote"></div>
        <div class="sub-head">统计（按时间桶 × 模型）</div>
        <div class="cost-bar">
          <div class="cost-tabs">
            <button type="button" class="cost-btn active" data-cost="volume">调用量</button>
            <button type="button" class="cost-btn" data-cost="requests">请求数</button>
            <button type="button" class="cost-btn" data-cost="spend">花费金额</button>
          </div>
          <div class="cost-tabs">
            <button type="button" class="cost-btn active" data-range="24h">24H</button>
            <button type="button" class="cost-btn" data-range="7d">近7天</button>
            <button type="button" class="cost-btn" data-range="30d">近30天</button>
          </div>
        </div>
        <div id="costChart"></div>
      </div>
      <div class="vm-pane" data-pane="detail">
        <div class="sub-head" style="margin-top:0;">逐请求明细（最贵的轮次）</div>
        <div class="tbl-wrap"><table class="tbl" id="costTurns"></table></div>
        <div id="costTurnsPager"></div>
        <div class="sub-head">按会话 / 模型 / 后端拆分</div>
        <div id="costBreak"></div>
      </div>
    </div>
  </div>

  <!-- ============ Tab 记忆 ============ -->
  <div class="view" id="view-memory">
    <div class="panel" style="padding:18px;">
      <div class="sub-head" style="margin-top:0;" data-icon="brain">长期记忆（sidecar） <span id="memCount" class="mini-tag"></span></div>
      <div id="memStoreHint"></div>
      <div class="grid3" id="memKpis" style="margin-top:12px;"></div>

      <div class="view-mode" id="memMode" role="tablist">
        <span class="vm-indicator"></span>
        <button type="button" class="vm-btn active" data-mode="net" data-pane="net">网络</button>
        <button type="button" class="vm-btn" data-mode="list" data-pane="list">列表</button>
      </div>

      <!-- 3D 神经网络（深色画布卡片） -->
      <div class="mem-net vm-pane active" id="memNet" data-pane="net">
        <canvas id="memCanvas"></canvas>
        <div class="mem-net-legend" id="memLegend"></div>
        <span class="mem-net-badge" id="memGraphBadge"></span>
        <button type="button" class="mem-net-reset" id="memReset">重置视角</button>
        <div class="mem-tip" id="memTip"></div>
        <div class="mem-net-empty" id="memNetEmpty" style="display:none;"></div>
      </div>
      <!-- 记忆分区（身份 / 价值 / 程序性）：不在 sidecar 里，但每轮都进 system prompt -->
      <div class="mem-partitions vm-pane active" data-pane="net" id="memPartitions"></div>
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
      <div class="mem-list-wrap vm-pane" id="memListWrap" data-pane="list">
        <div class="sub-head">按 kind / importance 分布</div>
        <div id="memDist"></div>
        <div class="sub-head">逐条内容（JSON 记忆卡）</div>
        <div class="grid2" id="memGrid"></div>
        <div id="memGridPager"></div>
      </div>

      <div class="danger" style="border-color:#f0d3a0;background:var(--surface)df7;">
        <h3 style="color:#a36500;">索引重建（危险操作）</h3>
        <div class="hint">真值永远是 sidecar 的 JSON 卡片；重建只是让它把 Chroma 向量索引和 BM25 索引按卡片重算一遍，卡片本身不会被改动。要执行请到「健康与维护」Tab 打开维护模式。</div>
      </div>
    </div>
  </div>

  <!-- ============ Tab 决策 ============ -->
  <div class="view" id="view-decision">
    <div class="panel" style="padding:18px;">
      <div class="view-mode">
        <span class="vm-indicator"></span>
        <button type="button" class="vm-btn active" data-pane="log">决策日志</button>
        <button type="button" class="vm-btn" data-pane="result">结果</button>
      </div>
      <div class="vm-pane active" data-pane="log">
        <div class="sub-head" style="margin-top:0;" data-icon="compass">桌宠决策日志 <span id="decisionSource" class="mini-tag"></span></div>
        <div class="grid3" id="decisionKpis"></div>
        <div class="sub-head">最近决策</div>
        <div id="decisionList"></div>
      </div>
      <div class="vm-pane" data-pane="result">
        <div class="sub-head" style="margin-top:0;">最近结果</div>
        <div id="outcomeList"></div>
        <div class="sub-head">养成规则（生效配置）</div>
        <div id="decisionConfig"></div>
      </div>
    </div>
  </div>

  <!-- ============ Tab 工具 ============ -->
  <div class="view" id="view-tools">
    <div class="panel" style="padding:18px;">
      <div class="view-mode">
        <span class="vm-indicator"></span>
        <button type="button" class="vm-btn active" data-pane="list">清单</button>
        <button type="button" class="vm-btn" data-pane="config">配置</button>
      </div>
      <div class="vm-pane active" data-pane="list">
        <div class="sub-head" style="margin-top:0;" data-icon="wrench">工具统计 · 清单 <span id="toolCount" class="mini-tag"></span></div>
        <div class="grid3" id="toolKpis"></div>
        <div id="toolErr"></div>
        <div class="sub-head">风险等级分布</div>
        <div id="toolRisk"></div>
        <div class="sub-head">全部工具</div>
        <div class="tbl-wrap"><table class="tbl" id="toolTable"></table></div>
        <div id="toolTablePager"></div>
      </div>
      <div class="vm-pane" data-pane="config">
        <div class="sub-head" style="margin-top:0;">执行期白名单（tool_allowlist）</div>
        <div id="toolAllowlist"></div>
        <div class="sub-head">对账结果（文档 ↔ 注册表）</div>
        <div id="reconBox"></div>
      </div>
    </div>
  </div>

  <!-- ============ Tab 语音 ============ -->
  <div class="view" id="view-voice">
    <div class="panel" style="padding:18px;">
      <div class="view-mode">
        <span class="vm-indicator"></span>
        <button type="button" class="vm-btn active" data-pane="service">服务</button>
        <button type="button" class="vm-btn" data-pane="voice">音色</button>
      </div>
      <div class="vm-pane active" data-pane="service">
        <div class="sub-head" style="margin-top:0;" data-icon="heart">本机语音 <span id="voiceTag" class="mini-tag"></span></div>
        <div id="voiceErr"></div>
        <div class="grid3" id="voiceKpis"></div>
        <div class="sub-head">服务与脚本</div>
        <div id="voiceServices"></div>
      </div>
      <div class="vm-pane" data-pane="voice">
        <div class="sub-head" style="margin-top:0;">音色模型（RVC 权重）</div>
        <div class="hint">这些 .pth 就是本机桌宠能用的嗓子。面板只读盘点，不会自动下载任何模型。</div>
        <div class="tbl-wrap"><table class="tbl" id="voiceModels"></table></div>
        <div id="voiceModelsPager"></div>
        <div class="sub-head">情绪参考库</div>
        <div id="voiceEmotions"></div>
      </div>
    </div>
  </div>

  <!-- ============ Tab 设置 ============ -->
  <div class="view" id="view-settings">
    <div class="panel" style="padding:18px;">
      <div class="view-mode">
        <span class="vm-indicator"></span>
        <button type="button" class="vm-btn active" data-pane="live2d">Live2D</button>
        <button type="button" class="vm-btn" data-pane="behavior">行为</button>
      </div>
      <div class="vm-pane active" data-pane="live2d">
        <div class="sub-head" style="margin-top:0;" data-icon="zap">桌宠设置 <span id="settingsTag" class="mini-tag"></span></div>
        <div id="settingsErr"></div>
        <div class="set-bar">
          <button type="button" class="btn" id="settingsSave">保存全部</button>
          <button type="button" class="btn" id="settingsReset">恢复默认</button>
          <button type="button" class="btn" id="settingsReload">重新读取</button>
          <span class="hint" style="margin:0;">改动点「保存」后立刻在桌宠上生效。</span>
        </div>
        <div id="settingsGroups" class="set-grid"></div>
        <div class="sub-head">Live2D 模型</div>
        <div id="settingsModels"></div>
        <div class="sub-head">Live2D 外观</div>
        <div id="settingsAppearance"></div>
      </div>
      <div class="vm-pane" data-pane="behavior">
        <div class="sub-head" style="margin-top:0;">触发场景配置</div>
        <div class="hint">给某个场景配一套外观 —— 触发时自动换上（如「摸头」时脸红 + 比心）。每个模型各配各的。</div>
        <div id="settingsScenes"></div>
        <div class="sub-head">自定义动作</div>
        <div class="hint">自己命名一个动作、配上外观，可以绑到钩子（工具动作或模型自带动作），也会出现在右键「玩一下」里。</div>
        <div id="settingsActions"></div>
        <div class="sub-head">桌宠头像</div>
        <div id="settingsAvatars"></div>
      </div>
    </div>
  </div>

  <!-- ============ Tab 配置 ============ -->
  <div class="view" id="view-config">
    <div class="panel" style="padding:18px;">
      <div class="view-mode">
        <span class="vm-indicator"></span>
        <button type="button" class="vm-btn active" data-pane="config">配置</button>
        <button type="button" class="vm-btn" data-pane="appearance">外观</button>
      </div>
      <div class="vm-pane active" data-pane="config">
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
      <div class="vm-pane" data-pane="appearance">
        <div class="sub-head" style="margin-top:0;" data-icon="gauge">外观</div>
        <div class="appearance-row" id="appearanceRow">
          <button type="button" class="theme-btn active" data-theme="light">☀ 浅色</button>
          <button type="button" class="theme-btn" data-theme="dark">🌙 深色</button>
          <button type="button" class="theme-btn" data-theme="system">💻 跟随系统</button>
        </div>
      </div>
    </div>
  </div>

  <!-- ============ Tab 状态 ============ -->
  <div class="view" id="view-state">
    <div class="panel" style="padding:18px;">
      <div class="view-mode">
        <span class="vm-indicator"></span>
        <button type="button" class="vm-btn active" data-pane="state">状态</button>
        <button type="button" class="vm-btn" data-pane="unlock">解锁</button>
      </div>
      <div class="vm-pane active" data-pane="state">
        <div class="sub-head" style="margin-top:0;" data-icon="heart">桌宠状态 <span id="statePath" class="mini-tag"></span></div>
        <div id="stateErr"></div>
        <div id="stateBars"></div>
      </div>
      <div class="vm-pane" data-pane="unlock">
        <div class="sub-head" style="margin-top:0;">工具解锁进度</div>
        <div id="stateUnlock"></div>
      </div>
    </div>
  </div>

  <!-- ============ Tab 健康与维护 ============ -->
  <div class="view" id="view-health">
    <div class="panel" style="padding:18px;">
      <div class="view-mode">
        <span class="vm-indicator"></span>
        <button type="button" class="vm-btn active" data-pane="process">进程</button>
        <button type="button" class="vm-btn" data-pane="files">文件</button>
      </div>
      <div class="vm-pane active" data-pane="process">
        <div class="sub-head" style="margin-top:0;" data-icon="alert">进程健康</div>
        <div class="grid3" id="healthKpis"></div>
        <div id="healthErr"></div>
        <div class="sub-head">限流 / 崩溃样本</div>
        <div id="healthSamples"></div>
      </div>
      <div class="vm-pane" data-pane="files">
        <div class="sub-head" style="margin-top:0;">data/ 文件体检</div>
        <div class="tbl-wrap"><table class="tbl" id="healthFiles"></table></div>
        <div id="healthFilesPager"></div>
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
  maintOn:false, memory:null, health:null, voice:null,
};

/* ============ 外观主题（浅色 / 深色 / 跟随系统） ============ */
let themeMode=(function(){try{return localStorage.getItem("dash-theme")||"system";}catch(_){return "system";}})();
const themeMedia=window.matchMedia?window.matchMedia("(prefers-color-scheme: dark)"):null;
function applyTheme(){
  const dark=themeMode==="dark"||(themeMode==="system"&&themeMedia&&themeMedia.matches);
  document.documentElement.setAttribute("data-theme",dark?"dark":"light");
  $$("#appearanceRow .theme-btn").forEach(function(b){b.classList.toggle("active",b.dataset.theme===themeMode);});
}
function setThemeMode(mode){
  themeMode=mode;
  try{localStorage.setItem("dash-theme",mode);}catch(_){}
  applyTheme();
}
$$("#appearanceRow .theme-btn").forEach(function(b){b.onclick=function(){setThemeMode(b.dataset.theme);};});
if(themeMedia&&themeMedia.addEventListener)themeMedia.addEventListener("change",function(){if(themeMode==="system")applyTheme();});
applyTheme();
let memMode="net"; // 记忆视图：net 神经网络 / list 列表

/* ============ 通用分页（零依赖，只切显示不改数据） ============ */
const PG_DEFAULT_SIZE=15;
const pgState={};
function pgPage(key,total,size){
  const pages=Math.max(1,Math.ceil(total/size));
  let p=pgState[key]||1;
  if(p>pages)p=pages;
  if(p<1)p=1;
  pgState[key]=p;
  return p;
}
function paged(arr,key,size){
  size=size||PG_DEFAULT_SIZE;
  const p=pgPage(key,arr.length,size);
  const start=(p-1)*size;
  return arr.slice(start,start+size);
}
function pagerBar(total,key,size,standalone){
  size=size||PG_DEFAULT_SIZE;
  if(total<=size)return "";
  const pages=Math.ceil(total/size);
  const p=pgPage(key,total,size);
  const from=(p-1)*size+1,to=Math.min(p*size,total);
  const seq=[];
  for(let i=1;i<=pages;i++){
    if(i===1||i===pages||(i>=p-1&&i<=p+1))seq.push(i);
    else if(seq.length&&seq[seq.length-1]!=="…")seq.push("…");
  }
  const kq="'"+key+"'";
  const nums=seq.map(i=>i==="…"
    ?'<span class="pg-gap">…</span>'
    :'<button type="button" class="'+(i===p?"active":"")+'" onclick="goPage('+kq+','+i+')">'+i+'</button>').join("");
  return '<div class="pager'+(standalone?" standalone":"")+'">'
    +'<span class="pg-info">'+from+'-'+to+' / '+fmtNum(total)+'</span>'
    +'<div class="pg-btns">'
    +'<button type="button" '+(p===1?"disabled":"")+' onclick="goPage('+kq+','+(p-1)+')">上一页</button>'
    +nums
    +'<button type="button" '+(p===pages?"disabled":"")+' onclick="goPage('+kq+','+(p+1)+')">下一页</button>'
    +'</div></div>';
}
function goPage(key,page){
  pgState[key]=page;
  const fn=PG_RENDER[key];
  if(fn)fn();
}

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
/* 金额：**币种跟着后端走**（本项目是人民币 ¥）。
   以前这里写死 "$"，面板显示的是美元，实际账单是人民币 —— 差 7 倍。 */
function costSymbol(c){
  const c2=c||{};
  if(c2.symbol) return c2.symbol;
  return c2.currency==="USD"?"$":"¥";
}
function costAmount(c){
  const c2=c||{};
  const v=(c2.amount!=null?c2.amount:c2.value);
  return Number(v||0).toFixed(6);
}
function costMoney(c){ return costSymbol(c)+costAmount(c); }
function costCell(c){
  const c2=c||{};
  return costMoney(c2)+(c2.estimated!==false?'<span class="est-tag">估算</span>':"");
}
function fmtCardValue(c){
  const label=(c&&c.label)||"", value=c&&c.value;
  if(label.indexOf("成功率")>=0 || label.indexOf("命中率")>=0) return String(value);
  if(label.indexOf("耗时")>=0) return fmtDur(value);
  if(label.indexOf("花费")>=0) return costCell(c);
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
    + '<div class="num">'+fmtCardValue(c)+'</div>'
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
  const pager=$("#runPager");
  if(!list.length){
    box.innerHTML='<div class="empty" style="min-height:200px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;">'
      + icon("search",42)+'<div>没有符合条件的会话</div>'
      + ((state.filter!=="all"||state.kw)
          ? '<button type="button" class="btn" onclick="clearRunFilter()">清除筛选</button>' : "")
      + '</div>';
    if(pager)pager.innerHTML="";
    return;
  }
  const SIZE=12;
  box.innerHTML=paged(list,"runs",SIZE).map(function(r){
    const tags=[];
    if(r.tool_calls>0) tags.push('<span class="mini-tag">'+icon("wrench",12)+' '+r.tool_calls+'</span>');
    if(r.total_steps>0) tags.push('<span class="mini-tag">'+icon("zap",12)+' '+r.total_steps+'</span>');
    return '<div class="run-item '+(r.id===state.selectedId?"active":"")+'" data-id="'+r.id+'">'
      + '<div class="goal">'+esc(r.goal||"(无内容)")+'</div>'
      + '<div class="meta">'+badge(r.status)+'<span class="t">'+timeAgo(r.started_at)+'</span>'+tags.join("")+'</div>'
      + '</div>';
  }).join("");
  box.scrollTop=0;
  if(pager)pager.innerHTML=pagerBar(list.length,"runs",SIZE);
}

function pickStreamText(events){
  let acc="";
  for(const e of events){
    if(e.kind==="text" && e.payload && e.payload.accumulated!=null)
      acc=e.payload.accumulated;
  }
  return acc;
}

/* JSON 语法高亮：先转义再上色，安全且零依赖 */
function jsonHighlight(value){
  let json;
  try{json=JSON.stringify(value,null,2);}catch(_){return esc(String(value));}
  return esc(json).replace(
    /("(\\u[a-f0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(true|false)\b|\bnull\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g,
    function(m){
      let cls="json-num";
      if(m.charAt(0)==='"')cls=/:$/.test(m)?"json-key":"json-str";
      else if(m==="true"||m==="false")cls="json-bool";
      else if(m==="null")cls="json-null";
      return '<span class="'+cls+'">'+m+'</span>';
    });
}
function rawKindClass(k){
  const map={error:"rk-err",tool:"rk-tool",plan:"rk-plan",text:"rk-text",
    finish:"rk-fin",reflection:"rk-refl",meta:"rk-meta",usage:"rk-usage"};
  return map[k]||"rk-other";
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
  const panel=$("#detailPanel");
  const sameRun = panel.getAttribute("data-run-id")===String(r.id);
  const openRaw = {};
  if(sameRun){
    $$("#detailPanel details").forEach(function(d){
      if(d.open) openRaw[d.getAttribute("data-raw")||""]=true;
    });
  }
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
    tlHtml='<div class="timeline">'+tl.map(function(e,i){
      const p=e.payload||{};
      const live=(r.status==="running"&&i===tl.length-1)?" live":"";
      if(e.kind==="tool")
        return '<div class="tl-item tool'+live+'"><div class="node">'+icon("wrench")+'</div>'+renderToolCard(p)+'</div>';
      if(e.kind==="plan")
        return '<div class="tl-item plan'+live+'"><div class="node">'+icon("compass")+'</div>'+renderPlanCard(p)+'</div>';
      if(e.kind==="reflection")
        return '<div class="tl-item reflection'+live+'"><div class="node">'+icon("eye")+'</div>'
          + '<div class="tl-card"><div class="cap" style="color:var(--accent-d);">'+icon("eye")+' 反思评估</div>'
          + '<div style="font-size:12.5px;">'+esc(p.comment||"")+' '
          + (p.verdict?'<span class="mini-tag" style="margin-left:6px;">'+esc(p.verdict)+'</span>':"")+'</div></div></div>';
      if(e.kind==="meta"){
        const label=p.event==="force_retry"?"自动重试":p.event||"内部事件";
        const note=p.reason||p.intent||"";
        return '<div class="tl-item meta'+live+'"><div class="node">'+icon("info")+'</div>'
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
    ? '<details class="raw" data-raw="stream"><summary>查看生成过程（流式文本）</summary><pre>'+esc(stream)+'</pre></details>' : "";
  const rawHtml='<details class="raw" data-raw="events"><summary>原始事件（'+ev.length+' 条 · 开发者）</summary>'
    + ev.map(function(e){
        return '<div class="raw-event"><div class="raw-event-head">'
          + '<span class="raw-event-seq">#'+esc(e.seq)+'</span>'
          + '<span class="raw-kind '+rawKindClass(e.kind)+'">'+esc(e.kind)+'</span></div>'
          + '<pre>'+jsonHighlight(e.payload)+'</pre></div>';
      }).join("")+'</details>';

  const finish=ev.find(e=>e.kind==="finish");
  const emo=finish&&finish.payload&&finish.payload.emotion;

  panel.setAttribute("data-run-id", String(r.id));
  panel.innerHTML='<div class="detail">'
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
        : '<div class="answer '+(r.status==="failed"?"error":"")+'">'+(miniMd(answer)||"(空)")+'</div>')
    + '<div class="section-title">'+icon("compass")+' 执行过程</div>'
    + tlHtml+streamHtml+rawHtml
    + '</div>';
  if(sameRun){
    $$("#detailPanel details").forEach(function(d){
      const k=d.getAttribute("data-raw")||"";
      if(openRaw[k]) d.open=true;
    });
  }
}

async function selectRun(id){
  state.selectedId=id;
  renderRunList();
  $("#detailPanel").innerHTML='<div class="detail">'+skeletonBlock(9)+'</div>';
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
let costMetric="volume";
let costRange="24h";
const COST_RANGES={"24h":{ms:3600000,n:24},"7d":{ms:21600000,n:28},"30d":{ms:86400000,n:30}};
const CC_PALETTE=["#4d6bfe","#10b981","#f59e0b","#ec4899","#8b5cf6","#06b6d4","#f43f5e","#84cc16","#f97316","#64748b"];
function ccLabel(m){ return m==="requests"?"请求数":m==="spend"?"花费金额":"调用量"; }
function ccTimeLabel(ts,range){
  const d=new Date(ts*1000), p=function(n){return String(n).padStart(2,"0");};
  if(range==="24h") return p(d.getHours())+":"+p(d.getMinutes());
  if(range==="7d") return p(d.getMonth()+1)+"-"+p(d.getDate())+" "+p(d.getHours())+":00";
  return p(d.getMonth()+1)+"-"+p(d.getDate());
}
function renderCostChart(series, cur){
  const box=$("#costChart");
  if(!box)return;
  const items=(series&&series.items)||[];
  if(!items.length){ box.innerHTML='<div class="hint">还没有 token 账本数据。</div>'; return; }
  if(!state.costColors)state.costColors={};
  const colors=state.costColors;
  const range=COST_RANGES[costRange]||COST_RANGES["24h"];
  function val(mm){
    if(costMetric==="requests") return Number(mm.requests||0);
    if(costMetric==="spend") return Number((mm.cost&&mm.cost.amount)||0);
    return Number(mm.total_tokens||0);
  }
  function fmtSmart(v){
    v=Number(v||0);
    if(costMetric==="spend") return (cur==="USD"?"$":"¥")+v.toFixed(2);
    if(v<1000) return String(Math.round(v));
    if(v<1e6) return (v/1e3).toFixed(1)+"K";
    if(v<1e9) return (v/1e6).toFixed(2)+"M";
    return (v/1e9).toFixed(2)+"B";
  }
  // 把 API items 按 ts 建索引，再填充完整时间轴
  const itemMap={};
  items.forEach(function(it){ itemMap[it.ts]=it; });
  // 以最新 ts 为终点，往前推 n 个桶
  const latestTs=items[items.length-1].ts;
  const buckets=[];
  for(let i=range.n-1;i>=0;i--){
    const ts=latestTs - i*(range.ms/1000);
    const it=itemMap[ts]||{ts:ts,models:{}};
    let mm=it.models||{};
    if(!Object.keys(mm).length && (it.total_tokens||it.requests||(it.cost&&it.cost.amount))) mm={unknown:it};
    buckets.push({it:it,mm:mm});
  }
  // 收集所有模型名
  const models=[];
  buckets.forEach(function(b){
    Object.keys(b.mm).forEach(function(mn){ if(models.indexOf(mn)<0)models.push(mn); });
  });
  // 计算 max 和 total
  let rawMax=0, total=0;
  buckets.forEach(function(b){
    let tot=0; Object.keys(b.mm).forEach(function(k){tot+=val(b.mm[k]);});
    if(tot>rawMax)rawMax=tot; total+=tot;
  });
  // Y轴 nice number：算出漂亮的 step，4个间隔5个刻度
  function niceScale(v){
    if(v<=0) v=1;
    const rawStep=v/4;
    const exp=Math.floor(Math.log10(rawStep));
    const base=Math.pow(10,exp);
    const f=rawStep/base;
    let nice;
    if(f<=1) nice=1;
    else if(f<=2) nice=2;
    else if(f<=2.5) nice=2.5;
    else if(f<=5) nice=5;
    else nice=10;
    const step=nice*base;
    return {step:step, max:step*4};
  }
  const scale=niceScale(rawMax);
  const yMax=scale.max, yStep=scale.step;
  const avg=buckets.length?total/buckets.length:0;
  const headTitle=costRange==="24h"?"小时趋势":(costRange==="7d"?"近7天趋势":"每日趋势");
  const headSub=costMetric==="requests"?"按请求数堆叠":(costMetric==="spend"?"按花费堆叠":"按 token 堆叠");
  // 生成柱子
  const cols=buckets.map(function(b){
    const mm=b.mm, keys=Object.keys(mm);
    let tot=0; keys.forEach(function(k){tot+=val(mm[k]);});
    const h=tot>0?Math.max(Math.round(tot/yMax*100),1.5):0;
    const segs=keys.map(function(k){
      const v=val(mm[k]);
      if(v<=0)return "";
      if(!colors[k])colors[k]=CC_PALETTE[Object.keys(colors).length%CC_PALETTE.length];
      const f=Math.max(Math.round(v/tot*100),1);
      return '<div class="cc-seg" style="flex:'+f+';background:'+colors[k]+'"></div>';
    }).join("");
    const rows=keys.map(function(k){
      const v=val(mm[k]);
      if(!colors[k])colors[k]=CC_PALETTE[Object.keys(colors).length%CC_PALETTE.length];
      return '<div class="cc-tip-r"><i style="background:'+colors[k]+'"></i>'+esc(k)+'<b>'+fmtSmart(v)+'</b></div>';
    }).join("");
    if(h===0) return '<div class="cc-col" style="height:2%"></div>';
    return '<div class="cc-col" style="height:'+h+'%">'
      + '<div class="cc-tip"><div class="cc-tip-t">'+esc(ccTimeLabel(b.it.ts,costRange))+'</div>'+rows+'</div>'
      + segs+'</div>';
  }).join("");
  // X轴标签间隔：24h每2桶(2h)、7d每4桶(1d)、30d每5桶(5d)
  const xStep=costRange==="24h"?2:(costRange==="7d"?4:5);
  const xaxis=buckets.map(function(b,idx){
    return '<span class="cc-x">'+(idx%xStep===0?esc(ccTimeLabel(b.it.ts,costRange)):"")+'</span>';
  }).join("");
  const legend=models.map(function(mn){
    if(!colors[mn])colors[mn]=CC_PALETTE[Object.keys(colors).length%CC_PALETTE.length];
    return '<span class="cc-lg"><i style="background:'+colors[mn]+'"></i>'+esc(mn)+'</span>';
  }).join("");
  // Y轴刻度：4,3,2,1,0 * step
  const ticks=[4,3,2,1,0].map(function(i){
    return '<span style="bottom:'+(i/4*100)+'%">'+fmtSmart(yStep*i)+'</span>';
  }).join("");
  const grid=[4,3,2,1,0].map(function(i){
    return '<i style="bottom:'+(i/4*100)+'%"></i>';
  }).join("");
  box.innerHTML='<div class="cc-head"><div class="cc-head-l"><span class="cc-head-t">'+headTitle+'</span>'
    + '<span class="cc-head-s">'+headSub+'</span></div>'
    + '<div class="cc-head-r"><span><b>'+fmtSmart(total)+'</b> 总量</span><span class="div">|</span>'
    + '<span><b>'+fmtSmart(avg)+'</b> 平均/桶</span></div></div>'
    + '<div class="cc-legend">'+legend+'</div>'
    + '<div class="cc-chart">'
    + '<div class="cc-yaxis">'+ticks+'</div>'
    + '<div class="cc-body"><div class="cc-grid">'+grid+'</div><div class="cc-plot">'+cols+'</div></div>'
    + '</div>'
    + '<div class="cc-xaxis">'+xaxis+'</div>';
}
async function loadCostSeries(){
  const cfg=COST_RANGES[costRange]||COST_RANGES["24h"];
  try{
    const series=await api("/api/tokens/series?bucket_ms="+cfg.ms);
    if(state.cost)state.cost[1]=series;
    renderCostChart(series,(state.cost[0]&&state.cost[0].cost&&state.cost[0].cost.currency)||"CNY");
  }catch(e){
    const box=$("#costChart"); if(box)box.innerHTML='<div class="hint">加载失败：'+esc(e.message)+'</div>';
  }
}
function renderCost(summary, series, turns){
  const t=summary||{};
  function hrClass(rate){ rate=Number(rate||0); return rate>=0.6?"hi":(rate>=0.3?"mid":"lo"); }
  function tShort(ts){ if(!ts)return "—"; const d=new Date(ts*1000); const p=n=>String(n).padStart(2,"0"); return p(d.getMonth()+1)+"-"+p(d.getDate())+" "+p(d.getHours())+":"+p(d.getMinutes()); }
  const cost=t.cost||{};
  const cur=cost.currency||"CNY";
  const src=cost.price_source||"";
  const est=cost.estimated!==false;
  $("#costSource").textContent=shortPath(t.source);
  $("#costErr").innerHTML = t.error
    ? '<div class="hint amber" style="margin-top:12px;">'+icon("alert")+' '+esc(t.error)+'</div>' : "";
  $("#costPriceNote").innerHTML='<div class="hint" style="margin-top:10px;">'+icon("info")
    +' 价目表：<span class="name">'+esc(cost.price_table||"（未标注）")+'</span> · 币种 <b>'
    +esc(cur)+' '+esc(costSymbol(cost))+'</b>'
    +(src?' · 按模型 '+esc(src):'')
    +(est?' · <b>含估算成分</b>（价目表里没这个模型，用兜底价猜的）':'')
    +'</div>';
  const hit=Number(t.cache_hit_tokens||0), miss=Number(t.cache_miss_tokens||0);
  $("#costKpis").innerHTML=[
    kpi("累计 token", fmtNum(t.total_tokens), "prompt "+fmtNum(t.prompt_tokens)+" / completion "+fmtNum(t.completion_tokens)),
    kpi("缓存命中率", pct(t.cache_hit_rate), "命中 "+fmtNum(hit)+" / 未命中 "+fmtNum(miss), "green"),
    kpi("请求数", fmtNum(t.requests), "会话数 "+fmtNum(t.sessions)),
    kpi("每轮平均 token", fmtNum(t.avg_tokens_per_turn), "按请求平均"),
    kpi("累计花费", costMoney(cost),
        cur+" "+(est?"· 含估算成分":"· 准数"), "amber"),
    kpi("缓存写入 token", fmtNum(t.cache_write_tokens), "cache_write_tokens"),
  ].join("");

  renderCostChart(series, cur);

  const brk=[];
  const bySession=t.by_session||{}, byModel=t.by_model||{}, byBackend=t.by_backend||{};
  [["会话",bySession],["模型",byModel],["后端",byBackend]].forEach(function(pair){
    const label=pair[0], table=pair[1]||{};
    const rows=Object.keys(table).map(function(k){return [k,table[k]];})
      .sort(function(a,b){return (b[1].total_tokens||0)-(a[1].total_tokens||0);});
    if(!rows.length) return;
    brk.push('<div class="sub-head" style="margin-top:14px;font-size:13px;">按'+label+'</div>'
      + '<div class="tbl-wrap"><table class="tbl cost-split"><colgroup>'
      + '<col style="width:20%"><col style="width:8%"><col style="width:14%">'
      + '<col style="width:12%"><col style="width:12%"><col style="width:12%"><col style="width:22%">'
      + '</colgroup><thead><tr><th>'+label+'</th><th class="num">请求</th>'
      + '<th class="num">token</th><th class="num">命中</th><th class="num">未命中</th><th class="num">命中率</th>'
      + '<th class="num">花费（'+esc(cur)+'）</th></tr></thead><tbody>'
      + rows.map(function(row){
          const k=row[0], v=row[1];
          return '<tr><td class="name">'+esc(k||"(无)")+'</td>'
            + '<td class="num">'+fmtNum(v.requests)+'</td><td class="num">'+fmtNum(v.total_tokens)+'</td>'
            + '<td class="num">'+fmtNum(v.cache_hit_tokens)+'</td><td class="num">'+fmtNum(v.cache_miss_tokens)+'</td>'
            + '<td class="num hitrate '+hrClass(v.cache_hit_rate)+'">'+pct(v.cache_hit_rate)+'</td>'
            + '<td class="num cost-money">'+costCell(v.cost)+'</td></tr>';
        }).join("")
      + '</tbody></table></div>');
  });
  $("#costBreak").innerHTML = brk.join("") || '<div class="hint">台账里还没有会话/模型拆分。</div>';

  const turnsItems=(turns&&turns.items)||[];
  const costTurnsPager=$("#costTurnsPager");
  if(!turnsItems.length){
    $("#costTurns").innerHTML='<tbody><tr><td style="color:var(--faint);">暂无逐请求明细</td></tr></tbody>';
    if(costTurnsPager)costTurnsPager.innerHTML="";
  }else{
    const rows=turnsItems.slice().sort(function(a,b){return (b.total_tokens||0)-(a.total_tokens||0);}).slice(0,100);
    const TURN_SIZE=10;
    $("#costTurns").innerHTML='<colgroup>'
      + '<col style="width:96px"><col style="width:108px"><col style="width:34px"><col style="width:72px">'
      + '<col style="width:150px"><col style="width:78px"><col style="width:78px"><col style="width:84px">'
      + '<col style="width:78px"><col style="width:78px"><col style="width:76px"><col style="width:96px">'
      + '</colgroup><thead><tr><th class="num">时间</th><th>session</th><th class="num">#</th><th>后端</th><th>模型</th>'
      + '<th class="num">prompt</th><th class="num">completion</th><th class="num">total</th>'
      + '<th class="num">命中</th><th class="num">未命中</th><th class="num">命中率</th><th class="num">花费（'+esc(cur)+'）</th></tr></thead>'
      + '<tbody>'+paged(rows,"costTurns",TURN_SIZE).map(function(r){
          return '<tr>'
            + '<td class="num">'+esc(tShort(r.ts))+'</td>'
            + '<td class="name">'+esc(r.session_id||"")+'</td>'
            + '<td class="num">'+r.request_index+'</td>'
            + '<td>'+esc(r.backend||"")+'</td><td>'+esc(r.model||"")+'</td>'
            + '<td class="num">'+fmtNum(r.prompt_tokens)+'</td>'
            + '<td class="num">'+fmtNum(r.completion_tokens)+'</td>'
            + '<td class="num">'+fmtNum(r.total_tokens)+'</td>'
            + '<td class="num">'+fmtNum(r.cache_hit_tokens)+'</td>'
            + '<td class="num">'+fmtNum(r.cache_miss_tokens)+'</td>'
            + '<td class="num hitrate '+hrClass(r.cache_hit_rate)+'">'+pct(r.cache_hit_rate)+'</td>'
            + '<td class="num cost-money">'+costCell(r.cost)+'</td></tr>';
        }).join("")+'</tbody>';
    if(costTurnsPager)costTurnsPager.innerHTML=pagerBar(rows.length,"costTurns",TURN_SIZE,true);
  }
}

/* ------------------------------------------------------------------
 * Tab 记忆：/api/memory（sidecar 的 JSON 卡片）+ /api/memory/sleep
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

/* 记忆分区（身份 / 价值 / 程序性）—— 常量块：不在 sidecar 的记忆卡里，
   但每一轮都进 system prompt，主人得能在控制台看见它们现在是什么。 */
function renderMemPartitions(data){
  const box=$("#memPartitions");
  if(!box)return;
  const blocks=(data&&data.blocks)||[];
  if(!blocks.length){ box.innerHTML=""; box.style.display="none"; return; }
  box.style.display="";
  let h='<div class="mp-sub">'+icon("brain")+' 记忆分区（常量块）'
    +'<span class="mini-tag">每轮进 system prompt · 改文件即生效</span>'
    +'<span class="mp-hint">'+esc((data&&data.note)||"")+'</span></div>';
  h+='<div class="mp-grid">';
  blocks.forEach(function(b){
    h+='<div class="mp-card"><div class="mp-head"><span class="mp-title">'+esc(b.header||b.key||"")
      +'</span><span class="mp-path">'+esc(b.path||"")+'</span></div>';
    const items=b.items||[];
    if(!items.length){ h+='<div class="mp-empty">（空）</div>'; }
    items.forEach(function(it){
      const text=it.text||it.content||it.task||"";
      let extra="";
      if(b.key==="values"&&it.level){
        extra='<span class="mp-lv">'+esc(it.level_label||it.level)+"</span>";
      }
      if(b.key==="procedures"){
        if(it.steps&&it.steps.length) extra+='<span class="mp-lv">'+esc(it.steps.join(" → "))+"</span>";
        if(it.success_rate!=null) extra+='<span class="mp-rate">成功率 '+Math.round(Number(it.success_rate)*100)+"%</span>";
      }
      h+='<div class="mp-item">'+extra+'<span class="mp-text">'+esc(text)+"</span></div>";
    });
    h+="</div>";
  });
  h+="</div>";
  box.innerHTML=h;
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

  const memGridPager=$("#memGridPager");
  if(!items.length){
    $("#memGrid").innerHTML=emptyBox(ok?"还没有记住任何事":"记忆 sidecar 没在跑","brain");
    if(memGridPager)memGridPager.innerHTML="";
    return;
  }
  const MEM_SIZE=12;
  $("#memGrid").innerHTML=paged(items,"memGrid",MEM_SIZE).map(function(m){
    return '<div class="mem">'
      + '<div class="content">'+esc(m.content)+'</div>'
      + '<div class="foot">'
      + '<span class="cat-tag">'+esc(m.category||m.kind||"未分类")+'</span>'
      + '<span>重要性 '+Number(m.importance||0).toFixed(2)+'</span>'
      + '<span>'+fmtTime(m.created_at)+'</span>'
      + '<span class="mini-tag '+(Number(m.hit_count||0)>0?"ok":"")+'">命中 '+(m.hit_count||0)+'</span>'
      + '</div></div>';
  }).join("");
  if(memGridPager)memGridPager.innerHTML=pagerBar(items.length,"memGrid",MEM_SIZE,true);
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
  let graphReason="";
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

  function _graphReason(data, graph){
    if(!(data&&data.ok)) return "记忆 sidecar 没在跑";
    if(!graph||!graph.ok) return (graph&&graph.reason)||"sidecar 没给出语义向量（向量索引没建起来或已关闭）";
    const n=(graph.nodes||[]).length;
    if(n<=1) return "只有 "+n+" 张记忆卡，画不出语义图";
    return "语义图不可用";
  }

  function build(data){
    const all=(data&&data.items)||[];
    const graph=(data&&data.graph)||null;
    const byId={};
    all.forEach(m=>{byId[m.id]=m;});

    const real=!!(graph&&graph.ok&&Array.isArray(graph.nodes)&&graph.nodes.length>1);
    /* 语义图拿不到时，**说清楚在看的是哪一种图** —— 分类图长得像语义图，
       但连线只表示「同类」，意思完全不一样。 */
    graphReason=real?"":_graphReason(data,graph);
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
      : '分类图：按 kind 摆位，连线只表示「同类」，不代表语义 —— '+graphReason;
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
        ? ("语义图 · "+nodes.length+" 个节点 · "+edgeLinks.length
           +(graphMode==="all"?" 条连线（全部）":" 条语义关联"))
        : ("分类图 · 语义图不可用："+graphReason);
      badge.style.display="";
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
  $("#decisionList").innerHTML = decs.length ? paged(decs,"decision",15).map(function(d){
    return '<div class="trace-row decision">'
      + '<div class="ts">'+esc(fmtTime(d.ts))+'</div>'
      + '<div class="body">'+esc(d.tool||d.action||"")+' · '+esc(d.decision||d.result||"")+' '
      + (d.rule?'<span class="mini-tag">'+esc(d.rule)+'</span>':"")
      + (d.thread_id?'<span class="mini-tag">'+esc(d.thread_id)+'</span>':"")+'</div>'
      + '<div class="arrow">'+esc(d.reason||"")+'</div></div>';
  }).join("")+pagerBar(decs.length,"decision",15,true) : '<div class="hint">还没有决策记录。</div>';

  const outs=c.outcomes||[];
  $("#outcomeList").innerHTML = outs.length ? paged(outs,"outcome",15).map(function(o){
    return '<div class="trace-row outcome">'
      + '<div class="ts">'+esc(fmtTime(o.ts))+'</div>'
      + '<div class="body">'+esc(o.tool||"")+' · Δ心情 '+Number(o.feeling_delta||0).toFixed(1)
      + ' · Δ饱食 '+Number(o.food_delta||0).toFixed(1)
      + ' · Δ好感 '+Number(o.favor_delta||0).toFixed(1)
      + ' · ΔEXP '+Number(o.exp_delta||0).toFixed(1)+'</div>'
      + '<div class="arrow">Lv.'+esc(o.level_before)+'→'+esc(o.level_after)+'</div></div>';
  }).join("")+pagerBar(outs.length,"outcome",15,true) : '<div class="hint">还没有结果记录。</div>';

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
    kpi("工具总数", fmtNum((inv&&inv.count)||0),
        fmtNum((inv&&inv.builtin_count)||0)+" 内置 + "
        +fmtNum((inv&&inv.mcp_count)||0)+" MCP", "violet"),
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

  const al=(inv&&inv.allowlist)||null;
  if(!al){
    $("#toolAllowlist").innerHTML='<div class="hint warn">'+icon("alert")
      +' 读不到执行期白名单（app/core/companion_config.yaml 的 tool_allowlist 段）。</div>';
  }else{
    const blocked=al.blocked||[], dead=al.dead_entries||[], deadP=al.dead_prefixes||[];
    let html='<div class="hint">'+(al.available
      ? ('来源：app/core/companion_config.yaml 的 tool_allowlist 段 · 精确名 '
         +fmtNum(al.exact_count)+' 个 · 前缀桶 '
         +((al.prefixes||[]).map(function(p){return esc(p);}).join("、")||"（无）"))
      : esc(al.note||"（白名单不可用）"))+'</div>';
    html+='<div class="grid3" style="margin-top:10px;">'
      +kpi("放行", fmtNum((al.allowed||[]).length), "执行期允许调用", "green")
      +kpi("拦截", fmtNum(blocked.length), "白名单没覆盖 → 执行期硬拒", blocked.length?"warn":"")
      +kpi("白名单死条目", fmtNum(dead.length+deadP.length), "登记了但注册表里没有", (dead.length||deadP.length)?"amber":"")
      +'</div>';
    if(blocked.length){
      html+='<div class="hint warn" style="margin-top:10px;">'+icon("alert")
        +' 这 '+blocked.length+' 个工具注册表里有、白名单没登记，执行期会被拒：'
        +'<span class="name">'+blocked.map(esc).join("、")+'</span></div>';
    }
    if(dead.length||deadP.length){
      html+='<div class="hint amber" style="margin-top:10px;">'+icon("info")
        +' 白名单里有、注册表里没有的死条目：<span class="name">'
        +dead.map(esc).join("、")
        +(deadP.length?("（前缀 "+deadP.map(esc).join("、")+"）"):"")
        +'</span></div>';
    }
    $("#toolAllowlist").innerHTML=html;
  }

  const toolTablePager=$("#toolTablePager");
  if(!tools.length){
    $("#toolTable").innerHTML='<tbody><tr><td style="color:var(--faint);">没有工具数据</td></tr></tbody>';
    if(toolTablePager)toolTablePager.innerHTML="";
  }else{
    const TOOL_SIZE=10;
    $("#toolTable").innerHTML='<thead><tr><th>工具</th><th>来源</th><th>MCP server</th><th>风险等级</th>'
      + '<th>解锁等级</th><th>白名单</th><th class="num">调用</th><th class="num">失败</th><th class="num">失败率</th></tr></thead>'
      + '<tbody>'+paged(tools,"toolTable",TOOL_SIZE).map(function(t){
          return '<tr>'
            + '<td class="name">'+esc(t.name)+'</td>'
            + '<td>'+(t.source==="mcp"?'<span class="mini-tag">MCP</span>':"内置")+'</td>'
            + '<td>'+esc(t.server||"—")+'</td>'
            + '<td>'+esc(t.risk_label||"—")+(t.risk_level!=null?' <span class="mini-tag">'+t.risk_level+'</span>':"")+'</td>'
            + '<td>'+(t.unlock_level==null?"—":esc(t.unlock_level))+'</td>'
            + '<td>'+(t.allowlisted===false
                ? '<span class="mini-tag warn">拦截</span>'
                : '<span class="mini-tag ok">放行</span>')+'</td>'
            + '<td class="num">'+fmtNum(t.calls)+'</td>'
            + '<td class="num">'+fmtNum(t.failures)+'</td>'
            + '<td class="num">'+pct(t.failure_rate)+'</td></tr>';
        }).join("")+'</tbody>';
    if(toolTablePager)toolTablePager.innerHTML=pagerBar(tools.length,"toolTable",TOOL_SIZE,true);
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
const SETTINGS = {schema:[], values:{}, appearance:{}, avatars:[], editing:""};

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

  renderL2dModels();
  renderL2dScenes();
  renderL2dActions();
}

/* ---- Live2D：模型 / 场景 / 动作 ---- */
function _l2d(){
  return SETTINGS.appearance||{};
}

function renderL2dModels(){
  const ap=_l2d();
  const models=ap.models||[];
  /* 当前模型只有一个真值：**渲染器实际加载的那个**（ap.currentModel）。
     桌宠进程那边还有第二个来源（config.yaml 里写的，重启才生效）——
     两者不一致时两个都摆出来，别让人以为已经换掉了。 */
  if(!models.length){
    $("#settingsModels").innerHTML='<div class="hint">没找到可用模型（assets/live2d 下没有 *.model3.json）。</div>';
    return;
  }
  const cur=ap.currentModel||"";
  const cfg=ap.configuredModel||"";
  const names={};
  models.forEach(function(m){names[m.path]=m.name||m.path;});
  let html='<div class="set-models">'+models.map(function(m){
    return '<button type="button" class="set-model'+((cur&&m.path===cur)?" on":"")+'" data-model="'+esc(m.path)+'">'+
      esc(m.name)+'<small>'+esc(m.path)+'</small></button>';
  }).join("")+'</div>';
  if(ap.modelMismatch){
    html+='<div class="hint warn" style="margin-top:8px;">'+icon("alert")+
      ' 当前实际跑着的是 '+esc(names[cur]||cur)+'（'+esc(cur)+'）；配置里写的是 '+
      esc(names[cfg]||cfg)+'（'+esc(cfg)+'）—— 改配置要重启桌宠才生效。</div>';
  }else if(cur){
    html+='<div class="hint" style="margin-top:8px;">当前模型：'+esc(names[cur]||cur)+
      '（'+esc(cur)+'）—— 渲染器实际加载的就是它。</div>';
  }
  $("#settingsModels").innerHTML=html+
    (ap.current!==undefined&&ap.current===false
      ? '<div class="hint" style="margin-top:8px;">换模型要重启桌宠才生效（每个模型各存一份场景配置）。</div>'
      : '');
}

function renderL2dScenes(){
  const ap=_l2d();
  const scenes=ap.scenes||[];
  if(!ap.available){
    $("#settingsScenes").innerHTML='<div class="hint">当前不是 Live2D 渲染器，没有场景可配。</div>';
    return;
  }
  const groups={};
  scenes.forEach(function(s){ (groups[s.group]=groups[s.group]||[]).push(s); });
  let html="";
  Object.keys(groups).forEach(function(g){
    html+='<div class="set-card" style="margin-bottom:12px;"><h4>'+esc(g)+'</h4>';
    groups[g].forEach(function(s){
      html+='<div class="set-scene"><div class="lft"><span class="nm">'+esc(s.title)+'</span>'+
        '<span class="sum">'+esc(s.summary||"")+'</span></div><div class="rgt">'+
        '<span class="tag'+(s.overridden?" over":"")+'">'+
          (s.overridden?"已自定义":(s.kind==="transient"?"一次性":"持续"))+'</span>'+
        '<button type="button" class="set-mini" data-scene-edit="'+esc(s.id)+'">配置</button>'+
        (s.overridden?'<button type="button" class="set-mini danger" data-scene-reset="'+esc(s.id)+'">恢复</button>':"")+
        '</div></div>';
      if(SETTINGS.editing==="scene:"+s.id){
        html+=_bundleEditor("scene:"+s.id,s.title+" · 外观",s.bundle,s.kind==="transient");
      }
    });
    html+='</div>';
  });
  $("#settingsScenes").innerHTML=html||'<div class="hint">没有场景。</div>';
}

function renderL2dActions(){
  const ap=_l2d();
  const actions=ap.actions||[];
  const modelActions=ap.modelActions||[];
  if(!ap.available){
    $("#settingsActions").innerHTML='<div class="hint">当前不是 Live2D 渲染器。</div>';
    return;
  }
  let html='<div class="set-card">';
  /* 模型自带/配置的动作：直接能播（profile 里定义的挥手/招手/点头…） */
  if(modelActions.length){
    html+='<div class="set-editor plain"><h5>模型动作</h5>';
    modelActions.forEach(function(a){
      html+='<div class="set-scene"><div class="lft"><span class="nm">'+esc(a.label||a.key)+'</span>'+
        '<span class="sum">'+esc(a.summary||"")+'</span></div><div class="rgt">'+
        '<span class="tag">'+esc(a.key)+'</span>'+
        '<button type="button" class="set-mini" data-model-action-play="'+esc(a.key)+'">播放</button>'+
        '</div></div>';
    });
    html+='</div>';
  }
  actions.forEach(function(a){
    html+='<div class="set-scene"><div class="lft"><span class="nm">'+esc(a.name)+'</span>'+
      '<span class="sum">'+esc(a.summary||"")+'</span></div><div class="rgt">'+
      '<span class="tag">'+esc(_hookLabel(a.hook))+'</span>'+
      '<button type="button" class="set-mini" data-action-play="'+esc(a.id)+'">播放</button>'+
      '<button type="button" class="set-mini" data-action-edit="'+esc(a.id)+'">编辑</button>'+
      '<button type="button" class="set-mini danger" data-action-del="'+esc(a.id)+'">删除</button>'+
      '</div></div>';
    if(SETTINGS.editing==="action:"+a.id){
      html+=_actionEditor(a);
    }
  });
  html+='<div class="row" style="margin-top:12px;">'+
    '<button type="button" class="btn" data-action-new="1">＋ 新建动作</button></div>';
  if(SETTINGS.editing==="action:new")html+=_actionEditor(null);
  html+='</div>';
  $("#settingsActions").innerHTML=html;
}

function _hookLabel(hook){
  if(!hook)return "仅手动";
  const hooks=_l2d().hooks||[];
  const hit=hooks.filter(function(h){return h.key===hook;})[0];
  return hit?hit.label:hook;
}

/* 外观组合编辑器（表情单选 / 发型单选 / 三类多选 / 时长）
   withActions=false 时只渲染字段，交给外层编辑器统一放按钮 */
function _bundleEditor(key,title,bundle,withHold,withActions){
  const ap=_l2d();
  const groups=ap.groups||[];
  const emotion=groups.filter(function(g){return g.kind==="emotion";})[0];
  const hair=groups.filter(function(g){return g.kind==="hairstyle";})[0];
  const toggles=groups.filter(function(g){return g.kind!=="emotion"&&g.kind!=="hairstyle";});
  bundle=bundle||{emotion:"",hairstyle:"",toggles:[],hold_ms:0};
  let html='<div class="set-editor'+(withActions===false?" plain":"")+'"><h5>'+esc(title)+'</h5>';
  if(emotion){
    html+='<div class="set-field"><span class="cap">表情</span><select data-bd="emotion" data-bk="'+esc(key)+'">'+
      '<option value=""'+(bundle.emotion?"":" selected")+'>不改变（保持当前）</option>'+
      '<option value="natural"'+(bundle.emotion==="natural"?" selected":"")+'>恢复自然（普通眼）</option>'+
      (emotion.items||[]).filter(function(it){return it[0]!=="__default__";}).map(function(it){
        return '<option value="'+esc(it[0])+'"'+(bundle.emotion===it[0]?" selected":"")+'>'+esc(it[1])+'</option>';
      }).join("")+'</select></div>';
  }
  if(hair){
    html+='<div class="set-field"><span class="cap">发型</span><select data-bd="hairstyle" data-bk="'+esc(key)+'">'+
      '<option value=""'+(bundle.hairstyle?"":" selected")+'>不改变（保持当前）</option>'+
      (hair.items||[]).map(function(it){
        const label=it[0]==="__default__"?"默认发型":it[1];
        return '<option value="'+esc(it[0])+'"'+(bundle.hairstyle===it[0]?" selected":"")+'>'+esc(label)+'</option>';
      }).join("")+'</select></div>';
  }
  toggles.forEach(function(g){
    html+='<div class="set-field"><span class="cap">'+esc(g.label)+'</span></div>'+
      '<div class="set-toggle-grid" data-tg="'+esc(g.id)+'" data-bk="'+esc(key)+'">'+
      (g.items||[]).map(function(it){
        const on=(bundle.toggles||[]).indexOf(it[0])>=0;
        return '<button type="button" class="set-chip'+(on?" on":"")+'" data-tg-item="'+esc(it[0])+'">'+esc(it[1])+'</button>';
      }).join("")+'</div>';
  });
  if(withHold){
    html+='<div class="set-field"><span class="cap">持续时长</span>'+
      '<input type="number" data-bd="hold_ms" data-bk="'+esc(key)+'" value="'+esc(bundle.hold_ms||1600)+'" step="200" min="400" max="15000"> 毫秒</div>';
  }
  if(withActions!==false){
    html+='<div class="row"><button type="button" class="btn" data-bd-preview="'+esc(key)+'">试穿预览</button>'+
      '<button type="button" class="btn" data-bd-save="'+esc(key)+'">保存</button>'+
      '<button type="button" class="btn" data-bd-cancel="1">取消</button></div>';
  }
  html+='</div>';
  return html;
}

function _actionEditor(a){
  const hooks=_l2d().hooks||[];
  const cur=a||{name:"",hook:"",bundle:{emotion:"",hairstyle:"",toggles:[],hold_ms:1600},hold_ms:1600};
  const key="action:"+(a?a.id:"new");
  let html='<div class="set-editor"><h5>'+(a?"编辑动作":"新建动作")+'</h5>';
  html+='<div class="set-field"><span class="cap">名称</span>'+
    '<input type="text" data-act="name" value="'+esc(cur.name)+'" placeholder="例如：打招呼"></div>';
  html+='<div class="set-field"><span class="cap">触发方式</span><select data-act="hook">'+
    hooks.map(function(h){
      return '<option value="'+esc(h.key)+'"'+(cur.hook===h.key?" selected":"")+'>'+esc(h.label)+'</option>';
    }).join("")+'</select></div>';
  html+=_bundleEditor(key,"动作外观",cur.bundle,true,false);
  html+='<div class="row"><button type="button" class="btn" data-bd-preview="'+esc(key)+'">试穿预览</button>'+
    '<button type="button" class="btn" data-act-save="'+esc(key)+'">保存</button>'+
    '<button type="button" class="btn" data-act-cancel="1">取消</button></div>';
  html+='</div>';
  return html;
}

/* 编辑器里的当前取值 */
function _bundleOf(key){
  const ap=_l2d();
  const out={emotion:"",hairstyle:"",toggles:[],hold_ms:0};
  const root=document.querySelector('[data-bk="'+key.replace(/"/g,'&quot;')+'"]');
  $$("[data-bk]").forEach(function(el){
    if(el.dataset.bk!==key)return;
    if(el.dataset.bd)out[el.dataset.bd]=(el.dataset.bd==="hold_ms")?Number(el.value||0):el.value;
  });
  $$("[data-tg][data-bk]").forEach(function(g){
    if(g.dataset.bk!==key)return;
    const picked=[];
    $$("[data-tg-item]",g).forEach(function(c){
      if(c.classList.contains("on"))picked.push(c.dataset.tgItem);
    });
    out.toggles=(out.toggles||[]).concat(picked);
  });
  return out;
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
    if(r&&r.ok){
      // 配置类动作做完就收起编辑器（场景/动作保存后不用再手动关）
      if(action.indexOf("live2d_scene_")===0||action.indexOf("live2d_action_")===0){
        SETTINGS.editing="";
      }
      await loadSettings(r.note?r.note:null);
    }
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
  state.health=h;
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
  const HF_SIZE=10;
  function fileIcon(name){
    const ext=name.split(".").pop().toLowerCase();
    if(ext==="jsonl"||ext==="json")return icon("file",13);
    if(ext==="db"||ext==="duckdb")return icon("database",13);
    if(ext==="log"||ext==="txt")return icon("doc",13);
    if(ext==="lock")return icon("lock",13);
    return icon("file",13);
  }
  $("#healthFiles").innerHTML='<thead><tr><th>文件</th><th class="num">大小</th><th class="num">占比</th></tr></thead>'
    + '<tbody>'+paged(files,"healthFiles",HF_SIZE).map(function(f){
      const total=files.reduce((a,x)=>a+(x.bytes||0),0)||1;
      const pct=(f.bytes||0)/total*100;
      const big=(f.bytes||0)>1048576; // >1MB
      return '<tr><td class="name">'+fileIcon(f.name)+' <span style="font-family:Consolas,monospace;font-size:12px;">'+esc(f.name)+'</span></td>'
        + '<td class="num" style="'+(big?'color:var(--amber);font-weight:700;':'')+'">'+fmtBytes(f.bytes)+'</td>'
        + '<td class="num" style="color:var(--faint);font-size:11px;">'+pct.toFixed(1)+'%</td></tr>';
    }).join("")+'</tbody>';
  const hfp=$("#healthFilesPager");
  if(hfp)hfp.innerHTML=pagerBar(files.length,"healthFiles",HF_SIZE,true);

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
function skeletonBlock(n){
  let h="";
  for(let i=0;i<n;i++){
    h+='<div class="skeleton" style="height:14px;margin:9px 0;width:'+(70+((i*13)%25))+'%"></div>';
  }
  return h;
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
 * Tab 语音：/api/voice（本机音色 / 情绪参考 / 服务状态）
 * ------------------------------------------------------------------ */
function renderVoice(v){
  if(!v){ $("#voiceErr").innerHTML='<div class="hint warn">语音状态读取失败。</div>'; return; }
  const ok=(v.ok!==false);
  $("#voiceTag").textContent = ok ? "已连上" : "不可达";
  $("#voiceErr").innerHTML = v.error
    ? '<div class="hint warn" style="margin-bottom:10px;">'+icon("alert")+' '+esc(v.error)+'</div>'
    : "";
  const tts=v.tts||{}, rvc=v.rvc||{};
  $("#voiceKpis").innerHTML=[
    kpi("TTS 合成服务", tts.up?"在线":"离线", esc(tts.url||""), tts.up?"green":"warn"),
    kpi("音色模型", fmtNum(v.model_count||0), "RVC .pth 权重"),
    kpi("情绪参考", fmtNum(v.emotion_count||0), "本机情绪库"),
    kpi("RVC 解释器", rvc.python_ok?"就绪":"不在本机", esc(shortPath(rvc.python||"")),
        rvc.python_ok?"green":"warn"),
  ].join("");

  const models=v.models||[];
  const VM_SIZE=10;
  $("#voiceModels").innerHTML = models.length
    ? "<tr><th>音色文件</th><th>大小</th><th>路径</th></tr>"
      + paged(models,"voiceModels",VM_SIZE).map(function(m){
          return "<tr><td>"+esc(m.name)+"</td><td>"+esc(m.size_kb||"")+"</td>"
            + '<td style="font-family:Consolas,monospace;">'+esc(m.path||"")+"</td></tr>";
        }).join("")
    : '<tr><td colspan="3"><div class="hint warn">本机没有音色模型（不会自动下载）。</div></td></tr>';
  const vmp=$("#voiceModelsPager");
  if(vmp)vmp.innerHTML=pagerBar(models.length,"voiceModels",VM_SIZE,true);

  const emo=v.emotions||[];
  $("#voiceEmotions").innerHTML = emo.length
    ? '<div class="kv">'+emo.map(function(e){
        return '<span class="pair"><span class="k">'+esc(e.label||e.name)+'</span>'
          + '<span class="v">'+esc(e.name)+'</span></span>';
      }).join("")+"</div>"
    : '<div class="hint warn">本机没有情绪参考库（'+esc(v.emotion_dir||"")+'）。</div>';

  const rows=[
    ["TTS 合成服务", tts.up?"在线":"离线", tts.detail||""],
    ["RVC 解释器", rvc.python_ok?"就绪":"不在本机", rvc.python||""],
    ["RVC 唱歌脚本", rvc.sing_script_ok?"就绪":"不在本机", rvc.sing_script||""],
    ["本机音色权重", rvc.weight_ok?"就绪":"音色不在本地", rvc.weight||""],
    ["翻唱下载脚本", rvc.fetch_script_ok?"就绪":"不在本机", rvc.fetch_script||""],
    ["人声分离模型", rvc.separate_model_ok?"就绪":"不在本地（不会自动下载）",
      rvc.separate_model||rvc.separate_model_name||""],
    ["语音产物目录", "—", v.out_dir||""],
    ["搜索后端", "—", v.search_backend||""],
  ];
  $("#voiceServices").innerHTML='<div class="kv">'+rows.map(function(r){
      return '<span class="pair"><span class="k">'+esc(r[0])+'</span>'
        + '<span class="v">'+esc(r[1])+" · "+esc(r[2])+'</span></span>';
    }).join("")+"</div>";
}

/* ------------------------------------------------------------------
 * 各 Tab 的加载器
 * ------------------------------------------------------------------ */
const TAB_LOADERS = {
  async cost(){
    const cfg=COST_RANGES[costRange]||COST_RANGES["24h"];
    const res=await Promise.all([
      api("/api/tokens/summary"),
      api("/api/tokens/series?bucket_ms="+cfg.ms),
      api("/api/tokens/turns?limit=200"),
    ]);
    state.cost=res;
    renderCost(res[0],res[1],res[2]);
  },
  async memory(){
    const data=await api("/api/memory");
    state.memory=data;
    renderMemory(data);
    MemNet.setData(data);
    if(memMode==="net")MemNet.start();
    await loadSleep();
    /* 记忆分区单独取：它不在 sidecar 里，取不到也不能拖垮整个 Tab */
    try{ renderMemPartitions(await api("/api/memory/partitions")); }
    catch(_){ renderMemPartitions(null); }
  },
  async decision(){
    state.companion = await api("/api/companion?decision_limit=200&outcome_limit=200");
    renderDecision(state.companion);
  },
  async tools(){
    const res=await Promise.all([api("/api/tools"),api("/api/reconciliation")]);
    state.tools=res;
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
  async voice(){
    const v=await api("/api/voice");
    state.voice=v;
    renderVoice(v);
  },
  async health(){
    const h=await api("/api/health");
    state.health=h;
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
      err.innerHTML='加载失败：'+esc(e.message)
        +' <button type="button" class="btn" onclick="retryTab(&#39;'+name+'&#39;)">重试</button>';
      box.prepend(err);
    }
  }
}
function retryTab(name){
  const box=$("#view-"+name);
  if(box){
    $$("#view-"+name+" .hint.warn").forEach(function(h){h.remove();});
  }
  loadTab(name);
}

/* 分页 key → 重渲染函数（全部基于已缓存数据，不重新请求） */
const PG_RENDER={
  runs:()=>renderRunList(),
  decision:()=>{if(state.companion)renderDecision(state.companion);},
  outcome:()=>{if(state.companion)renderDecision(state.companion);},
  memGrid:()=>{if(state.memory)renderMemory(state.memory);},
  toolTable:()=>{if(state.tools)renderTools(state.tools[0],state.tools[1]);},
  costTurns:()=>{if(state.cost)renderCost(state.cost[0],state.cost[1],state.cost[2]);},
  healthFiles:()=>{if(state.health)renderHealth(state.health);},
  voiceModels:()=>{if(state.voice)renderVoice(state.voice);},
};

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
    vmSyncIndicator($("#view-"+t.dataset.view));
    state.tab=t.dataset.view;
    // 同步顶栏面包屑（分组 + 页面名）
    var _groups = {runs:"数据总览",cost:"数据总览",memory:"数据总览",decision:"数据总览",
                   tools:"系统",voice:"系统",config:"系统",state:"系统",health:"系统"};
    var _names = {runs:"运行",cost:"成本",memory:"记忆",decision:"决策",
                  tools:"工具",voice:"语音",config:"配置",state:"状态",health:"健康与维护"};
    var _titles = {runs:"运行总览",cost:"成本分析",memory:"长期记忆",decision:"决策日志",
                   tools:"工具清单",voice:"本机语音",config:"生效配置",state:"桌宠状态",health:"健康与维护"};
    var _descs = {
      runs:"会话记录、成功率与工具调用概览",
      cost:"Token 用量、缓存命中与花费（人民币计价）",
      memory:"记忆卡片、语义图与 sidecar 状态",
      decision:"工具决策、风险拦截与养成结果",
      tools:"内置与 MCP 工具、风险等级、解锁与执行期白名单",
      voice:"本机音色权重、情绪参考库与 TTS / RVC 服务状态",
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
function clearRunFilter(){
  state.filter="all"; state.kw="";
  const s=$("#search"); if(s)s.value="";
  $$("#filters .chip").forEach(function(x){x.classList.toggle("active",x.dataset.f==="all");});
  delete pgState.runs; renderRunList();
}
document.addEventListener("keydown",function(e){
  const rv=document.getElementById("view-runs");
  if(!rv || !rv.classList.contains("active"))return;
  const ae=document.activeElement;
  const inList = ae===document.getElementById("runList") || (ae && ae.closest && ae.closest("#runList"));
  const onBody = ae===document.body || ae===document.documentElement;
  if(!inList && !onBody)return;
  const tag=(e.target&&e.target.tagName)||"";
  const typing=tag==="INPUT"||tag==="TEXTAREA"||tag==="SELECT"||(e.target&&e.target.isContentEditable);
  if(e.key==="Escape"){
    const s=$("#search");
    if(s && s.value){ s.value=""; state.kw=""; delete pgState.runs; renderRunList(); s.focus(); e.preventDefault(); }
    return;
  }
  if(typing)return;
  if(e.key==="ArrowDown"||e.key==="ArrowUp"){
    e.preventDefault();
    const items=$$("#runList .run-item");
    if(!items.length)return;
    let idx=items.findIndex(function(x){return x.dataset.id===String(state.selectedId);});
    if(idx<0)idx=(e.key==="ArrowDown"?-1:0);
    idx+=(e.key==="ArrowDown"?1:-1);
    if(idx<0)idx=0;
    if(idx>=items.length)idx=items.length-1;
    selectRun(items[idx].dataset.id);
  }else if(e.key==="Enter" && state.selectedId){
    selectRun(state.selectedId);
  }
});
$("#filters").addEventListener("click",function(e){
  const c=e.target.closest(".chip"); if(!c)return;
  $$("#filters .chip").forEach(function(x){x.classList.remove("active");});
  c.classList.add("active"); state.filter=c.dataset.f; delete pgState.runs; renderRunList();
});
$("#search").addEventListener("input",function(e){state.kw=e.target.value.trim().toLowerCase();delete pgState.runs;renderRunList();});
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
  const el=e.target.closest?e.target.closest(
    "[data-l2d],[data-l2d-reset],[data-avatar],[data-model],"+
    "[data-scene-edit],[data-scene-reset],[data-action-play],[data-action-edit],"+
    "[data-action-del],[data-action-new],[data-tg-item],[data-bd-preview],"+
    "[data-bd-save],[data-bd-cancel],[data-act-save],[data-act-cancel],"+
    "[data-model-action-play]"):null;
  if(!el)return;
  const d=el.dataset;
  if(d.modelActionPlay){ settingsAction("live2d_model_action_play",{key:d.modelActionPlay}); return; }
  if(d.l2dReset){ settingsAction("live2d_reset"); return; }
  if(d.l2d){ settingsAction("live2d_activate",{group_id:d.l2d,item_id:d.l2dItem}); return; }
  if(d.avatar){ settingsAction("set_avatar",{path:d.avatar}); return; }
  if(d.model){
    if(!window.confirm("换到模型「"+d.model+"」？\n\n要重启桌宠才生效，"+
      "每个模型各存一份场景配置。"))return;
    settingsAction("live2d_switch_model",{path:d.model});
    return;
  }
  if(d.sceneEdit){ SETTINGS.editing="scene:"+d.sceneEdit; renderL2dScenes(); return; }
  if(d.sceneReset){ settingsAction("live2d_scene_reset",{scene_id:d.sceneReset}); return; }
  if(d.actionPlay){ settingsAction("live2d_action_play",{id:d.actionPlay}); return; }
  if(d.actionEdit){ SETTINGS.editing="action:"+d.actionEdit; renderL2dActions(); return; }
  if(d.actionDel){
    if(!window.confirm("删掉这个自定义动作？"))return;
    settingsAction("live2d_action_delete",{id:d.actionDel});
    return;
  }
  if(d.actionNew){ SETTINGS.editing="action:new"; renderL2dActions(); return; }
  if(d.tgItem){
    el.classList.toggle("on");
    return;
  }
  if(d.bdPreview){
    settingsAction("live2d_preview",{bundle:_bundleOf(d.bdPreview)});
    return;
  }
  if(d.bdSave){
    const key=d.bdSave;
    if(key.indexOf("scene:")===0){
      settingsAction("live2d_scene_save",{
        scene_id:key.slice(6), bundle:_bundleOf(key)});
    }else{
      const id=key.slice(6);
      const act=(SETTINGS.appearance.actions||[]).filter(function(a){return a.id===id;})[0];
      const nameEl=document.querySelector('[data-act="name"]');
      const hookEl=document.querySelector('[data-act="hook"]');
      settingsAction("live2d_action_save",{
        id:id==="new"?"":id,
        name:nameEl?nameEl.value:(act?act.name:""),
        hook:hookEl?hookEl.value:"",
        bundle:_bundleOf(key),
        hold_ms:(_bundleOf(key).hold_ms||1600)});
    }
    return;
  }
  if(d.bdCancel||d.actCancel){ SETTINGS.editing=""; renderL2dScenes(); renderL2dActions(); return; }
  if(d.actSave){
    const key=d.actSave;
    const id=key.slice(6);
    const nameEl=document.querySelector('[data-act="name"]');
    const hookEl=document.querySelector('[data-act="hook"]');
    const bundle=_bundleOf(key);
    settingsAction("live2d_action_save",{
      id:id==="new"?"":id,
      name:nameEl?nameEl.value:"",
      hook:hookEl?hookEl.value:"",
      bundle:bundle,
      hold_ms:(bundle.hold_ms||1600)});
    return;
  }
});

/* 记忆：神经网络 / 列表 切换 */
$$("#memMode .vm-btn").forEach(function(b){
  b.addEventListener("click",function(){
    if(b.classList.contains("active"))return;
    $$("#memMode .vm-btn").forEach(function(x){x.classList.remove("active");});
    b.classList.add("active");
    memMode=b.dataset.mode;
    if(memMode==="net"){ MemNet.start(); }
    else{ MemNet.stop(); $("#memNodeDetail").style.display="none"; }
  });
});

/* 顶部分栏切换（统一）：vmGo + 全局点击 */
function vmGo(viewId,pane){
  const view=document.getElementById("view-"+viewId);
  if(!view)return;
  const bar=view.querySelector(".view-mode");
  if(bar)$$(".vm-btn",bar).forEach(function(b){b.classList.toggle("active",b.dataset.pane===pane);});
  $$(".vm-pane",view).forEach(function(p){p.classList.toggle("active",p.dataset.pane===pane);});
}
document.addEventListener("click",function(e){
  const b=e.target.closest(".vm-btn");
  if(!b)return;
  const bar=b.closest(".view-mode");
  if(!bar)return;
  const view=bar.closest(".view");
  if(!view)return;
  const pane=b.dataset.pane;
  if(!pane)return;
  $$(".vm-btn",bar).forEach(function(x){x.classList.toggle("active",x===b);});
  $$(".vm-pane",view).forEach(function(p){p.classList.toggle("active",p.dataset.pane===pane);});
  vmSyncIndicator(view);
});
document.addEventListener("click",function(e){
  const b=e.target.closest(".cost-btn");
  if(!b)return;
  const panel=b.closest(".panel");
  if(!panel)return;
  if(b.dataset.range){
    costRange=b.dataset.range;
    $$(".cost-btn[data-range]",panel).forEach(function(x){x.classList.toggle("active",x===b);});
    loadCostSeries();
    return;
  }
  const p=b.dataset.cost;
  costMetric=p;
  $$(".cost-btn[data-cost]",panel).forEach(function(x){x.classList.toggle("active",x===b);});
  if(state.cost)renderCostChart(state.cost[1],(state.cost[0]&&state.cost[0].cost&&state.cost[0].cost.currency)||"CNY");
});
function vmSyncIndicator(view){
  if(!view)return;
  requestAnimationFrame(function(){
    const bar=view.querySelector(".view-mode");
    if(!bar)return;
    const ind=bar.querySelector(".vm-indicator");
    const act=bar.querySelector(".vm-btn.active");
    if(!ind||!act)return;
    ind.style.left=act.offsetLeft+"px";
    ind.style.width=act.offsetWidth+"px";
  });
}
window.addEventListener("resize",function(){
  const av=document.querySelector(".view.active");
  if(av)vmSyncIndicator(av);
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

# 本项目实际调的是 MiniMax **国产平台**（https://api.minimaxi.com/v1），
# 账单按**元**结算 —— 所以面板的成本 Tab 一律用人民币口径。
# 美元表（DEFAULT_PRICE_TABLE，国际站）留在 app/brain/model_prices.py 里做对照，
# 面板**不混币种**：一个总数里只可能出现一种货币。
COST_CURRENCY = "CNY"
COST_SYMBOL = "¥"
COST_TABLE_LABEL = "MiniMax 国产平台官方价（api.minimaxi.com）"

# 占位价目表（每百万 token，**人民币**）—— 只在 app/brain/model_prices.py
# 拿不到时兜底（那时金额一律标 estimated=true，并把用到的单价原样吐出来，
# 方便人纠正）。数字取自国产平台 MiniMax-M3 标准档官方价。
_PLACEHOLDER_PRICE_CNY_PER_MILLION = {
    "input": 2.10,
    "output": 8.40,
    "cache_hit": 0.42,
    "cache_write": 2.10,
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


def _voice_payload(timeout: float = 4.0) -> dict:
    """本机语音资产盘点（音色权重 / 情绪参考 / 服务状态）。

    **复用语音 MCP server 的同一份盘点逻辑**
    （app/mcp/voice_server.py::voice_status_payload）——
    面板和工具各写一份的话，迟早会说两套（主人看到的和模型看到的对不上）。

    语音模块/服务不可达时**如实说不可达**（ok=false + 说明），
    永远返回 200，**绝不 500**。
    """
    empty = {"ok": False, "error": "", "models": [], "model_count": 0,
             "emotions": [], "emotion_count": 0, "tts": {}, "rvc": {}}
    try:
        from app.mcp.voice_server import voice_status_payload
    except Exception as e:  # noqa: BLE001 - 面板永远不许因为语音模块挂掉
        out = dict(empty)
        out["error"] = "语音模块不可用（%s）" % (str(e) or type(e).__name__)
        return out
    try:
        data = voice_status_payload(timeout=timeout) or {}
    except Exception as e:  # noqa: BLE001
        out = dict(empty)
        out["error"] = "语音服务没响应（%s）" % (str(e) or type(e).__name__)
        return out
    if not isinstance(data, dict) or not data:
        out = dict(empty)
        out["error"] = "语音服务没响应（盘点结果是空的）"
        return out
    data["ok"] = True
    data.setdefault("error", "")
    return data


def _memory_sidecar(base_url: Optional[str] = None, timeout: float = 2.0):
    """指向记忆 sidecar 的客户端（面板是独立进程 —— 只能走 HTTP）。"""
    from app.brain.memory_client import MemoryClient
    return MemoryClient(base_url or "", timeout=timeout)


def _pet_control(base_url: Optional[str] = None, timeout: float = 8.0):
    """指向桌宠进程控制端口的客户端（设置都走它）。"""
    from app.web.pet_control_client import PetControlClient
    return PetControlClient(base_url or "", timeout=timeout)


def _live2d_model_state(appearance: dict) -> dict:
    """Live2D「当前模型」只有一个真值：**渲染器实际加载的那个**。

    桌宠进程那边有两个来源：
      - 渲染器的 model_dir —— 真正跑着的模型（权威）；
      - config.yaml 的 pet.live2d.model_dir —— 切换模型时写进去的
        （要重启桌宠才生效）。
    两者不一致时（换了模型还没重启），面板必须**两个都说出来** ——
    只标一个「当前」会让人以为已经换掉了。

    桌宠进程那边（app/ui/ui_controller.py）两个来源本来就各算各的：
    _live2d_models() 用 cfg 标 current，_control_appearance() 报渲染器的
    model_dir。面板不替它们选边，只把差异如实摊开。
    """
    ap = dict(appearance or {})
    renderer_model = str(ap.get("model") or "")
    configured_model = ""
    for m in ap.get("models") or []:
        if isinstance(m, dict) and m.get("current"):
            configured_model = str(m.get("path") or "")
            break
    ap["currentModel"] = renderer_model
    ap["configuredModel"] = configured_model
    ap["modelMismatch"] = bool(renderer_model and configured_model
                                and renderer_model != configured_model)
    return ap


def _memory_payload(base_url: Optional[str] = None,
                    with_graph: bool = True) -> dict:
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

    graph = {"ok": False, "nodes": [], "edges": [],
             "reason": "记忆 sidecar 没在跑"}
    if reachable and with_graph:
        try:
            # 默认就把所有够格的连线都画出来；选中节点时前端只留它那几条
            graph = client.graph(limit=MEM_NET_MAX_NODES, top_k=4,
                                 mode="all", timeout=15.0)
        except Exception:  # noqa: BLE001
            graph = {"ok": False, "nodes": [], "edges": [],
                     "reason": "sidecar 没给回语义图"}
        if not graph.get("reason"):
            graph["reason"] = ""
    elif reachable:
        graph["reason"] = "（这次没要语义图）"

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

    拿不到就返回 None —— 面板退回占位价目表，金额一律标 estimated=true。
    落地之后：只要它提供 estimate_cost(buckets, model=..., table=...) -> dict，
    面板会自动改用它的结果（这里不用再改）。
    """
    try:
        from app.brain import model_prices
    except Exception:  # noqa: BLE001
        return None
    fn = getattr(model_prices, "estimate_cost", None)
    return fn if callable(fn) else None


def _price_table():
    """本项目实际用的价目表：**人民币**（MiniMax 国产平台）。

    app/brain/model_prices.py 里现在有两张表：
      - DEFAULT_PRICE_TABLE：美元（国际站）
      - CNY_PRICE_TABLE：人民币（国产平台）—— 本项目实际调的就是它
    一张表一个币种，面板只报人民币，绝不把两种币种加进同一个总数。
    """
    try:
        from app.brain import model_prices
    except Exception:  # noqa: BLE001
        return None
    tbl = getattr(model_prices, "CNY_PRICE_TABLE", None)
    return tbl if isinstance(tbl, dict) and tbl else None


def _cost_at_ms(ts) -> float:
    """台账时间戳 -> 毫秒（台账存的是秒；有些行是毫秒）。"""
    try:
        v = float(ts or 0)
    except (TypeError, ValueError):
        return 0.0
    if v <= 0:
        return 0.0
    return v * 1000.0 if v < 1e11 else v


def _estimate_cost(buckets: dict, model: Optional[str] = None,
                   at: Optional[float] = None) -> dict:
    """按价目表算钱（**人民币**）。

    估算标记是诚实的：
      - 传了 model 且价目表里查得到 -> estimated=False（这是准数）；
      - 模型不在表里 / 压根没传模型 -> estimated=True
        （拿兜底价去猜，天然是估的，不能当账单）。

    以前这里只调 estimate_cost(buckets) 不带模型名 -> 面板**每一行**都标
    「估算」，连价格已知的 MiniMax-M3 也是 —— 那是假的诚实。
    """
    price = _PLACEHOLDER_PRICE_CNY_PER_MILLION
    hit = _int(buckets.get("cache_hit_tokens"))
    miss = _int(buckets.get("cache_miss_tokens"))
    write = _int(buckets.get("cache_write_tokens"))
    prompt = _int(buckets.get("prompt_tokens"))
    completion = _int(buckets.get("completion_tokens"))
    uncached = max(0, prompt - hit - miss)
    cny = (hit * price["cache_hit"] + miss * price["input"]
           + uncached * price["input"] + write * price["cache_write"]
           + completion * price["output"]) / 1000000.0
    out = {
        "amount": round(cny, 8),
        "currency": COST_CURRENCY,
        "symbol": COST_SYMBOL,
        "estimated": True,
        "price_source": "placeholder",
        "price_table": COST_TABLE_LABEL,
        "rates_per_million": dict(price),
        "note": ("真实价目表（app/brain/model_prices.py）没加载上，"
                 "这里的金额是占位价目表算出的估算值"),
    }
    real = _load_model_prices()
    if real is not None:
        got = None
        kwargs = {"model": model or None}
        if at:
            kwargs["at"] = float(at)
        tbl = _price_table()
        if tbl is not None:
            kwargs["table"] = tbl
        try:
            got = real(dict(buckets or {}), **kwargs)
        except TypeError:
            # 价目表模块的签名变了（老版本没有 model/table 参数）—— 退回最老口径
            try:
                got = real(dict(buckets or {}))
            except Exception:  # noqa: BLE001
                got = None
        except Exception:  # noqa: BLE001
            got = None
        if isinstance(got, dict) and got.get("amount") is not None:
            out.update({k: v for k, v in got.items() if k != "note"})
            out["amount"] = round(float(got.get("amount") or 0.0), 8)
            out["currency"] = str(got.get("currency") or COST_CURRENCY)
            out["symbol"] = str(got.get("symbol") or COST_SYMBOL)
            out["estimated"] = bool(got.get("estimated"))
            out["price_source"] = got.get("price_source") or "model_prices"
            out["price_table"] = COST_TABLE_LABEL
            out["note"] = "金额来自 app/brain/model_prices.py"
    if model:
        out["model"] = str(model)
    return out


def _new_cost_bucket() -> dict:
    """一个待累加的金额桶（金额 + 币种 + 是否含估算成分）。"""
    return {"amount": 0.0, "currency": "", "symbol": "",
            "estimated": False, "price_source": "", "price_table": COST_TABLE_LABEL,
            "rates_per_million": {}, "tier": "", "models": []}


def _cost_accumulate(dst: dict, src: dict) -> dict:
    """把一行的金额并进某个汇总（**只允许同币种相加**）。"""
    if not isinstance(src, dict) or src.get("amount") is None:
        return dst
    cur = str(src.get("currency") or "")
    if cur and dst.get("currency") and cur != dst["currency"]:
        # 币种对不上 —— 宁可少算也不能把两种币种混进一个总数
        dst["estimated"] = True
        dst["note"] = "有行的币种与汇总不一致，已排除（不混币种）"
        return dst
    dst["amount"] = round(float(dst.get("amount") or 0.0)
                          + float(src.get("amount") or 0.0), 8)
    if cur:
        dst["currency"] = cur
    if src.get("symbol"):
        dst["symbol"] = src["symbol"]
    if src.get("estimated"):
        dst["estimated"] = True
    model = str(src.get("model") or "")
    if model and model not in dst["models"]:
        dst["models"].append(model)
    if src.get("rates_per_million"):
        dst["rates_per_million"] = src["rates_per_million"]
    if src.get("tier"):
        dst["tier"] = src["tier"]
    return dst


def _cost_finish(dst: dict) -> dict:
    """汇总完成：把用到的模型清单写进 price_source（面板要能看出按谁的价格算的）。"""
    models = dst.get("models") or []
    dst["price_source"] = ("、".join(models) if models
                           else (dst.get("price_source") or "unknown"))
    dst.pop("models", None)
    return dst


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
    total_cost = _new_cost_bucket()
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
        # 金额**逐行**算：每一行都知道自己是哪个模型 -> 估算标记才诚实
        # （汇总之后再算就只能拿兜底价猜，整张表都会变成「估算」）。
        row_cost = _estimate_cost(buckets, model=model,
                                  at=_cost_at_ms(row.get("ts")))
        for table, key in ((by_model, model), (by_backend, backend),
                           (by_session, sid)):
            rec = table.setdefault(key, {
                "requests": 0, "total_tokens": 0, "prompt_tokens": 0,
                "completion_tokens": 0, "cache_hit_tokens": 0,
                "cache_miss_tokens": 0, "cache_hit_rate": 0.0, "last_ts": 0.0,
                "_cost": _new_cost_bucket(),
            })
            rec["requests"] += n
            for b, v in buckets.items():
                rec[b] = rec.get(b, 0) + v
            rec["last_ts"] = max(_float(rec.get("last_ts")),
                                 _float(row.get("ts")))
            _cost_accumulate(rec["_cost"], row_cost)
        _cost_accumulate(total_cost, row_cost)
    for rec in (list(by_model.values()) + list(by_backend.values())
                + list(by_session.values())):
        rec["cache_hit_rate"] = _hit_rate(_int(rec.get("cache_hit_tokens")),
                                          _int(rec.get("cache_miss_tokens")))
        rec["cost"] = _cost_finish(rec.pop("_cost"))
    totals["cache_hit_rate"] = _hit_rate(totals["cache_hit_tokens"],
                                         totals["cache_miss_tokens"])
    totals["requests"] = requests
    totals["sessions"] = len(sessions)
    totals["avg_tokens_per_turn"] = (round(totals["total_tokens"] / requests, 2)
                                      if requests else 0.0)
    totals["cost"] = _cost_finish(total_cost)
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
        key = int(ts * 1000 // ms) * (ms // 1000)  # ts是秒、ms是毫秒，先统一到毫秒再对齐，结果存回秒
        slot = buckets.setdefault(key, {
            "ts": key, "requests": 0, "prompt_tokens": 0,
            "completion_tokens": 0, "total_tokens": 0,
            "cache_hit_tokens": 0, "cache_miss_tokens": 0,
            "cache_write_tokens": 0, "_cost": _new_cost_bucket(),
            "_models": {},
        })
        slot["requests"] += 1
        for b in _LEDGER_BUCKETS:
            slot[b] += _int(row.get(b))
        model = str(row.get("model") or "unknown")
        # 一个时间桶里的行可能来自不同模型 -> 逐行算再累加
        _cost_accumulate(slot["_cost"], _estimate_cost(
            {b: _int(row.get(b)) for b in _LEDGER_BUCKETS},
            model=model,
            at=_cost_at_ms(row.get("ts"))))
        m = slot["_models"].setdefault(model, {
            "requests": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "total_tokens": 0, "cache_hit_tokens": 0,
            "cache_miss_tokens": 0, "cache_write_tokens": 0,
            "_cost": _new_cost_bucket(),
        })
        m["requests"] += 1
        for b in _LEDGER_BUCKETS:
            m[b] += _int(row.get(b))
        _cost_accumulate(m["_cost"], _estimate_cost(
            {b: _int(row.get(b)) for b in _LEDGER_BUCKETS},
            model=model,
            at=_cost_at_ms(row.get("ts"))))
    items = []
    for key in sorted(buckets):
        slot = buckets[key]
        slot["cache_hit_rate"] = _hit_rate(slot["cache_hit_tokens"],
                                           slot["cache_miss_tokens"])
        slot["cost"] = _cost_finish(slot.pop("_cost"))
        models = {}
        for mn, mv in slot.pop("_models").items():
            mv["cache_hit_rate"] = _hit_rate(mv["cache_hit_tokens"],
                                             mv["cache_miss_tokens"])
            mv["cost"] = _cost_finish(mv.pop("_cost"))
            models[mn] = mv
        slot["models"] = models
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
        # 逐请求：这一行知道自己是哪个模型 -> 估算标记才诚实
        item["cost"] = _estimate_cost(item, model=item["model"] or None,
                                      at=_cost_at_ms(item["ts"]))
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
    """工具清单 + 风险等级 + 解锁等级 + 调用统计 + 对账结果。

    数字**不写死**：实际数量来自下面的盘点（数据源是 check_tool_docs.py 的对账结果），
    这里只给人看一句话 —— 工具集变了，写死的数字就会过期。"""
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
    allow = _tool_allowlist_status([r["name"] for r in rows])
    allowed_set = set(allow.get("allowed") or [])
    for row in rows:
        name = row["name"]
        row["allowlisted"] = name in allowed_set
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
        "allowlist": allow,
        "reconciliation": _reconciliation_summary(recon),
    }
    _tools_cache["inventory"] = out
    _tools_cache["ts"] = now
    return out


def _tool_allowlist_status(names: list) -> dict:
    """执行期白名单（companion_config.yaml 的 tool_allowlist 段）状态。

    口径跟工具层**同一份**（app/engine/tools/_core.load_tool_allowlist）：
    精确名命中 or 命中某个前缀桶 = 放行；否则**执行期一律拒绝**
    （模型硬点名也拦得住）。

    yaml 里没有该段 -> 运行期退回基线快照（全放行），面板如实这么说，
    绝不假装有一份白名单。
    """
    out = {
        "available": False, "exact_count": 0, "prefixes": [],
        "allowed": [], "blocked": [],
        "dead_entries": [], "dead_prefixes": [], "note": "",
    }
    names = [str(n) for n in (names or [])]
    policy = None
    default_prefixes = ()
    try:
        from app.engine.tools._core import (DEFAULT_ALLOWLIST_PREFIXES,
                                            load_tool_allowlist)
        default_prefixes = DEFAULT_ALLOWLIST_PREFIXES
        policy = load_tool_allowlist(force=True)
    except Exception:  # noqa: BLE001 - 读不到 yaml 不该让面板崩
        policy = None
    if policy is None:
        out["allowed"] = names
        out["note"] = ("companion_config.yaml 里没有 tool_allowlist 段 —— "
                       "运行期退回基线快照（全部放行）")
        return out
    exact = set(policy.exact or ())
    prefixes = tuple(p for p in (policy.prefixes or default_prefixes) if p)
    out["available"] = True
    out["exact_count"] = len(exact)
    out["prefixes"] = list(prefixes)
    for n in names:
        if n in exact or any(n.startswith(p) for p in prefixes):
            out["allowed"].append(n)
        else:
            out["blocked"].append(n)
    live = set(names)
    out["dead_entries"] = sorted(exact - live)
    out["dead_prefixes"] = sorted(p for p in prefixes
                                  if not any(n.startswith(p) for n in live))
    out["note"] = ("放行 %d 个 / 拦截 %d 个（执行期硬拒，模型硬点名也拦得住）"
                   % (len(out["allowed"]), len(out["blocked"])))
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
        from fastapi.responses import HTMLResponse, Response
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
        return Response(
            content=_HTML_PAGE.replace("__MEM_MAX_NODES__", str(MEM_NET_MAX_NODES)),
            headers={"Cache-Control": "no-store"},
        )

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
        """语义图。默认把所有超过相似度下限的连线都算出来。

        sidecar 没在跑 -> **200 + ok=False + 为什么没有**（绝不是 500）。
        """
        client = _memory_sidecar(sidecar_url, timeout=20.0)
        try:
            graph = client.graph(limit=limit, top_k=top_k, mode=mode,
                                 timeout=15.0)
        except Exception:  # noqa: BLE001
            graph = {"ok": False, "nodes": [], "edges": []}
        if not isinstance(graph, dict):
            graph = {"ok": False, "nodes": [], "edges": []}
        graph.setdefault("ok", False)
        graph.setdefault("nodes", [])
        graph.setdefault("edges", [])
        if not graph.get("ok") and not graph.get("reason"):
            graph["reason"] = (client.last_error
                               or "记忆 sidecar 没在跑（没有语义图）")
        return graph

    @app.get("/api/memory/partitions")
    def memory_partitions():
        """记忆分区（身份 / 价值 / 程序性）—— **常量块**，不在 sidecar 里。

        这三块是「设定」不是记忆卡：每轮都进 system prompt，
        所以主人得能在控制台的「网络」视图里看见它们现在是什么。
        读不到就返回 ok=False + 原因（绝不是 500）。
        """
        try:
            from app.brain import memory_partitions as mp
        except Exception:  # noqa: BLE001
            return {"ok": False, "reason": "记忆分区模块不可用", "blocks": []}
        spec = (
            ("identity", mp.IDENTITY_HEADER, mp.load_identity,
             "data/identity.json"),
            ("values", mp.VALUES_HEADER, mp.load_values,
             "data/values.json"),
            ("procedures", mp.PROCEDURES_HEADER, mp.load_procedures,
             "data/procedures.json"),
        )
        blocks = []
        for key, header, loader, path in spec:
            try:
                items = loader() or []
            except Exception:  # noqa: BLE001
                items = []
            if key == "values":
                # 层级给中文名（底线 / 偏好 / 风格）—— 界面上别甩英文 key
                labels = getattr(mp, "VALUE_LEVEL_LABELS", {})
                items = [dict(it, level_label=labels.get(str(it.get("level") or ""),
                                                    str(it.get("level") or "")))
                         for it in items]
            blocks.append({"key": key, "header": header, "path": path,
                           "count": len(items), "items": items})
        return {"ok": True, "blocks": blocks,
                "note": "常量块：每轮进 system prompt，改文件即生效"}

    @app.get("/api/memory/sleep")
    def memory_sleep():
        """睡觉面板：定时设置 + 这一觉的计划 + 上次的结果。"""
        try:
            plan = _sleep_service.plan()
        except Exception:  # noqa: BLE001
            plan = {"ok": False}
        if not isinstance(plan, dict):
            plan = {"ok": False}
        if plan.get("ok") is False and not plan.get("reason"):
            plan["reason"] = "记忆 sidecar 没在跑（算不出这一觉的计划）"
        try:
            state = _sleep_service.state()
        except Exception:  # noqa: BLE001
            state = {}
        return {"ok": True, "state": state, "plan": plan}

    @app.post("/api/memory/sleep/schedule")
    def memory_sleep_schedule(payload: Optional[dict] = None):
        """改定时睡觉的设置（enabled / time）。"""
        payload = payload or {}
        try:
            state = _sleep_service.set_schedule(
                enabled=payload.get("enabled"), time=payload.get("time"))
        except Exception as e:  # noqa: BLE001 - 面板不许因为这个 500
            return {"ok": False, "error": "改定时睡觉的设置失败：%s" % e,
                    "state": {}}
        return {"ok": True, "state": state}

    @app.post("/api/memory/sleep/run")
    def memory_sleep_run(payload: Optional[dict] = None):
        """现在睡一觉：模型写摘要 → sidecar 归组 / 模糊 / 遗忘。"""
        payload = payload or {}
        try:
            return _sleep_service.run(dry_run=bool(payload.get("dry_run")),
                                      trigger="manual")
        except Exception as e:  # noqa: BLE001 - 面板不许因为这个 500
            return {"ok": False, "error": "睡觉失败：%s" % e}

    @app.get("/api/tools")
    def tools():
        """工具 Tab：工具清单（数量动态）+ 风险/解锁等级 + 执行期白名单 + 调用统计 + 对账。"""
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
            "appearance": _live2d_model_state(
                client.appearance().get("appearance") or {}),
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

    @app.get("/api/voice")
    def voice():
        """语音 Tab：本机音色 / 情绪参考 / TTS 与 RVC 服务状态。

        语音服务不可达时**如实说不可达**（200 + ok=false），绝不 500。
        """
        return _voice_payload()

    @app.get("/api/health")
    def health():
        """健康与维护 Tab：429 限流 / 崩溃 / 账本污染 / data/ 文件体检。"""
        payload = _health_report(_data_dir, trace_db)
        # 记忆 sidecar 的两个索引（向量 / BM25 + 精排）现在归 sidecar 自己管，
        # 面板只读它 /health + /stats 报出来的状态 ——
        # 不再有「本进程的向量索引」这回事（那个模块已经删了）。
        # 健康 Tab 只要 sidecar 的状态，不要语义图 —— /graph 是一次全量
        # PCA 投影（sidecar 慢的时候能卡十几秒），不该拖住健康检查。
        mem = _memory_payload(sidecar_url, with_graph=False)
        payload["memory_sidecar"] = mem.get("sidecar") or {}
        payload["memory_stats"] = mem.get("stats") or {}
        return payload

    @app.get("/api/overview")
    def overview():
        """顶部卡片数据（8 张卡片一次拿全，前端不用发 8 个请求）。"""
        stats = _recorder.stats()
        tokens = _token_summary(_data_dir)
        # 概览只报记忆的条数/连通性，不要语义图（同上：别拖住概览）
        mem = _memory_payload(sidecar_url, with_graph=False)
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
                 "value": (tokens.get("cost") or {}).get("amount", 0.0),
                 "currency": (tokens.get("cost") or {}).get("currency")
                             or COST_CURRENCY,
                 "symbol": (tokens.get("cost") or {}).get("symbol")
                           or COST_SYMBOL,
                 "estimated": bool((tokens.get("cost") or {}).get("estimated"))},
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
