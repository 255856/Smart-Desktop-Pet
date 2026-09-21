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
// 0. 工具：日志 / 加载状态 / 工具栏 / URL 安全校验
// ============================================================
// URL 校验：仅允许 http/https；拒绝 localhost/环回/私有 IP（根据 Mimosa 安全约束）
// 注意：localhost 例外——本地 serve.py 场景需要
const ALLOW_LOOPBACK = true;  // demo 允许 localhost（用户本地启动 serve.py）
function validateModelUrl(url) {
  if (!url || typeof url !== "string") return { ok: false, error: "URL 为空" };
  const trimmed = url.trim();
  // 自动修复：用户可能输入 "http:127.0.0.1..." 少一个斜杠 → "http://127.0.0.1..."
  let fixed = trimmed;
  fixed = fixed.replace(/^(https?):(?![\/\\])/i, "$1://");
  if (!/^https?:\/\//i.test(fixed)) return { ok: false, error: "URL 必须以 http:// 或 https:// 开头（不要用 file:// 或 E:/ 本地路径）" };

  try {
    const u = new URL(fixed);
    if (u.protocol !== "http:" && u.protocol !== "https:") return { ok: false, error: "协议必须是 http 或 https" };
    const host = u.hostname.toLowerCase();
    // 检测 localhost / 环回 / 私有 IP
    const isLoopback = host === "localhost" || host === "127.0.0.1" || host === "::1" || host === "[::1]"
                    || /^127\./.test(host) || /^10\./.test(host) || /^192\.168\./.test(host)
                    || /^172\.(1[6-9]|2\d|3[01])\./.test(host);
    if (isLoopback && !ALLOW_LOOPBACK) return { ok: false, error: "禁止访问 loopback/私有 IP" };
    // 检查 URL 是否是本地文件路径（兜底）
    if (/^[a-z]:[\\\/]/i.test(trimmed)) return { ok: false, error: "URL 不能是 Windows 本地路径（如 E:/...）" };
    return { ok: true, url: fixed };
  } catch (e) {
    return { ok: false, error: "URL 格式错误：" + (e.message || e) };
  }
}

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
function showLoader(t) { loaderText.textContent = t || "加载中…"; loader.classList.remove("hidden"); }
function hideLoader() { loader.classList.add("hidden"); }
function setStatus(t, c = "#a8b0c0") { statusEl.textContent = t; statusEl.style.color = c; }

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
    // URL 校验：http/https only；host 是合法公网/本地（localhost 允许，因为 demo 要支持本地 serve.py）
    const valid = validateModelUrl(modelUrl);
    if (!valid.ok) throw new Error(valid.error);

    this.shutdown();
    this.ensureApp();
    if (PIXI.live2d && PIXI.live2d.Live2DModel && PIXI.live2d.Live2DModel.registerTicker && PIXI.Ticker) {
      PIXI.live2d.Live2DModel.registerTicker(PIXI.Ticker);
    }
    showLoader("加载模型…");
    setStatus("加载中…", "#ffce5c");
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
      this.ready = true;
      const exprs = (model.internalModel && model.internalModel.settings.expressions) || [];
      const motions = (model.internalModel && model.internalModel.settings.motions) || {};
      this._refreshControls();
      hideLoader();
      const llmTag = window.llm.hasKey() ? ` 🧠${window.llm.cfg.model}` : " 🎭mock";
      setStatus(`就绪 · ${exprs.length} 表情 / ${Object.keys(motions).length} 组动作${llmTag}`, "#55efc4");
      log(`模型加载完成：${exprs.length} 表情 / ${Object.keys(motions).length} 组动作`, "ok");
      if (window.llm.hasKey()) {
        log(`✅ LLM 已配置：${window.llm.status()}`, "ok");
      } else {
        log(`ℹ️ 未配置 LLM API，桌宠用 mock 回复（点 🧠 LLM 按钮填 key 启用真模型）`, "ok");
      }
      if (window.startIdleBehavior) window.startIdleBehavior();
      this._emitEmotion("neutral");
      return { expressions: exprs, motions };
    } catch (err) {
      hideLoader();
      setStatus("加载失败", "#ff7675");
      const msg = err.message || String(err);
      log(`加载失败：${msg}`, "err");
      // 常见错误提示
      if (/Network error|fetch|404|403|Access-Control|CORS/i.test(msg)) {
        log("💡 排查：检查 URL 拼写；浏览器拒绝混合 http/https；服务器需带 Access-Control-Allow-Origin: *", "err");
        log("💡 如果用本地模型：在仓库根目录运行 python docs/demo/serve.py --model-dir <你的模型目录>，然后填 http://127.0.0.1:8765/...", "err");
        log("💡 GitHub Pages 部署版（https://255856.github.io/Smart-Desktop-Pet/）无法直接访问你本机的 127.0.0.1，参见顶部横幅", "err");
      }
      throw err;
    }
  }
  _fit() {
    if (!this.model || !this.app) return;
    const w = this.app.screen.width, h = this.app.screen.height;
    const s = Math.min((w / this.model.width) * 0.94, (h / this.model.height) * 0.94);
    this.model.scale.set(s);
    try { this.model.anchor.set(0.5, 0.5); } catch (e) {}
    this.model.x = w / 2; this.model.y = h / 2;
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
      this.model.focus(this.model.width / 2 + nx * this.model.width / 2,
                        this.model.height / 2 + ny * this.model.height / 2);
    } catch (e) {}
  }
  shutdown() {
    this._talking = false;
    if (this._mouthTimer) { clearInterval(this._mouthTimer); this._mouthTimer = null; }
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
    this.currentEngine = "edge";  // edge / gptsovits / minimax
    this._currentUtter = null;
    this.refreshVoices();
    if (this.synth) this.synth.onvoiceschanged = () => this.refreshVoices();
  }
  refreshVoices() {
    if (!this.synth) return;
    this.voices = this.synth.getVoices().filter(v => v.lang.startsWith("zh") || v.lang.startsWith("en"));
    // 也保留所有 voice（包括 ja、ko 等），让用户自由选
    this.allVoices = this.synth.getVoices();
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
    if (v) { this._pinnedVoice = v; log(`TTS 锁定语音：${v.name}（${v.lang}）`, "ok"); }
    return v;
  }
  // 真实 speak：调用 SpeechSynthesis 同步驱动口型
  speak(text, onEnd) {
    if (!this.synth) { log("浏览器不支持 SpeechSynthesis", "err"); onEnd && onEnd(); return; }
    this.synth.cancel();
    // 选一个 voice
    let voice = null;
    if (this._pinnedVoice) {
      // 用户手动锁定的（如 Xiaoxiao）
      voice = this._pinnedVoice;
    } else if (this.voices.length > 0) {
      // 按引擎匹配
      const pref = this.currentEngine === "edge" ? ["xiaoxiao", "小晓", "female", "女"] :
                   this.currentEngine === "gptsovits" ? ["yunjian", "云健", "male", "男"] :
                   /* minimax */ ["yating", "云夏", "xiaoxiao", "小晓"];
      voice = this.voices.find(v => pref.some(p => v.name.toLowerCase().includes(p.toLowerCase())))
            || this.voices[0];
    }
    const utt = new SpeechSynthesisUtterance(text);
    if (voice) utt.voice = voice;
    utt.lang = "zh-CN";
    utt.rate = 1.0; utt.pitch = 1.0;
    // 用 TTS 边界事件驱动口型（更精准：TTS 真的在说话时 mouth 开合）
    utt.onstart = () => {
      window.demo.startTalk();
      log(`🔊 TTS 开始 (${this.currentEngine}): ${text.slice(0, 30)}${text.length > 30 ? '…' : ''}`);
    };
    utt.onend = () => {
      window.demo.stopTalk();
      log(`🔇 TTS 结束`, "ok");
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
      log(`🎤 ASR: ${(finalText + interim).slice(0, 40)}${(finalText + interim).length > 40 ? '…' : ''}`);
      onResult && onResult(finalText + interim, !!finalText);
    };
    this.recognition.onerror = (e) => {
      log(`ASR 错误：${e.error}`, "err");
      this.listening = false;
      onEnd && onEnd();
    };
    this.recognition.onend = () => {
      this.listening = false;
      log(`🎤 ASR 结束: "${finalText}"`, "ok");
      onEnd && onEnd();
    };
    this.recognition.start();
    log("🎤 ASR 开始（说话）", "ok");
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
      { title: `[Mock] ${query} 的搜索结果 1`, url: "https://example.com/1",
        content: `这是关于「${query}」的模拟搜索结果。在 Tavily key 配置后会显示真实内容。` },
      { title: `[Mock] ${query} 的搜索结果 2`, url: "https://example.com/2",
        content: `另一个相关结果，包含 ${query} 的延伸信息。` },
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
    log(`💾 记忆：${content}（重要度 ${importance}）`, "ok");
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
    log(`🗑️ 遗忘 ${before - this.items.length} 条`);
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
    log(`⏰ 提醒已设：${minutes} 分钟后「${content}」`, "ok");
    return item;
  }
  list() { return this.items.slice(); }
  remove(id) {
    const before = this.items.length;
    this.items = this.items.filter(x => x.id !== id);
    this._save();
    log(`🗑️ 取消 ${before - this.items.length} 个提醒`);
    return before - this.items.length;
  }
  tick() {
    const now = Date.now();
    const fire = this.items.filter(x => x.fireAt <= now);
    for (const r of fire) {
      log(`⏰ 提醒触发：${r.content}`, "ok");
      window.tts.speak(`提醒：${r.content}`);
      // 浏览器通知（如已授权）
      if ("Notification" in window && Notification.permission === "granted") {
        new Notification("桌宠提醒", { body: r.content });
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
      systemPrompt: systemPrompt || this.cfg.systemPrompt ||
        "你是桌宠「小白」，性格温柔黏人。回复 1-2 句话（30 字以内），像真人对主人说话。",
    };
    this._saveCfg();
  }
  hasKey() { return !!(this.cfg.apiKey && this.cfg.apiKey.length > 10); }
  status() { return this.hasKey() ? `${this.cfg.model} @ ${this.cfg.baseUrl}` : "未配置"; }

  // OpenAI 兼容 /chat/completions（流式）
  async chatStream(messages, onDelta, onDone, onError) {
    if (!this.hasKey()) {
      onError && onError(new Error("未配置 API key"));
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
            const delta = obj.choices?.[0]?.delta?.content || "";
            if (delta) {
              fullText += delta;
              onDelta && onDelta(delta, fullText);
            }
          } catch (e) {}
        }
      }
      onDone && onDone(fullText);
    } catch (e) {
      onError && onError(e);
    }
  }
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
  _getTimeContext() {
    const h = new Date().getHours();
    if (h < 6) return "深夜";
    if (h < 12) return "上午";
    if (h < 18) return "下午";
    return "晚上";
  }
  async _fire() {
    this._schedule();
    const timeCtx = this._getTimeContext();
    const memorySample = window.memory.recent(3).map(m => m.content);

    // mock 兜底（无 API key 时）
    const fallback = () => {
      const candidates = [
        `主人现在是${timeCtx}，要不要休息一下？${memorySample.length ? '上次你说 ' + memorySample[0].slice(0, 15) + '，进展如何？' : ''}`,
        `今天${timeCtx}好，记得多喝水呀～`,
        `主人看起来坐了很久了，起来动一动吧！`,
        `我刚才在发呆想主人～${memorySample.length > 1 ? '对了，' + memorySample[1].slice(0, 15) + '，后续怎么样了？' : ''}`,
        `${timeCtx}安，要不要听一首歌？`,
        `主人，我在呢，有事随时叫我。`,
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
          content: `现在是${timeCtx}。${memorySample.length ? '最近记忆：' + memorySample.join('；') : ''}
请用 1 句话（≤30 字）主动搭话主人，像真人在微信里突然冒出来。`
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
        log(`⚠️ LLM 调用失败：${e.message}（用 mock 兜底）`, "err");
        return fallback();
      }
    }
    fallback();
  }

  _show(remark) {
    log(`💭 ProactiveBrain 主动搭话：${remark}`, "ok");
    window.trace.add("remark", remark);
    const timeCtx = this._getTimeContext();
    const emotion = timeCtx === "上午" ? "happy" : timeCtx === "下午" ? "neutral" : "sleepy";
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

  // 简单意图识别
  const t = text.trim();
  // 1. 提醒类
  const remindMatch = t.match(/(\d+)\s*(分钟|秒|小时|秒钟)\s*(?:之后?|后)\s*(?:提醒|叫我|告诉我|喊我|记得)?\s*(.*)/);
  if (remindMatch) {
    const n = parseInt(remindMatch[1]);
    const unit = remindMatch[2];
    const content = remindMatch[3] || `${t}`;
    const minutes = unit === "秒" || unit === "秒钟" ? n / 60 :
                    unit === "小时" ? n * 60 : n;
    window.reminders.add(content || "该做事了", minutes);
    const reply = `好的，${n}${unit}后提醒你「${content || '该做事了'}」`;
    appendChatBubble("assistant", reply);
    window.tts.speak(reply);
    window.demo._emitEmotion("happy");
    return;
  }
  // 2. 搜索类
  if (/搜索|查一下|查查|看看|搜一下|帮我找|search/i.test(t)) {
    window.trace.add("tool_call", `web_search("${t}")`);
    const tavilyKey = localStorage.getItem("tavily_api_key") || "";
    try {
      const results = await webSearch(t, tavilyKey);
      window.trace.add("tool_result", `${results.length} 个结果`);
      const summary = results.slice(0, 3).map((r, i) =>
        `${i+1}. ${r.title}\n   ${r.content.slice(0, 100)}${r.content.length > 100 ? '…' : ''}`
      ).join("\n\n");
      const reply = `搜索到 ${results.length} 个结果：\n\n${summary}`;
      appendChatBubble("assistant", reply);
      window.tts.speak(`搜索到 ${results.length} 个结果，第一条是 ${results[0].title}`);
      window.demo._emitEmotion("surprised");
      return;
    } catch (e) {
      log(`搜索失败：${e.message}`, "err");
    }
  }
  // 3. 记忆类
  const memMatch = t.match(/(?:记住|记一下|别忘了|记着|记下)\s*[:：，,]?\s*(.+)/);
  if (memMatch) {
    window.memory.remember(memMatch[1].trim(), 7);
    const reply = `好的，我记住了「${memMatch[1].trim()}」`;
    appendChatBubble("assistant", reply);
    window.tts.speak(reply);
    window.demo._emitEmotion("happy");
    return;
  }
  // 4. 桌宠本体动作
  if (/笑一下|开心点|伤心|难过|睡觉|起床|跳舞|动一动/i.test(t)) {
    const exprList = (window.demo.model && window.demo.model.internalModel.settings.expressions) || [];
    const randomExpr = exprList[Math.floor(Math.random() * exprList.length)];
    if (randomExpr) window.demo.setExpression(randomExpr.Name || randomExpr.name);
    const reply = `好～`;
    appendChatBubble("assistant", reply);
    window.tts.speak(reply);
    window.demo._emitEmotion("happy");
    return;
  }
  // 5. 真实 LLM 调用（OpenAI 兼容 API；mock 兜底）
  window.trace.add("llm_call", `chat("${t.slice(0, 30)}${t.length > 30 ? '…' : ''}")`);
  const memoryCtx = window.memory.recent(3).map(m => m.content).join('；');

  if (!window.llm.hasKey()) {
    // 无 API key：mock 兜底
    await new Promise(r => setTimeout(r, 300));
    const fallbackReplies = [
      `主人说的是「${t.slice(0, 20)}${t.length > 20 ? '…' : ''}」对吧？我想想...`,
      `嗯嗯，我听到了。`,
      `好的，主人。${t.endsWith('?') || t.endsWith('？') ? '让我想想这个问题...' : ''}`,
      `收到～`,
    ];
    const reply = fallbackReplies[Math.floor(Math.random() * fallbackReplies.length)];
    window.trace.add("llm_response", reply);
    appendChatBubble("assistant", reply);
    window.tts.speak(reply);
    return;
  }

  // 真 API 调用
  const messages = [
    { role: "user", content: t },
    ...(memoryCtx ? [{ role: "system", content: `主人最近记忆：${memoryCtx}` }] : []),
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
        reply = full.trim() || "（模型未返回）";
        bubble.textContent = reply;
        window.trace.add("llm_response", reply);
        window.tts.speak(reply);
      },
      (err) => {
        bubble.textContent = `❌ 调用失败：${err.message}`;
        log(`❌ LLM 调用失败：${err.message}`, "err");
      }
    );
  } catch (e) {
    bubble.textContent = `❌ ${e.message}`;
    log(`❌ LLM 异常：${e.message}`, "err");
  }
}

