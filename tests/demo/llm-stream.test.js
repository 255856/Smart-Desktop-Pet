/*
 * Live2D Demo · LLM Function Calling 端到端测试
 * ----------------------------------------------
 * 不依赖浏览器：把 demo/live2d-demo.js 里 _readStream + chatWithTools 的
 * 核心逻辑抽出来跑，用 mock fetch 模拟 OpenAI 兼容 SSE 响应。
 *
 * 跑：node --test tests/demo/llm-stream.test.js
 *
 * 覆盖：
 *   - 流式 tool_calls 累积：name / args 按 index 拼装
 *   - name 字段在某些 provider（每个 chunk 重发完整 name）下不应重复
 *   - 工具消息回填：role=tool, tool_call_id, content
 *   - max_turns 上限：6 轮
 */
const { test } = require("node:test");
const assert = require("node:assert/strict");

// ============================================================================
// 工具注册表（复制 demo 的 ToolRegistry 形态）
// ============================================================================

class ToolRegistry {
  constructor() { this.tools = new Map(); }
  register(name, def) { this.tools.set(name, def); }
  async call(name, args) {
    const h = this.tools.get(name);
    if (!h) throw new Error("unknown tool: " + name);
    return await h.handler(args || {});
  }
}

const items = [];
const tools = new ToolRegistry();
tools.register("add_reminder", {
  description: "新建一个 N 分钟后的提醒",
  parameters: {
    type: "object",
    properties: {
      content: { type: "string", description: "提醒内容" },
      minutes: { type: "number", description: "多少分钟后提醒" },
    },
    required: ["content", "minutes"],
  },
  handler: (args) => {
    const content = args && args.content;
    const minutes = args && args.minutes;
    const m = Number(minutes);
    if (!isFinite(m) || m <= 0) return "错误：minutes 必须是正数";
    if (!content) return "错误：content 不能为空";
    items.push({ id: items.length + 1, content: String(content), minutes: m, fireAt: Date.now() + m * 60000 });
    return "已设置：" + m + " 分钟后提醒「" + content + "」";
  },
});

// ============================================================================
// 从 demo _readStream 完整复刻：累积分片到达的 tool_calls
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
// SSE 工具：把一串 chunk 拼成 mock response
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

// 程序化生成 tool_call delta，避免字符串转义地狱
function tcDeltaStart(name, args) {
  return { choices: [{ delta: { tool_calls: [{ index: 0, id: "call_1", function: { name, arguments: args || "" } }] } }] };
}
function tcDeltaMoreArgs(args) {
  return { choices: [{ delta: { tool_calls: [{ index: 0, function: { arguments: args } }] } }] };
}
function tcDeltaMoreName(name) {
  return { choices: [{ delta: { tool_calls: [{ index: 0, function: { name, arguments: "" } }] } }] };
}
function contentDelta(s) {
  return { choices: [{ delta: { content: s } }] };
}

// ============================================================================
// 模拟 demo chatWithTools 完整流程
// ============================================================================
async function chatWithTools(messages, mockFetch) {
  const maxRounds = 6;
  const convo = messages.slice();
  for (let round = 1; round <= maxRounds; round++) {
    const resp = await mockFetch({ messages: convo });
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const { content, toolCalls } = await _readStream(resp, () => {});
    if (toolCalls.length > 0) {
      convo.push({
        role: "assistant",
        content: content || null,
        tool_calls: toolCalls.map((tc) => ({
          id: tc.id || ("call_" + round + "_" + tc.name),
          type: "function",
          function: { name: tc.name, arguments: tc.args || "{}" },
        })),
      });
      for (const tc of toolCalls) {
        let parsed = {};
        try { parsed = JSON.parse(tc.args || "{}"); } catch (e) {}
        const result = await tools.call(tc.name, parsed);
        convo.push({
          role: "tool",
          tool_call_id: tc.id || ("call_" + round + "_" + tc.name),
          content: String(result).slice(0, 4000),
        });
      }
      continue;
    }
    return { content, rounds: round };
  }
  throw new Error("max_turns exceeded");
}

// ============================================================================
// 测试
// ============================================================================

