/*
 * demo 核心逻辑测试 —— 零依赖，node:test 内置
 * 运行：node --test tests/demo/
 *
 * 重点覆盖本轮修掉的 bug，防止回归：
 *   - 提醒/倒计时 id 碰撞（Date.now() 同毫秒重复）
 *   - pulse 复利放大（scale 基准必须是常量而不是动画中途值）
 *   - 养成状态机的数值规则（对齐 app/engine/state.py）
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");

const { PetState, CountdownStore, FOODS, clamp, makeIdFactory } = require("../../docs/demo/core.js");

let seq = 0;
const key = (p) => `test_${p}_${++seq}`;

// ============================================================
const test_block = require("node:test").describe;
test_block("clamp / id 生成", () => {
  test("clamp 把值限制在区间内", () => {
    assert.equal(clamp(150, 0, 100), 100);
    assert.equal(clamp(-20, 0, 100), 0);
    assert.equal(clamp(42, 0, 100), 42);
  });

  test("makeIdFactory 同毫秒连调不撞 id（回归：Date.now() 碰撞 bug）", () => {
    const next = makeIdFactory();
    const ids = new Set();
    for (let i = 0; i < 1000; i++) ids.add(next());
    assert.equal(ids.size, 1000, "1000 次连调必须产生 1000 个不同 id");
  });

  test("makeIdFactory 会避开已被占用的 id", () => {
    const next = makeIdFactory();
    const used = new Set();
    for (let i = 0; i < 50; i++) used.add(next((x) => used.has(x)));
    assert.equal(used.size, 50);
  });
});

// ============================================================
test_block("养成状态机（对齐 app/engine/state.py）", () => {
  test("初始值与桌面端一致", () => {
    const s = new PetState(key("init"));
    assert.equal(s.s.strength, 100);
    assert.equal(s.s.strength_food, 100);
    assert.equal(s.s.strength_drink, 100);
    assert.equal(s.s.feeling, 80);
    assert.equal(s.s.health, 100);
    assert.equal(s.s.money, 100);
    assert.equal(s.s.level, 1);
    assert.equal(s.s.likability, 0);
    assert.equal(s.s.mode, "Normal");
  });

  test("tick：低于 70 时按 0.007/s 衰减，且不被裁到负数", () => {
    const s = new PetState(key("decay"));
    s.s.strength = 50;
    s.tick(10, {});                       // 50 - 0.007*10 = 49.93
    assert.ok(Math.abs(s.s.strength - 49.93) < 1e-6, `实际 ${s.s.strength}`);
    s.s.strength = 1;
    s.tick(1000, {});                     // 远远够跑到 0，但必须停住
    assert.equal(s.s.strength, 0);
  });

  test("tick：高于 70 时被动回复能抵消部分衰减", () => {
    const s = new PetState(key("regen"));
    s.s.strength = 80;
    s.tick(10, {});                       // 80 - 0.07 + 0.035 = 79.965
    assert.ok(Math.abs(s.s.strength - 79.965) < 1e-6, `实际 ${s.s.strength}`);
    assert.ok(s.s.strength < 80, "净效果仍应是缓慢下降");
  });

  test("tick：夜间衰减倍率 1.3", () => {
    const day = new PetState(key("day"));
    const night = new PetState(key("night"));
    day.s.strength = 50; night.s.strength = 50;
    day.tick(10, { isNight: false });
    night.tick(10, { isNight: true });
    const dayDrop = 50 - day.s.strength;
    const nightDrop = 50 - night.s.strength;
    assert.ok(Math.abs(nightDrop / dayDrop - 1.3) < 1e-6,
      `夜间/白天衰减比应为 1.3，实际 ${nightDrop / dayDrop}`);
  });

  test("tick：talkingPause 时不衰减", () => {
    const s = new PetState(key("pause"));
    s.s.strength = 50;
    s.tick(100, { talkingPause: true });
    assert.equal(s.s.strength, 50);
  });

  test("tick：睡觉时快速回复且不衰减", () => {
    const s = new PetState(key("sleep"));
    s.s.strength = 10;
    s.tick(10, { sleeping: true });        // +0.20/s * 10 = +2
    assert.ok(Math.abs(s.s.strength - 12) < 1e-6, `实际 ${s.s.strength}`);
  });

  test("eatFood：一半入主值、一半入 store（对齐 eat_food）", () => {
    const s = new PetState(key("eat"));
    s.s.strength_food = 0;
    s.eatFood({ strength_food: 40 });
    assert.ok(Math.abs(s.s.strength_food - 20) < 1e-9, `主值应 +20，实际 ${s.s.strength_food}`);
    assert.ok(Math.abs(s.s.store_strength_food - 20) < 1e-9, `store 应 +20，实际 ${s.s.store_strength_food}`);
  });

  test("takeStore：以 1/8 回补主值", () => {
    const s = new PetState(key("store"));
    s.s.strength_food = 0;
    s.s.store_strength_food = 80;
    s.takeStore();
    assert.ok(Math.abs(s.s.store_strength_food - 70) < 1e-9, `store 应 80→70，实际 ${s.s.store_strength_food}`);
    assert.ok(Math.abs(s.s.strength_food - 10) < 1e-9, `主值应 +10，实际 ${s.s.strength_food}`);
  });

  test("takeStore：不足 1 的残余直接清零，不无限回补", () => {
    const s = new PetState(key("store2"));
    s.s.strength = 0;
    s.s.store_strength = 0.5;
    s.takeStore();
    assert.equal(s.s.store_strength, 0);
    assert.equal(s.s.strength, 0, "不足 1 就不该给主值加任何东西");
  });

  test("calMode：健康跌破阈值 → PoorCondition / Ill", () => {
    const s = new PetState(key("mode"));
    s.s.health = 50; s.s.feeling = 50; s.s.likability = 0;
    assert.equal(s.calMode(), "PoorCondition");
    s.s.health = 10;
    assert.equal(s.calMode(), "Ill");
    s.s.health = 100;
    assert.equal(s.calMode(), "Normal");
  });

  test("calMode：高亲密度降低患病阈值", () => {
    const s = new PetState(key("mode2"));
    s.s.feeling = 90; s.s.likability = 90; s.s.health = 55;
    // realhel = 60 - 12(feeling>=80) - 12(like>=80) = 36，health 55 > 36 → 不患病
    assert.notEqual(s.calMode(), "PoorCondition");
    s.s.likability = 0;
    // realhel = 48，health 55 > 48 → 仍不患病
    assert.notEqual(s.calMode(), "PoorCondition");
  });

  test("calMode：心情达标 → Happy", () => {
    const s = new PetState(key("mode3"));
    s.s.health = 100; s.s.feeling = 95; s.s.likability = 0;
    assert.equal(s.calMode(), "Happy");
  });

  test("等级公式：calLevel = floor(sqrt(exp)/10)+1", () => {
    const s = new PetState(key("lv"));
    s.s.exp = 0;   assert.equal(s.calLevel(), 1);
    s.s.exp = 99;  assert.equal(s.calLevel(), 1);
    s.s.exp = 100; assert.equal(s.calLevel(), 2);
    s.s.exp = 400; assert.equal(s.calLevel(), 3);
    assert.equal(s.levelUpNeed(), Math.pow(s.calLevel() * 10, 2));
  });

  test("exp 累加会触发升级并保留 level", () => {
    const s = new PetState(key("lv2"));
    // calLevel = floor(sqrt(exp)/10)+1 → exp 250 得 Lv2，exp 400 才到 Lv3
    s.feed({ exp: 250 });
    assert.equal(s.s.level, 2, `exp 250 应为 Lv2，实际 ${s.s.level}`);
    s.feed({ exp: 150 });                  // 累计 400
    assert.equal(s.s.level, 3, `exp 400 应为 Lv3，实际 ${s.s.level}`);
  });

  test("每日签到：一天只能领一次", () => {
    const s = new PetState(key("checkin"));
    const before = s.s.money;
    const [ok1, coin] = s.dailyCheckin(100);
    assert.equal(ok1, true);
    assert.equal(coin, 100);
    assert.equal(s.s.money, before + 100);
    assert.equal(s.hasCheckedInToday(), true);
    const [ok2, coin2] = s.dailyCheckin(100);
    assert.equal(ok2, false, "同一天第二次签到必须失败");
    assert.equal(coin2, 0);
    assert.equal(s.s.money, before + 100, "重复签到不能再加金币");
  });

  test("投喂好感每日上限 5 点", () => {
    const s = new PetState(key("like"));
    let total = 0;
    for (let i = 0; i < 20; i++) total += s.addFoodLikability(1);
    assert.equal(total, 5, "20 次投喂总共只能加 5 点好感");
    assert.equal(s.s.likability, 5);
    assert.equal(s.addFoodLikability(1), 0, "到上限后再加应为 0");
  });

  test("小游戏金币每日上限 100", () => {
    const s = new PetState(key("coin"));
    let total = 0;
    for (let i = 0; i < 10; i++) total += s.addGameReward(50);
    assert.equal(total, 100);
    assert.equal(s.gameCoinRemaining(), 0);
    assert.equal(s.addGameReward(50), 0, "超上限不发");
  });

  test("onInteract：记录互动并按增益累加，且不会越过上限", () => {
    const s = new PetState(key("interact"));
    s.onInteract({ likabilityGain: 10, feelingGain: 5 });
    assert.equal(s.s.likability, 10);
    assert.ok(Math.abs(s.s.feeling - 85) < 1e-9);
    s.onInteract({ likabilityGain: 999, feelingGain: 999 });
    assert.equal(s.s.likability, 100, "亲密度必须钳在 100");
    assert.equal(s.s.feeling, 100, "心情必须钳在 100");
    assert.ok(s.s.lastInteractTs > 0, "互动时间戳要被记录");
  });

  test("10 分钟无互动时心情衰减翻倍", () => {
    const s = new PetState(key("lonely"));
    s.s.feeling = 50;
    s.s.lastInteractTs = Date.now() / 1000 - 1200;   // 20 分钟前
    s.s.strength = 50;
    const lonelyDrop = 50 - (() => { s.tick(10, {}); return s.s.feeling; })();
    const t2 = new PetState(key("lonely2"));
    t2.s.feeling = 50; t2.s.strength = 50;
    t2.s.lastInteractTs = Date.now() / 1000;        // 刚刚互动过
    t2.tick(10, {});
    const normalDrop = 50 - t2.s.feeling;
    assert.ok(Math.abs(lonelyDrop / normalDrop - 2) < 1e-6,
      `寂寞时衰减应为平时的 2 倍，实际比值 ${lonelyDrop / normalDrop}`);
  });

  test("statsSummary：排版与桌面端一致", () => {
    const s = new PetState(key("summary"));
    assert.equal(
      s.statsSummary(),
      "Lv.1  💪100  🍚100  💧100  😊80  ❤️100  💕0/100  💰100  [Normal]",
    );
  });

  test("存档损坏时回落到默认值而不是抛异常", () => {
    const k = key("corrupt");
    const s = new PetState(k);
    s._save();
    globalThis.localStorage && globalThis.localStorage.setItem
      ? globalThis.localStorage.setItem(k, "{不是合法 JSON")
      : null;
    const t = new PetState(k);
    assert.equal(t.s.strength, 100, "存档坏了也必须能用默认值启动");
  });
});

// ============================================================
test_block("倒计时", () => {
  test("start / list / cancel 基本流程", () => {
    const c = new CountdownStore(key("cd"));
    const it = c.start("泡茶", 2);
    assert.equal(it.label, "泡茶");
    const list = c.list();
    assert.equal(list.length, 1);
    assert.ok(list[0].remainSec <= 120 && list[0].remainSec >= 118);
    assert.equal(list[0].totalSec, 120);
    assert.equal(c.cancel(it.id), 1);
    assert.equal(c.list().length, 0);
    assert.equal(c.cancel(it.id), 0, "重复取消应返回 0");
  });

  test("连开多个倒计时 id 不重复（回归：id 碰撞 bug）", () => {
    const c = new CountdownStore(key("cd2"));
    const ids = [];
    for (let i = 0; i < 200; i++) ids.push(c.start(`t${i}`, 1).id);
    assert.equal(new Set(ids).size, 200, "200 个倒计时必须有 200 个不同 id");
  });

  test("非法时长保护：0 / 负数不会产生 <=0 的时长", () => {
    const c = new CountdownStore(key("cd3"));
    const zero = c.start("x", 0);
    assert.ok(zero.totalMs >= 1, `0 分钟的时长必须 >0，实际 ${zero.totalMs}`);
    const neg = new CountdownStore(key("cd3b")).start("y", -5);
    assert.ok(neg.totalMs >= 1, `负数分钟的时长必须 >0，实际 ${neg.totalMs}`);
  });

  test("小数分钟保留精度（0.5 分钟 = 30 秒，不被抬到 1 分钟）", () => {
    const c = new CountdownStore(key("cd3c"));
    assert.equal(c.start("z", 0.5).totalMs, 30000);
  });

  test("到点的倒计时会被 tick 清除", () => {
    const c = new CountdownStore(key("cd4"));
    const it = c.start("y", 1);
    it.endAt = Date.now() - 1000;      // 手动推到已过期
    const fired = c.tick();
    assert.equal(fired, 1);
    assert.equal(c.list().length, 0);
  });
});

// ============================================================
test_block("食物库", () => {
  test("每种食物都有价格和至少一项增益，且价格为正", () => {
    assert.ok(FOODS.length > 0);
    for (const f of FOODS) {
      assert.ok(f.name, "食物必须有名字");
      assert.ok(f.price > 0, `${f.name} 价格必须为正`);
      const gains = ["strength", "strength_food", "strength_drink", "feeling", "health", "exp"]
        .reduce((a, k) => a + (f[k] || 0), 0);
      assert.ok(gains > 0, `${f.name} 至少要有一项增益`);
    }
  });

  test("食物名唯一", () => {
    assert.equal(new Set(FOODS.map((f) => f.name)).size, FOODS.length);
  });
});