// ============================================================
// 11. UI 绑定
// ============================================================
// 环境横幅：检测是 GitHub Pages 还是本地访问
(function setupEnvBanner() {
  const banner = document.getElementById("env-banner");
  const text = document.getElementById("env-banner-text");
  if (!banner || !text) return;
  const host = location.hostname.toLowerCase();
  const isGithubPages = /\.github\.io$/.test(host);
  if (isGithubPages) {
    text.innerHTML =
      '你正在访问 <strong>GitHub Pages 部署版</strong>（' + host + '）。' +
      '这里 <strong>无法直接加载你本机的模型</strong>（127.0.0.1 指向 GitHub 服务器，不是你电脑）。' +
      '要加载自己的模型：在仓库根运行 <code>python docs/demo/serve.py --port 8765 --root .</code>，' +
      '然后访问 <code>http://127.0.0.1:8765/docs/demo/index.html</code>。';
    banner.classList.remove("hidden");
  }
  const closeBtn = document.getElementById("env-banner-close");
  if (closeBtn) closeBtn.addEventListener("click", () => banner.classList.add("hidden"));
})();

// 内置 Hiyori Pro 模型（零配置自动加载）
// GitHub Pages 部署：模型放在 gh-pages 根的 hiyori_zh-Hans/ 下，index.html 在根
// 本地 serve.py：模型放在仓库根 docs/demo/hiyori_zh-Hans/，index.html 在 docs/demo/
// 因此路径需要按 IS_GITHUB_PAGES 切换
const IS_GITHUB_PAGES = /\.github\.io$/.test(location.hostname);
const BUILTIN_MODEL_PATHS = IS_GITHUB_PAGES ? [
  // GitHub Pages：index.html 在根，hiyori_zh-Hans/ 也在根
  "hiyori_zh-Hans/hiyori_pro/runtime/hiyori_pro_t11.model3.json",
  "hiyori_zh-Hans/hiyori_free/runtime/hiyori_free_t08.model3.json",
] : [
  // 本地 serve.py (--root E:/study/desktop-pet)：index.html 在 docs/demo/，
  // hiyori_zh-Hans/ 在 docs/demo/ 同级
  "hiyori_zh-Hans/hiyori_pro/runtime/hiyori_pro_t11.model3.json",
  "hiyori_zh-Hans/hiyori_free/runtime/hiyori_free_t08.model3.json",
];

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
  if (window.demo._talking) { window.demo.stopTalk(); e.target.textContent = "🔊 测试口型"; }
  else { window.demo.startTalk(); e.target.textContent = "🛑 停止口型"; }
});

