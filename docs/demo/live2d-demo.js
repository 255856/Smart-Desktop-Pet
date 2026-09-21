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
    // demo 仅加载内置官方模型（URL 由 loadBuiltinModel 基于当前页面构造），无需外部 URL 校验

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
      setStatus(window.t("status_ready", { expr: exprs.length, mot: Object.keys(motions).length, tag: llmTag }), "#55efc4");
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
      log(`内置模型加载失败：${msg}`, "err");
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
  // 真实 speak：调用 SpeechSynthesis 同步驱动口型
  speak(text, onEnd) {
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
      item.innerHTML = `<span class="trace-t">${evt.t}</span><span class="trace-type">${type}</span><span class="trace-content">${content}</span>`;
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
      const resp = await fetch(`${this.cfg.baseUrl}/chat/completions`, {
        method: "POST",
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
      onDone && onDone(fullText);
    } catch (e) {
      onError && onError(e);
    }
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
// 9. 聊天 UI：气泡 + 输入
// ============================================================
const chatEl = document.getElementById("chat-area");
function appendChatBubble(role, text) {
  if (!chatEl) return;
  const b = document.createElement("div");
  b.className = `bubble bubble-${role}`;
  b.textContent = text;
  chatEl.appendChild(b);
  // 限制最大 5 条
  while (chatEl.children.length > 5) chatEl.removeChild(chatEl.firstChild);
  chatEl.scrollTop = chatEl.scrollHeight;
}
function clearChat() {
  if (chatEl) chatEl.innerHTML = "";
}

// ============================================================
// 10. Agent 路由：消息进来后判断是否需要工具调用
// ============================================================
async function handleUserInput(text) {
  if (!text || !window.demo.ready) {
    if (!window.demo.ready) log("请先加载模型", "err");
    return;
  }
  appendChatBubble("user", text);
  window.trace.clear();
  window.trace.add("user", text);

  // 简单意图识别（中 / 英）
  const txt = text.trim();
  // 1. 提醒类（中文“3分钟后提醒我喝水” / 英文“in 5 minutes remind me to drink”）
  const remindMatch = txt.match(/(\d+)\s*(分钟|秒钟|秒|小时|minutes?|mins?|hours?|seconds?|secs?)\s*(?:之后?|后)?\s*(?:提醒(?:我)?|叫我|告诉我|喊我|记得|remind\s*me(?:\s*to)?)\s*(.*)/i);
  if (remindMatch) {
    const n = parseInt(remindMatch[1]);
    const unitRaw = remindMatch[2].toLowerCase();
    const isSec = /秒|sec/.test(unitRaw);
    const isHour = /小时|hour|^h$/.test(unitRaw);
    const minutes = isSec ? n / 60 : isHour ? n * 60 : n;
    const unitLabel = isSec ? window.t("unit_sec") : isHour ? window.t("unit_hour") : window.t("unit_min");
    const content = (remindMatch[3] || "").replace(/^[\s:：,，to]+/i, "").trim() || txt || window.t("remind_default");
    window.reminders.add(content, minutes);
    const reply = window.t("remind_confirm", { n, unit: unitLabel, content });
    appendChatBubble("assistant", reply);
    window.tts.speak(reply);
    window.demo._emitEmotion("happy");
    return;
  }
  // 2. 搜索类
  if (/搜索|查一下|查查|看看|搜一下|帮我找|search|look up|find (?:me |about )/i.test(txt)) {
    window.trace.add("tool_call", `web_search("${txt}")`);
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
      window.demo._emitEmotion("surprised");
      return;
    } catch (e) {
      log(`搜索失败：${e.message}`, "err");
    }
  }
  // 3. 记忆类
  const memMatch = txt.match(/(?:记住|记一下|别忘了|记着|记下|remember that|remember|don't forget|note that)\s*[:：，,]?\s*(.+)/i);
  if (memMatch) {
    const mc = memMatch[1].trim();
    window.memory.remember(mc, 7);
    const reply = window.t("mem_confirm", { c: mc });
    appendChatBubble("assistant", reply);
    window.tts.speak(reply);
    window.demo._emitEmotion("happy");
    return;
  }
  // 4. 桌宠本体动作
  if (/笑一下|开心点|伤心|难过|睡觉|起床|跳舞|动一动|smile|laugh|be happy|cheer up|sad|sleep|wake up|dance|move around/i.test(txt)) {
    const exprList = (window.demo.model && window.demo.model.internalModel.settings.expressions) || [];
    const randomExpr = exprList[Math.floor(Math.random() * exprList.length)];
    if (randomExpr) window.demo.setExpression(randomExpr.Name || randomExpr.name);
    const reply = window.t("action_ok");
    appendChatBubble("assistant", reply);
    window.tts.speak(reply);
    window.demo._emitEmotion("happy");
    return;
  }
  // 5. 真实 LLM 调用（OpenAI 兼容 API；mock 兜底）
  window.trace.add("llm_call", `chat("${txt.slice(0, 30)}${txt.length > 30 ? '…' : ''}")`);
  const memoryCtx = window.memory.recent(3).map(m => m.content).join('；');

  if (!window.llm.hasKey()) {
    // 无 API key：mock 兜底
    await new Promise(r => setTimeout(r, 300));
    const ask = (txt.endsWith('?') || txt.endsWith('？')) ? window.t("fb3q") : "";
    const fallbackReplies = [
      window.t("fb1", { x: txt.slice(0, 20) + (txt.length > 20 ? '…' : '') }),
      window.t("fb2"),
      window.t("fb3", { q: ask }),
      window.t("fb4"),
    ];
    const reply = fallbackReplies[Math.floor(Math.random() * fallbackReplies.length)];
    window.trace.add("llm_response", reply);
    appendChatBubble("assistant", reply);
    window.tts.speak(reply);
    return;
  }

  // 真 API 调用
  const messages = [
    { role: "user", content: txt },
    ...(memoryCtx ? [{ role: "system", content: window.t("memory_ctx", { m: memoryCtx }) }] : []),
  ];

  // 增量显示（流式）
  const bubble = document.createElement("div");
  bubble.className = "bubble bubble-assistant";
  bubble.textContent = "…";
  chatEl.appendChild(bubble);
  chatEl.scrollTop = chatEl.scrollHeight;
  while (chatEl.children.length > 6) chatEl.removeChild(chatEl.firstChild);

  try {
    let reply = "";
    await window.llm.chatStream(
      messages,
      (delta, full) => { reply = full; bubble.textContent = full; chatEl.scrollTop = chatEl.scrollHeight; },
      (full) => {
        reply = full.trim() || window.t("model_empty");
        bubble.textContent = reply;
        window.trace.add("llm_response", reply);
        window.tts.speak(reply);
      },
      (err) => {
        bubble.textContent = window.t("err_prefix") + err.message;
        log(`LLM 调用失败：${err.message}`, "err");
      }
    );
  } catch (e) {
    bubble.textContent = window.t("err_prefix") + e.message;
    log(`LLM 异常：${e.message}`, "err");
  }
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
      const node = document.createElement("div");
      node.className = "modal-list";
      const items = opts.items || [];
      if (!items.length) {
        const e = document.createElement("div");
        e.className = "modal-empty";
        e.textContent = opts.emptyText || window.t("modal_empty");
        node.appendChild(e);
      } else {
        items.forEach(function (it) {
          const row = document.createElement("div");
          row.className = "modal-list-row";
          if (it.meta) {
            const m = document.createElement("div");
            m.className = "modal-list-meta";
            m.textContent = it.meta;
            row.appendChild(m);
          }
          const t = document.createElement("div");
          t.className = "modal-list-text";
          t.textContent = it.text;
          row.appendChild(t);
          node.appendChild(row);
        });
      }
      return open({ title: opts.title, node: node, showCancel: false,
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
const MODEL_STORAGE_KEY = "desktop_pet_model";
function currentModelId() {
  const id = localStorage.getItem(MODEL_STORAGE_KEY);
  return BUILTIN_MODELS.some(m => m.id === id) ? id : BUILTIN_MODELS[0].id;
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

// 加载指定内置模型（id 缺省取上次选择 / 默认第一个）
async function loadBuiltinModel(id) {
  const model = BUILTIN_MODELS.find(m => m.id === id) || BUILTIN_MODELS.find(m => m.id === currentModelId());
  showLoader(window.t("loading_builtin"));
  setStatus(window.t("status_load_model", { name: model.name }), "#ffce5c");
  try {
    // 用 GET（不是 HEAD）—— GitHub Pages 对 HEAD 支持不一致
    const r = await fetch(model.path, { method: "GET" });
    if (!r.ok) {
      hideLoader();
      setStatus(window.t("status_builtin_missing"), "#ff7675");
      log(`内置模型 ${model.name} 未部署（${model.path} → ${r.status}）`, "err");
      return;
    }
    log(`加载内置模型：${model.name} ${model.badge}（${model.path}）`, "ok");
    await window.demo.loadModel(resolveModelUrl(model.path));
    localStorage.setItem(MODEL_STORAGE_KEY, model.id);
    updateModelCard(model);
    syncModelSelect(model.id);
  } catch (e) {
    hideLoader();
    setStatus(window.t("status_builtin_missing"), "#ff7675");
    log(`内置模型 ${model.name} 加载失败：${e.message || e}`, "err");
  }
}

// 功能面板：模型选择下拉
(function setupModelSelect() {
  const sel = document.getElementById("model-select");
  if (!sel) return;
  for (const m of BUILTIN_MODELS) {
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
  const idx = BUILTIN_MODELS.findIndex(m => m.id === currentModelId());
  loadBuiltinModel(BUILTIN_MODELS[(idx + 1) % BUILTIN_MODELS.length].id);
});
const reloadBtn = document.getElementById("reload-btn");
if (reloadBtn) reloadBtn.addEventListener("click", () => loadBuiltinModel(currentModelId()));

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

// 记忆面板
const memBtn = document.getElementById("mem-btn");
if (memBtn) {
  memBtn.addEventListener("click", () => {
    const items = window.memory.list();
    UI.list({
      title: window.t("mem_title") + (items.length ? ` · ${items.length}` : ""),
      emptyText: window.t("mem_empty"),
      items: items.slice().reverse().map(m => ({ meta: new Date(m.time).toLocaleString(), text: m.content }))
    });
    if (items.length) window.tts.speak(window.t("mem_tts", { n: items.length }));
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
    const items = window.reminders.list();
    UI.list({
      title: window.t("rem_title") + (items.length ? ` · ${items.length}` : ""),
      emptyText: window.t("rem_empty"),
      items: items.map(r => ({
        meta: window.t("rem_at") + " " + new Date(r.fireAt).toLocaleString(),
        text: r.content
      }))
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
      setStatus(window.t("status_ready", { expr: exprs.length, mot: Object.keys(motions).length, tag }), "#55efc4");
    }
  });
}

// ============================================================
// 12. 启动
// ============================================================
window.addEventListener("DOMContentLoaded", () => {
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
    setStatus(window.t("status_ready_loading"), "#55efc4");
    log(`PIXI v${PIXI.VERSION} · Live2DModel.from 已就绪`, "ok");
    if (window.asr.available) log("ASR 可用（按住说话按钮）", "ok");
    else log("ASR 不可用（请用 Chrome / Edge）", "err");
    if (window.tts.voices.length) log(`TTS 可用（${window.tts.voices.length} 个语音）`, "ok");
    else log("TTS 暂未加载语音", "err");
    // 零配置自动加载上次选择的内置模型（默认 Hiyori Pro）
    loadBuiltinModel(currentModelId());
  })();
});
