"use strict";

/*
 * Desktop Pet · Live2D Demo
 * ----------------------------------------------------
 * Web 端 SDK：PIXI v7 + 原生 Cubism Core + pixi-live2d-display 0.3.0（真 Live2D 渲染）
 * 复刻桌面版：
 *   - Live2D 模型加载 / 表情 / 动作 / 口型 / 视线
 *   - TTS（浏览器 SpeechSynthesis API + 多语音切换）
 *   - ASR（浏览器 SpeechRecognition API，按住说话）
 *   - Agent 工具调用（mock + 真 Tavily 搜索）
 *   - 长期记忆（localStorage）
 *   - 主动搭话（25-45 分钟模拟 + 时间上下文）
 *   - Trace Dashboard（内嵌 UI 显示工具调用流程）
 */

// ============================================================
// 0. 工具：日志 / 加载状态 / 工具栏
// ============================================================
const logEl = document.getElementById("log");
function log(msg, level = "") {
  const line = document.createElement("div");
  if (level) line.className = level;
  line.textContent = `[${new Date().toLocaleTimeString()}] ${msg}`;
  logEl.appendChild(line);
  logEl.scrollTop = logEl.scrollHeight;
  if (level === "err") console.error(msg); else console.log(msg);
}
// Toast 轻提示（替代只进日志的反馈）
const toastsEl = document.getElementById("toasts");
function toast(msg, kind = "ok", ms = 2600) {
  if (!toastsEl) return;
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  const ico = document.createElement("span");
  ico.className = "t-ico";
  ico.textContent = kind === "err" ? "✕" : "✓";
  const body = document.createElement("span");
  body.textContent = msg;
  el.appendChild(ico); el.appendChild(body);
  toastsEl.appendChild(el);
  setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 450); }, ms);
}
// 用户偏好（对齐桌面版设置项：TTS 静音 / 主动搭话开关）
const settings = {
  autoSpeak: localStorage.getItem("pet_auto_speak") !== "0",
  proactive: localStorage.getItem("pet_proactive") !== "0",
};
function saveSettings() {
  localStorage.setItem("pet_auto_speak", settings.autoSpeak ? "1" : "0");
  localStorage.setItem("pet_proactive", settings.proactive ? "1" : "0");
}
const loader = document.getElementById("loader");
const loaderText = document.getElementById("loader-text");
const statusEl = document.getElementById("status");
function showLoader(txt) { loaderText.textContent = txt || window.t("loading"); loader.classList.remove("hidden"); }
function hideLoader() { loader.classList.add("hidden"); }
// 浅色主题下把旧的深色背景亮色调成可读色，并联动顶栏状态点
const STATUS_COLORS = {"#a8b0c0": "#8f8aa8", "#ffce5c": "#e0a53e", "#55efc4": "#17a673", "#ff7675": "#e0506e"};
function setStatus(t, c = "#a8b0c0") {
  const cc = STATUS_COLORS[c] || c;
  statusEl.textContent = t;
  statusEl.style.color = cc;
  statusEl.classList.toggle("ready", cc === "#17a673");
  statusEl.classList.toggle("err", cc === "#e0506e");
}

// ============================================================
// 1. Live2D 渲染类（保留之前实现）
// ============================================================
class Live2DDemo {
  constructor() {
    this.app = null;
    this.model = null;
    this.ready = false;
    this._talkParam = "ParamMouthOpenY";
    this._talking = false;
    this._mouthTimer = null;
    this._simulating = false;
    this._emotionListeners = [];  // 监听 emotion 变化
    this.fitFactor = 0.7;  // 留 30% 边距（桌面默认 0.94）
  }
  onEmotion(fn) { this._emotionListeners.push(fn); }
  _emitEmotion(emotion) { for (const fn of this._emotionListeners) try { fn(emotion); } catch (e) {} }

  ensureApp() {
    if (this.app) return;
    if (typeof PIXI === "undefined") throw new Error("PIXI 未加载");
    const stage = document.getElementById("stage");
    const w = Math.max(stage.clientWidth, 400);
    const h = Math.max(stage.clientHeight, 400);
    this.app = new PIXI.Application({
      backgroundAlpha: 0, antialias: true, autoDensity: true,
      resolution: Math.min(window.devicePixelRatio || 1, 1.5),
      autoStart: true,
      width: w,
      height: h,
    });
    stage.appendChild(this.app.view);
    // 让 canvas CSS 跟随 stage 容器大小（PIXI Application canvas 是绝对定位元素）
    const canvas = this.app.view;
    canvas.style.position = "absolute";
    canvas.style.top = "0";
    canvas.style.left = "0";
    canvas.style.width = "100%";
    canvas.style.height = "100%";
    canvas.style.display = "block";
    this.app.ticker.maxFPS = 30;
    // resize 监听：画布跟着容器走
    const onResize = () => {
      if (!this.app) return;
      const nw = Math.max(stage.clientWidth, 400);
      const nh = Math.max(stage.clientHeight, 400);
      // 同时改 PIXI 内部 size + canvas style
      if (this.app.renderer.width !== nw || this.app.renderer.height !== nh) {
        this.app.renderer.resize(nw, nh);
      }
      this._fit();
    };
    window.addEventListener("resize", onResize);
    // 多次延迟 fit（确保 CSS 布局完成 + 窗口 resize 后再 fit）
    setTimeout(onResize, 50);
    setTimeout(onResize, 200);
    setTimeout(onResize, 600);
    setTimeout(onResize, 1500);
  }
  async loadModel(modelUrl) {
    if (!modelUrl) throw new Error("model URL 为空");
    // 支持内置官方样例、custom-models.js 私有模型、以及功能面板输入的外部 URL

    this.shutdown();
    this.ensureApp();
    if (PIXI.live2d && PIXI.live2d.Live2DModel && PIXI.live2d.Live2DModel.registerTicker && PIXI.Ticker) {
      PIXI.live2d.Live2DModel.registerTicker(PIXI.Ticker);
    }
    showLoader(window.t("loading_model"));
    setStatus(window.t("status_loading"), "#ffce5c");
    try {
      log(`加载模型：${modelUrl}`);
      const model = await PIXI.live2d.Live2DModel.from(modelUrl, { autoHitTest: false, autoFocus: false });
      this.model = model;
      this.app.stage.addChild(model);
      // mask 兜底
      try {
        const im = model.internalModel;
        const core = im.coreModel;
        if (core.isUsingMasking && core.isUsingMasking()) {
          const counts = core.getDrawableMaskCounts();
          const using = counts.reduce((a, c) => a + (c > 0 ? 1 : 0), 0);
          const need = Math.max(1, Math.ceil(using / 32));
          const cur = im.renderer.getRenderTextureCount ? im.renderer.getRenderTextureCount() : 1;
          if (need > cur) {
            im.renderer.initialize(core, need);
            log(`mask 纹理扩展：${cur} → ${need}`, "ok");
          }
        }
      } catch (e) {}
      // 隐藏通用 WaterMark
      try {
        const ids = model.internalModel.coreModel.getDrawableIds();
        let hidden = 0;
        for (let i = 0; i < ids.length; i++) {
          if (/watermark/i.test(String(ids[i]))) {
            try { model.internalModel.coreModel.setPartOpacityById(String(ids[i]), 0); hidden++; } catch (e) {}
          }
        }
        if (hidden) log(`隐藏通用 WaterMark 图层 ${hidden} 个`, "ok");
      } catch (e) {}
      // 隐藏 VTS 风格「授权说明卡」：读同名 cdi3 的参数显示名，匹配授权文案的参数每帧置 1
      // （该类模型 1 = 显示正常角色 / 0 = 显示说明卡；置值须每帧执行，否则会被参数快照循环重置）
      try {
        const base = String(model.internalModel.settings.moc || "").replace(/\.moc3$/i, "");
        if (base && model.internalModel.settings.url) {
          const cdiUrl = new URL(base + ".cdi3.json", model.internalModel.settings.url).href;
          const cdiResp = await fetch(cdiUrl);
          if (cdiResp.ok) {
            const cdi = await cdiResp.json();
            const LIC_RE = /水印|使用前须知|专属模型|版权|购入方式|盗版|二次传播|按人头|量贩|感谢购入|公皮|优质|按键部分结束/;
            const coreM = model.internalModel.coreModel;
            const licIds = (cdi.Parameters || [])
              .filter(p => LIC_RE.test(String(p.Name || "")))
              .map(p => String(p.Id));
            if (licIds.length) {
              const im = model.internalModel;
              const origUpdate = im.update.bind(im);
              im.update = function (transform, viewport) {
                origUpdate(transform, viewport);
                for (const id of licIds) { try { coreM.setParameterValueById(id, 1); } catch (e) {} }
              };
              log(`隐藏授权说明卡参数 ${licIds.length} 个`, "ok");
            }
          }
        }
      } catch (e) {}
      this._fit();
      // 原始画布尺寸就绪后 _fit 即稳定；少量延迟兜底首帧布局 / 尺寸就绪
      if (this._fitTicker) { try { this.app.ticker.remove(this._fitTicker); } catch (e) {} }
      if (this._fitTimers) this._fitTimers.forEach(clearTimeout);
      this._fitTimers = [50, 200, 600, 1200].map((ms) => setTimeout(() => {
        if (this.model === model) this._fit();
      }, ms));
      this.ready = true;
      const exprs = (model.internalModel && model.internalModel.settings.expressions) || [];
      const motions = (model.internalModel && model.internalModel.settings.motions) || {};
      this._refreshControls();
      hideLoader();
      const llmTag = window.llm.hasKey() ? ` · ${window.llm.cfg.model}` : window.t("mock_tag");
      setStatus(window.t("status_ready", { expr: exprs.length, mot: Object.keys(motions).length, tools: ToolRegistry.names().length, tag: llmTag }), "#55efc4");
      log(`模型加载完成：${exprs.length} 表情 / ${Object.keys(motions).length} 组动作`, "ok");
      if (window.llm.hasKey()) {
        log(`LLM 已配置：${window.llm.status()}`, "ok");
      } else {
        log(`未配置 LLM API，桌宠用 mock 回复（点 LLM 按钮填 key 启用真模型）`, "ok");
      }
      if (window.startIdleBehavior) window.startIdleBehavior();
      this._emitEmotion("neutral");
      return { expressions: exprs, motions };
    } catch (err) {
      hideLoader();
      setStatus(window.t("status_load_failed"), "#ff7675");
      const msg = err.message || String(err);
      log(`模型加载失败：${msg}`, "err");
      if (/Network error|fetch|404|403|Access-Control|CORS/i.test(msg)) {
        log("排查：网络或资源缺失，请检查网络后点击左下角刷新按钮重试", "err");
      }
      throw err;
    }
  }
  // 模型原始画布尺寸（固定，不随 scale / 渲染状态变化）。
  // pixi-live2d-display 0.3 的 model.width 会随渲染状态变化，若每帧用它反算 scale，
  // 在部分模型（如 Miara）上会出现 scale 在 0.129 与 1 之间自激振荡。
  _modelSize() {
    const im = this.model && this.model.internalModel;
    const mw = (im && im.originalWidth) || this.model.width;
    const mh = (im && im.originalHeight) || this.model.height;
    return { mw, mh };
  }
  _fit() {
    if (!this.model || !this.app) return;
    const w = this.app.screen.width, h = this.app.screen.height;
    // fitFactor 0.7 留 30% 边距（桌面默认 0.94，demo 用更大边距让 UI 不被遮挡）
    const { mw, mh } = this._modelSize();
    const scale = Math.min((w / mw) * this.fitFactor, (h / mh) * this.fitFactor);
    this.model.scale.set(scale);
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
      opt.value = g; opt.textContent = `${g} (${motions[g].length})`;
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
    try { this.model.motion(group); } catch (e) { log(`播放动作失败：${e.message || e}`, "err"); }
  }
  setExpression(name) {
    if (!this.model || !name) return;
    try { return this.model.expression(name); } catch (e) { log(`设置表情失败：${e.message || e}`, "err"); }
  }
  // 真实 TTS 口型同步：传入音频时长，每帧更新 mouth 参数
  startTalk(durationMs = 0) {
    if (!this.ready) return;
    this._talking = true;
    if (this._mouthTimer) clearInterval(this._mouthTimer);
    const t0 = performance.now();
    this._mouthTimer = setInterval(() => {
      if (!this.model || !this._talking) return;
      const t = performance.now() - t0;
      const v = Math.max(0, Math.sin(t / 80)) *
                (0.3 + 0.6 * Math.abs(Math.sin(t / 620)));
      try { this.model.internalModel.coreModel.setParameterValueById(this._talkParam, v); } catch (e) {}
    }, 30);
  }
  stopTalk() {
    this._talking = false;
    if (this._mouthTimer) { clearInterval(this._mouthTimer); this._mouthTimer = null; }
    if (this.model) {
      try { this.model.internalModel.coreModel.setParameterValueById(this._talkParam, 0); } catch (e) {}
    }
  }
  // 模拟说话（无 TTS）：逐字驱动 + 随机表情
  simulateSpeak(text) {
    if (this._simulating) return;
    this._simulating = true;
    const chars = Array.from(text);
    const perCharMs = 80;
    this.startTalk();
    let i = 0;
    const t0 = performance.now();
    const tick = () => {
      if (!this.model || !this._simulating) { clearInterval(this._mouthTimer); return; }
      i++;
      if (i % 4 === 0) {
        const list = (this.model.internalModel && this.model.internalModel.settings.expressions) || [];
        if (list.length > 0) {
          const e = list[Math.floor(Math.random() * list.length)];
          const name = e.Name || e.name;
          try { this.model.expression(name); } catch (err) {}
          log(`[speak] 表情 → ${name}`);
        }
      }
      if (i >= chars.length * 8) {
        clearInterval(this._mouthTimer);
        this.stopTalk();
        this._simulating = false;
        log(`[speak] 完成（共 ${chars.length} 字）`, "ok");
      }
    };
    this._mouthTimer = setInterval(tick, perCharMs / 8);
  }
  focus(nx, ny) {
    if (!this.model || !this.ready) return;
    try {
      const { mw, mh } = this._modelSize();
      this.model.focus(mw / 2 + nx * mw / 2, mh / 2 + ny * mh / 2);
    } catch (e) {}
  }
  // 轻微弹跳反馈（摸头 / 情绪切换时）
  pulse() {
    if (!this.model) return;
    const base = this.model.scale.x;
    const t0 = performance.now();
    const tick = () => {
      if (!this.model) return;
      const t = (performance.now() - t0) / 320;
      if (t >= 1) { this.model.scale.set(base); return; }
      this.model.scale.set(base * (1 + 0.06 * Math.sin(t * Math.PI)));
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }
  shutdown() {
    this._talking = false;
    if (this._mouthTimer) { clearInterval(this._mouthTimer); this._mouthTimer = null; }
    if (this._fitTicker) { try { this.app.ticker.remove(this._fitTicker); } catch (e) {} this._fitTicker = null; }
    if (this._fitTimers) { this._fitTimers.forEach(clearTimeout); this._fitTimers = null; }
    if (this.model && this.app) {
      try { this.app.stage.removeChild(this.model); this.model.destroy(); } catch (e) {}
    }
    this.model = null; this.ready = false;
  }
}

window.demo = new Live2DDemo();

// ============================================================
// 2. TTS 引擎（浏览器 SpeechSynthesis 模拟 edge/gptsovits/minimax 切换）
// ============================================================
class TTSBridge {
  constructor() {
    this.synth = window.speechSynthesis;
    this.voices = [];
    this.allVoices = [];
    this.currentEngine = "edge";  // edge / gptsovits / minimax
    this._currentUtter = null;
    this._userVoice = null;   // 用户手动选择（最高优先级）
    this._pinnedVoice = null; // 默认推荐（中文 Xiaoxiao）
    this.refreshVoices();
    if (this.synth) {
      // 用 addEventListener，避免被下拉初始化的同名监听覆盖
      if (this.synth.addEventListener) this.synth.addEventListener("voiceschanged", () => this.refreshVoices());
      // 兜底：部分环境不触发 voiceschanged，轮询直到拿到语音
      let tries = 0;
      this._voicePoll = setInterval(() => {
        this.refreshVoices();
        if ((this.allVoices && this.allVoices.length > 0) || ++tries > 20) clearInterval(this._voicePoll);
      }, 500);
    }
  }
  refreshVoices() {
    if (!this.synth) return;
    const all = this.synth.getVoices() || [];
    this.allVoices = all;
    // 仅保留 zh / en 语音作为常用集，同时 allVoices 保留全部（含 ja、ko 等）
    this.voices = all.filter(v => {
      const l = (v.lang || "").toLowerCase();
      return l.startsWith("zh") || l.startsWith("en");
    });
    // 未手动指定时，默认锁定推荐语音（中文 Xiaoxiao）
    if (!this._pinnedVoice) this._autoPickDefault();
  }
  _autoPickDefault() {
    if (!this.allVoices || this.allVoices.length === 0) return;
    // 优先级：Xiaoxiao（含“小晓”）> 第一个 zh-CN 女声 > 第一个中文语音
    let voice = this.allVoices.find(v => /xiaoxiao|小晓/i.test(v.name));
    if (!voice) {
      voice = this.allVoices.find(v =>
        (v.lang || "").toLowerCase().startsWith("zh-cn") && /female|女/i.test(v.name)
      ) || this.allVoices.find(v => (v.lang || "").toLowerCase().startsWith("zh"));
    }
    if (voice) {
      this._pinnedVoice = voice;
      console.log(`[TTS] 默认锁定语音：${voice.name}（${voice.lang}）`);
    }
  }
  // 按“用户手动选择 > 当前语言默认（中文 Xiaoxiao）> 引擎匹配”解析实际语音，speak 与下拉共用
  pickVoice(wantEn) {
    if (this._userVoice) return this._userVoice;
    if (wantEn) {
      return (this.voices || []).find(v => (v.lang || "").toLowerCase().startsWith("en"))
        || (this.allVoices || []).find(v => (v.lang || "").toLowerCase().startsWith("en"))
        || this._pinnedVoice
        || null;
    }
    if (this._pinnedVoice) return this._pinnedVoice;
    if (this.voices && this.voices.length > 0) {
      const pref = this.currentEngine === "edge" ? ["xiaoxiao", "小晓", "female", "女"] :
                   this.currentEngine === "gptsovits" ? ["yunjian", "云健", "male", "男"] :
                   /* minimax */ ["yating", "云夏", "xiaoxiao", "小晓"];
      return this.voices.find(v => pref.some(p => v.name.toLowerCase().includes(p.toLowerCase())))
        || this.voices[0];
    }
    return null;
  }
  listVoices() {
    return this.voices.map((v, i) => ({
      idx: i, name: v.name, lang: v.lang, local: v.localService
    }));
  }
  listAllVoices() {
    return (this.allVoices || []).map((v, i) => ({
      idx: i, name: v.name, lang: v.lang, local: v.localService
    }));
  }
  setEngine(engine) {
    this.currentEngine = engine;
    log(`TTS 引擎切换：${engine}`, "ok");
    // 不同引擎用不同 voice（仅作为模拟）
    // edge 小晓 = Microsoft Xiaoxiao Online (Natural) - Chinese (Simplified, PRC)
    // edge 云希 = Microsoft Yunxi Online (Natural)
    // edge 云健 = Microsoft Yunjian Online (Natural) - 男声
    // gptsovits = 男声克隆（桌面本地），demo 用任何男声替代
    // minimax = 任意
    this.refreshVoices();
  }
  setVoiceByName(name) {
    if (!name || !this.allVoices) return null;
    const v = this.allVoices.find(x => x.name === name);
    if (v) { this._userVoice = v; this._pinnedVoice = v; log(`TTS 锁定语音：${v.name}（${v.lang}）`, "ok"); }
    return v;
  }
  // 真实 speak：调用 SpeechSynthesis 同步驱动口型（受「自动朗读」开关控制）
  speak(text, onEnd, { force = false } = {}) {
    if (!settings.autoSpeak && !force) {
      log(`TTS 已静音（自动朗读关闭），跳过朗读`);
      onEnd && onEnd();
      return;
    }
    if (!this.synth) { log("浏览器不支持 SpeechSynthesis", "err"); onEnd && onEnd(); return; }
    this.synth.cancel();
    const wantEn = (window.I18N && window.I18N.lang === "en");
    // 刷新语音列表，选一个 voice：用户手动选择 > 当前语言默认（中文 Xiaoxiao）> 引擎匹配
    this.refreshVoices();
    let voice = this.pickVoice(wantEn);
    const utt = new SpeechSynthesisUtterance(text);
    if (voice) {
      // 取当前最新列表里的同名实例，避免持有过期的 SpeechSynthesisVoice
      const live = this.synth.getVoices().find(v => v.name === voice.name) || voice;
      try { utt.voice = live; } catch (_) { /* 个别环境对象失效，改用 utt.lang 让浏览器自选 */ }
    }
    utt.lang = wantEn ? "en-US" : "zh-CN";
    utt.rate = 1.0; utt.pitch = 1.0;
    // 用 TTS 边界事件驱动口型（更精准：TTS 真的在说话时 mouth 开合）
    utt.onstart = () => {
      window.demo.startTalk();
      log(`TTS 开始 (${this.currentEngine}): ${text.slice(0, 30)}${text.length > 30 ? '…' : ''}`);
    };
    utt.onend = () => {
      window.demo.stopTalk();
      log(`TTS 结束`, "ok");
      onEnd && onEnd();
    };
    utt.onerror = (e) => {
      log(`TTS 错误：${e.error || 'unknown'}`, "err");
      window.demo.stopTalk();
      onEnd && onEnd();
    };
    this._currentUtter = utt;
    this.synth.speak(utt);
  }
  stop() {
    if (this.synth) this.synth.cancel();
    window.demo.stopTalk();
  }
}
window.tts = new TTSBridge();

// ============================================================
// 3. ASR 引擎（浏览器 SpeechRecognition 按住说话）
// ============================================================
class ASRBridge {
  constructor() {
    this.recognition = null;
    this.listening = false;
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    this.available = !!SR;
    if (this.available) {
      this.recognition = new SR();
      this.recognition.lang = "zh-CN";
      this.recognition.interimResults = true;
      this.recognition.continuous = false;
    }
  }
  start(onResult, onEnd) {
    if (this.recognition) this.recognition.lang = (window.I18N && window.I18N.lang === "en") ? "en-US" : "zh-CN";
    if (!this.available) {
      log("浏览器不支持 SpeechRecognition（用 Chrome / Edge）", "err");
      onEnd && onEnd();
      return;
    }
    if (this.listening) return;
    this.listening = true;
    let finalText = "";
    this.recognition.onresult = (e) => {
      let interim = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const tr = e.results[i];
        if (tr.isFinal) finalText += tr[0].transcript;
        else interim += tr[0].transcript;
      }
      log(`ASR: ${(finalText + interim).slice(0, 40)}${(finalText + interim).length > 40 ? '…' : ''}`);
      onResult && onResult(finalText + interim, !!finalText);
    };
    this.recognition.onerror = (e) => {
      log(`ASR 错误：${e.error}`, "err");
      this.listening = false;
      onEnd && onEnd();
    };
    this.recognition.onend = () => {
      this.listening = false;
      log(`ASR 结束: "${finalText}"`, "ok");
      onEnd && onEnd();
    };
    this.recognition.start();
    log("ASR 开始（说话）", "ok");
  }
  stop() {
    if (this.recognition && this.listening) this.recognition.stop();
  }
}
window.asr = new ASRBridge();

