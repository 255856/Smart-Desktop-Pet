"use strict";

/*
 * Desktop Pet · Live2D Demo Renderer (真实 Live2D 渲染版)
 * ----------------------------------------------------
 * 用 PIXI v7 + 原生 Cubism Core + pixi-live2d-display 0.3.0 真实渲染 Live2D 模型。
 *
 * 与 `app/animation/live2d_bridge.html` 的桥接版对应：
 *   - 模型加载：fetch model3.json → Live2DModel.from 自动解析 moc3/textures/motions/expressions
 *   - 渲染：pixi-live2d-display 内置 Cubism 4 WebGL 渲染管线
 *   - 表情/动作/口型/视线跟随：直接用 PIXI.live2d.Live2DModel 内置 API
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

// ---------------- PIXI + Live2D 渲染 ----------------
class Live2DDemo {
  constructor() {
    this.app = null;
    this.model = null;
    this.ready = false;
    this._talkParam = "ParamMouthOpenY";
    this._talking = false;
    this._mouthTimer = null;
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
    window.addEventListener("resize", () => this._fit());
  }

  async loadModel(modelUrl) {
    if (!modelUrl) throw new Error("model URL 为空");
    this.shutdown();
    this.ensureApp();

    // 注册 Live2DModel 到 PIXI Ticker（每帧自动更新）
    if (PIXI.live2d && PIXI.live2d.Live2DModel && PIXI.live2d.Live2DModel.registerTicker && PIXI.Ticker) {
      PIXI.live2d.Live2DModel.registerTicker(PIXI.Ticker);
    }

    showLoader("加载模型…");
    setStatus("加载中…", "#ffce5c");

    try {
      log(`加载模型：${modelUrl}`);
      const model = await PIXI.live2d.Live2DModel.from(modelUrl, {
        autoHitTest: false, autoFocus: false,
      });
      this.model = model;
      this.app.stage.addChild(model);

      // mask 组数兜底（cubism4 内部需要）
      try {
        const im = model.internalModel;
        const core = im.coreModel;
        if (core.isUsingMasking && core.isUsingMasking()) {
          const counts = core.getDrawableMaskCounts();
          const using = counts.reduce((a, c) => a + (c > 0 ? 1 : 0), 0);
          const need = Math.max(1, Math.ceil(using / 32));
          const cur = im.renderer.getRenderTextureCount ?
            im.renderer.getRenderTextureCount() : 1;
          if (need > cur) {
            im.renderer.initialize(core, need);
            log(`mask 纹理扩展：${cur} → ${need}`, "ok");
          }
        }
      } catch (e) { /* 旧模型不需要 */ }

      // 隐藏通用 WaterMark 图层
      try {
        const ids = model.internalModel.coreModel.getDrawableIds();
        let hidden = 0;
        for (let i = 0; i < ids.length; i++) {
          if (/watermark/i.test(String(ids[i]))) {
            try { model.internalModel.coreModel.setPartOpacityById(String(ids[i]), 0); hidden++; }
            catch (e) {}
          }
        }
        if (hidden) log(`隐藏通用 WaterMark 图层 ${hidden} 个`, "ok");
      } catch (e) {}

      this._fit();
      this.ready = true;
      this._refreshControls();
      hideLoader();
      const exprCount = (model.internalModel && model.internalModel.settings.expressions || []).length;
      const motionGroups = Object.keys((model.internalModel && model.internalModel.settings.motions) || {});
      setStatus(`就绪 · ${exprCount} 表情 / ${motionGroups.length} 组动作`, "#55efc4");
      log(`模型加载完成：${exprCount} 表情 / ${motionGroups.length} 组动作`, "ok");
      // 启动 idle 行为（随机表情 + 随机动作，模拟桌宠挂机时行为）
      if (window.startIdleBehavior) window.startIdleBehavior();
    } catch (err) {
      hideLoader();
      setStatus("加载失败", "#ff7675");
      log(`加载失败：${err.message || err}`, "err");
      throw err;
    }
  }

  _fit() {
    if (!this.model || !this.app) return;
    const w = this.app.screen.width, h = this.app.screen.height;
    const sx = (w / this.model.width) * 0.94;
    const sy = (h / this.model.height) * 0.94;
    const s = Math.min(sx, sy);
    this.model.scale.set(s);
    try { this.model.anchor.set(0.5, 0.5); } catch (e) {}
    this.model.x = w / 2;
    this.model.y = h / 2;
  }

  _refreshControls() {
    const ctrl = document.getElementById("controls");
    ctrl.style.display = "flex";

    const mg = document.getElementById("motion-group");
    mg.innerHTML = "";
    const motions = (this.model.internalModel && this.model.internalModel.settings.motions) || {};
    for (const g of Object.keys(motions)) {
      const opt = document.createElement("option");
      opt.value = g;
      opt.textContent = `${g} (${motions[g].length})`;
      mg.appendChild(opt);
    }

    const exprSel = document.getElementById("expression");
    exprSel.innerHTML = "";
    const exprs = (this.model.internalModel && this.model.internalModel.settings.expressions) || [];
    for (const e of exprs) {
      const name = e.Name || e.name || "";
      const opt = document.createElement("option");
      opt.value = name; opt.textContent = name;
      exprSel.appendChild(opt);
    }
  }

  playMotion(group) {
    if (!this.model || !this.model.motion) return;
    try { this.model.motion(group); }
    catch (e) { log(`播放动作失败：${e.message || e}`, "err"); }
  }

  setExpression(name) {
    if (!this.model || !name) return;
    try { return this.model.expression(name); }
    catch (e) { log(`设置表情失败：${e.message || e}`, "err"); }
  }

  startTalk() {
    if (!this.ready) return;
    this._talking = true;
    this._mouthTimer && clearInterval(this._mouthTimer);
    const t0 = performance.now();
    this._mouthTimer = setInterval(() => {
      if (!this.model || !this._talking) return;
      const t = performance.now() - t0;
      const v = Math.max(0, Math.sin(t / 80)) *
                (0.3 + 0.6 * Math.abs(Math.sin(t / 620)));
      try {
        this.model.internalModel.coreModel.setParameterValueById(this._talkParam, v);
      } catch (e) {}
    }, 30);
    log("口型同步开", "ok");
  }
  stopTalk() {
    this._talking = false;
    if (this._mouthTimer) { clearInterval(this._mouthTimer); this._mouthTimer = null; }
    if (this.model) {
      try {
        this.model.internalModel.coreModel.setParameterValueById(this._talkParam, 0);
      } catch (e) {}
    }
    log("口型同步关");
  }

  focus(nx, ny) {
    if (!this.model || !this.ready) return;
    try { this.model.focus(this.model.width / 2 + nx * this.model.width / 2,
                          this.model.height / 2 + ny * this.model.height / 2); }
    catch (e) { /* 旧版 API 无 focus */ }
  }

  shutdown() {
    this._talking = false;
    if (this._mouthTimer) { clearInterval(this._mouthTimer); this._mouthTimer = null; }
    if (this.model && this.app) {
      try { this.app.stage.removeChild(this.model); this.model.destroy(); } catch (e) {}
    }
    this.model = null;
    this.ready = false;
  }

  // 模拟"桌宠说话"：逐字驱动口型 + 偶尔切换表情
  // 这对应桌面版：TTS 流式播放 → 逐句/逐字驱动 mouth 参数 + 情绪切换表情
  simulateSpeak(text) {
    if (this._simulating) return;
    this._simulating = true;
    const chars = Array.from(text);
    const perCharMs = 80;  // 每字 80ms（模拟正常语速）
    this._mouthTimer && clearInterval(this._mouthTimer);
    this._talking = true;

    let i = 0;
    const t0 = performance.now();
    this._mouthTimer = setInterval(() => {
      if (!this.model || !this._simulating) {
        clearInterval(this._mouthTimer);
        return;
      }
      const t = performance.now() - t0;
      // 口型：正弦 + 随机抖动（与桌面版公式一致）
      const v = Math.max(0, Math.sin(t / 80)) *
                (0.3 + 0.6 * Math.abs(Math.sin(t / 620)));
      try {
        this.model.internalModel.coreModel.setParameterValueById(this._talkParam, v);
      } catch (e) {}
      i++;
      // 每 4 个字随机切表情
      if (i % 4 === 0) {
        const list = (this.model.internalModel.settings.expressions) || [];
        if (list.length > 0) {
          const e = list[Math.floor(Math.random() * list.length)];
          const name = e.Name || e.name;
          try { this.model.expression(name); } catch (err) {}
          log(`[speak] 表情 → ${name}`);
        }
      }
      // 模拟完成
      if (i >= chars.length * 8) {
        clearInterval(this._mouthTimer);
        try {
          this.model.internalModel.coreModel.setParameterValueById(this._talkParam, 0);
        } catch (e) {}
        this._talking = false;
        this._simulating = false;
        log(`[speak] 完成（共 ${chars.length} 字）`, "ok");
      }
    }, perCharMs / 8);
  }
}

