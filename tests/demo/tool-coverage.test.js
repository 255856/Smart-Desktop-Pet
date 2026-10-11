/*
 * Live2D Demo · 全部 24 个工具的 Function Calling 兼容性测试
 * ----------------------------------------------------------
 * 跑遍 demo 注册的所有工具，验证两件事：
 *   1. OpenAI 风格 SSE（每个 chunk name 只在首个发）：工具名正确解析
 *   2. MiniMax/Groq 风格 SSE（每个 chunk 重发完整 name）：工具名仍正确
 *
 * 跑：node --test tests/demo/tool-coverage.test.js
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");

// ============================================================================
// 从 demo 复制 _readStream（修复版）
// ============================================================================
async function _readStream(resp, onDelta) {
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
        const delta = obj.choices && obj.choices[0] && obj.choices[0].delta || {};
        if (delta.content) { content += delta.content; onDelta && onDelta(delta.content, content); }
        if (Array.isArray(delta.tool_calls)) {
          for (const tc of delta.tool_calls) {
            const idx = tc.index || 0;
            if (!toolCalls[idx]) toolCalls[idx] = { id: "", name: "", args: "" };
            if (tc.id) toolCalls[idx].id = tc.id;
            if (tc.function && tc.function.name && !toolCalls[idx].name) {
              toolCalls[idx].name = tc.function.name;
            }
            if (tc.function && tc.function.arguments) toolCalls[idx].args += tc.function.arguments;
          }
        }
      } catch (e) {}
    }
  }
  return { content, toolCalls: toolCalls.filter(Boolean) };
}

// ============================================================================
// SSE mock
// ============================================================================
function sseFromChunks(chunks) {
  const enc = new TextEncoder();
  const body = new ReadableStream({
    async start(c) {
      for (const chunk of chunks) c.enqueue(enc.encode(chunk));
      c.close();
    },
  });
  return { body, ok: true };
}
function sseChunk(obj) { return "data: " + JSON.stringify(obj) + "\n\n"; }
function sseDone() { return "data: [DONE]\n\n"; }

// ============================================================================
// Demo 全部 24 个工具（与 docs/demo/live2d-demo.js 同步）
// ============================================================================
const ALL_TOOLS = [
  { name: "get_current_time", args: {} },
  { name: "countdown", args: { label: "泡茶", minutes: 5 } },
  { name: "list_countdowns", args: {} },
  { name: "cancel_countdown", args: { id: 1 } },
  { name: "say_to_user", args: { text: "你好" } },
  { name: "date_info", args: {} },
  { name: "calculate", args: { expression: "1+1" } },
  { name: "convert_units", args: { value: 1, from: "km", to: "mile" } },
  { name: "get_weather", args: { location: "北京" } },
  { name: "web_search", args: { query: "天气" } },
  { name: "remember_fact", args: { content: "我喜欢蓝色", importance: 8 } },
  { name: "recall_memory", args: { query: "颜色" } },
  { name: "forget_memory", args: { keyword: "蓝色" } },
  { name: "add_reminder", args: { content: "喝水", minutes: 3 } },
  { name: "list_reminders", args: {} },
  { name: "delete_reminder", args: { id: 1 } },
  { name: "open_website", args: { url: "https://github.com" } },
  { name: "clipboard_copy", args: { text: "复制" } },
  { name: "send_notification", args: { title: "提醒", body: "消息" } },
  { name: "change_pet_emotion", args: { emotion: "happy" } },
  { name: "play_animation", args: { name: "wave" } },
  { name: "get_pet_status", args: {} },
  { name: "feed_self", args: { food: "面包" } },
  { name: "daily_checkin", args: {} },
];

// ============================================================================
// OpenAI 风格：name 只在第一个 chunk 发完整，后续 chunk name 为空
// ============================================================================
for (const tool of ALL_TOOLS) {
  test(`OpenAI 风格 → ${tool.name}`, async () => {
    const argStr = JSON.stringify(tool.args);
    const resp = sseFromChunks([
      sseChunk({ choices: [{ delta: { tool_calls: [{ index: 0, id: "call_x", function: { name: tool.name, arguments: argStr } }] } }] }),
      sseDone(),
    ]);
    const { toolCalls } = await _readStream(resp, () => {});
    assert.equal(toolCalls.length, 1);
    assert.equal(toolCalls[0].name, tool.name, `name 应为 ${tool.name}，实际 ${toolCalls[0].name}`);
    assert.deepEqual(JSON.parse(toolCalls[0].args), tool.args, `args 应为 ${JSON.stringify(tool.args)}`);
  });
}

// ============================================================================
// MiniMax/Groq 风格：每个 chunk 都重发完整 name
// ============================================================================
for (const tool of ALL_TOOLS) {
  test(`MiniMax 风格（name 每 chunk 重发）→ ${tool.name}`, async () => {
    const argStr = JSON.stringify(tool.args);
    // 把 args 切成 3 段，模拟分片；每个 chunk 都重发完整 name
    const len = argStr.length;
    const p1 = argStr.slice(0, Math.floor(len / 3));
    const p2 = argStr.slice(Math.floor(len / 3), Math.floor(2 * len / 3));
    const p3 = argStr.slice(Math.floor(2 * len / 3));
    const resp = sseFromChunks([
      sseChunk({ choices: [{ delta: { tool_calls: [{ index: 0, id: "call_y", function: { name: tool.name, arguments: "" } }] } }] }),
      sseChunk({ choices: [{ delta: { tool_calls: [{ index: 0, function: { name: tool.name, arguments: p1 } }] } }] }),
      sseChunk({ choices: [{ delta: { tool_calls: [{ index: 0, function: { name: tool.name, arguments: p2 } }] } }] }),
      sseChunk({ choices: [{ delta: { tool_calls: [{ index: 0, function: { name: tool.name, arguments: p3 } }] } }] }),
      sseDone(),
    ]);
    const { toolCalls } = await _readStream(resp, () => {});
    assert.equal(toolCalls.length, 1);
    assert.equal(toolCalls[0].name, tool.name, `name 应为 ${tool.name}（不重复拼接），实际 ${toolCalls[0].name}`);
    assert.deepEqual(JSON.parse(toolCalls[0].args), tool.args, `args 应能完整拼接`);
  });
}

// ============================================================================
// 混合：2 个工具并行调用（OpenAI 风格），按 index 拼装
// ============================================================================
test("混合：2 个 tool 并行调用（OpenAI 风格）", async () => {
  const resp = sseFromChunks([
    sseChunk({ choices: [{ delta: { tool_calls: [
      { index: 0, id: "call_a", function: { name: "get_current_time", arguments: "" } },
      { index: 1, id: "call_b", function: { name: "add_reminder", arguments: "" } },
    ] } }] }),
    sseChunk({ choices: [{ delta: { tool_calls: [
      { index: 0, function: { arguments: "{}" } },
      { index: 1, function: { arguments: '{"content":"喝水","minutes":3}' } },
    ] } }] }),
    sseDone(),
  ]);
  const { toolCalls } = await _readStream(resp, () => {});
  assert.equal(toolCalls.length, 2);
  assert.equal(toolCalls[0].name, "get_current_time");
  assert.equal(toolCalls[1].name, "add_reminder");
  assert.equal(toolCalls[1].args, '{"content":"喝水","minutes":3}');
});

// ============================================================================
// 混合：2 个工具并行 + name 重发（MiniMax 风格）
// ============================================================================
test("混合：2 个 tool 并行 + name 每 chunk 重发", async () => {
  const resp = sseFromChunks([
    sseChunk({ choices: [{ delta: { tool_calls: [
      { index: 0, id: "call_a", function: { name: "get_current_time", arguments: "" } },
      { index: 1, id: "call_b", function: { name: "add_reminder", arguments: "" } },
    ] } }] }),
    sseChunk({ choices: [{ delta: { tool_calls: [
      { index: 0, function: { name: "get_current_time", arguments: "{}" } },
      { index: 1, function: { name: "add_reminder", arguments: '{"content":"喝水"' } },
    ] } }] }),
    sseChunk({ choices: [{ delta: { tool_calls: [
      { index: 1, function: { name: "add_reminder", arguments: ',"minutes":3}' } },
    ] } }] }),
    sseDone(),
  ]);
  const { toolCalls } = await _readStream(resp, () => {});
  assert.equal(toolCalls.length, 2);
  assert.equal(toolCalls[0].name, "get_current_time");
  assert.equal(toolCalls[1].name, "add_reminder");
  assert.equal(toolCalls[1].args, '{"content":"喝水","minutes":3}');
});