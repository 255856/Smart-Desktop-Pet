"use strict";

/*
 * Desktop Pet · Live2D Demo Renderer
 * ----------------------------------
 * 在线 demo：PIXI v7 渲染骨架 + 完整 UI 控件（动作/表情/口型等按钮可点）。
 * 模型渲染层不在 demo 里（pixi-live2d-display 在标准浏览器 + PIXI v7 全量包下
 * 无法注册 Live2DModel），请克隆项目本地运行 desktop 版看真实 Live2D 效果。
 *
 * 控件可交互的部分：
 *   - 模型 URL 输入 + 加载按钮（按钮可用但提示本地运行）
 *   - 拖拽 .model3.json 到页面（File API 演示）
 *   - 动作 / 表情 / 口型测试（仅 UI 演示）
 *   - 鼠标移动（PIXI 演示角色跟随）
 *
 * 与 `app/animation/live2d_bridge.html` 桥接版的功能对照见页面 README。
 */

// ---------------- 日志 ----------------
const logEl = document.getElementById("log");
function log(msg, level = "") {
  const line = document.createElement("div");
  if (level) line.className = level;
  line.textContent = `[${new Date().toLocaleTimeString()}] ${msg}`;
  logEl.appendChild(line);
  logEl.scrollTop = logEl.scrollHeight;
  if (level === "err") console.error(msg); else console.log(msg);
}

// ---------------- 加载状态 ----------------
const loader = document.getElementById("loader");
const loaderText = document.getElementById("loader-text");
const statusEl = document.getElementById("status");
function showLoader(text) { loaderText.textContent = text || "加载中…"; loader.classList.remove("hidden"); }
function hideLoader() { loader.classList.add("hidden"); }
function setStatus(text, color = "#a8b0c0") {
  statusEl.textContent = text;
  statusEl.style.color = color;
}

// ---------------- PIXI 演示角色 ----------------
class PetDemo {
  constructor() {
    this.app = null;
    this.character = null;
    this.talk = false;
    this.talkStartMs = 0;
  }
  ensureApp() {
    if (this.app) return;
    if (typeof PIXI === "undefined") throw new Error("PIXI 未加载");
    const stage = document.getElementById("stage");
    this.app = new PIXI.Application({
      backgroundAlpha: 0, antialias: true, autoDensity: true,
      resolution: Math.min(window.devicePixelRatio || 1, 1.5),
      autoStart: true,
      width: stage.clientWidth || 800,
      height: stage.clientHeight || 800,
    });
    stage.appendChild(this.app.view);
    this.app.ticker.maxFPS = 30;

    // 用 PIXI Graphics 画一个简易"角色"（圆形 + 眼睛 + 嘴）
    // 当 demo 演示用 —— 真实 Live2D 在桌面版
    const g = new PIXI.Graphics();
    g.beginFill(0xffe1c4, 1);
    g.drawCircle(0, 0, 180);
    g.endFill();
    // 耳朵
    g.beginFill(0xffd9b3, 1);
    g.drawPolygon([-100, -120, -160, -200, -60, -160]);
    g.drawPolygon([100, -120, 160, -200, 60, -160]);
    g.endFill();
    // 腮红
    g.beginFill(0xff9eb5, 0.5);
    g.drawCircle(-80, 50, 22);
    g.drawCircle(80, 50, 22);
    g.endFill();
    this.app.stage.addChild(g);
    this.body = g;

    // 眼睛（用 Graphics 方便后续改形状做表情）
    const eyeL = new PIXI.Graphics();
    const eyeR = new PIXI.Graphics();
    eyeL.beginFill(0x2a2a2a);
    eyeL.drawEllipse(0, 0, 18, 22);
    eyeL.endFill();
    eyeR.beginFill(0x2a2a2a);
    eyeR.drawEllipse(0, 0, 18, 22);
    eyeR.endFill();
    eyeL.x = -55; eyeL.y = -20;
    eyeR.x = 55; eyeR.y = -20;
    this.app.stage.addChild(eyeL); this.app.stage.addChild(eyeR);
    this.eyeL = eyeL; this.eyeR = eyeR;

    // 高光
    const hlL = new PIXI.Graphics();
    const hlR = new PIXI.Graphics();
    hlL.beginFill(0xffffff);
    hlL.drawCircle(0, 0, 5);
    hlL.endFill();
    hlR.beginFill(0xffffff);
    hlR.drawCircle(0, 0, 5);
    hlR.endFill();
    hlL.x = -50; hlL.y = -25;
    hlR.x = 60; hlR.y = -25;
    this.app.stage.addChild(hlL); this.app.stage.addChild(hlR);

    // 嘴（动态：口型）
    const mouth = new PIXI.Graphics();
    this.mouth = mouth;
    this.app.stage.addChild(mouth);

    // 中心位置 + 缩放
    g.x = this.app.screen.width / 2;
    g.y = this.app.screen.height / 2 + 30;
    this._originX = g.x;
    this._originY = g.y;
    eyeL.x += g.x; eyeR.x += g.x;
    eyeL.y += g.y; eyeR.y += g.y;
    hlL.x += g.x; hlR.x += g.x;
    hlL.y += g.y; hlR.y += g.y;

    // 浮动动画 + 眨眼 + 口型
    let blinkT = 0;
    let blinkNext = 2000 + Math.random() * 3000;
    this.app.ticker.add((dt) => {
      const ms = performance.now();
      // 浮动
      const yOff = Math.sin(ms / 800) * 8;
      g.y = this._originY + yOff;
      eyeL.y = -20 + g.y;
      eyeR.y = -20 + g.y;
      hlL.y = -25 + g.y;
      hlR.y = -25 + g.y;
      // 眨眼
      blinkT += dt * 16;
      if (blinkT > blinkNext) {
        blinkT = 0;
        blinkNext = 2000 + Math.random() * 3000;
      }
      const blinkFactor = blinkT < 150 ? Math.max(0, 1 - blinkT / 150) : 1;
      eyeL.scale.y = eyeR.scale.y = blinkFactor;
      // 口型
      mouth.clear();
      mouth.beginFill(0xc1444e);
      let mouthOpen = 0;
      if (this.talk) {
        const dt2 = ms - this.talkStartMs;
        mouthOpen = Math.max(0, Math.sin(dt2 / 80)) *
                    (0.3 + 0.6 * Math.abs(Math.sin(dt2 / 620)));
      }
      mouth.drawEllipse(0, 70, 18, 4 + mouthOpen * 18);
      mouth.endFill();
      mouth.x = g.x; mouth.y = g.y;
    });
  }