window.demo = new Live2DDemo();

// ---------------- 拖拽 ----------------
let dropHint = null;
let dragDepth = 0;
window.addEventListener("dragenter", (e) => {
  e.preventDefault();
  dragDepth++;
  if (!dropHint) dropHint = document.getElementById("drop-hint");
  if (dropHint) dropHint.classList.add("show");
});
window.addEventListener("dragleave", (e) => {
  e.preventDefault();
  dragDepth--;
  if (dragDepth <= 0) {
    dragDepth = 0;
    if (dropHint) dropHint.classList.remove("show");
  }
});
window.addEventListener("dragover", (e) => { e.preventDefault(); });
window.addEventListener("drop", (e) => {
  e.preventDefault();
  dragDepth = 0;
  if (dropHint) dropHint.classList.remove("show");
  const file = e.dataTransfer.files && e.dataTransfer.files[0];
  if (!file) return;
  if (!file.name.endsWith(".model3.json")) {
    log("请拖入 .model3.json 文件", "err");
    return;
  }
  const url = URL.createObjectURL(file);
  const urlInput = document.getElementById("model-url");
  if (urlInput) urlInput.value = `file://${file.name}（内存中）`;
  if (window.demo) window.demo.loadModel(url);
});

// ---------------- UI 绑定 ----------------
document.getElementById("load-btn").addEventListener("click", () => {
  const url = document.getElementById("model-url").value.trim();
  if (!url) { log("请填写 Model URL", "err"); return; }
  window.demo.loadModel(url);
});
document.getElementById("model-url").addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("load-btn").click();
});
document.getElementById("motion-btn").addEventListener("click", () => {
  const g = document.getElementById("motion-group").value;
  if (g) window.demo.playMotion(g);
});
document.getElementById("expression-btn").addEventListener("click", () => {
  const n = document.getElementById("expression").value;
  if (n) window.demo.setExpression(n);
});
document.getElementById("reset-btn").addEventListener("click", () => {
  const url = document.getElementById("model-url").value.trim();
  if (url) window.demo.loadModel(url);
});
document.getElementById("speak-btn").addEventListener("click", (e) => {
  if (window.demo._talking) {
    window.demo.stopTalk();
    e.target.textContent = "💬 测试口型";
  } else {
    window.demo.startTalk();
    e.target.textContent = "🛑 停止口型";
  }
});

