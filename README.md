# 🐱 桌面宠物 · Smart Desktop Pet

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)](https://www.python.org)
[![PyQt5](https://img.shields.io/badge/UI-PyQt5%2BWebEngine-41CD52?logo=qt&logoColor=white)](https://riverbankcomputing.com)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-740%20passed%20%2F%208%20skipped-brightgreen?logo=pytest)](tests/)
[![Verify](https://img.shields.io/badge/verify_features-82%20checks-blueviolet)](scripts/verify_features.py)
[![Live2D Demo](https://img.shields.io/badge/Live2D%20Demo-Try%20Online-6c5ce7?logo=githubpages&logoColor=white)](https://255856.github.io/Smart-Desktop-Pet/)

---

[![中文](https://img.shields.io/badge/%E4%B8%AD%E6%96%87-1976D2?style=for-the-badge&logo=github&logoColor=white)](#zh-hans)
[![English](https://img.shields.io/badge/English-24292F?style=for-the-badge&logo=github&logoColor=white)](#english)

🎮 [Live2D 在线 Demo / Live2D Demo](https://255856.github.io/Smart-Desktop-Pet/) · 📚 [文档 / Docs](docs/)

---

<a name="zh-hans"></a>

# 桌面宠物

> **一个真正能"养"的桌面 AI 宠物** —— 不只是会动、会说话，而是会**记住你、主动搭话、调用工具、跨多轮思考**，还能**陪你下五子棋、下中国象棋、玩狼人杀**的数字伙伴。

## 它能做什么？

- **真智能**：手写 **ReAct Agent** 循环 + **3 层抗幻觉**（强制调工具 → 跨轮持续到 `max_turns` → 工具结果直接总结），可选 **LangChain** 后端
- **52 个工具**：时间 / 提醒 / 记忆 / 计算 / 文件 / 截图 / 系统 / 应用 / 网页搜索（Tavily + DuckDuckGo）/ 快捷指令...
- **真记忆**：可插拔 `TF-IDF` / `sentence-transformers` 检索；重要性评分 + 时间衰减 + 冲突检测 + 长期演化
- **真说话**：**3 引擎 TTS**（edge-tts / GPT-SoVITS 本地克隆 / MiniMax 云端）+ 浏览器 SpeechRecognition **ASR 按住说话** + 嘴型同步
- **真模样**：**双渲染**（Live2D Cubism 4 + pixi-live2d-display / PNG 帧动画），模型缺失自动 fallback
- **真玩法**：**五子棋**（3 档 AI 难度）+ **中国象棋**（**走法生成 / 将军 / 将死 / 困毙 / 飞将全部走 [cchess](https://pypi.org/project/cchess/) 库**，MIT、PyPI 1.20+，我们自己只写 alpha-beta 搜索 + 估值；3 档 AI 难度）+ **9 人狼人杀**（你 + 8 个独立 NPC Agent + 桌宠主持，无 API key 也能离线玩）+ 每日签到 + 喂食 + 5 项状态养成
- **真开放**：内置 **MCP stdio JSON-RPC** + filesystem server，可外挂任何 MCP 兼容 server
- **可观测**：所有 LLM / 工具调用落 SQLite，**FastAPI Dashboard**（`localhost:8766`）可视化 Trace、回放、调参

## 5 分钟跑起来

### 安装前置（适用于 macOS / Linux）

```bash
# Python 3.10+
python3 --version
pip3 install -r requirements.txt

# 可选：Live2D 渲染 + Chromium 内核（建议 250MB+）
pip3 install PyQtWebEngine

# 启动
python3 main.py
```

### 安装前置（适用于 Windows）

```powershell
# Python 3.10+（勾选 Add Python to PATH）
python --version
pip install -r requirements.txt

# 可选：Live2D 渲染 + Chromium 内核（建议 250MB+）
pip install PyQtWebEngine

# 启动
python main.py
```

### 第一次打开会怎样？

1. **默认自动开启内置模型 Hiyori**（Live2D 官方样例，零配置）
2. **桌宠主动打招呼**：检测当前时间段说「早上好 / 中午好 / 晚上好」
3. **任意键说话 / 按住说话键 ASR** → 桌宠回复 + 异步嘴型同步
4. **无 API key**？桌宠用 mock 回复兜底（功能演示完整，但不能调真实 Tavily 搜索 / 真 LLM）
5. **右键模型 → 「设置」** 配置 LLM / Tavily Key 启用真模型

### 试用在线 Demo

任何浏览器直接打开（不需要任何运行时数据 / 零依赖）：

> **https://255856.github.io/Smart-Desktop-Pet/**

> 在线 Demo 来自 gh-pages 分支。Live2D 模型版权属原作者——Demo 内置官方授权样例，**用户自备**模型可填 URL 加载。

## 它长什么样？

### 主屏（Live2D 模型 + 浮动聊天栏 + 拖拽）

![主屏](docs/_assets/icon.png)

> **左上**：5 项状态 / 亲密度 / 等级 / 提醒计数 / 经验
> **右上**：动作选择 / 表情选择 / 测试口型 / 隐藏窗口
> **右键模型**：设置 / 模型切换 / 截图 / 重启 / 喂养 / 触发回忆

### 设置面板（5 个分组 18+ 选项）

LLM / TTS / ASR / 提醒 / 记忆 / 快捷指令 全部可调

### FastAPI Dashboard（https://localhost:8766）

所有 LLM / 工具调用落 SQLite 后在此可视化、可回放、可调参

## 它怎么做到的？

### 1. Agent 工具调用循环（抗幻觉 3 层）

```
用户消息
    │
    ▼
强制先调工具（除非明确知识类）
    │
    ├─ 工具失败 → 反思重试，最多 max_turns 轮
    │
    ├─ 工具成功 → 把结果拼到上下文，继续推进
    │
    └─ 达到 max_turns → 汇总所有工具结果成最终回答
```

**3 层抗幻觉**：

1. **强制调工具**（除非明确知识类）：不允许模型直接作答，先要求它调用一次工具
2. **跨轮持续到 `max_turns`**：不放弃，一次工具调用不够就继续追问
3. **工具结果直接总结成答案**：严格基于工具输出作答，杜绝自由发挥式编造

### 2. 多 Agent 协同

桌宠本体 + Life Agent + Research Agent + Code Agent + Orchestrator：

- **Orchestrator**：把请求拆给子任务，仲裁各子 Agent 的回复
- **Life Agent**：日程 / 提醒 / 习惯 / 主动搭话
- **Research Agent**：搜资料 / 查资料 / 写报告
- **Code Agent**：写脚本 / 改文件 / git 提交

### 3. 长期记忆（双记忆引擎 + 重要性 + 冲突检测）

- **检索**：`TF-IDF`（离线）/ `sentence-transformers`（语义）
- **重要度评分**（1–10）
- **时间衰减**（记忆越久越靠后）
- **冲突检测**（更新与已有记忆冲突时合并）

## 与桌宠本体不同

| 维度 | 桌面版 | 网页 Demo |
|---|---|---|
| 渲染 | Live2D / PNG / **Voice** | ✓ Live2D |
| 语音 | ✅ Speech / ASR / TTS / Voice | ✓ Speech / ASR / TTS / Voice |
| 大脑 | ✅ ReAct + LangChain + Multi-Agent | ✓ ReAct + Multi-Agent |
| 工具 | ✅ 52 个 | ✓ 18 个（browser sandbox） |
| 记忆 | ✅ TF-IDF / 向量 / 持久化 | ✓ localStorage |
| 提醒 | ✅ 系统通知 + cron | ✓ Browser Notifications |
| 游戏 | ✅ 五子棋 / 象棋 / 狼人杀 | ✗（browser sandbox） |
| 养成 | ✅ 5 状态 / 喂食 / 签到 | ✗ |
| 主动搭话 | ✅ 时段上下文 / 习惯 | ✓ 时段上下文 / habit |
| Trace | ✅ SQLite + Dashboard | ✓ 内嵌 |

### 怎样补齐？

本仓库内网页 Demo 与桌面版**始终同步更新**——新增的桌面工具可零成本移植到 Web（browser sandbox 限制除外：截图 / 本地文件 / 系统调用 / 应用启动）。

## 它能怎么玩？

### 基础对话

- 「今天天气怎么样？」 → 自动搜索 → 真回答
- 「3 分钟后提醒我喝水」 → 系统通知 + TTS
- 「记住我最爱的颜色是蓝色」 → 写入长期记忆 → 下次自动想起

### 设置项

| 类别 | 项目 |
|---|---|
| LLM | 模型名 / base URL / API key / system prompt |
| TTS / TTS | 引擎（edge / GPT-SoVITS / MiniMax）/ 语速 / 音调 / 试听 |
| ASR / ASR | 语言 / 自动发送 |
| 提醒 / Reminder | 默认 5 / 10 / 30 分钟 |
| 记忆 / Memory | 重要度阈值 / 冲突合并 / 检索策略 |
| 快捷指令 / Shortcut | 自定义 / 系统级 |

### 游戏

- **五子棋**：右键菜单 → 五子棋 → 选难度 → 3 档 AI（你可以选 0 难度 5 子棋 → 必赢）
- **中国象棋**：规则（**走法生成 / 将军 / 将死 / 困毙 / 飞将**）走 [cchess](https://pypi.org/project/cchess/) 库，AI 自己写
- **狼人杀**：9 人（你 + 8 NPC + 桌宠主持），全离线

### MCP

桌面版内置 **MCP stdio JSON-RPC** + filesystem server：

```yaml
# 例：使用 mcp-server-filesystem 访问桌面
mcp_servers.yaml:
  filesystem:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/Users/you/Documents"]
```

在设置 → MCP 服务器中启用 → 桌宠即可读写你的 Documents 目录。

## 文档

- [Live2D 在线 Demo](https://255856.github.io/Smart-Desktop-Pet/) — 浏览器即可体验，零依赖
- [Live2D Demo 部署说明](docs/demo/DEPLOY.md) — 怎么部署到 GitHub Pages + 自定义模型放上 Pages
- [Live2D Demo 功能对齐报告](docs/demo/FEATURE_ALIGNMENT.html) — 网页 Demo 与桌面版能力对比表
- [快速开始](docs/quickstart.md)
- [架构](docs/architecture.md)
- [Live2D 集成](docs/live2d-integration.md)
- [情绪系统](docs/emotion-system.md)
- [TTS 集成](docs/tts-integration.md)
- [桌面版工具参考](docs/tools-reference.md) — 52 个工具详细说明

## 测试

```bash
# 跑全部测试（约 1 分钟）
pytest tests/ -q --no-header

# 跑特性验证（5 分钟全量，与 TTS / 文件 / 记忆 / 工具交互）
python scripts/verify_features.py

# 跑单个测试
pytest tests/test_langchain_agent.py -v
```

测试结果：

- **740 passed** / 8 skipped
- 验证：82 项功能检查

CI 在 Windows runner 上跑（Qt / pywin32 / WMI 亮度 / 电量等都依赖 Windows 专有 API）。

## 贡献

欢迎任何贡献！特别需要：

- 新工具（web API / 本地应用 / 桌面操作）
- 新游戏（五子棋 / 象棋 AI 调优 / 狼人杀规则扩展）
- 角色与对话（人格 / system prompt / 语气）
- 文档与翻译（README / 注释 / 演示 / 国际化）

提交前请确认 `pytest tests/ -q --no-header --cov-fail-under=25` 通过。

## 许可

**MIT** — 详见 [LICENSE](LICENSE)。

第三方库：

- **Pixi.js** — MIT © GoodBoy Digital
- **pixi-live2d-display** — MIT © avgjs
- **Live2D Cubism Core** — Live2D Cubism SDK EULA（可重分发于应用程序内，不可独立售卖）

> **模型版权**：Live2D 模型版权归原作者所有。官方样例（Hiyori / Miara）仅限个人非商用演示。

---

**[🇬🇧 切换到英文 / Switch to English](#english)**

---

<a name="english"></a>

# Smart Desktop Pet

> A truly *livestockable* desktop AI companion — remembers you, speaks proactively, calls tools, reasons across turns, and plays Gomoku, Xiangqi & Werewolf with you.

## What can it do?

- **Real intelligence**: hand-rolled **ReAct Agent** loop + **3-layer anti-hallucination** (force tool calls → keep going up to `max_turns` → summarize tool outputs), with an optional **LangChain** backend
- **52 tools**: time / reminder / calc / files / screenshot / system / apps / web search (Tavily + DuckDuckGo) / shortcuts...
- **Real memory**: pluggable `TF-IDF` / `sentence-transformers` retrieval; importance scoring + time decay + conflict detection + long-term evolution
- **Real voice**: **3 TTS engines** (edge-tts / GPT-SoVITS local cloning / MiniMax cloud) + browser SpeechRecognition **ASR hold-to-talk** + mouth-sync
- **Real look**: **dual renderer** (Live2D Cubism 4 + pixi-live2d-display / PNG frame animation), graceful fallback when the model is missing
- **Real games**: **Gomoku** (3 AI difficulty levels) + **Chinese Chess** (move generation / check / checkmate / stalemate / flying general all come from the **[cchess](https://pypi.org/project/cchess/)** library, MIT, PyPI 1.20+; we only write the alpha-beta search + evaluation, with 3 AI difficulty levels) + **9-player Werewolf** (you + 8 independent NPC agents + the pet as moderator, fully offline without any API key) + daily check-in + feeding + 5-stat nurturing
- **Real extensibility**: built-in **MCP stdio JSON-RPC** + filesystem server, attach any MCP-compatible server
- **Observable**: all LLM / tool calls land in SQLite; a **FastAPI Dashboard** (`localhost:8766`) visualizes Trace, replay, and runtime parameters

## 5-Min Quick Start

### Prerequisites (macOS / Linux)

```bash
# Python 3.10+
python3 --version
pip3 install -r requirements.txt

# Optional: Live2D rendering + Chromium core (250MB+ recommended)
pip3 install PyQtWebEngine

# Run
python3 main.py
```

### Prerequisites (Windows)

```powershell
# Python 3.10+ (tick "Add Python to PATH")
python --version
pip install -r requirements.txt

# Optional: Live2D rendering + Chromium core (250MB+ recommended)
pip install PyQtWebEngine

# Run
python main.py
```

### What happens on first launch?

1. **The bundled model Hiyori is enabled by default** (an official Live2D sample, zero configuration)
2. **The pet greets you first**: it detects the time of day and says "Good morning / afternoon / evening"
3. **Type to chat / hold-to-talk for ASR** → the pet replies with async mouth-sync
4. **No API key?** The pet falls back to mock replies (full feature demo, but no real Tavily search / real LLM)
5. **Right-click the model → "Settings"** to configure LLM / Tavily keys and enable the real models

### Try the online demo

Open it in any browser (no runtime required / zero dependencies):

> **https://255856.github.io/Smart-Desktop-Pet/**

> The online demo ships from the `gh-pages` branch. Live2D models are copyrighted — the demo bundles officially-licensed samples; load **your own** model by URL.

## What does it look like?

### Main screen (Live2D model + floating chat + drag)

![Main screen](docs/_assets/icon.png)

> **Top-left**: 5 stats / affection / level / reminder count / XP.
> **Top-right**: motion picker / expression picker / mouth-sync test / hide window.
> **Right-click on the model**: settings / switch model / screenshot / restart / feed / recall memory.

### Settings panel (5 groups, 18+ options)

LLM / TTS / ASR / reminders / memory / shortcuts — all adjustable.

### FastAPI Dashboard (https://localhost:8766)

All LLM / tool calls land in SQLite and are visualized, replayed, and tunable here.

## How does it work?

### 1. Agent tool-calling loop (3 anti-hallucination layers)

```
User message
    │
    ▼
Force a tool call first (unless it's pure knowledge)
    │
    ├─ Tool fails → reflect and retry, up to max_turns
    │
    ├─ Tool succeeds → append the result to context and keep going
    │
    └─ Reaches max_turns → summarize all tool results as the final answer
```

**3 anti-hallucination layers**:

1. **Force a tool call first** (unless it's pure knowledge): the model may not answer directly — require one tool call first
2. **Keep going up to `max_turns`**: never give up after a single tool call
3. **Strictly summarize tool outputs into the final answer**: no free-form fabrication

### 2. Multi-agent coordination

Pet + Life + Research + Code agents + Orchestrator:

- **Orchestrator**: breaks down requests into sub-tasks and arbitrates their replies
- **Life Agent**: schedules, reminders, habits, proactive chat
- **Research Agent**: searches for material, looks things up, writes reports
- **Code Agent**: writes scripts, edits files, makes git commits

### 3. Long-term memory (dual backends + importance + conflict detection)

- **Retrieval**: `TF-IDF` (offline) / `sentence-transformers` (semantic)
- **Importance score** (1–10)
- **Time decay** (older memories rank lower)
- **Conflict detection** (merge when an update conflicts with an existing memory)

## How it's different from the desktop app

| Aspect | Desktop | Web Demo |
|---|---|---|
| Renderer | Live2D / PNG / **Voice** | ✓ Live2D |
| Voice | ✅ Speech / ASR / TTS / Voice | ✓ Speech / ASR / TTS / Voice |
| Brain | ✅ ReAct + LangChain + Multi-Agent | ✓ ReAct + Multi-Agent |
| Tools | ✅ 52 | ✓ 18 (browser sandbox) |
| Memory | ✅ TF-IDF / vector / persistent | ✓ localStorage |
| Reminders | ✅ System notification + cron | ✓ Browser Notifications |
| Games | ✅ Gomoku / Xiangqi / Werewolf | ✗ (browser sandbox) |
| Nurturing | ✅ 5 stats / feeding / check-in | ✗ |
| Proactive | ✅ Time-of-day context / habits | ✓ time-of-day context / habit |
| Trace | ✅ SQLite + Dashboard | ✓ embedded |

### How to close the gap?

This repo keeps the Web demo **in sync** with the desktop app — new desktop tools can be ported to the web for free (browser sandbox limits aside: screenshot / local files / system calls / app launching).

## How can you play with it?

### Basic chat

- "How's the weather today?" → auto-search → a real answer
- "Remind me to drink water in 3 minutes" → system notification + TTS
- "Remember my favorite color is blue" → written to long-term memory → recalled next time

### Settings

| Category | Items |
|---|---|
| LLM | Model name / base URL / API key / system prompt |
| TTS | Engine (edge / GPT-SoVITS / MiniMax) / speed / pitch / test |
| ASR | Language / auto-send |
| Reminder | Default 5 / 10 / 30 minutes |
| Memory | Importance threshold / conflict merge / retrieval strategy |
| Shortcut | Custom / system-level |

### Games

- **Gomoku**: right-click → Gomoku → pick difficulty → 3 AI levels (you can pick 0 to always win)
- **Chinese Chess**: rules (move generation / check / checkmate / stalemate / flying general) come from [cchess](https://pypi.org/project/cchess/); the AI is hand-rolled
- **Werewolf**: 9 players (you + 8 NPCs + the pet as moderator), fully offline

### MCP

The desktop app ships with **MCP stdio JSON-RPC** + a filesystem server:

```yaml
# Example: use mcp-server-filesystem to reach the desktop
mcp_servers.yaml:
  filesystem:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/Users/you/Documents"]
```

Enable it in Settings → MCP Servers → the pet can now read/write your Documents directory.

## Documentation

- [Live2D Online Demo](https://255856.github.io/Smart-Desktop-Pet/) — try it in any browser, zero dependencies
- [Demo deployment](docs/demo/DEPLOY.md) — how to deploy to GitHub Pages + put your custom model on Pages
- [Feature alignment report](docs/demo/FEATURE_ALIGNMENT.html) — capability comparison between the web demo and the desktop app
- [Quick start](docs/quickstart.md)
- [Architecture](docs/architecture.md)
- [Live2D integration](docs/live2d-integration.md)
- [Emotion system](docs/emotion-system.md)
- [TTS integration](docs/tts-integration.md)
- [Tools reference](docs/tools-reference.md) — detailed description of all 52 tools

## Testing

```bash
# Run the full test suite (~1 min)
pytest tests/ -q --no-header

# Run the feature-verification suite (5 min full coverage, exercises TTS / files / memory / tool interactions)
python scripts/verify_features.py

# Run a single test
pytest tests/test_langchain_agent.py -v
```

Test results:

- **740 passed** / 8 skipped
- Verification: 82 feature checks

CI runs on Windows runners (Qt / pywin32 / WMI brightness / battery all need Windows-specific APIs).

## Contributing

Any contribution is welcome! Especially:

- New tools (web APIs / local apps / desktop ops)
- New games (Gomoku / Xiangqi AI tuning / Werewolf rule extensions)
- Character & conversation (persona / system prompts / tone)
- Docs & translation (README / comments / demos / i18n)

Before submitting, please make sure `pytest tests/ -q --no-header --cov-fail-under=25` passes.

## License

**MIT** — see [LICENSE](LICENSE).

Third-party:

- **Pixi.js** — MIT © GoodBoy Digital
- **pixi-live2d-display** — MIT © avgjs
- **Live2D Cubism Core** — Live2D Cubism SDK EULA (redistributable inside an application, not standalone-saleable)

> **Model copyright**: Live2D model copyrights belong to their creators. Official samples (Hiyori / Miara) are for personal non-commercial demo use only.

---

**[🇨🇳 切换到中文 / Back to Chinese](#zh-hans)**
