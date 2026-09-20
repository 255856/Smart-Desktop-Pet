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
  .stat .ico{font-size:18px;}
  .stat .num{font-size:28px;font-weight:700;margin-top:6px;line-height:1.1;}
  .stat .label{font-size:12px;color:var(--sub);margin-top:4px;}
  .stat .bar{position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--accent);}
  .stat.green .bar{background:var(--green);} .stat.green .num{color:var(--green);}
  .stat.blue .bar{background:var(--blue);} .stat.blue .num{color:var(--blue);}
  .stat.amber .bar{background:var(--amber);} .stat.amber .num{color:var(--amber);}
  .stat.violet .bar{background:var(--accent);} .stat.violet .num{color:var(--accent-d);}

  /* 标签页 */
  .tabs{display:flex;gap:6px;margin-bottom:18px;}
  .tab{padding:9px 20px;border-radius:11px;cursor:pointer;font-weight:600;font-size:14px;
    color:var(--sub);background:transparent;border:1px solid transparent;transition:.15s;}
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
    <div class="tab active" data-view="sessions">💬 会话记录</div>
    <div class="tab" data-view="memory">🧠 长期记忆</div>
    <div class="tab" data-view="tools">🔧 工具统计</div>
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
        <div class="empty"><div class="big">🔍</div><div>选择左侧会话，查看完整执行过程</div></div>
      </div>
    </div>
  </div>

  <!-- 记忆 -->
  <div class="view" id="view-memory">
    <div class="panel" style="padding:18px;">
      <div class="sub-head" style="margin-top:0;">🧠 桌宠记住的事 <span id="memCount" class="mini-tag"></span></div>
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
    {cls:"violet", ico:"💬", num:s.total_runs??0, label:"总会话数"},
    {cls:"green", ico:"✅", num:rate+"%", label:`成功率（${s.success_runs||0}/${s.total_runs||0}）`},
    {cls:"blue", ico:"🔧", num:s.total_tool_calls??0, label:"工具调用次数"},
    {cls:"amber", ico:"⚡", num:s.avg_steps??0, label:"平均步骤"},
    {cls:"violet", ico:"⏱️", num:fmtDur(s.avg_duration), label:"平均耗时"},
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
    box.innerHTML=`<div class="empty" style="min-height:200px;"><div class="big">📭</div><div>没有符合条件的会话</div></div>`;
    return;
  }
  const scrollTop=box.scrollTop;
  box.innerHTML=list.map(r=>{
    const tags=[];
    if(r.tool_calls>0) tags.push(`<span class="mini-tag">🔧 ${r.tool_calls}</span>`);
    if(r.total_steps>0) tags.push(`<span class="mini-tag">⚡ ${r.total_steps}</span>`);
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
    <div class="cap">🔧 ${esc(cn)}<span class="mini-tag">${esc(name)}</span></div>
    ${kvHtml}${resultHtml}
  </div>`;
}

function renderPlanCard(p){
  const steps=p.steps||[];
  return `<div class="tl-card">
    <div class="cap" style="color:#1d4ed8;">🧭 执行计划</div>
    <ol style="margin:4px 0 0;padding-left:20px;">
      ${steps.map(s=>`<li style="margin-bottom:6px;font-size:12.5px;line-height:1.6;">
        <div>${esc(s.thought||"")}</div>
        ${s.tool_name?`<span class="mini-tag" style="margin-top:3px;">🔧 ${esc(TOOL_NAME[s.tool_name]||s.tool_name)}</span>`:""}
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
        return `<div class="tl-item tool"><div class="node">🔧</div>${renderToolCard(p)}</div>`;
      if(e.kind==="plan")
        return `<div class="tl-item plan"><div class="node">🧭</div>${renderPlanCard(p)}</div>`;
      if(e.kind==="reflection")
        return `<div class="tl-item reflection"><div class="node">💭</div>
          <div class="tl-card"><div class="cap" style="color:var(--accent-d);">💭 反思评估</div>
          <div style="font-size:12.5px;">${esc(p.comment||"")}
          ${p.verdict?`<span class="mini-tag" style="margin-left:6px;">${esc(p.verdict)}</span>`:""}</div></div></div>`;
      if(e.kind==="meta"){
        const label=p.event==="force_retry"?"自动重试":p.event||"内部事件";
        const note=p.reason||p.intent||"";
        return `<div class="tl-item meta"><div class="node">ℹ️</div>
          <div class="tl-card"><div class="cap" style="color:var(--sub);">ℹ️ ${esc(label)}</div>
          <div style="font-size:12.5px;color:var(--sub);">${esc(note)}</div></div></div>`;
      }
      return "";
    }).join("")}</div>`;
  } else {
    tlHtml=`<div class="hint">💬 这是一条普通对话，没有调用工具或额外步骤。</div>`;
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
      <span class="meta-cell">🕐 ${fmtTime(r.started_at)}</span>
      <span class="meta-cell">⏱️ 耗时 <b>${fmtDur(dur)}</b></span>
      <span class="meta-cell">模式 <b>${MODE_CN[r.mode]||esc(r.mode)}</b></span>
      <span class="meta-cell">⚡ ${r.total_steps} 步 · 🔧 ${r.tool_calls} 次</span>
      ${model?`<span class="meta-cell">模型 <b>${esc(model)}</b></span>`:""}
      ${emo?`<span class="meta-cell">情绪 <b>${esc(emo)}</b></span>`:""}
    </div>

    ${isFail&&errMsg?`<div class="section-title">❌ 错误信息</div>
      <div class="error-banner"><span>⚠️</span><div>${esc(errMsg)}</div></div>`:""}

    <div class="section-title">💬 最终回答</div>
    ${answerDupError
      ? `<div class="hint">⚠️ 本次生成在出错前未产生有效回答，原因见上方错误信息。</div>`
      : `<div class="answer ${r.status==="failed"?"error":""}">${miniMd(answer)||"(空)"}</div>`}

    <div class="section-title">🧭 执行过程</div>
    ${tlHtml}
    ${streamHtml}
    ${rawHtml}
  </div>`;
}

async function selectRun(id){
  state.selectedId=id;
  renderRunList();
  $("#detailPanel").innerHTML=`<div class="empty"><div class="big">⏳</div><div>加载中…</div></div>`;
  try{
    const r=await api("/api/runs/"+id);
    if(state.selectedId===id){ state.detail=r; renderDetail(r); }
  }catch(e){
    $("#detailPanel").innerHTML=`<div class="empty"><div class="big">⚠️</div><div>加载失败：${esc(e.message)}</div></div>`;
  }
}

function renderMemory(data){
  const items=(data&&data.items)||[];
  $("#memCount").textContent=items.length+" 条";
  if(!items.length){
    $("#memGrid").innerHTML=`<div class="empty" style="grid-column:1/-1;min-height:220px;"><div class="big">🧠</div><div>还没有记住任何事</div></div>`;
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

async function refresh(){
  try{
    const [s,runs]=await Promise.all([api("/api/stats"),api("/api/runs?limit=100")]);
    state.stats=s; state.runs=runs;
    renderStats(); renderRunList(); renderTools();
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