// ============================================================
// 4. 网页搜索工具（Tavily REST API）
// ============================================================
async function webSearch(query, tavilyKey) {
  if (!tavilyKey) {
    // Mock 结果（无 key 时）
    await new Promise(r => setTimeout(r, 500));
    return [
      { title: window.t("mock_s1", { q: query }), url: "https://example.com/1",
        content: window.t("mock_c1", { q: query }) },
      { title: window.t("mock_s2", { q: query }), url: "https://example.com/2",
        content: window.t("mock_c2", { q: query }) },
    ];
  }
  // 真 Tavily API
  const r = await fetch("https://api.tavily.com/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ api_key: tavilyKey, query, max_results: 5,
                         include_raw_content: true, days: 30 }),
  });
  if (!r.ok) throw new Error(`Tavily ${r.status}: ${await r.text()}`);
  const data = await r.json();
  return (data.results || []).map(x => ({
    title: x.title, url: x.url,
    content: (x.raw_content || x.content || "").slice(0, 400).replace(/\s+/g, " ").trim(),
  }));
}

// ============================================================
// 5. 长期记忆（localStorage mock）
// ============================================================
class MemoryStore {
  constructor() { this.key = "desktop_pet_memory"; this.items = this._load(); }
  _load() {
    try { return JSON.parse(localStorage.getItem(this.key)) || []; }
    catch { return []; }
  }
  _save() { localStorage.setItem(this.key, JSON.stringify(this.items)); }
  remember(content, importance = 5) {
    const item = { content, importance, time: Date.now() };
    this.items.push(item);
    if (this.items.length > 200) this.items = this.items.slice(-200);
    this._save();
    log(`记忆：${content}（重要度 ${importance}）`, "ok");
    return item;
  }
  recent(n = 5) { return this.items.slice(-n); }
  search(query) {
    const q = query.toLowerCase();
    return this.items.filter(x => x.content.toLowerCase().includes(q));
  }
  list() { return this.items.slice(); }
  forget(content) {
    const before = this.items.length;
    this.items = this.items.filter(x => !x.content.includes(content));
    this._save();
    log(`遗忘 ${before - this.items.length} 条`);
    return before - this.items.length;
  }
  removeOne(time) {
    const before = this.items.length;
    this.items = this.items.filter(x => x.time !== time);
    this._save();
    return before - this.items.length;
  }
}
window.memory = new MemoryStore();

// ============================================================
// 6. 提醒（localStorage + 浏览器 Notification）
// ============================================================
class ReminderStore {
  constructor() { this.key = "desktop_pet_reminders"; this.items = this._load(); }
  _load() { try { return JSON.parse(localStorage.getItem(this.key)) || []; } catch { return []; } }
  _save() { localStorage.setItem(this.key, JSON.stringify(this.items)); }
  add(content, minutes) {
    const fireAt = Date.now() + minutes * 60 * 1000;
    const item = { id: Date.now(), content, fireAt };
    this.items.push(item);
    this._save();
    log(`提醒已设：${minutes} 分钟后「${content}」`, "ok");
    return item;
  }
  list() { return this.items.slice(); }
  remove(id) {
    const before = this.items.length;
    this.items = this.items.filter(x => x.id !== id);
    this._save();
    log(`取消 ${before - this.items.length} 个提醒`);
    return before - this.items.length;
  }
  tick() {
    const now = Date.now();
    const fire = this.items.filter(x => x.fireAt <= now);
    for (const r of fire) {
      log(`提醒触发：${r.content}`, "ok");
      window.tts.speak(window.t("rem_tts", { c: r.content }));
      // 浏览器通知（如已授权）
      if ("Notification" in window && Notification.permission === "granted") {
        new Notification(window.t("rem_notify_title"), { body: r.content });
      }
    }
    if (fire.length > 0) {
      this.items = this.items.filter(x => x.fireAt > now);
      this._save();
    }
  }
  start() {
    if (this._timer) return;
    this._timer = setInterval(() => this.tick(), 5000);
  }
}
window.reminders = new ReminderStore();
window.reminders.start();

// 请求通知权限（用户首次启动时）
if ("Notification" in window && Notification.permission === "default") {
  Notification.requestPermission();
}

// ============================================================
// 7. Agent Trace Dashboard（内嵌 UI）
// ============================================================
class TraceDashboard {
  constructor() {
    this.el = document.getElementById("trace-list");
    this.events = [];
  }
  add(type, content) {
    const evt = { type, content, t: new Date().toLocaleTimeString() };
    this.events.push(evt);
    if (this.el) {
      const item = document.createElement("div");
      item.className = `trace-item trace-${type}`;
      const tt = document.createElement("span");
      tt.className = "trace-t"; tt.textContent = evt.t;
      const ty = document.createElement("span");
      ty.className = "trace-type"; ty.textContent = type;
      const ct = document.createElement("span");
      ct.className = "trace-content"; ct.textContent = content;
      item.appendChild(tt); item.appendChild(ty); item.appendChild(ct);
      this.el.appendChild(item);
      this.el.scrollTop = this.el.scrollHeight;
    }
  }
  clear() {
    this.events = [];
    if (this.el) this.el.innerHTML = "";
  }
}
window.trace = new TraceDashboard();

