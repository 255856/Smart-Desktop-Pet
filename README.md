# 🐳 桌面宠物 · Smart Desktop Pet

> **一个真正能"养"的桌面 AI 宠物** —— 不只是会动、会说话，而是会**记住你、主动搭话、调用工具、跨多轮思考，还能陪你下棋、玩狼人杀**的数字伙伴。
>
> A truly *livestockable* desktop AI companion — not just animated and voiced, but one that **remembers you, speaks proactively, calls tools, reasons across multiple turns, and even plays Gomoku and Werewolf with you**.

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)](https://www.python.org)
[![PyQt5](https://img.shields.io/badge/UI-PyQt5%2BWebEngine-41CD52?logo=qt&logoColor=white)](https://riverbankcomputing.com)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-389%20passing-brightgreen?logo=pytest)](tests/)
[![Tools](https://img.shields.io/badge/tools-32%20%2F%209%20groups-blueviolet)](docs/tool-catalog.md)
[![Live2D Demo](https://img.shields.io/badge/Live2D%20Demo-Try%20Online-6c5ce7?logo=githubpages&logoColor=white)](https://255856.github.io/Smart-Desktop-Pet/)

[中文](#中文) · [English](#english) · [**Live2D 在线 Demo**](https://255856.github.io/Smart-Desktop-Pet/) · [文档 / Docs](docs/) · [快速开始 / Quick Start](#快速开始)

---

## 它能做什么？ / What can it do?

|  **多模态表现**<br/>Multimodal Presence |  **真正能思考的大脑**<br/>A Brain That Actually Thinks |
|---|---|
| 双渲染架构：**Live2D Cubism 4**（QWebEngineView，唇形/呼吸/动作/场景触发）与 **PNG 帧动画** 自由切换；Live2D 不可用自动 fallback | 手写 **ReAct Agent 循环**，可选 **LangChain** 后端；3 层抗幻觉：① 第一轮强制调工具 ② 跨多轮持续到 `max_turns` ③ 工具结果 → 直接总结，不再二次套娃 |
|  **32 个工具随时调用**<br/>32 Tools On Call |  **真的会记住你**<br/>It Actually Remembers You |
| 时间 / 提醒 / 记忆 / 计算 / 单位换算 / 文件 / 截图 / 系统状态 / 应用启动 / 网页搜索 / 快捷指令…… | **TF-IDF ↔ sentence-transformers** 可插拔后端；**重要性评分 + 时间衰减 + 冲突检测**；长期记忆演化（合并/升级/降级/淘汰）；聊天窗可一键增删 |
|  **3 引擎 TTS + 流式同步**<br/>3 TTS Engines, Stream-Synced |  **能陪你玩、有养成系统**<br/>Games & Nurturing |
| `edge-tts`（默认）/ `GPT-SoVITS`（本地克隆）/ `MiniMax`（云端克隆）；每句后台 prepare、声画对齐；`faster-whisper` 按住说话 | **五子棋**（三档难度，赢金币）/ **9 人狼人杀**（8 个独立 NPC Agent + 主持人，警长竞选、投票 PK、狼队私聊）；每日签到、喂食、状态衰减 |

---

## 现场演示 / Demo at a Glance

```
┌─ 桌宠本体（无边框、可拖拽、贴边半透明）──────────────┐
│   ╭─────────╮      "主人，上次你说的模型我跑过了，     │
│   │ (◕‿◕)  │       结果放在 data/works.json"         │
│   ╰─────────╯                                       │
└────────────────────────────────────────────────────┘

┌─ 聊天窗（工具步骤卡片 + 最终回答，TTS 流式朗读）──────┐
│ 你：无职转生最新一集讲了什么？                        │
│ 桌宠：[搜索] → 拉取剧评 → [整理] → "第 24 集……"      │
└────────────────────────────────────────────────────┘

┌─ 9 人狼人杀（多 Agent）──────────────────────────────┐
│ 主持人：天黑请闭眼……狼人请睁眼，选择击杀目标          │
│ 3 狼 / 预言家 / 女巫 / 猎人 / 3 平民，1 真人 + 8 NPC  │
│ 警长竞选 · 发言倒计时 · 平票 PK · 投票记票（谁投谁）  │
└────────────────────────────────────────────────────┘
```

> 架构与时序图见 [`docs/architecture.md`](docs/architecture.md)；浏览器即可体验的纯静态 Live2D Demo 见 [在线地址](https://255856.github.io/Smart-Desktop-Pet/)。

---

## 设计哲学 / Design Philosophy

- **可观测优先 / Observability-first**：每一次 Agent 决策落 SQLite，可视化、可回放、可重放调参
- **抗幻觉是工程问题，不是 prompt 问题 / Hallucination is engineering, not prompting**：用三层强制机制兜底，而不是堆砌"请你务必"
- **本地优先 / Local-first**：默认依赖可离线打包；TTS / ASR / Memory / 离线 Agent 均可本地跑
- **降级而非崩溃 / Degrade, never crash**：Live2D 挂了自动切 sprite；GPT-SoVITS 挂自动切 edge-tts；狼人杀无 API key 时 NPC 走规则行为，仍可玩
- **协议开放 / Open protocols**：MCP stdio server 内置（filesystem），任何 MCP 兼容 server 可外挂

---

# 中文

## 🐳 桌面宠物

把一个"会思考的数字生物"养在桌面上。它不是又一个聊天机器人壳子，而是一只会**主动搭话、调用工具、多轮推理、长期记忆**，还能**陪你下棋、玩狼人杀**的桌宠。

### 智能层
- **手写 ReAct Agent**：单循环实现「思考 → 行动 → 观察」；模型自主决定调什么工具、调几次、何时给最终答案；3 层抗幻觉兜底
- **可观测 Trace**：所有 LLM / 工具调用落 SQLite（`data/traces.db`），工具调用以步骤卡片展示，聊天窗只朗读最终结果
- **Live2D Demo（主入口）**：托盘菜单「Live2D Demo」或聊天窗「Demo」按钮打开本地纯静态演示页 `http://127.0.0.1:8765`
- **FastAPI Agent Trace（开发者）**：`http://127.0.0.1:8766` 可视化 Trace、回放、调参（托盘「Agent Trace（开发）」）
- **主动搭话**：空闲一段时间后桌宠主动冒泡（可关）
- **多角色 YAML**：每个角色独立的 system prompt / 情绪映射 / 声线

### 表现层
- **双渲染**：Live2D（Cubism 4 + pixi-live2d-display + QWebEngineView）/ PNG 帧动画，模型缺失自动 fallback
- **Live2D 能力**：唇形同步 / 呼吸 / 随机眨眼 / 头部跟随鼠标 / 动作队列；支持模型自带的表情、发型、头饰切换
- **场景触发动作**：在设置面板为「闲置、开心、思考、喂食……」等场景配置动作组合（可多选叠加表情 + 配饰 + 自定义动作），动作可自定义命名并持久化（仅 Live2D）
- **流式 TTS**：`edge-tts`（默认）/ `GPT-SoVITS`（本地克隆）/ `MiniMax`（云端克隆）；每句后台合成完才显示文字，杜绝抢话；引擎异常自动切换
- **ASR**：`faster-whisper` 按住说话（默认 F 键 / 可自定义）
- **表情包贴纸**：食物 / 角色表情以 45° 倾斜贴纸弹在角色右上角

### 陪伴玩法与养成
- **五子棋**：三档难度（简单 / 普通 / 困难，AI 强度与金币倍率不同），对战获胜可获得金币（每日有获取上限，防刷）
- **9 人狼人杀（多 Agent）**：标准 9 人屠边局（3 狼 / 预言家 / 女巫 / 猎人 / 3 平民），1 名真人 + 8 个**独立 NPC Agent**，桌宠担任主持人；每个 NPC 有独立人格与视角信息，狼人在独立"狼频道"协商击杀；含警长竞选、平票 PK、发言倒计时、投票记票（显示谁投了谁）；只有主持人有 TTS 音色；无 API key 时自动降级为规则 AI，离线可玩
- **每日签到**：每天签到领取 100 金币
- **养成系统**：体力 / 饱食 / 口渴 / 心情 / 健康 五项状态，按约 4 小时周期衰减；喂食（`data/foods.json`，多类别食物）改变状态，右上角弹出食物贴纸、气泡播报描述；经验、等级、好感度随互动成长

### 工具层（32 个，9 个分组）
| 分组 | 数量 | 典型工具 |
|---|---|---|
| `_time` | 2 | `get_current_time`、`get_pet_status` |
| `_reminder` | 3 | `add_reminder`、`list_reminders`、`delete_reminder` |
| `_memory` | 3 | `remember_fact`、`recall_memory`、`forget_memory` |
| `_pet` | 4 | `feed_self`、`play_animation`、`change_pet_emotion`、`say_to_user` |
| `_math` | 3 | `calculate`、`convert_units`、`date_info` |
| `_file` | 2 | `list_desktop_files`、`read_text_file` |
| `_system` | 7 | `open_app`、`system_info`、`take_screenshot`、`clipboard_copy`、`send_notification`、`open_website`、`list_installed_apps` |
| `_search` | 1 | `web_search`（Tavily + DuckDuckGo 双源） |
| `_shortcuts` | 7 | 任务管理器 / 控制面板 / 设置 / 文件资源管理器 / 终端 / 记事本 / 计算器 |

完整目录与"如何添加新工具" → [docs/tool-catalog.md](docs/tool-catalog.md)

### 记忆层
- **可插拔后端**：`TF-IDF`（零依赖）/ `sentence-transformers`（语义）
- **重要性评分**：写入时自动 / 手动打分
- **时间衰减**：旧记忆自动降权
- **冲突检测**：写入新记忆时检测与已有记忆冲突，给出合并 / 升级 / 降级建议
- **长期记忆演化**：定期任务合并重复、降级过期、淘汰噪声
- **聊天窗记忆面板**：聊天框内置记忆按钮，弹窗即可快速查看 / 新增 / 删除长期记忆

### 协议层
- **MCP stdio JSON-RPC** 内置（filesystem server 示例），可外挂任何 MCP 兼容 server
- **多 backend**：OpenAI 兼容接口 / Ollama / 自定义 base_url

---

## 快速开始

### 桌面端（完整桌宠）

1. 安装依赖

```powershell
cd E:\study\desktop-pet
pip install -r requirements.txt
pip install PyQtWebEngine        # Live2D 渲染依赖
```

2. 准备配置

```powershell
copy config.example.yaml config.yaml
# 编辑 config.yaml，填入 llm.api_key（或改用 Ollama）
```

最小可用配置（Ollama 本地，零成本）：

```yaml
llm:
  base_url: http://127.0.0.1:11434/v1
  api_key: ollama
  model: qwen2.5:7b
```

3. 启动

```powershell
python main.py                  # 启动桌宠本体
python -m app.web.dashboard     # 仅启动 Agent Trace 面板（默认 8766）
```

> 首次启动会引导你选择 Live2D 模型与 TTS 引擎；资源下载见 [docs/资源下载说明.md](docs/资源下载说明.md)。

### 纯静态 Live2D Demo（浏览器即可，无需后端）

`docs/demo/` 是一个**零构建、纯静态**的 Live2D 演示页（`index.html` + `live2d-demo.js`），内置 **Hiyori Pro / Hiyori Free / Miara Pro** 三个官方样例模型，支持中英文切换与模型切换，已部署到 GitHub Pages。

```powershell
# 本地预览（默认 http://127.0.0.1:8765/）
python docs/demo/serve.py
```

- 在线体验：<https://255856.github.io/Smart-Desktop-Pet/>
- 部署方式：见 [docs/demo/DEPLOY.md](docs/demo/DEPLOY.md)
- 官方样例模型来源：<https://www.live2d.com/zh-CHS/learn/sample/>
- 模型体积大、受版权约束，**不随 master 仓库提交**（仅部署在 gh-pages 分支）

---

## 快速验证

```powershell
python -m pytest tests/ -q        # 389 个测试
python scripts/verify_features.py # 97 项功能冒烟
python main.py                    # 启动桌宠本体
```

---

## 文件结构

```
desktop-pet/
├── main.py                     # 启动壳（import app.main）
├── config.example.yaml         # 配置示例
├── requirements.txt
├── app/
│   ├── main.py                 # 真正入口（横幅 + Ollama 探测 + 装配）
│   ├── core/                   # config / settings / save / tray
│   ├── brain/                  # agent / llm_client / memory / proactive / trace
│   │   └── _legacy/            # Plan-Execute-Reflect 路线遗产（仅 tests 引用）
│   ├── animation/              # sprite + live2d 双渲染 + factory
│   ├── voice/                  # TTS（3 引擎）+ ASR + 角色情绪
│   ├── engine/                 # state / tools(32) / chat_store / reminder / dashboard
│   │   └── tools/              # _time _reminder _memory _pet _math _file _system _search _shortcuts
│   ├── games/                  # 五子棋 gomoku / 狼人杀 werewolf + werewolf_agents + director
│   ├── ui/                     # 桌宠本体 / 聊天窗 / 设置 / 五子棋 / 狼人杀窗口 / UI 控制器
│   ├── mcp/                    # MCP 协议 + filesystem server
│   ├── agents/                 # Sub-agent 抽象基类
│   └── web/                    # FastAPI Dashboard（Agent Trace 可视化）
├── assets/
│   ├── icon.ico / icon.png     # 应用图标（也是聊天窗桌宠头像）
│   ├── sprites/                # 帧动画（gitignore，不入库）
│   ├── live2d_profiles/        # Live2D 模型 profile（gitignore）
│   └── food/                   # 投喂素材
├── characters/                 # 多角色 YAML（jingyuniang / …）
├── docs/
│   ├── demo/                   # 纯静态 Live2D Demo（GitHub Pages）
│   └── *.md                    # 架构 / 接入 / 工具 / 配置等说明
├── data/                       # 运行时数据（foods.json / 存档等，gitignore）
├── scripts/                    # verify_features.py 等
├── tests/                      # 389 个测试
├── voice/                      # GPT-SoVITS 训练音频（gitignore，仅 .gitkeep）
└── GPT-SoVITS-v2pro-*/         # 整合包（gitignore，仅 .gitkeep）
```

---

## 详细文档

| 文档 | 内容 |
|---|---|
| **[Live2D 在线 Demo](https://255856.github.io/Smart-Desktop-Pet/)** | 浏览器直接体验 Live2D 渲染（Hiyori / Miara，中英文） |
| [docs/demo/](docs/demo/README.md) | 纯静态 Demo 结构与本地预览 |
| [docs/demo/DEPLOY.md](docs/demo/DEPLOY.md) | Demo 部署到 GitHub Pages |
| [docs/quickstart.md](docs/quickstart.md) | 5 分钟跑起来 |
| [docs/architecture.md](docs/architecture.md) | 启动时序 + Live2D 渲染子图 + Agent 流程图 |
| [docs/live2d-integration.md](docs/live2d-integration.md) | `.model3.json` + profile 配置 + cubism-sdk 来源 + fallback 触发条件 |
| [docs/tts-integration.md](docs/tts-integration.md) | edge / gptsovits / minimax 三引擎 + 自愈逻辑 + 缓存格式嗅探 |
| [docs/config-reference.md](docs/config-reference.md) | `config.yaml` 全字段（默认 / 范围 / 说明） |
| [docs/tool-catalog.md](docs/tool-catalog.md) | 32 个工具完整说明（输入 / 输出 / 示例） |
| [docs/emotion-system.md](docs/emotion-system.md) | Emotion 枚举 + 标签解析 + 关键词兜底 |
| [docs/dev-testing.md](docs/dev-testing.md) | pytest 跑法 + verify_features 解读 + CI 接入 |
| [docs/troubleshooting.md](docs/troubleshooting.md) | FAQ 展开 |
| [docs/资源下载说明.md](docs/资源下载说明.md) | voice / sprites / GPT-SoVITS / Live2D 模型下载 |

---

## FAQ

- **桌宠启动后看不到？** 看右下角系统托盘，单击显隐，或右键菜单 → 显示桌宠；托盘右键可打开聊天 / 设置 / Live2D Demo / Agent Trace（开发）。
- **静态 Demo 本地打不开 `http://127.0.0.1:8765/`？** 直接用托盘「Live2D Demo」或聊天窗「Demo」按钮（会自动拉起本地服务）；也可手动 `python docs/demo/serve.py`（`--root .` 时根路径重定向到 `/docs/demo/index.html`）。
- **聊天窗闪退？** 看 `crash.log` 末尾错误；删 `data/chat_history.json` 重试。
- **LLM 报错？** 检查 `config.yaml` 的 `llm.api_key` / `base_url`；可切 Ollama（`base_url: http://127.0.0.1:11434/v1`）。
- **怎么玩五子棋 / 狼人杀？** 在桌宠右键菜单「小游戏」子菜单进入；五子棋选难度获胜得金币，狼人杀由桌宠主持、其他 8 位玩家由 AI 扮演。
- **金币怎么获得？** 每日签到（100 金币）与玩小游戏（每日有上限）；金币用于购买食物等。
- **切换 Live2D？** `config.yaml` → `pet.renderer: live2d` + `pet.live2d.model_dir`；不可用自动 fallback sprite。
- **切换 TTS？** `config.yaml` → `character.tts_engine: edge | gptsovits | minimax`；切换后会自动嗅探缓存格式。
- **GPT-SoVITS 启动失败？** 看 `data/tts_api.log`；确认 `voice/<角色>/` 下有参考音频。
- **想加自己的工具？** 参考 [docs/tool-catalog.md](docs/tool-catalog.md) 末尾的"如何添加新工具"。
- **想加自己的角色？** 在 `characters/` 下复制 `jingyuniang.yaml`，改 `system_prompt` / `tts_engine` / `live2d_model_dir`。

---

## 贡献

PR 欢迎，但请先阅读：

- [docs/dev-testing.md](docs/dev-testing.md) — 测试约定
- [docs/architecture.md](docs/architecture.md) — 模块边界
- `.gitignore` — 哪些目录**不能**提交（版权资源、运行时数据、计划类文档）

---

## 协议

MIT

版权资源（Live2D 官方模型、参考音频、GPT-SoVITS 整合包）受上游版权约束，**不随仓库分发**，仅在 `assets/` / `voice/` / `GPT-SoVITS-*/` 留 `.gitkeep` 骨架；纯静态 Demo 所用的官方样例模型只部署在 gh-pages 分支。

---
---

# English

## 🐳 Desktop Pet

Raise a **thinking digital being** on your desktop. Not just another chatbot shell — this is a companion that **speaks proactively, calls tools, reasons across turns, remembers you long-term, and even plays Gomoku and Werewolf with you**.

### Intelligence Layer
- **Hand-rolled ReAct Agent** — single-loop "think → act → observe"; the model decides which tools to call and when to answer; 3-layer anti-hallucination.
- **Observable Traces** — every LLM / tool call lands in SQLite (`data/traces.db`); tool calls render as step cards, and only the final answer is read aloud.
- **Live2D Demo (main entry)** — tray menu "Live2D Demo" or the chat "Demo" button opens the local static demo at `http://127.0.0.1:8765`.
- **FastAPI Agent Trace (dev)** — `http://127.0.0.1:8766` for visualization, replay, tuning (tray "Agent Trace").
- **Proactive Chatter** — the pet pops up after idle (toggleable).
- **Multi-character YAML** — each character has independent prompt / emotion map / voice.

### Presentation Layer
- **Dual renderer** — Live2D (Cubism 4 + pixi-live2d-display + QWebEngineView) / PNG sprite; auto-fallback if Live2D fails.
- **Live2D goodies** — lip-sync / breathing / random blink / head follow / motion queue; built-in expression / hairstyle / accessory switching.
- **Scene-triggered actions** — map scenes (idle, happy, thinking, feeding, …) to motion combos (expression + accessory + custom named actions, multi-select), persisted per Live2D model.
- **Streaming TTS** — `edge-tts` (default) / `GPT-SoVITS` (local clone) / `MiniMax` (cloud clone); text only renders after a sentence is synthesized; self-healing engine switch.
- **ASR** — `faster-whisper` push-to-talk (default `F` key / rebindable).
- **Sticker emoji** — food / expression stickers pop up tilted at 45° beside the character.

### Games & Nurturing
- **Gomoku** — three AI difficulty levels with different coin rewards; a daily coin cap prevents farming.
- **9-player Werewolf (multi-agent)** — standard 9-player setup (3 wolves / seer / witch / hunter / 3 villagers): 1 human + **8 independent NPC agents**, with the pet as host; each NPC has its own persona and view; wolves coordinate in a private wolf channel; includes sheriff election, tie PK, speech countdown, and per-vote tally; only the host has a TTS voice; with no API key the NPCs fall back to rule-based behavior so it stays playable offline.
- **Daily check-in** — claim 100 coins once per day.
- **Nurturing** — stamina / fullness / thirst / mood / health decay on an ~4-hour cycle; feed foods (`data/foods.json`) to change stats, with a food sticker and bubble description; XP, level, and affinity grow with interaction.

### Tooling (32 tools in 9 groups)
| Group | Count | Examples |
|---|---|---|
| `_time` | 2 | `get_current_time`, `get_pet_status` |
| `_reminder` | 3 | `add_reminder`, `list_reminders`, `delete_reminder` |
| `_memory` | 3 | `remember_fact`, `recall_memory`, `forget_memory` |
| `_pet` | 4 | `feed_self`, `play_animation`, `change_pet_emotion`, `say_to_user` |
| `_math` | 3 | `calculate`, `convert_units`, `date_info` |
| `_file` | 2 | `list_desktop_files`, `read_text_file` |
| `_system` | 7 | `open_app`, `system_info`, `take_screenshot`, `clipboard_copy`, `send_notification`, `open_website`, `list_installed_apps` |
| `_search` | 1 | `web_search` (Tavily + DuckDuckGo) |
| `_shortcuts` | 7 | task manager / control panel / settings / file explorer / terminal / notepad / calculator |

Full catalog → [docs/tool-catalog.md](docs/tool-catalog.md)

### Memory Layer
- **Pluggable backend** — `TF-IDF` (zero-dep) / `sentence-transformers` (semantic).
- **Importance scoring**, **time decay**, **conflict detection** (merge / upgrade / downgrade suggestions).
- **Long-term evolution** — periodic merge of duplicates, demotion of stale, retirement of noise.
- **In-chat memory panel** — a memory button opens a dialog to quickly view / add / delete long-term memories.

### Protocol Layer
- **MCP stdio JSON-RPC** built-in (filesystem server sample); plug any MCP-compatible server.
- **Multi-backend** — OpenAI-compatible / Ollama / custom `base_url`.

---

## Quick Start

### Desktop app (full pet)

```powershell
cd E:\study\desktop-pet
pip install -r requirements.txt
pip install PyQtWebEngine         # Live2D renderer dependency

copy config.example.yaml config.yaml   # set llm.api_key (or use Ollama)
python main.py
python -m app.web.dashboard        # Agent Trace panel (port 8766)
```

Minimal config (Ollama local, zero-cost):

```yaml
llm:
  base_url: http://127.0.0.1:11434/v1
  api_key: ollama
  model: qwen2.5:7b
```

### Static Live2D Demo (browser-only, no backend)

`docs/demo/` is a **zero-build, fully static** Live2D page (`index.html` + `live2d-demo.js`) shipping three official sample models — **Hiyori Pro / Hiyori Free / Miara Pro** — with Chinese/English UI and model switching, deployed to GitHub Pages.

```powershell
python docs/demo/serve.py         # http://127.0.0.1:8765/
```

- Online: <https://255856.github.io/Smart-Desktop-Pet/>
- Deployment: [docs/demo/DEPLOY.md](docs/demo/DEPLOY.md)
- Official samples: <https://www.live2d.com/en/learn/sample/>
- Models are large and copyright-bound, so they are **not committed to master** (they live only on the gh-pages branch).

---

## Verify

```powershell
python -m pytest tests/ -q          # 389 tests
python scripts/verify_features.py  # 97 smoke checks
python main.py                      # launch the pet
```

---

## FAQ

- **Pet not visible after launch?** Check the system tray (bottom-right); click to show/hide, or use the tray menu for chat / settings / Live2D Demo / Agent Trace.
- **Local Demo `http://127.0.0.1:8765/` won't open?** Use the tray "Live2D Demo" or chat "Demo" button (auto-starts the local server), or run `python docs/demo/serve.py` (root auto-redirects to the demo).
- **Chat window crashes?** See the tail of `crash.log`; try deleting `data/chat_history.json`.
- **LLM errors?** Check `llm.api_key` / `base_url` in `config.yaml`, or switch to Ollama.
- **How do I play Gomoku / Werewolf?** Open the "Mini Games" submenu in the pet's right-click menu; Gomoku pays coins for wins, and Werewolf is hosted by the pet with 8 AI players.
- **How do I earn coins?** Daily check-in (100 coins) and mini-games (with a daily cap); coins buy food and items.
- **Switch to Live2D?** Set `pet.renderer: live2d` + `pet.live2d.model_dir`; auto-fallback to sprite if missing.
- **Switch TTS?** Set `character.tts_engine: edge | gptsovits | minimax`; cache format is auto-sniffed.
- **GPT-SoVITS fails to start?** Check `data/tts_api.log`; ensure `voice/<character>/` has reference audio.
- **Add a tool?** See "Adding new tools" at the end of [docs/tool-catalog.md](docs/tool-catalog.md).
- **Add a character?** Copy `characters/jingyuniang.yaml` and edit `system_prompt` / `tts_engine` / `live2d_model_dir`.

---

## Contributing

PRs welcome. Please first read:

- [docs/dev-testing.md](docs/dev-testing.md) — testing conventions
- [docs/architecture.md](docs/architecture.md) — module boundaries
- `.gitignore` — what **must not** be committed (copyrighted assets, runtime data, planning docs)

---

## License

MIT

Copyrighted assets (official Live2D models, reference audio, the GPT-SoVITS bundle) are restricted by upstream licenses and **are not distributed with this repo** — only `.gitkeep` skeletons remain in `assets/` / `voice/` / `GPT-SoVITS-*/`. The official sample models used by the static Demo are deployed solely on the gh-pages branch.