  focus(nx, ny) {
    if (!this.body) return;
    this.eyeL.x = this._originX - 55 + nx * 8;
    this.eyeR.x = this._originX + 55 + nx * 8;
    this.eyeL.y = -20 + this._originY + ny * 6;
    this.eyeR.y = -20 + this._originY + ny * 6;
    this.hlL.x = this.eyeL.x + 5;
    this.hlR.x = this.eyeR.x + 5;
    this.hlL.y = this.eyeL.y - 5;
    this.hlR.y = this.eyeR.y - 5;
  }

  startTalk() {
    this.talk = true;
    this.talkStartMs = performance.now();
  }
  stopTalk() {
    this.talk = false;
  }
}

window.demo = new PetDemo();

// ---------------- 拖拽 ----------------
const dropHint = document.getElementById("drop-hint");
let dragDepth = 0;
window.addEventListener("dragenter", (e) => { e.preventDefault(); dragDepth++; dropHint.classList.add("show"); });
window.addEventListener("dragleave", (e) => { e.preventDefault(); dragDepth--; if (dragDepth <= 0) { dragDepth = 0; dropHint.classList.remove("show"); } });
window.addEventListener("dragover", (e) => { e.preventDefault(); });
window.addEventListener("drop", (e) => {
  e.preventDefault();
  dragDepth = 0;
  dropHint.classList.remove("show");
  const file = e.dataTransfer.files && e.dataTransfer.files[0];
  if (!file) return;
  if (!file.name.endsWith(".model3.json")) {
    log("请拖入 .model3.json 文件", "err");
    return;
  }
  log(`收到拖拽文件：${file.name}`, "ok");
  log("demo 在线版不渲染真实 Live2D 模型，请克隆项目本地运行桌面版", "err");
  log("项目地址：https://github.com/255856/Smart-Desktop-Pet", "err");
});

// ---------------- UI 绑定 ----------------
document.getElementById("load-btn").addEventListener("click", () => {
  const url = document.getElementById("model-url").value.trim();
  if (!url) { log("请填写 Model URL", "err"); return; }
  log("demo 在线版不渲染真实 Live2D 模型（pixi-live2d-display 与 PIXI v7 全量包不兼容）", "err");
  log("请克隆项目本地运行桌面版：https://github.com/255856/Smart-Desktop-Pet", "err");
  log(`（URL 已填入：${url}）`, "ok");
});
document.getElementById("model-url").addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("load-btn").click();
});