// ============================================================
// 8. LLM 桥接（OpenAI 兼容 API；支持填 key + 切 base_url）
// ============================================================
class LLMBridge {
  constructor() {
    this.cfg = this._loadCfg();
  }
  _loadCfg() {
    try {
      return JSON.parse(localStorage.getItem("llm_cfg") || "{}");
    } catch { return {}; }
  }
  _saveCfg() {
    localStorage.setItem("llm_cfg", JSON.stringify(this.cfg));
  }
  setConfig({ baseUrl, apiKey, model, systemPrompt }) {
    this.cfg = {
      baseUrl: (baseUrl || this.cfg.baseUrl || "https://api.openai.com/v1").replace(/\/+$/, ""),
      apiKey: apiKey || this.cfg.apiKey || "",
      model: model || this.cfg.model || "gpt-4o-mini",
      systemPrompt: systemPrompt || this.cfg.systemPrompt || window.t("llm_default_sys"),
    };
    this._saveCfg();
  }
  hasKey() { return !!(this.cfg.apiKey && this.cfg.apiKey.length > 10); }
  status() { return this.hasKey() ? `${this.cfg.model} @ ${this.cfg.baseUrl}` : window.t("llm_unconfigured"); }

  // OpenAI 兼容 /chat/completions（流式）
  async chatStream(messages, onDelta, onDone, onError) {
    if (!this.hasKey()) {
      onError && onError(new Error(window.t("err_no_key")));
      return;
    }
    try {
      const controller = new AbortController();
      const abortTimer = setTimeout(() => controller.abort(), 60000);
      const resp = await fetch(`${this.cfg.baseUrl}/chat/completions`, {
        method: "POST",
        signal: controller.signal,
        headers: {
          "Content-Type": "application/json",
          "Authorization": `Bearer ${this.cfg.apiKey}`,
        },
        body: JSON.stringify({
          model: this.cfg.model,
          messages: [{ role: "system", content: this.cfg.systemPrompt }, ...messages],
          stream: true,
          temperature: 0.8,
          max_tokens: 200,
        }),
      });
      if (!resp.ok) {
        const errText = await resp.text().catch(() => "");
        throw new Error(`HTTP ${resp.status}: ${errText.slice(0, 200)}`);
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let fullText = "";
      let buf = "";
      let rawBuf = "";          // 未清洗的原始缓冲（用于 finalize）
      let inThink = false;       // 是否在 <think>...</think> 内
      const THINK_OPEN_RE = /<think>/gi;
      const THINK_CLOSE_RE = /<\/think>/gi;
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        const chunk = decoder.decode(value, { stream: true });
        buf += chunk;
        rawBuf += chunk;
        const lines = buf.split("\n");
        buf = lines.pop() || "";
        for (const line of lines) {
          const t = line.trim();
          if (!t || !t.startsWith("data:")) continue;
          const payload = t.slice(5).trim();
          if (payload === "[DONE]") continue;
          try {
            const obj = JSON.parse(payload);
            const choice = obj.choices?.[0] || {};
            const delta = choice.delta || {};
            // 1) reasoning_content / reasoning 字段（DeepSeek-r1 / Ollama qwen-thinking）
            const reasoningChunk = delta.reasoning_content || delta.reasoning || "";
            // 2) content 字段里可能夹杂 <think>...</think>
            let contentChunk = delta.content || "";
            if (contentChunk) {
              // 维护 inThink 状态（流式 chunk 可能被切断在 <think> 中间）
              let cleaned = contentChunk;
              // 处理跨 chunk 的 <think>...</think>
              if (inThink) {
                const closeIdx = cleaned.search(/<\/think>/i);
                if (closeIdx >= 0) {
                  cleaned = cleaned.slice(closeIdx + cleaned.match(/<\/think>/i)[0].length);
                  inThink = false;
                } else {
                  cleaned = "";
                }
              }
              // 剩余 chunk 里出现新的 <think> → 切到丢弃模式
              const openMatch = cleaned.match(/<think>/gi);
              if (openMatch) {
                let dropFrom = -1;
                for (const m of cleaned.matchAll(/<think>/gi)) {
                  dropFrom = m.index;
                  break;
                }
                if (dropFrom >= 0) {
                  cleaned = cleaned.slice(0, dropFrom);
                  inThink = true;
                }
              }
              contentChunk = cleaned;
            }
            // 合并到 rawBuf 供最终清洗
            if (contentChunk || reasoningChunk) {
              fullText += contentChunk;
              // 仅把"真正显示"的内容传给 onDelta（reasoning 一律不显示）
              if (contentChunk) onDelta && onDelta(contentChunk, fullText);
            }
          } catch (e) {}
        }
      }
      // 最终全量清洗（流式 chunk 不剥的多余空白 + 表情标签）
      fullText = sanitizeLLMText(fullText, true);
      clearTimeout(abortTimer);
      onDone && onDone(fullText);
    } catch (e) {
      onError && onError(e.name === "AbortError" ? new Error("请求超时（60s）") : e);
    }
  }

  // OpenAI 兼容 Function Calling 多轮循环（对齐桌面版 AgentLoop 的 ReAct 模式）：
  // 流式读取 → delta 累积 tool_calls → 命中工具则执行并回填 tool 消息进入下一轮；
  // 纯文本回复即最终答案。最多 6 轮，防止无限循环。
  async chatWithTools(messages, tools, hooks = {}) {
    if (!this.hasKey()) throw new Error(window.t("err_no_key"));
    const maxRounds = 6;
    const convo = messages.slice();
    const systemContent = [this.cfg.systemPrompt, hooks.systemExtra].filter(Boolean).join("\n\n");
    for (let round = 1; round <= maxRounds; round++) {
      // 60s 超时兜底：provider 挂住时不让打字动画无限转
      const controller = new AbortController();
      const abortTimer = setTimeout(() => controller.abort(), 60000);
      let content, toolCalls;
      try {
        const resp = await fetch(`${this.cfg.baseUrl}/chat/completions`, {
          method: "POST",
          signal: controller.signal,
          headers: {
            "Content-Type": "application/json",
            "Authorization": `Bearer ${this.cfg.apiKey}`,
          },
          body: JSON.stringify({
            model: this.cfg.model,
            messages: [{ role: "system", content: systemContent }, ...convo],
            tools, tool_choice: "auto",
            stream: true, temperature: 0.8, max_tokens: 800,
          }),
        });
        if (!resp.ok) {
          const errText = await resp.text().catch(() => "");
          const e = new Error(`HTTP ${resp.status}: ${errText.slice(0, 200)}`);
          // 服务商不支持 tools 参数时标记，让上层降级为普通对话
          e.noTools = resp.status === 400 || resp.status === 404 || resp.status === 422 || /tools?|function/i.test(errText);
          throw e;
        }
        ({ content, toolCalls } = await this._readStream(resp, (delta, full) => {
          hooks.onDelta && hooks.onDelta(delta, full);
        }));
      } catch (e) {
        if (e.name === "AbortError") throw new Error("请求超时（60s）");
        throw e;
      } finally {
        clearTimeout(abortTimer);
      }
      if (toolCalls.length > 0) {
        convo.push({
          role: "assistant",
          content: content || null,
          tool_calls: toolCalls.map(tc => ({
            id: tc.id || `call_${round}_${tc.name}`,
            type: "function",
            function: { name: tc.name, arguments: tc.args || "{}" },
          })),
        });
        for (const tc of toolCalls) {
          let parsed = {};
          try { parsed = JSON.parse(tc.args || "{}"); } catch (e) {}
          hooks.onToolCall && hooks.onToolCall(tc.name, parsed);
          let result;
          try { result = await window.tools.call(tc.name, parsed); }
          catch (err) { result = `error: ${err.message || err}`; }
          hooks.onToolResult && hooks.onToolResult(tc.name, result);
          convo.push({ role: "tool", tool_call_id: tc.id || `call_${round}_${tc.name}`, content: String(result).slice(0, 4000) });
        }
        hooks.onRoundEnd && hooks.onRoundEnd(round);
        continue;
      }
      return content || "";
    }
    return "";
  }

  // 读 SSE 流：累积 content 文本 + 按 index 拼装分片到达的 tool_calls
  async _readStream(resp, onDelta) {
    const reader = resp.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buf = "", content = "";
    const toolCalls = [];
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split("\n");
      buf = lines.pop() || "";
      for (const line of lines) {
        const t = line.trim();
        if (!t || !t.startsWith("data:")) continue;
        const payload = t.slice(5).trim();
        if (payload === "[DONE]") continue;
        try {
          const obj = JSON.parse(payload);
          const delta = obj.choices?.[0]?.delta || {};
          if (delta.content) {
            content += delta.content;
            onDelta && onDelta(delta.content, content);
          }
          if (Array.isArray(delta.tool_calls)) {
            for (const tc of delta.tool_calls) {
              const idx = tc.index || 0;
              if (!toolCalls[idx]) toolCalls[idx] = { id: "", name: "", args: "" };
              if (tc.id) toolCalls[idx].id = tc.id;
              if (tc.function && tc.function.name) toolCalls[idx].name += tc.function.name;
              if (tc.function && tc.function.arguments) toolCalls[idx].args += tc.function.arguments;
            }
          }
        } catch (e) {}
      }
    }
    return { content, toolCalls: toolCalls.filter(Boolean) };
  }
}

// LLM 输出清洗：剥离 <think>...</think> 残留、情绪标签、emoji、多余空白
function sanitizeLLMText(text, isFinal = true) {
  if (!text) return text;
  // 1) 残留 <think>...</think>（流式 chunk 边界可能漏掉）
  text = text.replace(/<think>[\s\S]*?<\/think>/gi, "");
  text = text.replace(/<think>[\s\S]*$/gi, "");   // 未闭合的起始标签
  text = text.replace(/<\/think>/gi, "");         // 孤立闭合标签
  // 2) 情绪标签（最终回复不允许出现）
  if (isFinal) {
    text = text.replace(/\[\s*(?:happy|sad|angry|surprised|sleepy|neutral|neutral2|talk|joy|smile|laugh|shy|confuse|shock|worry|anger|disgust|love|fun|bored|excited|thinking|greeting|thinking1|thinking2|thinking3|thinking4)\s*[,\s\]]/gi, " ");
    // 3) emoji + 装饰符号
    text = text.replace(/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{1F000}-\u{1F2FF}]/gu, "");
    text = text.replace(/[✨★☆♥♡♪♫]/g, "");
    // 4) 多余空白
    text = text.replace(/[ \t]+/g, " ").replace(/\n[ \t]+/g, "\n").replace(/\n{3,}/g, "\n\n");
    text = text.trim();
    // 5) 整段中文占比 < 15% → 视为英文 CoT 漏入正文，整体丢弃
    if (text) {
      const cjk = [...text].filter(ch => /[\u4e00-\u9fff\u3040-\u30ff]/.test(ch)).length;
      if (cjk / text.length < 0.15) text = "";
    }
  }
  return text;
}
window.llm = new LLMBridge();

// ============================================================
// 9. 主动搭话（用 LLM API 生成，mock 兜底）
// ============================================================
class ProactiveBrain {
  constructor() {
    this.lastRemarks = [];
    this.minMin = 1;
    this.maxMin = 2;
    this._timer = null;
  }
  start() {
    if (this._timer) return;
    this._schedule();
  }
  stop() {
    if (this._timer) clearTimeout(this._timer);
    this._timer = null;
  }
  _schedule() {
    const delay = (this.minMin + Math.random() * (this.maxMin - this.minMin)) * 60 * 1000;
    log(`ProactiveBrain: 下次主动搭话在 ${Math.round(delay/60000)} 分钟后`);
    this._timer = setTimeout(() => this._fire(), delay);
  }
  _getTimeKey() {
    const h = new Date().getHours();
    if (h < 6) return "tctx_night";
    if (h < 12) return "tctx_morning";
    if (h < 18) return "tctx_afternoon";
    return "tctx_evening";
  }
  async _fire() {
    this._schedule();
    if (!settings.proactive) return;   // 偏好开关：关闭后静默不搭话
    const timeCtx = window.t(this._getTimeKey());
    const memorySample = window.memory.recent(3).map(m => m.content);

    // mock 兜底（无 API key 时）
    const fallback = () => {
      const mem1 = memorySample.length ? window.t("pa1mem", { x: memorySample[0].slice(0, 15) }) : "";
      const mem2 = memorySample.length > 1 ? window.t("pa4mem", { x: memorySample[1].slice(0, 15) }) : "";
      const candidates = [
        window.t("pa1", { t: timeCtx, mem: mem1 }),
        window.t("pa2", { t: timeCtx }),
        window.t("pa3"),
        window.t("pa4", { mem: mem2 }),
        window.t("pa5", { t: timeCtx }),
        window.t("pa6"),
      ];
      const remark = candidates[Math.floor(Math.random() * candidates.length)];
      if (this.lastRemarks.includes(remark)) return;
      this.lastRemarks.push(remark);
      if (this.lastRemarks.length > 3) this.lastRemarks.shift();
      this._show(remark);
    };

    // 真实 LLM（有 API key 时）
    if (window.llm.hasKey()) {
      try {
        const messages = [{
          role: "user",
          content: window.t("pa_llm", { t: timeCtx, mem: memorySample.length ? window.t("pa_llm_mem", { m: memorySample.join("；") }) : "" })
        }];
        const text = await new Promise((resolve, reject) => {
          window.llm.chatStream(
            messages,
            () => {},  // 流式增量暂不处理
            (full) => resolve(full),
            (err) => reject(err)
          );
        });
        const remark = text.trim();
        if (!remark || this.lastRemarks.includes(remark)) return fallback();
        this.lastRemarks.push(remark);
        if (this.lastRemarks.length > 3) this.lastRemarks.shift();
        window.trace.add("proactive", remark);
        return this._show(remark);
      } catch (e) {
        log(`LLM 调用失败：${e.message}（用 mock 兜底）`, "err");
        return fallback();
      }
    }
    fallback();
  }

