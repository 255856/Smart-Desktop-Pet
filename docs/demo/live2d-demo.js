"use strict";

/*
 * Desktop Pet · Live2D Demo Renderer
 * ----------------------------------
 * 这是 app/animation/live2d_bridge.html 的独立 Web 版本：
 *   - 去掉 QWebChannel / Python 桥（用纯浏览器 UI 替代）
 *   - 模型 URL 由用户在输入框填写（或拖拽 .model3.json 进来）
 *   - 不预设任何具体模型的指纹（冰糖 / 超频猫猫等），仅隐藏名称含 "watermark" 的通用图层
 *   - Pixi.js / Cubism Core / pixi-live2d-display 走 CDN（这些库是 MIT / Cubism EULA，可重分发）
 *
 * 模型文件 (*.model3.json / *.moc3 / textures/*.png / *.motion3.json …)
 * **不在此仓库**，用户必须自行提供合法授权的模型。
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

// ---------------- Pixi App 单例 ----------------
const demo = {
  app: null,
  model: null,
  expressions: [],
  motionsByGroup: {},
  fitFactor: 0.94,
  ready: false,
  _talking: false,
  _talkParam: "ParamMouthOpenY",
  _keptParams: {},
  _keptParts: {},

  ensureApp() {
    if (this.app) return;
    if (typeof PIXI === "undefined") throw new Error("PIXI 未加载");
    if (!PIXI.live2d || !PIXI.live2d.Live2DModel) throw new Error("pixi-live2d-display cubism4 插件未注册");
    if (typeof Live2DCubismCore === "undefined") throw new Error("Live2DCubismCore 未加载");

    const stage = document.getElementById("stage");
    this.app = new PIXI.Application({
      backgroundAlpha: 0, antialias: true, autoDensity: true,
      resolution: Math.min(window.devicePixelRatio || 1, 1.5),
      autoStart: true,
      width: stage.clientWidth || 800,
      height: stage.clientHeight || 800,
    });
    stage.appendChild(this.app.view);
    if (PIXI.live2d.Live2DModel.registerTicker) {
      PIXI.live2d.Live2DModel.registerTicker(PIXI.Ticker);
    }
    this.app.ticker.maxFPS = 30;
    window.addEventListener("resize", () => this.fit());
  },

  async loadModel(modelUrl) {
    if (!modelUrl) throw new Error("model URL 为空");
    this.shutdown();  // 重新加载前先清掉
    this.ensureApp();

    showLoader("加载模型…");
    setStatus("加载中…", "#ffce5c");

    try {
      log(`加载模型：${modelUrl}`);
      const model = await PIXI.live2d.Live2DModel.from(modelUrl, {
        autoHitTest: false, autoFocus: false,
      });
      this.model = model;
      this.app.stage.addChild(model);

      // beforeModelUpdate 钩子：参数持久 + 口型同步
      const im = model.internalModel;
      im.on("beforeModelUpdate", () => {
        const core = model && model.internalModel.coreModel;
        if (!core) return;
        for (const id in this._keptParams) {
          try { core.setParameterValueById(id, this._keptParams[id]); } catch (e) {}
        }
        for (const id in this._keptParts) {
          try { core.setPartOpacityById(id, this._keptParts[id]); } catch (e) {}
        }
        if (this._talking) {
          const t = performance.now();
          const v = Math.max(0, Math.sin(t / 80)) *
                    (0.3 + 0.6 * Math.abs(Math.sin(t / 620)));
          try { core.setParameterValueById(this._talkParam, v); } catch (e) {}
        }
      });

      // mask 组数兜底（与桌面版一致）
      try {
        const im0 = model.internalModel;
        const core0 = im0.coreModel;
        if (core0.isUsingMasking && core0.isUsingMasking()) {
          const counts = core0.getDrawableMaskCounts();
          const using = counts.reduce((a, c) => a + (c > 0 ? 1 : 0), 0);
          const need = Math.max(1, Math.ceil(using / 32));
          const cur = im0.renderer.getRenderTextureCount ?
                      im0.renderer.getRenderTextureCount() : 1;
          if (need > cur) {
            im0.renderer.initialize(core0, need);
            log(`re-init renderer: mask textures ${cur} → ${need}`, "ok");
          }
        }
      } catch (e) {
        log(`mask 兜底跳过：${e.message || e}`);
      }

      // 隐藏通用 WaterMark 图层（仅按名称匹配，不预设具体模型指纹）
      try {
        const ids = core0 => core0.getDrawableIds();
        const core = model.internalModel.coreModel;
        const drawableIds = ids(core);
        let hidden = 0;
        for (let i = 0; i < drawableIds.length; i++) {
          if (/watermark/i.test(String(drawableIds[i]))) {
            try { core.setPartOpacityById(String(drawableIds[i]), 0); hidden++; } catch (e) {}
          }
        }
        if (hidden > 0) log(`隐藏通用 WaterMark 图层 ${hidden} 个`, "ok");
      } catch (e) {
        log(`watermark 隐藏跳过：${e.message || e}`);
      }

      this.fit();
      this.ready = true;

      // 枚举表情 + 动作
      this.expressions = [];
      try {
        const defs = (model.internalModel.settings.expressions) || [];
        for (const e of defs) {
          const name = e.Name !== undefined ? e.Name : e.name;
          if (name) this.expressions.push(name);
        }
      } catch (e) { log(`枚举表情失败：${e.message || e}`); }

      this.motionsByGroup = {};
      try {
        const defs = (model.internalModel.settings.motions) || {};
        for (const g of Object.keys(defs)) {
          this.motionsByGroup[g] = (defs[g] || []).length;
        }
      } catch (e) { log(`枚举动作失败：${e.message || e}`); }

      this.refreshControls();
      hideLoader();
      setStatus(`就绪 · ${this.expressions.length} 个表情 / ${Object.keys(this.motionsByGroup).length} 组动作`, "#55efc4");
      log("模型加载完成", "ok");
    } catch (err) {
      hideLoader();
      setStatus("加载失败", "#ff7675");
      log(`加载失败：${err.message || err}`, "err");
      throw err;
    }
  },

  fit() {
    if (!this.model || !this.app) return;
    const im = this.model.internalModel;
    const w = this.app.screen.width, h = this.app.screen.height;
    const scale = Math.min(
      w / (im.originalWidth || this.model.width),
      h / (im.originalHeight || this.model.height)
    ) * this.fitFactor;
    this.model.scale.set(scale);
    try { this.model.anchor.set(0.5, 0.5); } catch (e) {}
    this.model.x = w / 2;
    this.model.y = h / 2;
  },

  refreshControls() {
    const ctrl = document.getElementById("controls");
    ctrl.style.display = "flex";

    const mg = document.getElementById("motion-group");
    mg.innerHTML = "";
    for (const g of Object.keys(this.motionsByGroup)) {
      const opt = document.createElement("option");
      opt.value = g; opt.textContent = `${g} (${this.motionsByGroup[g]})`;
      mg.appendChild(opt);
    }

    const exprSel = document.getElementById("expression");
    exprSel.innerHTML = "";
    for (const name of this.expressions) {
      const opt = document.createElement("option");
      opt.value = name; opt.textContent = name;
      exprSel.appendChild(opt);
    }
  },

  playMotion(group) {
    if (!this.model || !this.model.motion) return;
    try { this.model.motion(group); }
    catch (e) { log(`播放动作失败：${e.message || e}`, "err"); }
  },

  setExpression(name) {
    if (!this.model) return;
    try { return this.model.expression(name); }
    catch (e) { log(`设置表情失败：${e.message || e}`, "err"); }
  },

  startTalk() {
    if (!this.model) return;
    this._talking = true;
    log("口型同步开", "ok");
  },
  stopTalk() {
    this._talking = false;
    try {
      const c = this.model && this.model.internalModel.coreModel;
      if (c) c.setParameterValueById(this._talkParam, 0);
    } catch (e) {}
    log("口型同步关");
  },

  shutdown() {
    if (this.model) {
      try { this.app.stage.removeChild(this.model); this.model.destroy(); } catch (e) {}
      this.model = null;
    }
    this._keptParams = {};
    this._keptParts = {};
    this._talking = false;
    this.ready = false;
  },
};

window.demo = demo;

// ---------------- 拖拽支持 ----------------
// 用户拖一个 .model3.json 进来 → 用文件 URL 直接加载（同源，无 CORS）
const dropHint = document.getElementById("drop-hint");
let dragDepth = 0;
window.addEventListener("dragenter", (e) => {
  e.preventDefault();
  dragDepth++;
  dropHint.classList.add("show");
});
window.addEventListener("dragleave", (e) => {
  e.preventDefault();
  dragDepth--;
  if (dragDepth <= 0) { dragDepth = 0; dropHint.classList.remove("show"); }
});
window.addEventListener("dragover", (e) => { e.preventDefault(); });
window.addEventListener("drop", async (e) => {
  e.preventDefault();
  dragDepth = 0;
  dropHint.classList.remove("show");
  const file = e.dataTransfer.files && e.dataTransfer.files[0];
  if (!file) return;
  if (!file.name.endsWith(".model3.json")) {
    log("请拖入 .model3.json 文件", "err");
    return;
  }
  const url = URL.createObjectURL(file);
  document.getElementById("model-url").value = `file://${file.name}（内存中）`;
  await demo.loadModel(url);
});

// ---------------- UI 绑定 ----------------
document.getElementById("load-btn").addEventListener("click", () => {
  const url = document.getElementById("model-url").value.trim();
  if (!url) { log("请填写 Model URL", "err"); return; }
  demo.loadModel(url).catch(() => {});
});
document.getElementById("model-url").addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("load-btn").click();
});
document.getElementById("motion-btn").addEventListener("click", () => {
  const g = document.getElementById("motion-group").value;
  if (g) demo.playMotion(g);
});
document.getElementById("expression-btn").addEventListener("click", () => {
  const n = document.getElementById("expression").value;
  if (n) demo.setExpression(n);
});
document.getElementById("reset-btn").addEventListener("click", () => {
  const url = document.getElementById("model-url").value.trim();
  if (url) demo.loadModel(url).catch(() => {});
});
document.getElementById("speak-btn").addEventListener("click", (e) => {
  if (demo._talking) {
    demo.stopTalk();
    e.target.textContent = "💬 测试口型";
  } else {
    demo.startTalk();
    e.target.textContent = "🛑 停止口型";
  }
});

// 鼠标拖动 / 聚焦
document.getElementById("stage").addEventListener("pointermove", (e) => {
  if (!demo.model || !demo.ready) return;
  const r = demo.app.view.getBoundingClientRect();
  const nx = (e.clientX - r.left) / r.width * 2 - 1;
  const ny = (e.clientY - r.top) / r.height * 2 - 1;
  demo.focus(nx, ny);
});
document.getElementById("stage").addEventListener("pointerdown", (e) => {
  if (!demo.model || !demo.ready) return;
  const r = demo.app.view.getBoundingClientRect();
  demo.startDrag((e.clientX - r.left) / r.width, (e.clientY - r.top) / r.height);
});

// ---------------- 启动 ----------------
window.addEventListener("DOMContentLoaded", () => {
  if (typeof PIXI !== "undefined" && PIXI.live2d && PIXI.live2d.Live2DModel) {
    log(`Pixi v${PIXI.VERSION} · cubism plugin OK`, "ok");
    setStatus("就绪 · 请填写 Model URL", "#55efc4");
  } else {
    setStatus("SDK 加载失败（检查 CDN）", "#ff7675");
    log("Pixi / cubism 插件未加载", "err");
  }
});