// 重新加载按钮（如果用户主动想刷）
const reloadBtn = document.getElementById("reload-btn");
if (reloadBtn) reloadBtn.addEventListener("click", () => loadBuiltinModel());

// 自动加载内置 Hiyori 模型
async function loadBuiltinModel() {
  showLoader("加载内置模型…");
  setStatus("加载 Hiyori…", "#ffce5c");
  for (const p of BUILTIN_MODEL_PATHS) {
    try {
      // 用 GET（不是 HEAD）—— GitHub Pages 对 HEAD 支持不一致
      const r = await fetch(p, { method: "GET" });
      if (r.ok) {
        log(`✓ 找到内置模型：${p}`, "ok");
        const url = location.origin + location.pathname.replace(/index\.html?$/, "") + p;
        await window.demo.loadModel(url);
        return;
      } else {
        log(`探测 ${p} → ${r.status}`, "ok");
      }
    } catch (e) {
      log(`探测 ${p} 异常：${e.message}`, "err");
    }
  }
  hideLoader();
  setStatus("内置模型未找到", "#ff7675");
  log("❌ demo 内置的 Hiyori 模型未部署，请检查 hiyori_zh-Hans/ 目录", "err");
}
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
    asrBtn.textContent = "🎤 正在听…";
    window.asr.start(
      (text, isFinal) => {
        if (isFinal) document.getElementById("chat-input").value = text;
      },
      () => {
        asrBtn.classList.remove("recording");
        asrBtn.textContent = "🎤 按住说话";
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
    if (!window.tts || !window.tts.allVoices || window.tts.allVoices.length === 0) return;
    voiceSel.innerHTML = '<option value="">自动（按引擎选）</option>';
    for (const v of window.tts.allVoices) {
      const opt = document.createElement("option");
      opt.value = v.name;
      const marker = /xiaoxiao|小晓/i.test(v.name) ? " 🔥小晓" :
                     /yunjian|云健/i.test(v.name) ? " 男" :
                     /yating|云夏/i.test(v.name) ? " 女" :
                     /yunxi|云希/i.test(v.name) ? " 男" :
                     /female|女/i.test(v.name) ? " 女" :
                     /male|男/i.test(v.name) ? " 男" : "";
      opt.textContent = `${v.name} (${v.lang})${marker}`;
      voiceSel.appendChild(opt);
    }
    log(`📢 可用语音 ${window.tts.allVoices.length} 个（找小晓请选含 'Xiaoxiao' 的）`, "ok");
  }
  // 延迟多次尝试，因为 voiceschanged 触发有延迟
  setTimeout(fillVoices, 500);
  setTimeout(fillVoices, 2000);
  setTimeout(fillVoices, 5000);
  if (window.speechSynthesis) {
    window.speechSynthesis.onvoiceschanged = fillVoices;
  }
  voiceSel.addEventListener("change", (e) => {
    if (!e.target.value) {
      window.tts._pinnedVoice = null;
      log("TTS 解除语音锁定（按引擎自动选）", "ok");
    } else {
      window.tts.setVoiceByName(e.target.value);
    }
  });
})();