  _show(remark) {
    log(`ProactiveBrain 主动搭话：${remark}`, "ok");
    window.trace.add("remark", remark);
    const tk = this._getTimeKey();
    const emotion = tk === "tctx_morning" ? "happy" : tk === "tctx_afternoon" ? "neutral" : "sleepy";
    window.demo._emitEmotion(emotion);
    window.tts.speak(remark);
    appendChatBubble("proactive", remark);
  }
}
window.proactive = new ProactiveBrain();
window.proactive.start();

// ============================================================
// 9. 聊天 UI：气泡（头像 + 时间戳） + 打字指示 + 历史持久化
// ============================================================
const chatEl = document.getElementById("chat-area");

class ChatHistory {
  constructor() { this.key = "desktop_pet_chat_history"; this.items = this._load(); }
  _load() { try { return JSON.parse(localStorage.getItem(this.key)) || []; } catch { return []; } }
  _save() { localStorage.setItem(this.key, JSON.stringify(this.items)); }
  add(role, text) {
    this.items.push({ role, text, ts: Date.now() });
    if (this.items.length > 60) this.items = this.items.slice(-60);
    this._save();
  }
  clear() { this.items = []; this._save(); }
  // 喂给 LLM 的最近对话（proactive / assistant 都按 assistant 处理）
  forLLM(n = 10) {
    return this.items.slice(-n).map(m => ({
      role: m.role === "user" ? "user" : "assistant",
      content: m.text,
    }));
  }
}
window.chatHistory = new ChatHistory();

// 非加密 UI 随机数统一走 CSPRNG（crypto.getRandomValues），无 crypto 环境用时间兜底
function randIndex(n) {
  n = Math.max(1, n | 0);
  try {
    if (window.crypto && crypto.getRandomValues) {
      const a = new Uint32Array(1);
      crypto.getRandomValues(a);
      return a[0] % n;
    }
  } catch (e) {}
  return (Date.now() + performance.now()) % n | 0;
}

const AVATARS = { user: "🙂", assistant: "🐾", proactive: "💭" };
function _makeMsg(role) {
  const wrap = document.createElement("div");
  wrap.className = `msg msg-${role}`;
  const av = document.createElement("div");
  av.className = "avatar";
  av.textContent = AVATARS[role] || "🐾";
  const b = document.createElement("div");
  b.className = `bubble bubble-${role}`;
  wrap.appendChild(av); wrap.appendChild(b);
  return { wrap, bubble: b };
}
function _stamp(bubble) {
  const s = document.createElement("span");
  s.className = "b-time";
  s.textContent = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  bubble.appendChild(s);
}
function appendChatBubble(role, text, { restore = false, save = true } = {}) {
  if (!chatEl) return;
  const { wrap, bubble } = _makeMsg(role);
  bubble.textContent = text;
  _stamp(bubble);
  chatEl.appendChild(wrap);
  while (chatEl.children.length > 12) chatEl.removeChild(chatEl.firstChild);
  chatEl.scrollTop = chatEl.scrollHeight;
  if (!restore && save) window.chatHistory.add(role, text);
}
// 打字中指示器（流式回复占位）
let _typingEl = null;
function showTyping() {
  if (!chatEl) return null;
  const { wrap, bubble } = _makeMsg("assistant");
  bubble.classList.add("typing");
  for (let i = 0; i < 3; i++) { const d = document.createElement("span"); d.className = "dot"; bubble.appendChild(d); }
  chatEl.appendChild(wrap);
  chatEl.scrollTop = chatEl.scrollHeight;
  _typingEl = wrap;
  return bubble;
}
function hideTyping() {
  if (_typingEl) { _typingEl.remove(); _typingEl = null; }
}
// 恢复上次对话历史（刷新不丢，对齐桌面版 chat_history.json）
function restoreChatHistory() {
  const items = window.chatHistory.items;
  if (items.length) {
    for (const m of items) appendChatBubble(m.role, m.text, { restore: true });
  } else {
    appendChatBubble("proactive", window.t("welcome_bubble"), { save: false });
  }
}

// ============================================================
// 9.5 工具注册表：与桌面版 app/engine/tools 同名对齐（浏览器沙箱内可用的 18 个）
// ============================================================
const ToolRegistry = {
  defs: new Map(),
  register(name, schema, fn) { this.defs.set(name, { schema, fn }); },
  names() { return [...this.defs.keys()]; },
  schemas() { return [...this.defs.values()].map(d => d.schema); },
  async call(name, args) {
    const d = this.defs.get(name);
    if (!d) throw new Error(`unknown tool: ${name}`);
    return await d.fn(args || {});
  },
};
window.tools = ToolRegistry;

// —— 时间 / 日期（对齐桌面 fmt_time 格式） ——
const WEEK_ZH = ["日", "一", "二", "三", "四", "五", "六"];
function fmtTime(d = new Date()) {
  const p = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())} 星期${WEEK_ZH[d.getDay()]}`;
}
ToolRegistry.register("get_current_time", {
  type: "function",
  function: {
    name: "get_current_time",
    description: "获取当前本地日期时间（含星期）",
    parameters: { type: "object", properties: {} },
  },
}, () => JSON.stringify({ now: fmtTime() }));

ToolRegistry.register("date_info", {
  type: "function",
  function: {
    name: "date_info",
    description: "获取今天的日期详情：年月日、星期、季度、今年第几天、年进度",
    parameters: { type: "object", properties: {} },
  },
}, () => {
  const d = new Date();
  const start = new Date(d.getFullYear(), 0, 0);
  const dayOfYear = Math.floor((d - start) / 86400000);
  const total = (new Date(d.getFullYear(), 11, 31) - start) / 86400000;
  return JSON.stringify({
    today: `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`,
    weekday: `星期${WEEK_ZH[d.getDay()]}`,
    quarter: Math.floor(d.getMonth() / 3) + 1,
    day_of_year: dayOfYear,
    year_progress: `${(dayOfYear / total * 100).toFixed(1)}%`,
  });
});

// —— 计算 / 单位换算 ——
// 安全表达式求值：手写递归下降解析器，不使用 eval / new Function，
// 只接受数字、四则运算、括号与白名单函数，杜绝代码注入。
function safeCalc(input) {
  const s = String(input).replace(/×/g, "*").replace(/÷/g, "/").replace(/π/g, "PI").replace(/\s+/g, "");
  const FN = {
    sqrt: Math.sqrt, sin: Math.sin, cos: Math.cos, tan: Math.tan,
    log: Math.log10, ln: Math.log, abs: Math.abs, round: Math.round,
    floor: Math.floor, ceil: Math.ceil, min: Math.min, max: Math.max,
  };
  let i = 0;
  const peek = () => s[i];
  const eat = ch => { if (s[i] === ch) { i++; return true; } return false; };
  function parseExpr() {
    let v = parseTerm();
    while (peek() === "+" || peek() === "-") { const op = s[i++]; const r = parseTerm(); v = op === "+" ? v + r : v - r; }
    return v;
  }
  function parseTerm() {
    let v = parsePow();
    while (peek() === "*" || peek() === "/" || peek() === "%") {
      const op = s[i++]; const r = parsePow();
      if ((op === "/" || op === "%") && r === 0) throw new Error("除零");
      v = op === "*" ? v * r : op === "/" ? v / r : v % r;
    }
    return v;
  }
  function parsePow() {
    const base = parseUnary();
    if (peek() === "^") { i++; return Math.pow(base, parsePow()); }
    return base;
  }
  function parseUnary() {
    if (eat("-")) return -parseUnary();
    if (eat("+")) return parseUnary();
    return parseAtom();
  }
  function parseAtom() {
    if (eat("(")) { const v = parseExpr(); if (!eat(")")) throw new Error("括号不匹配"); return v; }
    const idm = /^[a-zA-Z]+/.exec(s.slice(i));
    if (idm) {
      const name = idm[0].toLowerCase();
      if (name === "pi") { i += idm[0].length; return Math.PI; }
      if (name === "e") { i += idm[0].length; return Math.E; }
      const fn = FN[name];
      if (!fn) throw new Error(`未知函数 ${name}`);
      i += idm[0].length;
      let args = [];
      if (eat("(")) {
        args.push(parseExpr());
        while (eat(",")) args.push(parseExpr());
        if (!eat(")")) throw new Error("括号不匹配");
      } else {
        args.push(parseAtom());   // 允许 sqrt 9 这类写法
      }
      return fn(...args);
    }
    const nm = /^\d+(\.\d+)?/.exec(s.slice(i));
    if (!nm) throw new Error(`位置 ${i} 无法解析`);
    i += nm[0].length;
    return parseFloat(nm[0]);
  }
  const v = parseExpr();
  if (i < s.length) throw new Error(`多余字符 "${s.slice(i, i + 8)}"`);
  return v;
}
ToolRegistry.register("calculate", {
  type: "function",
  function: {
    name: "calculate",
    description: "数学计算。支持 + - * / % ^ 括号、sqrt sin cos tan log ln abs round floor ceil min max、pi、e",
    parameters: {
      type: "object",
      properties: { expression: { type: "string", description: "算式，如 (3+4)*2/7" } },
      required: ["expression"],
    },
  },
}, ({ expression }) => {
  if (!String(expression || "").trim()) return "错误：算式为空";
  try {
    const r = safeCalc(expression);
    if (typeof r !== "number" || !isFinite(r)) return "错误：结果不是有限数";
    return JSON.stringify({ expression: String(expression), result: Math.round(r * 1e10) / 1e10 });
  } catch (err) {
    return `错误：无法计算（${err.message}）`;
  }
});

const UNITS = {
  length: { m: 1, km: 1000, cm: 0.01, mm: 0.001, mile: 1609.344, ft: 0.3048, inch: 0.0254 },
  weight: { kg: 1, g: 0.001, t: 1000, lb: 0.45359237, oz: 0.028349523 },
  data: { B: 1, KB: 1024, MB: 1048576, GB: 1073741824, TB: 1099511627776 },
  speed: { "km/h": 1, "m/s": 3.6, mph: 1.609344, knot: 1.852 },
};
ToolRegistry.register("convert_units", {
  type: "function",
  function: {
    name: "convert_units",
    description: "单位换算：长度(m km cm mm mile ft inch)、重量(kg g t lb oz)、数据(B KB MB GB TB)、速度(km/h m/s mph knot)、温度(c f k)",
    parameters: {
      type: "object",
      properties: {
        value: { type: "number", description: "数值" },
        from: { type: "string", description: "源单位" },
        to: { type: "string", description: "目标单位" },
      },
      required: ["value", "from", "to"],
    },
  },
}, ({ value, from, to }) => {
  from = String(from || "").trim(); to = String(to || "").trim();
  const f = from.toLowerCase(), t2 = to.toLowerCase();
  if (["c", "°c", "f", "°f", "k"].includes(f) || ["c", "°c", "f", "°f", "k"].includes(t2)) {
    const v = Number(value);
    if (!isFinite(v)) return "错误：数值无效";
    let c = f[0] === "f" ? (v - 32) * 5 / 9 : f === "k" ? v - 273.15 : v;
    const r = t2[0] === "f" ? c * 9 / 5 + 32 : t2 === "k" ? c + 273.15 : c;
    const label = u => u === "k" ? "K" : `°${u[0].toUpperCase()}`;
    return JSON.stringify({ from: `${value}${label(f)}`, to: `${Math.round(r * 100) / 100}${label(t2)}` });
  }
  for (const cat of Object.values(UNITS)) {
    const fk = Object.keys(cat).find(k => k.toLowerCase() === f);
    const tk = Object.keys(cat).find(k => k.toLowerCase() === t2);
    if (fk && tk) return JSON.stringify({ from: `${value} ${fk}`, to: `${Math.round(value * cat[fk] / cat[tk] * 1e6) / 1e6} ${tk}` });
  }
  return `错误：不支持的单位 ${from} → ${to}`;
});

// —— 天气（open-meteo，与桌面版同源、无需 key） ——
const WMO = {
  0: ["晴", "Clear sky"], 1: ["大致晴", "Mainly clear"], 2: ["局部多云", "Partly cloudy"], 3: ["阴", "Overcast"],
  45: ["雾", "Fog"], 48: ["雾凇", "Rime fog"],
  51: ["小毛雨", "Light drizzle"], 53: ["毛雨", "Drizzle"], 55: ["大毛雨", "Dense drizzle"],
  61: ["小雨", "Light rain"], 63: ["中雨", "Rain"], 65: ["大雨", "Heavy rain"],
  71: ["小雪", "Light snow"], 73: ["中雪", "Snow"], 75: ["大雪", "Heavy snow"], 77: ["雪粒", "Snow grains"],
  80: ["阵雨", "Rain showers"], 81: ["强阵雨", "Heavy showers"], 82: ["暴雨", "Violent showers"],
  85: ["阵雪", "Snow showers"], 86: ["强阵雪", "Heavy snow showers"],
  95: ["雷暴", "Thunderstorm"], 96: ["雷暴伴冰雹", "Thunderstorm + hail"], 99: ["强雷暴伴冰雹", "Severe thunderstorm + hail"],
};
const DEFAULT_LOCATION = { lat: 39.9042, lon: 116.4074, name: "北京" };
async function _geoCode(location) {
  const r = await fetch(`https://geocoding-api.open-meteo.com/v1/search?name=${encodeURIComponent(location)}&count=1&language=zh&format=json`);
  if (!r.ok) throw new Error(`geocoding HTTP ${r.status}`);
  const j = await r.json();
  if (!j.results || !j.results.length) throw new Error(`未找到城市: ${location}`);
  const g = j.results[0];
  return { lat: g.latitude, lon: g.longitude, name: g.name || location };
}
ToolRegistry.register("get_weather", {
  type: "function",
  function: {
    name: "get_weather",
    description: "查询城市当前天气与预报（默认北京）。返回温度、体感、天气现象、风速",
    parameters: {
      type: "object",
      properties: {
        location: { type: "string", description: "城市名，如 北京 / Shanghai" },
        days: { type: "number", description: "预报天数 1-7，默认 1" },
      },
    },
  },
}, async ({ location, days }) => {
  const n = Math.min(Math.max(parseInt(days) || 1, 1), 7);
  let loc = DEFAULT_LOCATION;
  if (location) { try { loc = await _geoCode(location); } catch (e) { loc = { ...DEFAULT_LOCATION, name: `${location}(未找到，回退默认)` }; } }
  const url = `https://api.open-meteo.com/v1/forecast?latitude=${loc.lat}&longitude=${loc.lon}` +
    `&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m,relative_humidity_2m` +
    (n > 1 ? `&daily=weather_code,temperature_2m_max,temperature_2m_min&forecast_days=${n}` : "") +
    `&timezone=auto`;
  const zh = !(window.I18N && window.I18N.lang === "en");
  const r = await fetch(url);
  if (!r.ok) return `错误：天气查询失败 HTTP ${r.status}`;
  const j = await r.json();
  const c = j.current || {};
  const desc = (WMO[c.weather_code] || ["未知", "Unknown"])[zh ? 0 : 1];
  const lines = [`${loc.name} 当前天气：${desc}，${c.temperature_2m}°C（体感 ${c.apparent_temperature}°C），风速 ${c.wind_speed_10m} km/h，湿度 ${c.relative_humidity_2m}%`];
  if (j.daily) {
    for (let i = 0; i < j.daily.time.length; i++) {
      const d2 = (WMO[j.daily.weather_code[i]] || ["未知", "Unknown"])[zh ? 0 : 1];
      lines.push(`${j.daily.time[i]}: ${d2}，${j.daily.temperature_2m_min[i]}~${j.daily.temperature_2m_max[i]}°C`);
    }
  }
  return lines.join("\n");
});

