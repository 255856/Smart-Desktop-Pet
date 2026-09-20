"use strict";

/*
 * Desktop Pet · Live2D Demo（尽量还原桌面版功能）
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
// 0. 工具：日志 / 加载状态 / 工具栏（保留之前版本）
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
      setStatus(`就绪 · ${exprs.length} 表情 / ${Object.keys(motions).length} 组动作`, "#55efc4");
      log(`模型加载完成：${exprs.length} 表情 / ${Object.keys(motions).length} 组动作`, "ok");
      if (window.startIdleBehavior) window.startIdleBehavior();
      this._emitEmotion("neutral");
      return { expressions: exprs, motions };
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
  }
  listVoices() {
    return this.voices.map((v, i) => ({
      idx: i, name: v.name, lang: v.lang, local: v.localService
    }));
  }
  setEngine(engine) {
    this.currentEngine = engine;
    log(`TTS 引擎切换：${engine}`, "ok");
    // 不同引擎用不同语音（仅作为模拟）
    this.refreshVoices();
  }
  // 真实 speak：调用 SpeechSynthesis 同步驱动口型
  speak(text, onEnd) {
    if (!this.synth) { log("浏览器不支持 SpeechSynthesis", "err"); onEnd && onEnd(); return; }
    this.synth.cancel();
    // 选一个 voice（按引擎）
    let voice = null;
    if (this.voices.length > 0) {
      // edge 偏好女声、gptsovits 偏好男声、minimax 任意
      const pref = this.currentEngine === "edge" ? ["female", "女"] :
                   this.currentEngine === "gptsovits" ? ["male", "男"] : [];
      voice = this.voices.find(v => pref.some(p => v.name.toLowerCase().includes(p))) || this.voices[0];
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
// 8. 主动搭话（按时间上下文生成 mock 关心话语）
// ============================================================
class ProactiveBrain {
  constructor() {
    this.lastRemarks = [];
    this.minMin = 1;   // demo 用 1-2 分钟（桌面 25-45 分钟太长）
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
  _fire() {
    this._schedule();
    const timeCtx = this._getTimeContext();
    const memorySample = window.memory.recent(3).map(m => m.content);
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
    log(`💭 ProactiveBrain 主动搭话：${remark}`, "ok");
    window.trace.add("remark", remark);
    // 桌面情绪映射（简化：按时段）
    const emotion = timeCtx === "上午" ? "happy" : timeCtx === "下午" ? "neutral" : "sleepy";
    window.demo._emitEmotion(emotion);
    // TTS + UI 显示
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
  // 5. 模拟"智能体"回复（mock LLM）
  window.trace.add("llm_call", "mock chat");
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
}

// ============================================================
// 11. UI 绑定
// ============================================================
let dropHint = null, dragDepth = 0;
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
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => {
  e.preventDefault();
  dragDepth = 0;
  if (dropHint) dropHint.classList.remove("show");
  const file = e.dataTransfer.files && e.dataTransfer.files[0];
  if (!file) return;
  if (!file.name.endsWith(".model3.json")) { log("请拖入 .model3.json 文件", "err"); return; }
  const url = URL.createObjectURL(file);
  const urlInput = document.getElementById("model-url");
  if (urlInput) urlInput.value = `file://${file.name}（内存中）`;
  if (window.demo) window.demo.loadModel(url);
});

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
  if (window.demo._talking) { window.demo.stopTalk(); e.target.textContent = "🔊 测试口型"; }
  else { window.demo.startTalk(); e.target.textContent = "🛑 停止口型"; }
});
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
    // 填默认 URL（如果有）
    const savedUrl = localStorage.getItem("last_model_url");
    if (savedUrl) document.getElementById("model-url").value = savedUrl;
    // 保存 URL
    const urlInput = document.getElementById("model-url");
    if (urlInput) {
      urlInput.addEventListener("change", () => localStorage.setItem("last_model_url", urlInput.value));
    }
  })();
});