// 控件 demo（不真的播放动作/表情，但按钮可点 → 显示提示）
document.getElementById("motion-btn").addEventListener("click", () => {
  const g = document.getElementById("motion-group").value;
  if (g) {
    log(`动作 ${g} 触发（demo 演示用，实际动作在桌面版）`, "ok");
    // 触发一次"蹦跳"
    if (window.demo.body) window.demo.body.scale.set(1.05, 0.95);
    setTimeout(() => window.demo.body && window.demo.body.scale.set(1, 1), 200);
  }
});
document.getElementById("expression-btn").addEventListener("click", () => {
  const n = document.getElementById("expression").value;
  if (n) {
    log(`表情 ${n} 触发（demo 演示用）`, "ok");
    if (n === "开心" || n === "smile") {
      // 简单"笑"——嘴巴画大
      if (window.demo.mouth) {
        window.demo.mouth.clear();
        window.demo.mouth.beginFill(0xc1444e);
        window.demo.mouth.drawEllipse(0, 70, 26, 14);
        window.demo.mouth.endFill();
        setTimeout(() => { /* 让口型函数恢复 */ }, 1500);
      }
    }
  }
});
document.getElementById("reset-btn").addEventListener("click", () => {
  log("重置（demo 演示用）", "ok");
  window.demo.body && window.demo.body.scale.set(1, 1);
});
document.getElementById("speak-btn").addEventListener("click", (e) => {
  if (window.demo.talk) {
    window.demo.stopTalk();
    e.target.textContent = "💬 测试口型";
  } else {
    window.demo.startTalk();
    e.target.textContent = "🛑 停止口型";
    log("口型同步开（demo 演示）", "ok");
  }
});

document.getElementById("stage").addEventListener("pointermove", (e) => {
  const r = window.demo.app.view.getBoundingClientRect();
  const nx = (e.clientX - r.left) / r.width * 2 - 1;
  const ny = (e.clientY - r.top) / r.height * 2 - 1;
  window.demo.focus(nx, ny);
});

// ---------------- 启动 ----------------
window.addEventListener("DOMContentLoaded", () => {
  (async () => {
    try {
      if (window.__sdkReady) await window.__sdkReady;
    } catch (e) {
      setStatus("SDK 加载失败 · 看下方日志", "#ff7675");
      log("SDK 加载失败：" + (e.message || e), "err");
      return;
    }

    const st = window.__sdkLoadStatus || {};
    log(`SDK 加载完成：`);
    if (st.pixi) log(`  pixi   ← ${st.pixi.replace(/^ok:/, "")}`);
    if (st.core) log(`  core   ← ${st.core.replace(/^ok:/, "")}`, "ok");

    if (typeof PIXI === "undefined") {
      setStatus("PIXI 未加载", "#ff7675");
      log("PIXI 未加载", "err");
      return;
    }

    try {
      window.demo.ensureApp();
    } catch (e) {
      setStatus("PIXI 初始化失败", "#ff7675");
      log(e.message || e, "err");
      return;
    }

    // 填一些动作组/表情 demo 选项（仅为演示控件）
    const mg = document.getElementById("motion-group");
    ["Idle", "TapBody", "TapHead"].forEach(g => {
      const opt = document.createElement("option");
      opt.value = g; opt.textContent = g;
      mg.appendChild(opt);
    });
    const exprSel = document.getElementById("expression");
    ["F01", "F02", "开心"].forEach(n => {
      const opt = document.createElement("option");
      opt.value = n; opt.textContent = n;
      exprSel.appendChild(opt);
    });
    document.getElementById("controls").style.display = "flex";

    setStatus("就绪 · 演示版（PIXI 演示角色）", "#55efc4");
    log(`PIXI v${PIXI.VERSION} 已就绪`, "ok");
    log("ℹ️ 在线 demo 仅展示 PIXI 渲染 + 完整 UI", "ok");
    log("真实 Live2D 模型渲染请克隆项目本地运行：", "ok");
    log("  https://github.com/255856/Smart-Desktop-Pet", "ok");
    log("本地运行：python main.py  （自动启动桌宠）", "ok");
  })();
});