// —— 网页搜索（Tavily，无 key 用 mock） ——
ToolRegistry.register("web_search", {
  type: "function",
  function: {
    name: "web_search",
    description: "联网搜索。返回多条网页结果的标题与摘要",
    parameters: {
      type: "object",
      properties: { query: { type: "string", description: "搜索关键词" } },
      required: ["query"],
    },
  },
}, async ({ query }) => {
  const tavilyKey = localStorage.getItem("tavily_api_key") || "";
  const results = await webSearch(String(query || ""), tavilyKey);
  if (!results.length) return "没有搜索到结果";
  return results.map((r, i) => `${i + 1}. ${r.title}\n   ${r.content.slice(0, 200)}`).join("\n");
});

// —— 记忆（localStorage，对齐桌面 remember/recall/forget） ——
ToolRegistry.register("remember_fact", {
  type: "function",
  function: {
    name: "remember_fact",
    description: "把关于主人的一条事实存入长期记忆",
    parameters: {
      type: "object",
      properties: {
        content: { type: "string", description: "要记住的内容" },
        importance: { type: "number", description: "重要度 1-10，默认 7" },
      },
      required: ["content"],
    },
  },
}, ({ content, importance }) => {
  window.memory.remember(String(content), Math.min(Math.max(parseInt(importance) || 7, 1), 10));
  return `已记住：${content}`;
});
ToolRegistry.register("recall_memory", {
  type: "function",
  function: {
    name: "recall_memory",
    description: "检索长期记忆。带 query 按关键词搜索，不带则返回最近记忆",
    parameters: {
      type: "object",
      properties: { query: { type: "string", description: "关键词（可选）" } },
    },
  },
}, ({ query }) => {
  const items = query ? window.memory.search(String(query)) : window.memory.recent(10);
  if (!items.length) return "（记忆为空或没有匹配）";
  return items.map(m => `[${new Date(m.time).toLocaleString()}] ${m.content}`).join("\n");
});
ToolRegistry.register("forget_memory", {
  type: "function",
  function: {
    name: "forget_memory",
    description: "遗忘包含关键词的记忆",
    parameters: {
      type: "object",
      properties: { keyword: { type: "string", description: "关键词" } },
      required: ["keyword"],
    },
  },
}, ({ keyword }) => {
  const n = window.memory.forget(String(keyword || ""));
  return `已遗忘 ${n} 条包含「${keyword}」的记忆`;
});

// —— 提醒 ——
ToolRegistry.register("add_reminder", {
  type: "function",
  function: {
    name: "add_reminder",
    description: "新建一个 N 分钟后的提醒",
    parameters: {
      type: "object",
      properties: {
        content: { type: "string", description: "提醒内容" },
        minutes: { type: "number", description: "多少分钟后提醒" },
      },
      required: ["content", "minutes"],
    },
  },
}, ({ content, minutes }) => {
  const m = Number(minutes);
  if (!isFinite(m) || m <= 0) return "错误：minutes 必须是正数";
  const item = window.reminders.add(String(content || window.t("remind_default")), m);
  return `已设置：${new Date(item.fireAt).toLocaleString()} 提醒「${item.content}」`;
});
ToolRegistry.register("list_reminders", {
  type: "function",
  function: {
    name: "list_reminders",
    description: "列出所有待触发提醒（含 id）",
    parameters: { type: "object", properties: {} },
  },
}, () => {
  const items = window.reminders.list();
  if (!items.length) return "（暂无提醒）";
  return items.map(r => `#${r.id} ${new Date(r.fireAt).toLocaleString()} ${r.content}`).join("\n");
});
ToolRegistry.register("delete_reminder", {
  type: "function",
  function: {
    name: "delete_reminder",
    description: "按 id 删除一个提醒（id 从 list_reminders 获取）",
    parameters: {
      type: "object",
      properties: { id: { type: "number", description: "提醒 id" } },
      required: ["id"],
    },
  },
}, ({ id }) => {
  const n = window.reminders.remove(Number(id));
  return n ? "已删除" : `错误：未找到 id=${id} 的提醒`;
});