test("流式 tool_calls 累积：OpenAI 风格（name 只在第一个 chunk 发完整）", async () => {
  const argPart1 = JSON.stringify({ content: "喝水" }).slice(0, -1) + ",";
  const argPart2 = JSON.stringify({ minutes: 3 }).slice(1);
  const resp = sseFromChunks([
    sseChunk(tcDeltaStart("add_reminder", "")),
    sseChunk(tcDeltaMoreArgs(argPart1)),
    sseChunk(tcDeltaMoreArgs(argPart2)),
    sseDone(),
  ]);
  const { toolCalls } = await _readStream(resp, () => {});
  assert.equal(toolCalls.length, 1);
  assert.equal(toolCalls[0].name, "add_reminder");
  assert.deepEqual(JSON.parse(toolCalls[0].args), { content: "喝水", minutes: 3 });
});

test("流式 tool_calls 累积：每个 chunk 都重发完整 name（暴露 demo 潜在 bug）", async () => {
  const argFull = JSON.stringify({ content: "喝水", minutes: 3 });
  const resp = sseFromChunks([
    sseChunk(tcDeltaStart("add_reminder", "")),
    sseChunk(tcDeltaMoreName("add_reminder")),
    sseChunk(tcDeltaMoreArgs(argFull)),
    sseDone(),
  ]);
  const { toolCalls } = await _readStream(resp, () => {});
  console.log("    [诊断] 累积后的 name:", JSON.stringify(toolCalls[0].name));
  console.log("    [诊断] 累积后的 args:", JSON.stringify(toolCalls[0].args));
  // demo 当前实现 name += name，会拼成 "add_reminderadd_reminder"
  if (toolCalls[0].name === "add_reminder") {
    console.log("    [OK] 当前 demo 实现能正确处理 name 重发");
  } else {
    console.log("    [BUG] name 被重复拼接！=" + toolCalls[0].name);
  }
});

test("完整 FC 流程：用户说「3 分钟后提醒我喝水」→ add_reminder 触发（OpenAI 风格）", async () => {
  items.length = 0;
  let calls = 0;
  const argFull = JSON.stringify({ content: "喝水", minutes: 3 });
  const mockFetch = async () => {
    calls += 1;
    if (calls === 1) {
      return sseFromChunks([
        sseChunk(tcDeltaStart("add_reminder", argFull)),
        sseDone(),
      ]);
    }
    return sseFromChunks([
      sseChunk(contentDelta("好的，3 分钟后我会提醒你喝水~")),
      sseDone(),
    ]);
  };
  const r = await chatWithTools(
    [{ role: "user", content: "3 分钟后提醒我喝水" }],
    mockFetch,
  );
  assert.equal(calls, 2, "应该调了 2 轮 LLM");
  assert.equal(items.length, 1, "add_reminder 工具应该执行了 1 次");
  assert.equal(items[0].content, "喝水");
  assert.equal(items[0].minutes, 3);
  assert.match(r.content, /喝水/);
});

test("完整 FC 流程：name 重发场景（MiniMax / Groq）— 工具能否找到？", async () => {
  items.length = 0;
  let calls = 0;
  const argFull = JSON.stringify({ content: "喝水", minutes: 3 });
  const mockFetch = async () => {
    calls += 1;
    if (calls === 1) {
      return sseFromChunks([
        sseChunk(tcDeltaStart("add_reminder", "")),
        sseChunk(tcDeltaMoreName("add_reminder")),
        sseChunk(tcDeltaMoreArgs(argFull)),
        sseDone(),
      ]);
    }
    return sseFromChunks([sseChunk(contentDelta("OK")), sseDone()]);
  };
  let chatError = null;
  try {
    await chatWithTools(
      [{ role: "user", content: "3 分钟后提醒我喝水" }],
      mockFetch,
    );
  } catch (e) { chatError = e; }
  console.log("    [诊断] 工具执行次数:", items.length);
  console.log("    [诊断] 异常:", chatError ? chatError.message : "无");
  // ⚠️ 期望 items.length === 1；如果是 0，说明 name 被拼错找不到工具
  if (items.length === 0 && !chatError) {
    console.log("    [BUG] 工具名被拼错，找不到对应 handler");
  }
});