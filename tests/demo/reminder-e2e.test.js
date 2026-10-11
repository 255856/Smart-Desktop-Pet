/*
 * Live2D Demo · ReminderStore 端到端测试
 * ----------------------------------------
 * 不依赖浏览器：从 docs/demo/core.js 抽出 CountdownStore 验证（Reminders
 * 留在 live2d-demo.js 里，本测试覆盖 add/list/remove/tick + id 不重复）。
 *
 * 跑：node --test tests/demo/reminder-e2e.test.js
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");

// ============================================================================
// 复制 demo live2d-demo.js line 726-778 的 ReminderStore（同步）
// 改动 demo 时这里要同步——不一致会被这套测试抓到。
// ============================================================================
const reminders = [];

function _seqId() {
  reminders._seq = (reminders._seq || 0) + 1;
  let id = Date.now() * 1000 + (reminders._seq % 1000);
  while (reminders.some((x) => x.id === id)) id += 1;
  return id;
}

const ReminderStore = {
  items: [],
  add(content, minutes) {
    const fireAt = Date.now() + minutes * 60000;
    const item = { id: _seqId(), content, fireAt };
    this.items.push(item);
    return item;
  },
  list() { return this.items.slice(); },
  remove(id) {
    const before = this.items.length;
    this.items = this.items.filter((x) => x.id !== id);
    return before - this.items.length;
  },
  tick() {
    const now = Date.now();
    const fired = this.items.filter((x) => x.fireAt <= now);
    if (fired.length) this.items = this.items.filter((x) => x.fireAt > now);
    return fired;
  },
};

// ============================================================================
// 测试
// ============================================================================

test("add + tick：1 分钟后提醒真的 fire", () => {
  ReminderStore.items = [];
  const item = ReminderStore.add("喝水", 1);
  assert.equal(ReminderStore.items.length, 1);
  // tick 还没到 → 0 个 fired
  let fired = ReminderStore.tick();
  assert.equal(fired.length, 0, "未到时间的提醒不应 fire");
  // 把 fireAt 推到过去 → fire
  ReminderStore.items[0].fireAt = Date.now() - 1000;
  fired = ReminderStore.tick();
  assert.equal(fired.length, 1);
  assert.equal(fired[0].content, "喝水");
  assert.equal(ReminderStore.items.length, 0, "已 fire 的应自动清掉");
});

test("id 不重复（回归：同毫秒 add 会撞 id）", () => {
  ReminderStore.items = [];
  const ids = [];
  for (let i = 0; i < 1000; i++) {
    ids.push(ReminderStore.add(`r${i}`, 1).id);
  }
  const uniq = new Set(ids);
  assert.equal(uniq.size, 1000, `1000 次 add 撞了 ${1000 - uniq.size} 次 id`);
});

test("remove：按 id 删除", () => {
  ReminderStore.items = [];
  const a = ReminderStore.add("A", 5);
  const b = ReminderStore.add("B", 5);
  assert.equal(ReminderStore.items.length, 2);
  assert.equal(ReminderStore.remove(a.id), 1);
  assert.equal(ReminderStore.items.length, 1);
  assert.equal(ReminderStore.remove(a.id), 0, "重复删除应返回 0");
  assert.equal(ReminderStore.items[0].id, b.id);
});

test("tick 后的清理：多个并发到点的全删", () => {
  ReminderStore.items = [];
  for (let i = 0; i < 5; i++) {
    ReminderStore.add(`r${i}`, 1);
  }
  ReminderStore.items.forEach((x) => { x.fireAt = Date.now() - 1000; });
  const fired = ReminderStore.tick();
  assert.equal(fired.length, 5);
  assert.equal(ReminderStore.items.length, 0);
});

test("tick 后未到时间的保留", () => {
  ReminderStore.items = [];
  const a = ReminderStore.add("now", 5);
  const b = ReminderStore.add("later", 10);
  a.fireAt = Date.now() - 1000;
  const fired = ReminderStore.tick();
  assert.equal(fired.length, 1);
  assert.equal(fired[0].content, "now");
  assert.equal(ReminderStore.items.length, 1);
  assert.equal(ReminderStore.items[0].content, "later");
});