// —— 打开网页 / 剪贴板 / 通知 ——
ToolRegistry.register("open_website", {
  type: "function",
  function: {
    name: "open_website",
    description: "在浏览器新标签页打开一个 http(s) 网址",
    parameters: {
      type: "object",
      properties: { url: { type: "string", description: "完整网址，https:// 开头" } },
      required: ["url"],
    },
  },
}, ({ url }) => {
  const u = String(url || "").trim();
  if (!/^https?:\/\//i.test(u)) return `错误：仅支持 http(s) URL，收到 ${u.slice(0, 60)}`;
  const w = window.open(u, "_blank", "noopener");
  return w ? `已打开 ${u}` : `浏览器拦截了弹窗，请允许弹窗后重试：${u}`;
});
ToolRegistry.register("clipboard_copy", {
  type: "function",
  function: {
    name: "clipboard_copy",
    description: "把文本复制到剪贴板",
    parameters: {
      type: "object",
      properties: { text: { type: "string", description: "要复制的文本" } },
      required: ["text"],
    },
  },
}, async ({ text }) => {
  try {
    await navigator.clipboard.writeText(String(text || ""));
    return "已复制到剪贴板";
  } catch (e) {
    return `错误：剪贴板写入失败（${e.message || e}）`;
  }
});
ToolRegistry.register("send_notification", {
  type: "function",
  function: {
    name: "send_notification",
    description: "发送一条浏览器系统通知",
    parameters: {
      type: "object",
      properties: {
        title: { type: "string", description: "通知标题" },
        body: { type: "string", description: "通知正文" },
      },
    },
  },
}, async ({ title, body }) => {
  if (!("Notification" in window)) return "错误：浏览器不支持 Notification";
  let perm = Notification.permission;
  if (perm === "default") { try { perm = await Notification.requestPermission(); } catch (e) {} }
  if (perm !== "granted") return "错误：通知权限未授权";
  new Notification(title || window.t("rem_notify_title"), { body: body || "" });
  return "通知已发送";
});

// —— 宠物表情 / 动作 / 状态（对齐桌面 change_pet_emotion / play_animation / get_pet_status） ——
function applyEmotion(em) {
  em = String(em || "neutral").toLowerCase().replace(/[^a-z0-9_]/g, "");
  const im = window.demo.model && window.demo.model.internalModel;
  const list = (im && im.settings && im.settings.expressions) || [];
  const match = list.find(e => String(e.Name || e.name || "").toLowerCase() === em);
  if (match) {
    window.demo.setExpression(match.Name || match.name);
    return `已切换表情: ${match.Name || match.name}`;
  }
  if (list.length && em !== "neutral") {
    const pick = list[randIndex(list.length)];
    window.demo.setExpression(pick.Name || pick.name);
    return `模型无「${em}」表情，已随机切换: ${pick.Name || pick.name}`;
  }
  window.demo._emitEmotion(em);
  window.demo.pulse();
  return `已应用情绪: ${em}`;
}
ToolRegistry.register("change_pet_emotion", {
  type: "function",
  function: {
    name: "change_pet_emotion",
    description: "切换宠物情绪/表情（happy sad angry surprised sleepy neutral 等，取决于模型支持）",
    parameters: {
      type: "object",
      properties: { emotion: { type: "string", description: "情绪名" } },
      required: ["emotion"],
    },
  },
}, ({ emotion }) => applyEmotion(emotion));
ToolRegistry.register("play_animation", {
  type: "function",
  function: {
    name: "play_animation",
    description: "播放模型动作。不传 group 则随机选一个动作组",
    parameters: {
      type: "object",
      properties: { group: { type: "string", description: "动作组名（可选）" } },
    },
  },
}, ({ group }) => {
  const motions = (window.demo.model && window.demo.model.internalModel.settings.motions) || {};
  const groups = Object.keys(motions);
  if (!groups.length) return "模型没有动作组";
  const g = group && motions[group] ? group : groups[randIndex(groups.length)];
  window.demo.playMotion(g);
  return `已播放动作组: ${g}`;
});
ToolRegistry.register("get_pet_status", {
  type: "function",
  function: {
    name: "get_pet_status",
    description: "查询宠物状态：当前模型、亲密度、设置",
    parameters: { type: "object", properties: {} },
  },
}, () => JSON.stringify({
  model: (window.demo.model && window.demo.model.internalModel.settings.name) || "unknown",
  affection: getAffection(),
  auto_speak: settings.autoSpeak,
  proactive: settings.proactive,
}));

// —— 亲密度（摸头 / 聊天累积，localStorage 持久化） ——
function getAffection() { return parseInt(localStorage.getItem("pet_affection")) || 0; }
function addAffection(n = 1) {
  const v = Math.min(getAffection() + n, 9999);
  localStorage.setItem("pet_affection", String(v));
  const el = document.getElementById("affection");
  if (el) el.textContent = `❤ ${v}`;
  return v;
}

// 摸宠爱心特效（位置/字号用 CSPRNG 抖动）
function spawnHearts(x, y, count = 5) {
  const layer = document.getElementById("fx-layer");
  if (!layer) return;
  for (let i = 0; i < count; i++) {
    const h = document.createElement("span");
    h.className = "heart-fx";
    h.textContent = ["❤", "💕", "✨"][randIndex(3)];
    h.style.left = `${x + randIndex(45) - 22}px`;
    h.style.top = `${y + randIndex(21) - 10}px`;
    h.style.setProperty("--dx", `${randIndex(51) - 25}px`);
    h.style.setProperty("--rot", `${randIndex(41) - 20}deg`);
    h.style.animationDelay = `${i * 90}ms`;
    h.style.fontSize = `${13 + randIndex(10)}px`;
    layer.appendChild(h);
    setTimeout(() => h.remove(), 1600);
  }
}

// ============================================================
// 10. Agent 流程：LLM Function Calling（ReAct） + 无 key 正则路由
// ============================================================
const TOOL_GUIDE = [
  "你可以调用工具完成任务，规则：",
  "1) 需要实时信息（时间/日期/天气/搜索/记忆/提醒）时先调用工具，拿到结果后再回答；",
  "2) 计算类问题用 calculate，单位换算用 convert_units；",
  "3) 让你打开网页用 open_website，复制文本用 clipboard_copy，系统通知用 send_notification；",
  "4) 让你记住事情用 remember_fact，查记忆用 recall_memory；",
  "5) 适当时可用 change_pet_emotion / play_animation 表达情绪；",
  "6) 不需要工具就直接回答；不要向用户暴露工具名和 JSON。",
  "回答用用户的语言，口语化、简短（60 字以内），像真人在聊天。",
].join("\n");

// LLM 输出中的情绪标签（[happy] 等）→ 移出正文并应用到模型表情
const EMOTION_TAGS = "happy|sad|angry|surprised|sleepy|neutral|talk|joy|smile|laugh|shy|confuse|shock|worry|anger|disgust|love|fun|bored|excited|thinking|greeting";
function extractEmotion(raw) {
  const m = String(raw || "").match(new RegExp(`\\[\\s*(${EMOTION_TAGS})\\s*\\]`, "i"));
  return { emotion: m ? m[1].toLowerCase() : null, text: m ? String(raw).replace(m[0], " ") : raw };
}

function toolTraceArgs(args) {
  try { return JSON.stringify(args).slice(0, 90); } catch (e) { return ""; }
}

async function handleUserInput(text) {
  if (!text || !window.demo.ready) {
    if (!window.demo.ready) log("请先加载模型", "err");
    return;
  }
  appendChatBubble("user", text);
  window.trace.clear();
  window.trace.add("user", text);
  addAffection(1);
  const txt = text.trim();

  // ---------- 无 LLM key：正则意图路由（mock 兜底，保持零配置可玩） ----------
  if (!window.llm.hasKey()) return handleMockIntent(txt);

  // ---------- LLM Function Calling（ReAct 多轮工具调用） ----------
  window.trace.add("llm_call", `agent("${txt.slice(0, 30)}${txt.length > 30 ? "…" : ""}")`);
  const memoryCtx = window.memory.recent(3).map(m => m.content).join("；");
  const messages = [
    ...(memoryCtx ? [{ role: "system", content: window.t("memory_ctx", { m: memoryCtx }) }] : []),
    ...window.chatHistory.forLLM(10).slice(0, -1),  // 末条刚 add 过（= 本条用户消息），去重
    { role: "user", content: txt },
  ];

  // 流式展示气泡
  const streamBubble = showTyping();
  let streamStarted = false;

  const finalize = (raw) => {
    hideTyping();
    const { emotion, text: noTag } = extractEmotion(raw);
    const cleaned = sanitizeLLMText(noTag, true) || window.t("model_empty");
    if (emotion) { try { applyEmotion(emotion); } catch (e) {} }
    window.trace.add("llm_response", cleaned);
    appendChatBubble("assistant", cleaned);
    window.tts.speak(cleaned);
  };

  try {
    const content = await window.llm.chatWithTools(messages, ToolRegistry.schemas(), {
      systemExtra: TOOL_GUIDE,
      onDelta: (delta, full) => {
        if (!streamStarted && streamBubble) { streamBubble.classList.remove("typing"); streamBubble.textContent = ""; streamStarted = true; }
        if (streamBubble) {
          streamBubble.textContent = sanitizeLLMText(full, false);
          chatEl.scrollTop = chatEl.scrollHeight;
        }
      },
      onToolCall: (name, args) => window.trace.add("tool_call", `${name}(${toolTraceArgs(args)})`),
      onToolResult: (name, result) => window.trace.add("tool_result", `${name} → ${String(result).slice(0, 70)}`),
      onRoundEnd: () => {
        // 工具轮展示的中间文本清空，为最终回复腾位
        if (streamBubble) {
          while (streamBubble.firstChild) streamBubble.removeChild(streamBubble.firstChild);
          streamBubble.classList.add("typing");
          for (let i = 0; i < 3; i++) { const d = document.createElement("span"); d.className = "dot"; streamBubble.appendChild(d); }
        }
        streamStarted = false;
      },
    });
    finalize(content);
  } catch (err) {
    if (err.noTools) {
      // 服务商不支持 tools 参数 → 降级为普通流式对话
      log(`服务商不支持 Function Calling，降级为普通对话（${String(err.message).slice(0, 60)}）`, "err");
      hideTyping();
      await plainChatStream(messages, txt);
      return;
    }
    hideTyping();
    const bubble = _makeMsg("assistant");
    bubble.bubble.textContent = window.t("err_prefix") + err.message;
    _stamp(bubble.bubble);
    chatEl.appendChild(bubble.wrap);
    chatEl.scrollTop = chatEl.scrollHeight;
    window.trace.add("error", err.message);
    log(`LLM 调用失败：${err.message}`, "err");
  }
}

// 普通流式对话（无 tools 参数的降级路径）
async function plainChatStream(messages) {
  const bubble = showTyping();
  try {
    await window.llm.chatStream(
      messages,
      (delta, full) => {
        if (bubble) { bubble.classList.remove("typing"); bubble.textContent = sanitizeLLMText(full, false); chatEl.scrollTop = chatEl.scrollHeight; }
      },
      (full) => {
        hideTyping();
        const reply = (sanitizeLLMText(full, true) || window.t("model_empty")).trim();
        window.trace.add("llm_response", reply);
        appendChatBubble("assistant", reply);
        window.tts.speak(reply);
      },
      (err) => {
        hideTyping();
        const b = _makeMsg("assistant");
        b.bubble.textContent = window.t("err_prefix") + err.message;
        _stamp(b.bubble);
        chatEl.appendChild(b.wrap);
        log(`LLM 调用失败：${err.message}`, "err");
      }
    );
  } catch (e) {
    hideTyping();
    log(`LLM 异常：${e.message}`, "err");
  }
}

// 无 key 时的正则意图路由（mock）
async function handleMockIntent(txt) {
  // 1. 提醒类
  const remindMatch = txt.match(/(\d+)\s*(分钟|秒钟|秒|小时|minutes?|mins?|hours?|seconds?|secs?)\s*(?:之后?|后)?\s*(?:提醒(?:我)?|叫我|告诉我|喊我|记得|remind\s*me(?:\s*to)?)\s*(.*)/i);
  if (remindMatch) {
    const n = parseInt(remindMatch[1]);
    const unitRaw = remindMatch[2].toLowerCase();
    const isSec = /秒|sec/.test(unitRaw);
    const isHour = /小时|hour|^h$/.test(unitRaw);
    const minutes = isSec ? n / 60 : isHour ? n * 60 : n;
    const unitLabel = isSec ? window.t("unit_sec") : isHour ? window.t("unit_hour") : window.t("unit_min");
    const content = (remindMatch[3] || "").replace(/^[\s:：,，to]+/i, "").trim() || window.t("remind_default");
    window.trace.add("tool_call", `add_reminder(${toolTraceArgs({ content, minutes })})`);
    window.reminders.add(content, minutes);
    window.trace.add("tool_result", "ok");
    const reply = window.t("remind_confirm", { n, unit: unitLabel, content });
    appendChatBubble("assistant", reply);
    window.tts.speak(reply);
    applyEmotion("happy");
    return;
  }
  // 2. 天气（先于搜索判断，避免“查一下北京天气”被搜索意图吞掉）
  if (/天气|weather|气温|temperature/i.test(txt)) {
    const m2 = txt.match(/(?:在|查一下|查询|看看)?\s*([\u4e00-\u9fa5]{2,7}|[A-Za-z]+(?:\s[A-Za-z]+)?)\s*(?:的)?(?:天气|weather)/i)
      || txt.match(/(?:weather(?: in)?|temperature in)\s+([A-Za-z\s]+)$/i);
    const loc = m2 ? m2[1].trim() : "";
    window.trace.add("tool_call", `get_weather(location="${loc || "北京"}")`);
    const r = await ToolRegistry.call("get_weather", { location: loc });
    window.trace.add("tool_result", String(r).split("\n")[0].slice(0, 70));
    appendChatBubble("assistant", r);
    window.tts.speak(String(r).split("\n")[0]);
    return;
  }
  // 3. 搜索类
  if (/搜索|查一下|查查|看看|搜一下|帮我找|search|look up|find (?:me |about )/i.test(txt)) {
    window.trace.add("tool_call", `web_search(query="${txt.slice(0, 40)}")`);
    const tavilyKey = localStorage.getItem("tavily_api_key") || "";
    try {
      const results = await webSearch(txt, tavilyKey);
      window.trace.add("tool_result", window.t("search_count", { n: results.length }));
      const summary = results.slice(0, 3).map((r, i) =>
        `${i+1}. ${r.title}\n   ${r.content.slice(0, 100)}${r.content.length > 100 ? '…' : ''}`
      ).join("\n\n");
      const reply = window.t("search_reply", { n: results.length, summary });
      appendChatBubble("assistant", reply);
      window.tts.speak(window.t("search_tts", { n: results.length, title: results[0].title }));
      applyEmotion("surprised");
      return;
    } catch (e) {
      log(`搜索失败：${e.message}`, "err");
    }
  }
  // 3. 记忆类
  const memMatch = txt.match(/(?:记住|记一下|别忘了|记着|记下|remember that|remember|don't forget|note that)\s*[:：，,]?\s*(.+)/i);
  if (memMatch) {
    const mc = memMatch[1].trim();
    window.trace.add("tool_call", `remember_fact(${toolTraceArgs({ content: mc })})`);
    window.memory.remember(mc, 7);
    window.trace.add("tool_result", "ok");
    const reply = window.t("mem_confirm", { c: mc });
    appendChatBubble("assistant", reply);
    window.tts.speak(reply);
    applyEmotion("happy");
    return;
  }
  // 4. 时间 / 计算快捷意图（浏览器本地即可完成，对齐桌面工具）
  if (/^(现在)?(几点了?|什么时间|现在时间|时间|date|time)[?？]?$/i.test(txt) || /现在几点/.test(txt)) {
    const now = fmtTime();
    window.trace.add("tool_call", "get_current_time()");
    window.trace.add("tool_result", now);
    const reply = window.t("q_time_ans", { t: now });
    appendChatBubble("assistant", reply);
    window.tts.speak(reply);
    return;
  }
  const calcMatch = txt.match(/(?:计算|算一下|calculate|compute|calc)\s*[:：]?\s*([0-9+\-*/^().\sπe]+)$/i)
    || txt.match(/^([0-9][0-9+\-*/^().\sπe]*[0-9)])$/);
  if (calcMatch) {
    const r = await ToolRegistry.call("calculate", { expression: calcMatch[1] });
    let reply;
    try {
      const j = JSON.parse(r);
      reply = window.t("q_calc", { e: j.expression, r: j.result });
      window.trace.add("tool_call", `calculate("${j.expression}")`);
      window.trace.add("tool_result", String(j.result));
    } catch (e) { reply = window.t("q_calc_err"); }
    appendChatBubble("assistant", reply);
    window.tts.speak(reply);
    return;
  }
  // 5. 打开网站
  if (/^(打开|open)\s*(.+)/i.test(txt)) {
    const target = txt.replace(/^(打开|open)\s*/i, "").replace(/(的)?(网站|网页|网址|homepage)$/i, "").trim();
    const SITES = { "b站": "https://www.bilibili.com", "bilibili": "https://www.bilibili.com", "github": "https://github.com", "百度": "https://www.baidu.com", "bing": "https://www.bing.com", "知乎": "https://www.zhihu.com", "微博": "https://weibo.com" };
    let url = /^https?:\/\//i.test(target) ? target : SITES[target.toLowerCase()];
    if (!url && /^[\w-]+(\.[\w-]+)+/.test(target)) url = "https://" + target;
    if (url) {
      window.trace.add("tool_call", `open_website("${url}")`);
      const r = await ToolRegistry.call("open_website", { url });
      window.trace.add("tool_result", r);
      appendChatBubble("assistant", r);
      window.tts.speak(/^已打开/.test(r) ? window.t("action_ok") : r);
      return;
    }
  }
  // 6. 宠物动作
  if (/笑一下|开心点|伤心|难过|睡觉|起床|跳舞|动一动|smile|laugh|be happy|cheer up|sad|sleep|wake up|dance|move around/i.test(txt)) {
    applyEmotion(/伤心|难过|sad/i.test(txt) ? "sad" : "happy");
    if (/跳舞|dance|动一动|move/i.test(txt)) ToolRegistry.call("play_animation", {});
    const reply = window.t("action_ok");
    appendChatBubble("assistant", reply);
    window.tts.speak(reply);
    return;
  }
  // 7. mock 对话兜底
  window.trace.add("llm_call", `chat("${txt.slice(0, 30)}${txt.length > 30 ? '…' : ''}")`);
  await new Promise(r => setTimeout(r, 300));
  const ask = (txt.endsWith('?') || txt.endsWith('？')) ? window.t("fb3q") : "";
  const fallbackReplies = [
    window.t("fb1", { x: txt.slice(0, 20) + (txt.length > 20 ? '…' : '') }),
    window.t("fb2"),
    window.t("fb3", { q: ask }),
    window.t("fb4"),
  ];
  const reply = fallbackReplies[randIndex(fallbackReplies.length)];
  window.trace.add("llm_response", reply);
  appendChatBubble("assistant", reply);
  window.tts.speak(reply);
}

// ============================================================
// 11. UI 绑定
// ============================================================
// ============================================================
// 自定义弹窗（替代原生 prompt / alert / confirm），Promise 风格
// ============================================================
const Modal = (function () {
  const overlay = document.getElementById("modal-overlay");
  const titleEl = overlay.querySelector(".modal-title");
  const bodyEl = overlay.querySelector(".modal-body");
  const footEl = overlay.querySelector(".modal-foot");
  let lastFocus = null, keyHandler = null;

  function show() {
    lastFocus = document.activeElement;
    overlay.classList.add("show");
    document.body.style.overflow = "hidden";
  }
  function hide() {
    overlay.classList.remove("show");
    document.body.style.overflow = "";
    if (keyHandler) { document.removeEventListener("keydown", keyHandler); keyHandler = null; }
    if (lastFocus && lastFocus.focus) { try { lastFocus.focus(); } catch (e) {} }
  }

  function open(opt) {
    return new Promise(function (resolve) {
      titleEl.textContent = opt.title || "";
      titleEl.parentElement.style.display = opt.title ? "flex" : "none";
      bodyEl.innerHTML = "";
      bodyEl.appendChild(opt.node);
      footEl.innerHTML = "";

      function finish(val) { hide(); resolve(val); }
      const dismissable = opt.dismissable !== false;

      if (opt.showCancel !== false) {
        const c = document.createElement("button");
        c.type = "button"; c.className = "modal-btn ghost";
        c.textContent = opt.cancelText || window.t("modal_cancel");
        c.addEventListener("click", function () { finish(null); });
        footEl.appendChild(c);
      }
      const ok = document.createElement("button");
      ok.type = "button";
      ok.className = "modal-btn primary" + (opt.danger ? " danger" : "");
      ok.textContent = opt.okText || window.t("modal_ok");
      ok.addEventListener("click", async function () {
        if (opt.onOk) {
          let val;
          try { val = await opt.onOk(); } catch (e) { val = false; }
          if (val === false) return;          // 校验失败，保持弹窗
          finish(val === undefined ? true : val);
        } else {
          finish(true);
        }
      });
      footEl.appendChild(ok);

      overlay.onclick = function (e) { if (dismissable && e.target === overlay) finish(null); };
      keyHandler = function (e) {
        if (!overlay.classList.contains("show")) return;
        if (e.key === "Escape") { if (dismissable) finish(null); }
        else if (e.key === "Enter" && e.target.tagName !== "TEXTAREA") {
          e.preventDefault(); ok.click();
        }
      };
      document.addEventListener("keydown", keyHandler);
      show();
      if (opt.onShown) opt.onShown();
    });
  }

  function textNode(text) {
    const d = document.createElement("div");
    d.className = "modal-text";
    d.textContent = text || "";
    return d;
  }

  function makeField(f) {
    const wrap = document.createElement("label");
    wrap.className = "modal-field";
    if (f.label) {
      const lab = document.createElement("span");
      lab.className = "modal-label";
      lab.textContent = f.label;
      wrap.appendChild(lab);
    }
    let input;
    if (f.textarea) {
      input = document.createElement("textarea");
      input.rows = f.rows || 3;
    } else {
      input = document.createElement("input");
      input.type = f.type || "text";
    }
    input.className = "modal-input";
    input.value = f.value || "";
    if (f.placeholder) input.placeholder = f.placeholder;
    if (f.autocomplete === false) input.setAttribute("autocomplete", "off");
    wrap.appendChild(input);
    f._input = input;
    return wrap;
  }

  return {
    alert: function (text, title) {
      return open({ title: title, node: textNode(text), showCancel: false });
    },
    confirm: function (text, opts) {
      opts = opts || {};
      return open({ title: opts.title, node: textNode(text), okText: opts.okText, danger: opts.danger });
    },
    prompt: function (opts) {
      const node = document.createElement("div");
      if (opts.text) node.appendChild(textNode(opts.text));
      const f = { label: opts.label, type: opts.type, value: opts.value,
                  placeholder: opts.placeholder, textarea: opts.textarea, autocomplete: false };
      node.appendChild(makeField(f));
      return open({
        title: opts.title, node: node, okText: opts.okText,
        onShown: function () { f._input.focus(); if (f._input.select) { try { f._input.select(); } catch (e) {} } },
        onOk: function () {
          const v = f._input.value;
          if (opts.required && !v.trim()) { f._input.classList.add("invalid"); return false; }
          return opts.trim === false ? v : v.trim();
        }
      });
    },
    form: function (opts) {
      const node = document.createElement("div");
      if (opts.text) node.appendChild(textNode(opts.text));
      const fields = opts.fields.map(makeField);
      fields.forEach(function (fe) { node.appendChild(fe); });
      const inputs = opts.fields.map(function (f) { return f._input; });
      return open({
        title: opts.title, node: node,
        okText: opts.okText || window.t("modal_save"),
        onShown: function () { if (inputs[0]) inputs[0].focus(); },
        onOk: function () {
          const values = {};
          let bad = null;
          for (let i = 0; i < opts.fields.length; i++) {
            const f = opts.fields[i];
            let v = inputs[i].value;
            if (f.trim !== false) v = v.trim();
            inputs[i].classList.remove("invalid");
            values[f.name] = v;
            let err = (f.required && !v) ? "required" : null;
            if (!err && typeof f.validate === "function") err = f.validate(v, values);
            if (err) { inputs[i].classList.add("invalid"); if (!bad) bad = inputs[i]; }
          }
          if (bad) { bad.focus(); return false; }
          return values;
        }
      });
    },
    list: function (opts) {
      // getItems/getTitle 为函数时，每次打开（含删除后重开）都取最新数据
      const build = function () {
        const node = document.createElement("div");
        node.className = "modal-list";
        const items = typeof opts.getItems === "function" ? opts.getItems() : (opts.items || []);
        if (!items.length) {
          const e = document.createElement("div");
          e.className = "modal-empty";
          e.textContent = opts.emptyText || window.t("modal_empty");
          node.appendChild(e);
        } else {
          items.forEach(function (it) {
            const row = document.createElement("div");
            row.className = "modal-list-row";
            const body = document.createElement("div");
            body.className = "modal-list-body";
            if (it.meta) {
              const m = document.createElement("div");
              m.className = "modal-list-meta";
              m.textContent = it.meta;
              body.appendChild(m);
            }
            const t = document.createElement("div");
            t.className = "modal-list-text";
            t.textContent = it.text;
            body.appendChild(t);
            row.appendChild(body);
            if (typeof it.onDelete === "function") {
              const del = document.createElement("button");
              del.type = "button";
              del.className = "modal-row-del";
              del.setAttribute("aria-label", "delete");
              del.textContent = "✕";
              del.addEventListener("click", async function () {
                const again = await it.onDelete();
                if (again === false) return;   // 用户取消确认
                hide();
                Modal.list(opts);               // 删除后重开刷新列表
              });
              row.appendChild(del);
            }
            node.appendChild(row);
          });
        }
        return node;
      };
      const itemCount = (typeof opts.getItems === "function" ? opts.getItems() : (opts.items || [])).length;
      const title = typeof opts.getTitle === "function"
        ? opts.getTitle(itemCount)
        : opts.title;
      return open({ title: title, node: build(), showCancel: false,
                    okText: opts.okText || window.t("modal_close") });
    }
  };
})();
window.UI = Modal;

// 内置官方样例模型（零配置自动加载；功能面板 / 角色卡可切换）
// 本地 serve.py 与 GitHub Pages 部署中，模型目录均与 index.html 同级，统一用相对路径
const BUILTIN_MODELS = [
  { id: "hiyori_pro",  name: "Hiyori", badge: "Pro",  path: "hiyori_zh-Hans/hiyori_pro/runtime/hiyori_pro_t11.model3.json" },
  { id: "hiyori_free", name: "Hiyori", badge: "Free", path: "hiyori_zh-Hans/hiyori_free/runtime/hiyori_free_t08.model3.json" },
  { id: "miara_pro",   name: "Miara",  badge: "Pro",  path: "miara_en/runtime/miara_pro_t03.model3.json" },
];
// 私有自定义模型（custom-models.js 提供，已 gitignore；gh-pages 部署时由 Actions 注入）
const CUSTOM_MODELS = (Array.isArray(window.CUSTOM_MODELS) ? window.CUSTOM_MODELS : [])
  .filter(m => m && m.id && m.name && m.path)
  .map(m => ({ id: String(m.id), name: String(m.name), badge: String(m.badge || "Custom"), path: String(m.path), custom: true }));
const ALL_MODELS = BUILTIN_MODELS.concat(CUSTOM_MODELS);
if (CUSTOM_MODELS.length) {
  log(`自定义模型 ${CUSTOM_MODELS.length} 个：` + CUSTOM_MODELS.map(m => `${m.name}(${m.id})`).join("、"), "ok");
}
const MODEL_STORAGE_KEY = "desktop_pet_model";
function currentModelId() {
  const id = localStorage.getItem(MODEL_STORAGE_KEY);
  return ALL_MODELS.some(m => m.id === id) ? id : BUILTIN_MODELS[0].id;
}
function resolveModelUrl(p) {
  return location.origin + location.pathname.replace(/index\.html?$/, "") + p;
}

document.getElementById("motion-btn").addEventListener("click", () => {
  const g = document.getElementById("motion-group").value;
  if (g) window.demo.playMotion(g);
});
document.getElementById("expression-btn").addEventListener("click", () => {
  const n = document.getElementById("expression").value;
  if (n) window.demo.setExpression(n);
});
document.getElementById("reset-btn").addEventListener("click", () => loadBuiltinModel());
document.getElementById("speak-btn").addEventListener("click", (e) => {
  if (window.demo._talking) { window.demo.stopTalk(); e.target.textContent = window.t("test_lipsync"); e.target.classList.remove("live"); }
  else { window.demo.startTalk(); e.target.textContent = window.t("stop_lipsync"); e.target.classList.add("live"); }
});

// 角色卡显示当前模型
function updateModelCard(model) {
  const title = document.querySelector("#model-info .model-title");
  if (title) title.innerHTML = `${model.name} <span class="badge-pro">${model.badge}</span>`;
}
function syncModelSelect(id) {
  const sel = document.getElementById("model-select");
  if (sel) sel.value = id;
}

// 加载指定内置/自定义模型（id 缺省取上次选择 / 默认第一个）
async function loadBuiltinModel(id) {
  const model = ALL_MODELS.find(m => m.id === id) || ALL_MODELS.find(m => m.id === currentModelId());
  showLoader(window.t("loading_builtin"));
  setStatus(window.t("status_load_model", { name: model.name }), "#ffce5c");
  try {
    // 用 GET（不是 HEAD）—— GitHub Pages 对 HEAD 支持不一致
    const r = await fetch(model.path, { method: "GET" });
    if (!r.ok) {
      hideLoader();
      // 私有自定义模型缺失（例如别人 clone 了 master / gh-pages 未放模型）→ 回退内置样例
      if (model.custom) {
        log(`自定义模型 ${model.name} 不可用（${model.path} → ${r.status}），回退内置模型`, "err");
        localStorage.removeItem(MODEL_STORAGE_KEY);
        return loadBuiltinModel(BUILTIN_MODELS[0].id);
      }
      setStatus(window.t("status_builtin_missing"), "#ff7675");
      log(`内置模型 ${model.name} 未部署（${model.path} → ${r.status}）`, "err");
      return;
    }
    log(`加载模型：${model.name} ${model.badge}（${model.path}）`, "ok");
    await window.demo.loadModel(resolveModelUrl(model.path));
    localStorage.setItem(MODEL_STORAGE_KEY, model.id);
    updateModelCard(model);
    syncModelSelect(model.id);
  } catch (e) {
    hideLoader();
    if (model.custom) {
      log(`自定义模型 ${model.name} 加载失败：${e.message || e}，回退内置模型`, "err");
      localStorage.removeItem(MODEL_STORAGE_KEY);
      return loadBuiltinModel(BUILTIN_MODELS[0].id);
    }
    setStatus(window.t("status_builtin_missing"), "#ff7675");
    log(`内置模型 ${model.name} 加载失败：${e.message || e}`, "err");
  }
}

// 功能面板：模型选择下拉（内置 + 自定义）
(function setupModelSelect() {
  const sel = document.getElementById("model-select");
  if (!sel) return;
  for (const m of ALL_MODELS) {
    const opt = document.createElement("option");
    opt.value = m.id;
    opt.textContent = `${m.name} ${m.badge}`;
    sel.appendChild(opt);
  }
  sel.value = currentModelId();
  sel.addEventListener("change", () => { if (sel.value) loadBuiltinModel(sel.value); });
})();

// 角色卡：循环切换模型 / 重新加载当前模型
const switchBtn = document.getElementById("switch-model-btn");
if (switchBtn) switchBtn.addEventListener("click", () => {
  const idx = ALL_MODELS.findIndex(m => m.id === currentModelId());
  loadBuiltinModel(ALL_MODELS[(idx + 1) % ALL_MODELS.length].id);
});
const reloadBtn = document.getElementById("reload-btn");
if (reloadBtn) reloadBtn.addEventListener("click", () => loadBuiltinModel(currentModelId()));

// 外部 URL 模型加载（http/https 校验；对齐桌面版"任意模型 URL"能力）
function externalModelName(url) {
  try {
    const file = decodeURIComponent(new URL(url).pathname.split("/").pop() || "External");
    return file.replace(/\.model3\.json.*$/i, "") || "External";
  } catch (e) { return "External"; }
}
async function loadExternalModel(url) {
  try {
    await window.demo.loadModel(url);
    updateModelCard({ name: externalModelName(url), badge: "URL" });
    syncModelSelect("");   // 自定义模型不在内置下拉中
    toast(window.t("status_ready_loading"), "ok");
  } catch (e) { /* loadModel 内已 log + 状态栏提示 */ }
}
const urlBtn = document.getElementById("model-url-btn");
if (urlBtn) urlBtn.addEventListener("click", () => {
  const input = document.getElementById("model-url");
  const url = (input.value || "").trim();
  if (!/^https?:\/\//i.test(url)) {
    input.classList.add("invalid");
    toast(window.t("url_invalid"), "err");
    return;
  }
  input.classList.remove("invalid");
  loadExternalModel(url);
});
const urlInput = document.getElementById("model-url");
if (urlInput) urlInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); document.getElementById("model-url-btn").click(); }
});

