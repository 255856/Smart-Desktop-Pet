# 桌面宠物 · Smart Desktop Pet

> **一个真正能"养"的桌面 AI 宠物** —— 不只是会动、会说话，而是会**记住你、主动搭话、调用工具、跨多轮思考**，还能**陪你下五子棋、下中国象棋、玩狼人杀**的数字伙伴。
>
> A truly *livestockable* desktop AI companion — remembers you, speaks proactively, calls tools, reasons across turns, and plays Gomoku, Xiangqi & Werewolf with you.

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)](https://www.python.org)
[![PyQt5](https://img.shields.io/badge/UI-PyQt5%2BWebEngine-41CD52?logo=qt&logoColor=white)](https://riverbankcomputing.com)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-674%20passed%20%2F%206%20skipped-brightgreen?logo=pytest)](tests/)
[![Verify](https://img.shields.io/badge/verify_features-82%20checks-blueviolet)](scripts/verify_features.py)
[![Live2D Demo](https://img.shields.io/badge/Live2D%20Demo-Try%20Online-6c5ce7?logo=githubpages&logoColor=white)](https://255856.github.io/Smart-Desktop-Pet/)

[中文](#5-分钟跑起来) · [English](#5-min-quick-start) · [**Live2D 在线 Demo / Live2D Demo**](https://255856.github.io/Smart-Desktop-Pet/) · [文档 / Docs](docs/)

---

## 它能做什么？ / What can it do?

- **真智能 / Real intelligence**：手写 **ReAct Agent** 循环 + **3 层抗幻觉**（强制调工具 → 跨轮持续到 max_turns → 工具结果直接总结），可选 **LangChain** 后端
  / Hand-rolled **ReAct Agent** loop + **3-layer anti-hallucination** (force tool calls → keep going up to `max_turns` → summarize tool outputs) and an optional **LangChain** backend.

- **52 个工具**：时间 / 提醒 / 记忆 / 计算 / 文件 / 截图 / 系统 / 应用 / 网页搜索（Tavily + DuckDuckGo）/ 快捷指令...
  / **52 tools**: time / reminder / calc / files / screenshot / system / apps / web search (Tavily + DuckDuckGo) / shortcuts...

- **真记忆**：可插拔 `TF-IDF` / `sentence-transformers` 检索；重要性评分 + 时间衰减 + 冲突检测 + 长期演化
  / **Real memory**: pluggable `TF-IDF` / `sentence-transformers` retrieval; importance scoring + time decay + conflict detection + long-term evolution.

- **真说话**：**3 引擎 TTS**（edge-tts / GPT-SoVITS 本地克隆 / MiniMax 云端）+ 浏览器 SpeechRecognition **ASR 按住说话** + 嘴型同步
  / **Real voice**: **3 TTS engines** (edge-tts / GPT-SoVITS local cloning / MiniMax cloud) + browser SpeechRecognition **ASR hold-to-talk** + mouth-sync.

- **真模样**：**双渲染**（Live2D Cubism 4 + pixi-live2d-display / PNG 帧动画），模型缺失自动 fallback
  / **Real look**: **dual renderer** (Live2D Cubism 4 + pixi-live2d-display / PNG frame animation), graceful fallback when the model is missing.

- **真玩法**：**五子棋**（3 档 AI 难度）+ **中国象棋**（**走法生成 / 将军 / 将死 / 困毙 / 飞将全部走 [cchess](https://pypi.org/project/cchess/) 库**，MIT、PyPI 1.20+，我们自己只写 alpha-beta 搜索 + 估值；3 档 AI 难度）+ **9 人狼人杀**（你 + 8 个独立 NPC Agent + 桌宠主持，无 API key 也能离线玩）+ 每日签到 + 喂食 + 5 项状态养成
  / **Real games**: **Gomoku** (3 AI difficulty levels) + **Chinese Chess** (move generation / check / checkmate / stalemate / flying general all from the **[cchess](https://pypi.org/project/cchess/)** library, MIT, PyPI 1.20+**; we only write the alpha-beta search + evaluation; 3 AI difficulty levels) + **9-player Werewolf** (you + 8 independent NPC agents + the pet as moderator, fully offline without any API key) + daily check-in + feeding + 5-stat nurturing.

- **真开放**：内置 **MCP stdio JSON-RPC** + filesystem server，可外挂任何 MCP 兼容 server
  / **Real extensibility**: built-in **MCP stdio JSON-RPC** + filesystem server, attach any MCP-compatible server.

- **可观测**：所有 LLM/工具调用落 SQLite，**FastAPI Dashboard** (`localhost:8766`) 可视化 Trace、回放、调参
  / **Observable**: all LLM / tool calls land in SQLite; **FastAPI Dashboard** (`localhost:8766`) visualizes Trace, replay, and runtime parameters.

---

## 5 分钟跑起来 / 5-Min Quick Start

### 安装前置（适用于 macOS / Linux）/ Prerequisites (macOS / Linux)

```bash
# Python 3.10+
python3 --version
pip3 install -r requirements.txt

# 可选：Live2D 渲染 + Chromium 内核（建议 250MB+）
pip3 install PyQtWebEngine

# 启动 / Run
python3 main.py
```

### 安装前置（适用于 Windows）/ Prerequisites (Windows)

```powershell
# Python 3.10+ (勾选 Add Python to PATH)
python --version
pip install -r requirements.txt

# 可选：Live2D 渲染 + Chromium 内核（建议 250MB+）
pip install PyQtWebEngine

# 启动 / Run
python main.py
```

### 第一次打开会怎样？ / What happens on first launch?

1. **默认自动开启内置模型 Hiyori**（Live2D 官方样例，零配置）
   / The default model is the Live2D official sample **Hiyori**, which works out of the box.
2. **桌宠主动打招呼**：检测当前时间段说「早上好 / 中午好 / 晚上好」
   / The pet greets you based on the time of day ("Good morning / afternoon / evening").
3. **任意键说话 / 按住说话键 ASR** → 桌宠回复 + 异步嘴型同步
   / Type to chat / hold-to-talk for ASR → the pet replies with mouth-sync.
4. **无 API key**？桌宠用 mock 回复兜底（功能演示完整，但不能调真实 Tavily 搜索 / 真 LLM）
   / **No API key**? The pet falls back to mock replies (full UI demo, but no real Tavily search / real LLM).
5. **右键模型 → 「设置」** 配置 LLM / Tavily Key 启用真模型
   / **Right-click the model → "Settings"** to configure LLM / Tavily keys.

### 试用在线 Demo / Try the online demo

任何浏览器直接打开（不需要任何运行时数据 / 零依赖）：
Open it in any browser (no runtime required / zero dependencies):

> **https://255856.github.io/Smart-Desktop-Pet/**

> 在线 Demo 来自 gh-pages 分支。Live2D 模型版权属原作者——Demo 内置官方授权样例，**用户自备**模型可填 URL 加载。
> The online demo ships from the `gh-pages` branch. Live2D models are copyrighted — the demo bundles officially-licensed samples; load **your own** model by URL.

---

## 它长什么样？ / What does it look like?

### 主屏（Live2D 模型 + 浮动聊天栏 + 拖拽）/ Main screen (Live2D model + floating chat + drag)

![主屏](docs/_assets/icon.png)

> **左上**：5 项状态 / 亲密度 / 等级 / 提醒计数 / 经验
> **右上**：动作选择 / 表情选择 / 测试口型 / 隐藏窗口
> **右键模型**：设置 / 模型切换 / 截图 / 重启 / 喂养 / 触发回忆
>
> **Top-left**: 5 stats / affection / level / reminder count / XP.
> **Top-right**: motion picker / expression picker / mouth-sync test / hide window.
> **Right-click on the model**: settings / switch model / screenshot / restart / feed / recall memory.

### 设置面板（5 个分组 18+ 选项）/ Settings panel (5 groups, 18+ options)

LLM / TTS / ASR / 提醒 / 记忆 / 快捷指令 全部可调

LLM / TTS / ASR / reminders / memory / shortcuts — all adjustable.

### FastAPI Dashboard (https://localhost:8766)

所有 LLM/工具调用落 SQLite 后在此可视化、可回放、可调参
All LLM / tool calls land in SQLite and are visualized, replayed, and tunable here.

---

## 它怎么做到的？ / How does it work?

### 1. Agent 工具调用循环（抗幻觉 3 层）/ Agent tool-calling loop (3 anti-hallucination layers)

```
用户消息 / User message
    │
    ▼
强制先调工具（除非明确知识类）/ Force a tool call first (unless it's pure knowledge)
    │
    ├─ 工具失败 → 反思重试，最多 max_turns 轮 / Retry on failure up to max_turns
    │
    ├─ 工具成功 → 把结果拼到上下文，继续推进 / Append result and keep going
    │
    └─ 达到 max_turns → 汇总所有工具结果成最终回答 / Summarize all results as final answer
```

**3 层抗幻觉 / 3 anti-hallucination layers**：
1. **强制调工具**（除非明确知识类）：deflect a tool call first (unless it's pure knowledge).
2. **跨轮持续到 max_turns**：不放弃多轮顽固成见。keep going up to `max_turns`; never give up after one tool call.
3. **工具结果直接总结成答案**：strictly summarize tool outputs into the final answer — no free-form fabrication.

### 2. 多 Agent 协同 / Multi-agent coordination

桌宠本体 + Life Agent + Research Agent + Code Agent + Orchestrator：
Pet + Life + Research + Code agents + Orchestrator:

- **Orchestrator**：把请求拆给子任务。回复职责。 breaks down requests into sub-tasks and arbitrates replies.
- **Life Agent**：日程 / 提醒 / 习惯 / 主动搭话。schedules, reminders, habits, proactive chat.
- **Research Agent**：搜资料 / 查资料 / 写报告。searches and writes reports.
- **Code Agent**：写脚本 / 改文件 / git 提交。writes scripts, edits files, makes git commits.

### 3. 长期记忆（双记忆引擎 + 重要性 + 冲突检测）/ Long-term memory (dual backends + importance + conflict detection)

- **检索 / Retrieval**: `TF-IDF` (offline) / `sentence-transformers` (semantic)
- **重要度评分 / Importance score** (1–10)
- **时间衰减 / Time decay**（记忆越久越靠后）
- **冲突检测 / Conflict detection**（更新与已有记忆冲突时合并）

---

## 与桌宠本体不同 / How it's different from the desktop app

| 维度 / Aspect | 桌面版 / Desktop | 网页 Demo / Web Demo |
|---|---|---|
| 渲染 / Renderer | Live2D / PNG / **Voice** | ✓ Live2D |
| 语音 / Voice | ✅ Speech / ASR / TTS / Voice | ✓ Speech / ASR / TTS / Voice |
| 大脑 / Brain | ✅ ReAct + LangChain + Multi-Agent | ✓ ReAct + Multi-Agent |
| 工具 / Tools | ✅ 52 个 / 52 | ✓ 18 个 / 18 (browser sandbox) |
| 记忆 / Memory | ✅ TF-IDF / 向量 / 持久化 | ✓ localStorage |
| 提醒 / Reminders | ✅ 系统通知 + cron | ✓ Browser Notifications |
| 游戏 / Games | ✅ 五子棋 / 象棋 / 狼人杀 | ✗ (browser sandbox) |
| 养成 / Nurturing | ✅ 5 状态 / 喂食 / 签到 | ✗ |
| 主动搭话 / Proactive | ✅ 时段上下文 / 习惯 | ✓ 时段上下文 / habit |
| Trace / Trace | ✅ SQLite + Dashboard | ✓ 内嵌 / embedded |

### 怎样补齐？/ How to close the gap?

本仓库内网页 Demo 与桌面版**始终同步更新**——新增的桌面工具可零成本移植到 Web（browser sandbox 限制除外：截图 / 本地文件 / 系统调用 / 应用启动）。
This repo keeps the Web demo **in sync** with the desktop app — new desktop tools can be ported to the web for free (browser sandbox limits aside: screenshot / local files / system calls / app launching).

---

## 它能怎么玩？ / How can you play with it?

### 基础对话 / Basic chat

- 「今天天气怎么样？」 → 自动搜索 → 真回答。/"How's the weather today?" → auto-search → real answer.
- 「3 分钟后提醒我喝水」 → 系统通知 + TTS。/"Remind me to drink water in 3 minutes" → system notification + TTS.
- 「记住我最爱的颜色是蓝色」 → 写入长期记忆 → 下次自动想起。/"Remember my favorite color is blue" → long-term memory.

### 设置项 / Settings

| 类别 / Category | 项目 / Items |
|---|---|
| LLM | 模型名 / base URL / API key / system prompt |
| TTS / TTS | 引擎（edge / GPT-SoVITS / MiniMax）/ 语速 / 音调 / 试听 / Test |
| ASR / ASR | 语言 / 自动发送 |
| 提醒 / Reminder | 默认 5/10/30 分钟 |
| 记忆 / Memory | 重要度阈值 / 冲突合并 / 检索策略 |
| 快捷指令 / Shortcut | 自定义 / 系统级 |

### 游戏 / Games

- **五子棋**：右键菜单 → 五子棋 → 选难度 → 3 档 AI（你可以 0 难度 5 子棋 → 必赢）
  / **Gomoku**: right-click → Gomoku → pick difficulty → 3 AI levels (you can pick 0 to always win).
- **中国象棋**：规则（**走法生成 / 将军 / 将死 / 困毙 / 飞将**）走 [cchess](https://pypi.org/project/cchess/) 库，AI 自己写
  / **Chinese Chess**: rules (**move generation / check / checkmate / stalemate / flying general**) come from [cchess](https://pypi.org/project/cchess/), AI is hand-rolled.
- **狼人杀**：9 人（你 + 8 NPC + 桌宠主持），全离线 / 9 players (you + 8 NPCs + the pet as moderator), fully offline.

### MCP / MCP

桌面版内置 **MCP stdio JSON-RPC** + filesystem server：

```bash
# 例：使用 mcp-server-filesystem 访问桌面 / Example: mcp-server-filesystem
mcp_servers.yaml:
  filesystem:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/Users/you/Documents"]
```

在设置 → MCP 服务器中启用 → 桌宠即可读写你的 Documents 目录。
Enable it in Settings → MCP Servers → the pet can now read/write your Documents directory.

---

## 文档 / Documentation

- [Live2D 在线 Demo / Live2D Online Demo](https://255856.github.io/Smart-Desktop-Pet/) — 浏览器即可体验，零依赖
  / Try it in any browser, zero dependencies.
- [Live2D Demo 部署说明 / Demo deployment](docs/demo/DEPLOY.md) — 怎么部署到 GitHub Pages + 自定义模型放上 Pages
  / How to deploy to GitHub Pages + put your custom model on Pages.
- [Live2D Demo 功能对齐报告 / Feature alignment report](docs/demo/FEATURE_ALIGNMENT.html) — 网页 Demo 与桌面版能力对比表
  / Capability comparison between web demo and desktop app.
- [快速开始 / Quick start](docs/quickstart.md)
- [架构 / Architecture](docs/architecture.md)
- [Live2D 集成 / Live2D integration](docs/live2d-integration.md)
- [情绪系统 / Emotion system](docs/emotion-system.md)
- [TTS 集成 / TTS integration](docs/tts-integration.md)
- [桌面版工具参考 / Tools reference](docs/tools-reference.md) — 52 个工具详细说明
  / Detailed description of all 52 tools.

---

## 测试 / Testing

```bash
# 跑全部测试（约 1 分钟）/ Run the full test suite (~1 min)
pytest tests/ -q --no-header

# 跑特性验证（5 分钟全量，与 TTS / 文件 / 记忆 / 工具交互）
# / Run the feature-verification suite (5 min full coverage, exercises TTS / files / memory / tool interactions)
python scripts/verify_features.py

# 跑单个测试 / Run a single test
pytest tests/test_langchain_agent.py -v
```

测试结果：
- **674 passed** / 6 skipped
- 验证：82 项功能检查
  *674 passed / 6 skipped; verification: 82 feature checks.*

CI 在 Windows runner 上跑（Qt / pywin32 / WMI 亮度 / 电量等都依赖 Windows 专有 API）
CI runs on Windows runners (Qt / pywin32 / WMI brightness / battery all need Windows APIs).

---

## 贡献 / Contributing

欢迎任何贡献！特别需要：
Any contribution is welcome! Especially:

- 新工具（web API / 本地应用 / 桌面操作）
  / New tools (web APIs / local apps / desktop ops).
- 新游戏（五子棋 / 象棋 AI 调优 / 狼人杀规则扩展）
  / New games (Gomoku / Xiangqi AI tuning / Werewolf rule extensions).
- 角色与对话（人格 / 系统 prompt / 语气）
  / Character & conversation (persona / system prompts / tone).
- 文档与翻译（README / 注释 / 演示 / 国际化）
  / Docs & translation (README / comments / demos / i18n).

提交前请确认 `pytest tests/ -q --no-header --cov-fail-under=25` 通过。
Before submitting, please make sure `pytest tests/ -q --no-header --cov-fail-under=25` passes.

---

## 许可 / License

**MIT** — 详见 [LICENSE](LICENSE)。
**MIT** — see [LICENSE](LICENSE).

第三方库：
Third-party:

- **Pixi.js** — MIT © GoodBoy Digital
- **pixi-live2d-display** — MIT © avgjs
- **Live2D Cubism Core** — Live2D Cubism SDK EULA（可重分发于应用程序内，不可独立售卖）
  / Live2D Cubism SDK EULA (redistributable inside an application, not standalone-saleable).

> **模型版权 / Model copyright**: Live2D 模型版权归原作者。官方样例（Hiyori / Miara）仅限个人非商用演示。
  Live2D model copyrights belong to their creators. Official samples (Hiyori / Miara) are for personal non-commercial demo only.