// Tavily key 配置
const tavilyBtn = document.getElementById("tavily-btn");
if (tavilyBtn) {
  tavilyBtn.addEventListener("click", () => {
    const key = prompt("输入 Tavily API key（留空用 mock）：", localStorage.getItem("tavily_api_key") || "");
    if (key !== null) {
      localStorage.setItem("tavily_api_key", key);
      log(`Tavily key ${key ? "已设置" : "已清空"}`, "ok");
    }
  });
}

// LLM API 配置（OpenAI 兼容）
const llmBtn = document.getElementById("llm-btn");
if (llmBtn) {
  llmBtn.addEventListener("click", () => {
    const cur = window.llm.cfg;
    // 用 prompt 分多步收集（demo 简单实现，未来可换 form modal）
    const baseUrl = prompt("Base URL (OpenAI 兼容)：", cur.baseUrl || "https://api.openai.com/v1");
    if (!baseUrl) return;
    const apiKey = prompt("API Key：", cur.apiKey || "");
    if (!apiKey) return;
    const model = prompt("模型名（如 gpt-4o-mini / deepseek-chat / qwen-turbo）：", cur.model || "gpt-4o-mini");
    if (!model) return;
    const sysDefault = "你是桌宠「小白」，性格温柔黏人。回复 1-2 句话（30 字以内），像真人对主人说话。";
    const systemPrompt = prompt("System Prompt（回车用默认）：", cur.systemPrompt || sysDefault) || sysDefault;
    window.llm.setConfig({ baseUrl, apiKey, model, systemPrompt });
    log(`✅ LLM 已配置：${window.llm.status()}`, "ok");
    log(`现在桌宠会调用真实 LLM 生成回复（不再用 mock）`, "ok");
  });
}