// 拖拽加载：http(s) 的 .model3.json 链接可直接拖入；本地文件给出沙箱提示
(function setupDragDrop() {
  const wrap = document.getElementById("stage-wrap");
  const overlay = document.getElementById("drop-overlay");
  if (!wrap || !overlay) return;
  let depth = 0;
  wrap.addEventListener("dragenter", (e) => { e.preventDefault(); depth++; overlay.classList.add("over"); });
  wrap.addEventListener("dragover", (e) => { e.preventDefault(); });
  wrap.addEventListener("dragleave", () => { if (--depth <= 0) { depth = 0; overlay.classList.remove("over"); } });
  wrap.addEventListener("drop", async (e) => {
    e.preventDefault();
    depth = 0; overlay.classList.remove("over");
    if (e.dataTransfer.files && e.dataTransfer.files.length) {
      toast(window.t("drop_file_err"), "err", 4200);
      return;
    }
    let url = (e.dataTransfer.getData("text/uri-list") || e.dataTransfer.getData("text/plain") || "").split("\n")[0].trim();
    if (!url) return;
    if (!/^https?:\/\//i.test(url) || !/\.model3\.json(\?|$)/i.test(url)) {
      toast(window.t("url_invalid"), "err");
      return;
    }
    loadExternalModel(url);
  });
})();

// 快捷指令 chips（点击即发送；语言切换时重渲染）
(function setupChips() {
  const box = document.getElementById("chips");
  if (!box) return;
  const KEYS = ["chip_time", "chip_remind", "chip_remember", "chip_weather", "chip_search", "chip_website"];
  function render() {
    box.innerHTML = "";
    for (const k of KEYS) {
      const c = document.createElement("button");
      c.type = "button";
      c.className = "chip";
      c.textContent = window.t(k);
      c.addEventListener("click", () => {
        const input = document.getElementById("chat-input");
        input.value = c.textContent.replace(/^\S+\s+/, "");  // 去掉 emoji 前缀
        document.getElementById("chat-btn").click();
      });
      box.appendChild(c);
    }
  }
  render();
  if (window.I18N) window.I18N.onChange(render);
})();

document.getElementById("chat-btn").addEventListener("click", async () => {
  const text = document.getElementById("chat-input").value.trim();
  if (!text) { log("请输入文字", "err"); return; }
  document.getElementById("chat-input").value = "";
  await handleUserInput(text);
});
document.getElementById("chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("chat-btn").click();
});

// ASR 按住说话
const asrBtn = document.getElementById("asr-btn");
if (asrBtn) {
  asrBtn.addEventListener("mousedown", () => {
    if (!window.demo.ready) { log("请先加载模型", "err"); return; }
    asrBtn.classList.add("recording");
    asrBtn.title = window.t("listening");
    window.asr.start(
      (text, isFinal) => {
        if (isFinal) document.getElementById("chat-input").value = text;
      },
      () => {
        asrBtn.classList.remove("recording");
        asrBtn.title = window.t("hold_to_talk");
      }
    );
  });
  asrBtn.addEventListener("mouseup", () => window.asr.stop());
  asrBtn.addEventListener("mouseleave", () => window.asr.stop());
}

// TTS 引擎切换
const ttsSel = document.getElementById("tts-engine");
if (ttsSel) {
  ttsSel.addEventListener("change", (e) => window.tts.setEngine(e.target.value));
}