// 模拟"桌宠说话"：输入文字 → 按字符模拟 TTS → 口型同步 + 随机表情
document.getElementById("chat-btn").addEventListener("click", () => {
  const text = document.getElementById("chat-input").value.trim();
  if (!text) { log("请输入文字", "err"); return; }
  if (!window.demo.ready) { log("先加载模型", "err"); return; }
  log(`💬 模拟说话：「${text}」`, "ok");
  window.demo.simulateSpeak(text);
});
document.getElementById("chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("chat-btn").click();
});

// 随机表情 / 随机动作：每隔 8-15s 触发一次（桌宠 idle 行为）
function startIdleBehavior() {
  if (window._idleTimer) clearInterval(window._idleTimer);
  function tick() {
    if (!window.demo.ready || window.demo._talking || window.demo._simulating) return;
    // 50% 概率触发随机表情或随机动作
    if (Math.random() < 0.5) {
      const list = (window.demo.model.internalModel.settings.expressions) || [];
      if (list.length > 0) {
        const e = list[Math.floor(Math.random() * list.length)];
        const name = e.Name || e.name;
        window.demo.setExpression(name);
        log(`[idle] 随机表情: ${name}`);
      }
    } else {
      const motions = window.demo.model.internalModel.settings.motions || {};
      const groups = Object.keys(motions);
      if (groups.length > 0) {
        const g = groups[Math.floor(Math.random() * groups.length)];
        window.demo.playMotion(g);
        log(`[idle] 随机动作: ${g}`);
      }
    }
  }
  window._idleTimer = setInterval(() => {
    if (Math.random() < 0.5) tick();
  }, 8000 + Math.random() * 7000);
  // 启动后 3s 第一次
  setTimeout(tick, 3000);
}
window.startIdleBehavior = startIdleBehavior;

document.getElementById("stage").addEventListener("pointermove", (e) => {
  if (!window.demo.ready) return;
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
    if (st.pixi)     log(`  pixi      ← ${st.pixi.replace(/^ok:/, "")}`);
    if (st.core)     log(`  core      ← ${st.core.replace(/^ok:/, "")}`);
    if (st.cubism4)  log(`  cubism4   ← ${st.cubism4.replace(/^ok:/, "")}`, "ok");

    if (typeof PIXI === "undefined") {
      setStatus("PIXI 未加载", "#ff7675");
      log("PIXI 未加载", "err");
      return;
    }
    if (typeof Live2DCubismCore === "undefined") {
      setStatus("Cubism Core 未加载", "#ff7675");
      log("Live2DCubismCore 未加载", "err");
      return;
    }
    if (!PIXI.live2d || !PIXI.live2d.Live2DModel || typeof PIXI.live2d.Live2DModel.from !== "function") {
      setStatus("Live2DModel.from 不可用", "#ff7675");
      log("pixi-live2d-display 未注册 Live2DModel.from", "err");
      return;
    }

    try {
      window.demo.ensureApp();
    } catch (e) {
      setStatus("PIXI 初始化失败", "#ff7675");
      log(e.message || e, "err");
      return;
    }

    setStatus("就绪 · 请填写 Model URL", "#55efc4");
    log(`PIXI v${PIXI.VERSION} · Live2DModel.from 已就绪`, "ok");
  })();
});