// 记忆面板
const memBtn = document.getElementById("mem-btn");
if (memBtn) {
  memBtn.addEventListener("click", () => {
    const items = window.memory.list();
    if (items.length === 0) { appendChatBubble("assistant", "（暂无记忆）"); return; }
    const summary = items.slice(-5).map(m => `· ${m.content}`).join("\n");
    appendChatBubble("assistant", `最近 ${items.length} 条记忆：\n${summary}`);
    window.tts.speak(`我记着 ${items.length} 件事`);
  });
}
const forgetBtn = document.getElementById("forget-btn");
if (forgetBtn) {
  forgetBtn.addEventListener("click", () => {
    const q = prompt("遗忘包含此关键词的记忆：");
    if (q) { window.memory.forget(q); }
  });
}

// 提醒面板
const remindBtn = document.getElementById("remind-btn");
if (remindBtn) {
  remindBtn.addEventListener("click", () => {
    const mins = prompt("几分钟后来提醒？");
    if (mins && !isNaN(parseInt(mins))) {
      const content = prompt("提醒内容？") || "该做事了";
      window.reminders.add(content, parseInt(mins));
    }
  });
}
const remindListBtn = document.getElementById("remind-list-btn");
if (remindListBtn) {
  remindListBtn.addEventListener("click", () => {
    const items = window.reminders.list();
    if (items.length === 0) { appendChatBubble("assistant", "（暂无提醒）"); return; }
    const summary = items.map(r => `· ${new Date(r.fireAt).toLocaleTimeString()} - ${r.content}`).join("\n");
    appendChatBubble("assistant", `${items.length} 个待触发提醒：\n${summary}`);
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

// ============================================================
// 12. 启动
// ============================================================
window.addEventListener("DOMContentLoaded", () => {
  (async () => {
    try { if (window.__sdkReady) await window.__sdkReady; }
    catch (e) {
      setStatus("SDK 加载失败", "#ff7675");
      log("SDK 加载失败：" + (e.message || e), "err");
      return;
    }
    const st = window.__sdkLoadStatus || {};
    log(`SDK 加载完成：`);
    if (st.pixi)    log(`  pixi    ← ${st.pixi.replace(/^ok:/, "")}`);
    if (st.core)    log(`  core    ← ${st.core.replace(/^ok:/, "")}`);
    if (st.cubism4) log(`  cubism4 ← ${st.cubism4.replace(/^ok:/, "")}`, "ok");
    if (typeof PIXI === "undefined") { setStatus("PIXI 未加载", "#ff7675"); return; }
    if (typeof Live2DCubismCore === "undefined") { setStatus("Cubism Core 未加载", "#ff7675"); return; }
    if (!PIXI.live2d || !PIXI.live2d.Live2DModel || typeof PIXI.live2d.Live2DModel.from !== "function") {
      setStatus("Live2DModel.from 不可用", "#ff7675");
      log("pixi-live2d-display 未注册 Live2DModel.from", "err");
      return;
    }
    try { window.demo.ensureApp(); }
    catch (e) { setStatus("PIXI 初始化失败", "#ff7675"); log(e.message || e, "err"); return; }
    setStatus("就绪 · 请填写 Model URL（或拖入 .model3.json）", "#55efc4");
    log(`PIXI v${PIXI.VERSION} · Live2DModel.from 已就绪`, "ok");
    if (window.asr.available) log("✅ ASR 可用（按住说话按钮）", "ok");
    else log("⚠️ ASR 不可用（请用 Chrome / Edge）", "err");
    if (window.tts.voices.length) log(`✅ TTS 可用（${window.tts.voices.length} 个语音）`, "ok");
    else log("⚠️ TTS 暂未加载语音", "err");
    // 零配置自动加载内置 Hiyori Pro 模型
    loadBuiltinModel();
  })();
});