// TTS 语音填充（等 voiceschanged 触发）
(function setupTTSVoice() {
  const voiceSel = document.getElementById("tts-voice");
  if (!voiceSel) return;
  function fillVoices() {
    if (!window.tts || !window.tts.synth) return;
    // 兜底刷新内部语音缓存（防止 voiceschanged 被覆盖、内部列表为空）
    window.tts.refreshVoices();
    const all = window.tts.allVoices || [];
    if (all.length === 0) return;
    voiceSel.innerHTML = '<option value="">' + window.t("auto_voice") + '</option>';
    for (const v of all) {
      const opt = document.createElement("option");
      opt.value = v.name;
      const marker = /xiaoxiao|小晓/i.test(v.name) ? window.t("voice_reco") :
                     /yunjian|云健|yunxi|云希|male|男/i.test(v.name) ? window.t("voice_male") :
                     /yating|云夏|female|女/i.test(v.name) ? window.t("voice_female") : "";
      opt.textContent = `${v.name} (${v.lang})${marker}`;
      voiceSel.appendChild(opt);
    }
    // 重建后选中当前实际生效的语音：pickVoice 已处理“手动优先 / 中文默认 Xiaoxiao / 英文默认英文语音”
    const wantEn = window.I18N && window.I18N.lang === "en";
    const cur = window.tts.pickVoice(wantEn);
    if (cur) {
      const target = [...voiceSel.options].find(o => o.value === cur.name);
      if (target) voiceSel.value = target.value;
    }
    log(`可用语音 ${all.length} 个（默认 Xiaoxiao，也可在下拉中切换）`, "ok");
  }
  // 延迟多次尝试，因为 voiceschanged 触发有延迟
  setTimeout(fillVoices, 500);
  setTimeout(fillVoices, 2000);
  setTimeout(fillVoices, 5000);
  if (window.speechSynthesis && window.speechSynthesis.addEventListener) {
    window.speechSynthesis.addEventListener("voiceschanged", fillVoices);
  }
  voiceSel.addEventListener("change", (e) => {
    if (!e.target.value) {
      // 选“自动”：清手动选择，恢复推荐默认（中文 Xiaoxiao）
      window.tts._userVoice = null;
      window.tts._pinnedVoice = null;
      window.tts.refreshVoices();
      const wantEn = window.I18N && window.I18N.lang === "en";
      const cur = window.tts.pickVoice(wantEn);
      if (cur) voiceSel.value = cur.name;
      log("TTS 已恢复自动选择（默认 Xiaoxiao）", "ok");
    } else {
      window.tts.setVoiceByName(e.target.value);
    }
  });
  window.__refreshVoiceOptions = fillVoices;
})();

// Tavily key 配置（自定义弹窗）
const tavilyBtn = document.getElementById("tavily-btn");
if (tavilyBtn) {
  tavilyBtn.addEventListener("click", async () => {
    const cur = localStorage.getItem("tavily_api_key") || "";
    const v = await UI.prompt({
      title: window.t("tavily_title"),
      text: window.t("tavily_prompt"),
      value: cur
    });
    if (v !== null) {
      localStorage.setItem("tavily_api_key", v);
      log(`Tavily key ${v ? window.t("tavily_set") : window.t("tavily_cleared")}`, "ok");
    }
  });
}

// LLM API 配置（OpenAI 兼容，单个表单弹窗）
const llmBtn = document.getElementById("llm-btn");
if (llmBtn) {
  llmBtn.addEventListener("click", async () => {
    const cur = window.llm.cfg;
    const vals = await UI.form({
      title: window.t("llm_title"),
      fields: [
        { name: "baseUrl", label: window.t("llm_base_url"), value: cur.baseUrl || "https://api.openai.com/v1" },
        { name: "apiKey", label: window.t("llm_api_key"), value: cur.apiKey || "", type: "password", required: true },
        { name: "model", label: window.t("llm_model"), value: cur.model || "gpt-4o-mini" },
        { name: "systemPrompt", label: window.t("llm_sysprompt"), textarea: true, value: cur.systemPrompt || window.t("llm_default_sys") }
      ]
    });
    if (!vals) return;
    if (!vals.apiKey) { log(window.t("llm_need_key"), "err"); return; }
    window.llm.setConfig({
      baseUrl: vals.baseUrl || "https://api.openai.com/v1",
      apiKey: vals.apiKey,
      model: vals.model || "gpt-4o-mini",
      systemPrompt: vals.systemPrompt || window.t("llm_default_sys")
    });
    log(`LLM 已配置：${window.llm.status()}`, "ok");
    log(window.t("llm_configured_log"), "ok");
  });
}

// 记忆面板（每条可单独遗忘）
const memBtn = document.getElementById("mem-btn");
if (memBtn) {
  memBtn.addEventListener("click", () => {
    UI.list({
      getTitle: n => window.t("mem_title") + (n ? ` · ${n}` : ""),
      emptyText: window.t("mem_empty"),
      getItems: () => window.memory.list().slice().reverse().map(m => ({
        meta: new Date(m.time).toLocaleString(),
        text: m.content,
        onDelete: async () => {
          const ok = await UI.confirm(window.t("del_confirm_mem"), { danger: true, okText: window.t("modal_ok") });
          if (!ok) return false;
          window.memory.removeOne(m.time);
          toast(window.t("mem_deleted", { n: 1 }));
          return true;
        },
      })),
    });
  });
}
const forgetBtn = document.getElementById("forget-btn");
if (forgetBtn) {
  forgetBtn.addEventListener("click", async () => {
    const q = await UI.prompt({
      title: window.t("forget_title"),
      text: window.t("forget_prompt"),
      label: window.t("forget_keyword")
    });
    if (q) { window.memory.forget(q); }
  });
}

// 提醒面板
const remindBtn = document.getElementById("remind-btn");
if (remindBtn) {
  remindBtn.addEventListener("click", async () => {
    const v = await UI.form({
      title: window.t("remind_title"),
      fields: [
        { name: "mins", label: window.t("remind_minutes"), type: "number", placeholder: "10",
          required: true, validate: function (v) { return /^[1-9]\d*$/.test(v) ? null : "bad"; } },
        { name: "content", label: window.t("remind_content_label"), placeholder: window.t("remind_default") }
      ]
    });
    if (!v) return;
    const mins = parseInt(v.mins, 10);
    if (!isNaN(mins)) {
      window.reminders.add(v.content || window.t("remind_default"), mins);
    }
  });
}
const remindListBtn = document.getElementById("remind-list-btn");
if (remindListBtn) {
  remindListBtn.addEventListener("click", () => {
    UI.list({
      getTitle: n => window.t("rem_title") + (n ? ` · ${n}` : ""),
      emptyText: window.t("rem_empty"),
      getItems: () => window.reminders.list().map(r => ({
        meta: window.t("rem_at") + " " + new Date(r.fireAt).toLocaleString(),
        text: r.content,
        onDelete: async () => {
          const ok = await UI.confirm(window.t("del_confirm_rem"), { danger: true, okText: window.t("modal_ok") });
          if (!ok) return false;
          window.reminders.remove(r.id);
          toast(window.t("rem_deleted"));
          return true;
        },
      })),
    });
  });
}

// 清空 / 导出对话历史
const chatClearBtn = document.getElementById("chat-clear-btn");
if (chatClearBtn) {
  chatClearBtn.addEventListener("click", async () => {
    const ok = await UI.confirm(window.t("clear_chat_confirm"), { title: window.t("clear_chat"), danger: true, okText: window.t("clear") });
    if (!ok) return;
    window.chatHistory.clear();
    if (chatEl) chatEl.innerHTML = "";
    appendChatBubble("proactive", window.t("welcome_bubble"), { save: false });
    toast(window.t("chat_cleared"));
  });
}
const chatExportBtn = document.getElementById("chat-export-btn");
if (chatExportBtn) {
  chatExportBtn.addEventListener("click", () => {
    const items = window.chatHistory.items;
    if (!items.length) { toast(window.t("modal_empty"), "err"); return; }
    const lines = items.map(m => `[${new Date(m.ts).toLocaleString()}] (${m.role}) ${m.text}`);
    const blob = new Blob([lines.join("\n\n")], { type: "text/plain;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `desktop-pet-chat-${new Date().toISOString().slice(0, 10)}.txt`;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
    toast(window.t("chat_exported"));
  });
}

// 偏好开关：自动朗读 / 主动搭话（对齐桌面 TTS 静音与主动搭话设置）
const autoSpeakToggle = document.getElementById("auto-speak-toggle");
if (autoSpeakToggle) {
  autoSpeakToggle.checked = settings.autoSpeak;
  autoSpeakToggle.addEventListener("change", () => {
    settings.autoSpeak = autoSpeakToggle.checked;
    saveSettings();
    if (!settings.autoSpeak) window.tts.stop();
  });
}
const proactiveToggle = document.getElementById("proactive-toggle");
if (proactiveToggle) {
  proactiveToggle.checked = settings.proactive;
  proactiveToggle.addEventListener("change", () => {
    settings.proactive = proactiveToggle.checked;
    saveSettings();
  });
}

// 工具清单（展示与桌面版同名对齐的工具集）
const toolsBtn = document.getElementById("tools-btn");
if (toolsBtn) {
  toolsBtn.addEventListener("click", () => {
    const fns = [...ToolRegistry.defs.values()].map(d => d.schema.function);
    UI.list({
      title: `${window.t("tools_title")} · ${fns.length}`,
      items: fns.map(f => ({ meta: f.name, text: f.description })),
    });
  });
}

// 视线跟随
document.getElementById("stage").addEventListener("pointermove", (e) => {
  if (!window.demo.ready) return;
  const r = window.demo.app.view.getBoundingClientRect();
  const nx = (e.clientX - r.left) / r.width * 2 - 1;
  const ny = (e.clientY - r.top) / r.height * 2 - 1;
  window.demo.focus(nx, ny);
});

// 摸宠互动：点击模型 → 爱心特效 + 亲密度 +1 + 随机回应（对齐桌面触摸热区）
document.getElementById("stage").addEventListener("pointerdown", (e) => {
  if (!window.demo.ready || !window.demo.model) return;
  const r = window.demo.app.view.getBoundingClientRect();
  spawnHearts(e.clientX - r.left, e.clientY - r.top, 5);
  addAffection(1);
  window.demo.pulse();
  applyEmotion("happy");
  window.tts.speak(window.t(`pet${1 + randIndex(3)}`));
  window.trace.add("remark", window.t("pet_log"));
});

// idle 行为
function startIdleBehavior() {
  if (window._idleTimer) clearInterval(window._idleTimer);
  const tick = () => {
    if (!window.demo.ready || window.demo._talking || window.demo._simulating) return;
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
  };
  window._idleTimer = setInterval(() => {
    if (Math.random() < 0.5) tick();
  }, 6000 + Math.random() * 6000);
  setTimeout(tick, 3000);
}
window.startIdleBehavior = startIdleBehavior;

// 语言切换时重渲染动态 UI（语音下拉 / 口型按钮 / 就绪状态）
if (window.I18N) {
  window.I18N.onChange(function () {
    if (window.__refreshVoiceOptions) window.__refreshVoiceOptions();
    const sb = document.getElementById("speak-btn");
    if (sb) sb.textContent = window.t(window.demo && window.demo._talking ? "stop_lipsync" : "test_lipsync");
    if (window.demo && window.demo.ready && window.demo.model) {
      const m = window.demo.model;
      const exprs = (m.internalModel && m.internalModel.settings.expressions) || [];
      const motions = (m.internalModel && m.internalModel.settings.motions) || {};
      const tag = window.llm.hasKey() ? ` · ${window.llm.cfg.model}` : window.t("mock_tag");
      setStatus(window.t("status_ready", { expr: exprs.length, mot: Object.keys(motions).length, tools: ToolRegistry.names().length, tag }), "#55efc4");
    }
  });
}

// ============================================================
// 12. 启动
// ============================================================
window.addEventListener("DOMContentLoaded", () => {
  // 恢复上次对话历史（刷新不丢）+ 亲密度显示
  restoreChatHistory();
  const aff = document.getElementById("affection");
  if (aff) aff.textContent = `❤ ${getAffection()}`;
  (async () => {
    try { if (window.__sdkReady) await window.__sdkReady; }
    catch (e) {
      setStatus(window.t("status_sdk_failed"), "#ff7675");
      log("SDK 加载失败：" + (e.message || e), "err");
      return;
    }
    const st = window.__sdkLoadStatus || {};
    log(`SDK 加载完成：`);
    if (st.pixi)    log(`  pixi    ← ${st.pixi.replace(/^ok:/, "")}`);
    if (st.core)    log(`  core    ← ${st.core.replace(/^ok:/, "")}`);
    if (st.cubism4) log(`  cubism4 ← ${st.cubism4.replace(/^ok:/, "")}`, "ok");
    if (typeof PIXI === "undefined") { setStatus(window.t("status_pixi_missing"), "#ff7675"); return; }
    if (typeof Live2DCubismCore === "undefined") { setStatus(window.t("status_core_missing"), "#ff7675"); return; }
    if (!PIXI.live2d || !PIXI.live2d.Live2DModel || typeof PIXI.live2d.Live2DModel.from !== "function") {
      setStatus(window.t("status_from_unavailable"), "#ff7675");
      log("pixi-live2d-display 未注册 Live2DModel.from", "err");
      return;
    }
    try { window.demo.ensureApp(); }
    catch (e) { setStatus(window.t("status_pixi_init_failed"), "#ff7675"); log(e.message || e, "err"); return; }
    // SDK 就绪即可展示功能面板（模型加载后填充动作/表情下拉）
    const ctrl = document.getElementById("controls");
    if (ctrl) ctrl.style.display = "flex";
    setStatus(window.t("status_ready_loading"), "#55efc4");
    log(`PIXI v${PIXI.VERSION} · Live2DModel.from 已就绪 · 工具 ${ToolRegistry.names().length} 个`, "ok");
    if (window.asr.available) log("ASR 可用（按住说话按钮）", "ok");
    else log("ASR 不可用（请用 Chrome / Edge）", "err");
    if (window.tts.voices.length) log(`TTS 可用（${window.tts.voices.length} 个语音）`, "ok");
    else log("TTS 暂未加载语音", "err");
    // 零配置自动加载上次选择的内置模型（默认 Hiyori Pro）
    loadBuiltinModel(currentModelId());
  })();
});
