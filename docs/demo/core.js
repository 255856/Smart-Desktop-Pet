/*
 * Desktop Pet · Live2D Demo —— 可测试核心
 * ----------------------------------------------------
 * 这里只放「纯逻辑」：养成状态机、倒计时、id 生成、数值裁剪。
 * 它们不碰 DOM / 网络 / 浏览器 API（或只做安全降级），
 * 因此可以被 Node 的 node --test 直接 require，也能在浏览器里当普通脚本用。
 *
 * browser  : <script src="core.js"></script>  → 挂到 globalThis
 * Node     : require("./core.js")            → module.exports
 *
 * 对齐来源：app/engine/state.py（PetState）与 ReminderStore 约定。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else Object.assign(root, api);
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const STAT_MAX = 100;
  const LIKABILITY_MAX = 100;

  // —— 安全降级：浏览器里有 log/t/tts/localStorage，Node 里都没有 ——
  function _log(msg, level) {
    try { if (typeof log === "function") log(msg, level); } catch (e) {}
  }
  function _t(key, params) {
    try {
      if (typeof window !== "undefined" && window && typeof window.t === "function") {
        return window.t(key, params);
      }
    } catch (e) {}
    // 没装 i18n 时自己把占位符替掉，至少不是 undefined
    let s = String(key);
    if (params) s = s.replace(/\{(\w+)\}/g, (_, k) => (params[k] != null ? params[k] : ""));
    return s;
  }
  function _speak(text) {
    try { if (typeof window !== "undefined" && window && window.tts) window.tts.speak(text); } catch (e) {}
  }
  const _memoryStore = new Map();
  function _storage() {
    try {
      if (typeof localStorage !== "undefined" && localStorage) return localStorage;
    } catch (e) {}
    return {
      getItem: (k) => (_memoryStore.has(k) ? _memoryStore.get(k) : null),
      setItem: (k, v) => _memoryStore.set(k, String(v)),
      removeItem: (k) => _memoryStore.delete(k),
    };
  }
  const _now = () => (typeof Date !== "undefined" ? Date.now() : 0);

  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

  // demo 侧食物库：结构对齐桌面端 items（价格 + 各状态增益）。
  // 桌面端从数据文件动态加载，这里给一份精简内置库。
  const FOODS = [
    { name: "面包",   price: 10, strength_food: 20, strength: 5,  feeling: 3, exp: 3 },
    { name: "牛奶",   price: 8,  strength_drink: 25, feeling: 2,  exp: 2 },
    { name: "便当",   price: 25, strength_food: 40, strength: 10, health: 5, exp: 5 },
    { name: "奶茶",   price: 18, strength_drink: 15, strength_food: 10, feeling: 8, exp: 4 },
    { name: "营养餐", price: 40, strength_food: 50, strength_drink: 50, health: 15, feeling: 5, exp: 8 },
  ];

  // 唯一 id：同一毫秒内多次生成不能撞。
  // 桌面端 / 旧版 demo 都用 Date.now()，LLM 一轮里连调两次 add_reminder 就会撞，
  // 导致 delete_reminder 一次删掉两条。
  function makeIdFactory() {
    let seq = 0;
    return function nextId(taken) {
      seq += 1;
      let id = _now() * 1000 + (seq % 1000);
      const used = taken || (() => false);
      while (used(id)) id += 1;
      return id;
    };
  }

  // —— 倒计时（对齐桌面 countdown） ——
  class CountdownStore {
    constructor(key) {
      this.key = key || "desktop_pet_countdowns";
      this.items = this._load();
      this._timer = null;
      this._nextId = makeIdFactory();
    }
    _load() {
      try { return JSON.parse(_storage().getItem(this.key)) || []; } catch (e) { return []; }
    }
    _save() {
      try { _storage().setItem(this.key, JSON.stringify(this.items)); }
      catch (e) { _log(`倒计时写入失败（本地存储已满或被禁用）：${e.message || e}`, "err"); }
    }
    start(label, minutes) {
      const totalMs = Math.max(1, Math.round(minutes * 60 * 1000));
      const item = { id: this._nextId((x) => this.items.some((i) => i.id === x)), label, endAt: _now() + totalMs, totalMs, announced: {} };
      this.items.push(item);
      this._save();
      _log(`倒计时开始：${label}（${minutes} 分钟）`, "ok");
      return item;
    }
    list() {
      const now = _now();
      return this.items.map((x) => ({
        id: x.id, label: x.label,
        remainSec: Math.max(0, Math.round((x.endAt - now) / 1000)),
        totalSec: Math.round(x.totalMs / 1000),
      }));
    }
    cancel(id) {
      const before = this.items.length;
      this.items = this.items.filter((x) => x.id !== id);
      if (before !== this.items.length) {
        this._save();
        _log(`已取消倒计时（${before - this.items.length} 个）`);
      }
      return before - this.items.length;
    }
    tick() {
      const now = _now();
      for (const c of this.items) {
        const left = Math.round((c.endAt - now) / 1000);
        for (const mark of [600, 300, 60]) {
          if (left <= mark && left > mark - 5 && !c.announced[mark]) {
            c.announced[mark] = true;
            _speak(_t("cd_mark", { n: left }));
          }
        }
      }
      const done = this.items.filter((x) => x.endAt <= now);
      for (const d of done) {
        _log(`倒计时结束：${d.label}`, "ok");
        _speak(_t("cd_done", { label: d.label }));
      }
      if (done.length) { this.items = this.items.filter((x) => x.endAt > now); this._save(); }
      return done.length;
    }
    startTicking(intervalMs) {
      if (this._timer) return;
      this._timer = setInterval(() => this.tick(), intervalMs || 1000);
    }
    stop() { if (this._timer) { clearInterval(this._timer); this._timer = null; } }
  }

  // —— 养成状态机（逐条对齐 app/engine/state.py） ——
  class PetState {
    constructor(key) {
      this.key = key || "desktop_pet_state";
      this.s = {
        strength: 100, strength_food: 100, strength_drink: 100,
        feeling: 80, health: 100,
        store_strength: 0, store_strength_food: 0, store_strength_drink: 0,
        money: 100, exp: 0, level: 1, likability: 0, mode: "Normal",
        decay_strength: 0.007, decay_strength_food: 0.007, decay_strength_drink: 0.007,
        decay_feeling: 0.007,
        regen_strength: 0.0035, regen_strength_food: 0.0035, regen_strength_drink: 0.0035,
        regen_feeling: 0.0035, regen_threshold: 70, healthy_min: 40,
        night_multiplier: 1.3,
        lastInteractTs: 0,
        feed_like_date: "", feed_like_count: 0,
        checkin_date: "",
        game_coin_date: "", game_coin_count: 0,
      };
      this._nextId = makeIdFactory();
      this._load();
      this.normalize();
    }
    _load() {
      try {
        const raw = _storage().getItem(this.key);
        if (raw) Object.assign(this.s, JSON.parse(raw));
      } catch (e) { /* 存档损坏则用默认值 */ }
    }
    _save() {
      try { _storage().setItem(this.key, JSON.stringify(this.s)); }
      catch (e) { _log(`养成存档写入失败（本地存储已满或被禁用）：${e.message || e}`, "err"); }
    }
    reset() {
      try { _storage().removeItem(this.key); } catch (e) {}
      const fresh = new PetState(this.key);
      Object.assign(this.s, fresh.s);
    }
    normalize() {
      const s = this.s;
      for (const k of ["strength", "strength_food", "strength_drink", "feeling", "health"]) {
        s[k] = clamp(Number(s[k]) || 0, 0, STAT_MAX);
      }
      s.likability = clamp(Number(s.likability) || 0, 0, LIKABILITY_MAX);
      for (const k of ["store_strength", "store_strength_food", "store_strength_drink",
                       "money", "exp", "feed_like_count", "game_coin_count"]) {
        s[k] = Math.max(0, Number(s[k]) || 0);
      }
      s.level = Math.max(1, parseInt(s.level, 10) || 1);
    }
    // —— 等级 / 经验（对齐 cal_level / level_up_need） ——
    calLevel() { return this.s.exp < 0 ? 1 : Math.floor(Math.sqrt(this.s.exp) / 10) + 1; }
    levelUpNeed() { return Math.pow(this.calLevel() * 10, 2); }
    get expToNext() { return Math.max(0, this.levelUpNeed() - this.s.exp); }
    _maybeLevelup() {
      const nl = this.calLevel();
      if (nl > this.s.level) {
        this.s.level = nl;
        _log(`升级到 Lv.${nl}（exp=${this.s.exp.toFixed(1)}）`, "ok");
        _speak(_t("lv_up", { lv: nl }));
      }
    }
    // —— 状态模式判定（对齐 cal_mode） ——
    calMode() {
      const s = this.s;
      let realhel = 60;
      if (s.feeling >= 80) realhel -= 12;
      if (s.likability >= 80) realhel -= 12;
      else if (s.likability >= 40) realhel -= 6;
      if (s.health <= realhel) return s.health <= realhel / 2 ? "Ill" : "PoorCondition";
      let realfel = 0.90;
      if (s.likability >= 80) realfel -= 0.20;
      else if (s.likability >= 40) realfel -= 0.10;
      const felps = s.feeling / STAT_MAX;
      if (felps >= realfel) return "Happy";
      if (felps <= realfel / 2) return "PoorCondition";
      return "Normal";
    }
    _reevaluateMode() {
      const nm = this.calMode();
      if (nm !== this.s.mode) {
        _log(`状态：${this.s.mode} → ${nm}`);
        this.s.mode = nm;
      }
    }
    feed(d) {
      d = d || {};
      const s = this.s;
      s.strength = clamp(s.strength + (d.strength || 0), 0, STAT_MAX);
      s.strength_food = clamp(s.strength_food + (d.strength_food || 0), 0, STAT_MAX);
      s.strength_drink = clamp(s.strength_drink + (d.strength_drink || 0), 0, STAT_MAX);
      s.feeling = clamp(s.feeling + (d.feeling || 0), 0, STAT_MAX);
      s.health = clamp(s.health + (d.health || 0), 0, STAT_MAX);
      s.likability = clamp(s.likability + (d.likability || 0), 0, LIKABILITY_MAX);
      if (d.exp) { s.exp += d.exp; this._maybeLevelup(); }
      if (d.money_delta) s.money = Math.max(0, s.money + d.money_delta);
      this._reevaluateMode();
      this._save();
    }
    eatFood(d) {
      d = d || {};
      const s = this.s;
      const pairs = [["strength", "store_strength"], ["strength_food", "store_strength_food"],
                     ["strength_drink", "store_strength_drink"]];
      for (const [main, store] of pairs) {
        const v = (d[main] || 0) / 2;
        s[main] = clamp(s[main] + v, 0, STAT_MAX);
        s[store] += v;
      }
      s.feeling = clamp(s.feeling + (d.feeling || 0), 0, STAT_MAX);
      s.health = clamp(s.health + (d.health || 0), 0, STAT_MAX);
      s.likability = clamp(s.likability + (d.likability || 0), 0, LIKABILITY_MAX);
      if (d.exp) { s.exp += d.exp; this._maybeLevelup(); }
      this._reevaluateMode();
      this._save();
    }
    takeStore() {
      const s = this.s;
      const pairs = [["strength", "store_strength"], ["strength_food", "store_strength_food"],
                     ["strength_drink", "store_strength_drink"]];
      for (const [main, store] of pairs) {
        const v = s[store];
        if (v < 1.0) { s[store] = 0; continue; }
        const d = v / 8.0;
        s[store] = Math.max(0, v - d);
        s[main] = clamp(s[main] + d, 0, STAT_MAX);
      }
    }
    addMoney(delta) { this.s.money = Math.max(0, this.s.money + delta); this._save(); }
    _feelingDecay() {
      if (this.s.lastInteractTs && (_now() / 1000 - this.s.lastInteractTs) > 600) {
        return this.s.decay_feeling * 2.0;
      }
      return this.s.decay_feeling;
    }
    static isNight(d) {
      const hour = (d || new Date()).getHours();
      return hour >= 22 || hour < 6;
    }
    tick(dtSeconds, opts) {
      opts = opts || {};
      if (opts.talkingPause) return;
      const s = this.s;
      const mult = opts.isNight ? s.night_multiplier : 1.0;
      if (opts.sleeping) {
        s.strength = clamp(s.strength + 0.20 * dtSeconds, 0, STAT_MAX);
        s.strength_food = clamp(s.strength_food + 0.10 * dtSeconds, 0, STAT_MAX);
        s.strength_drink = clamp(s.strength_drink + 0.10 * dtSeconds, 0, STAT_MAX);
        s.feeling = clamp(s.feeling + 0.05 * dtSeconds, 0, STAT_MAX);
        s.health = clamp(s.health + 0.05 * dtSeconds, 0, STAT_MAX);
        this.takeStore(); this._reevaluateMode(); this._save();
        return;
      }
      const rows = [
        ["strength", s.decay_strength, s.regen_strength],
        ["strength_food", s.decay_strength_food, s.regen_strength_food],
        ["strength_drink", s.decay_strength_drink, s.regen_strength_drink],
        ["feeling", this._feelingDecay(), s.regen_feeling],
      ];
      for (const [name, decayVal, regenVal] of rows) {
        const cur = Math.max(0, s[name]);
        let v = cur - decayVal * dtSeconds * mult;
        if (cur > s.regen_threshold) v += regenVal * dtSeconds;
        s[name] = clamp(v, 0, STAT_MAX);
      }
      const food = s.strength_food, drink = s.strength_drink, feeling = s.feeling;
      if (food < s.healthy_min && drink < s.healthy_min && feeling < s.healthy_min) {
        s.health = Math.max(0, s.health - 0.03 * dtSeconds * mult);
      } else if (food > s.healthy_min && drink > s.healthy_min && feeling > 60.0) {
        s.health = Math.min(STAT_MAX, s.health + 0.02 * dtSeconds);
      }
      this.takeStore();
      this._reevaluateMode();
      this._save();
    }
    onInteract(opts) {
      opts = opts || {};
      this.s.lastInteractTs = _now() / 1000;
      if (opts.feelingGain) this.s.feeling = clamp(this.s.feeling + opts.feelingGain, 0, STAT_MAX);
      if (opts.likabilityGain) this.s.likability = clamp(this.s.likability + opts.likabilityGain, 0, LIKABILITY_MAX);
      if (opts.feed) this.feed({ strength_food: 20, strength_drink: 20, feeling: 10, exp: 2 });
      this._reevaluateMode();
      this._save();
    }
    static today() {
      const d = new Date();
      const p = (n) => String(n).padStart(2, "0");
      return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
    }
    addFoodLikability(like) {
      const today = PetState.today();
      if (this.s.feed_like_date !== today) { this.s.feed_like_date = today; this.s.feed_like_count = 0; }
      const allowed = Math.max(0, 5.0 - this.s.feed_like_count);
      const added = Math.min(Math.max(0, like), allowed);
      this.s.feed_like_count += added;
      if (added > 0) { this.s.likability = clamp(this.s.likability + added, 0, LIKABILITY_MAX); this._save(); }
      return added;
    }
    hasCheckedInToday() { return this.s.checkin_date === PetState.today(); }
    dailyCheckin(reward) {
      const r = reward === undefined ? 100.0 : reward;
      const today = PetState.today();
      if (this.s.checkin_date === today) return [false, 0];
      this.s.checkin_date = today;
      this.s.money = Math.max(0, this.s.money + Math.max(0, r));
      this._save();
      return [true, Math.max(0, r)];
    }
    addGameReward(amount) {
      const today = PetState.today();
      if (this.s.game_coin_date !== today) { this.s.game_coin_date = today; this.s.game_coin_count = 0; }
      const added = Math.min(Math.max(0, amount), Math.max(0, 100 - this.s.game_coin_count));
      this.s.game_coin_count += added;
      if (added > 0) { this.s.money += added; this._save(); }
      return added;
    }
    gameCoinRemaining() {
      const c = this.s.game_coin_date === PetState.today() ? this.s.game_coin_count : 0;
      return Math.max(0, 100 - c);
    }
    // —— 展示摘要：排版逐字对齐桌面 stats_summary ——
    statsSummary() {
      const s = this.s;
      const f = (v) => v.toFixed(0);
      return `Lv.${s.level}  💪${f(s.strength)}  🍚${f(s.strength_food)}  💧${f(s.strength_drink)}  `
           + `😊${f(s.feeling)}  ❤️${f(s.health)}  💕${f(s.likability)}/${f(LIKABILITY_MAX)}  `
           + `💰${f(s.money)}  [${s.mode}]`;
    }
  }

  return { PetState, CountdownStore, FOODS, clamp, makeIdFactory, STAT_MAX, LIKABILITY_MAX };
});
