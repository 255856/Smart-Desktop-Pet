# 🐳 桌面宠物 · Desktop Pet

> **一个真正能"养"的桌面 AI 宠物** —— 不只是会动、会说话，而是会**记住你、主动搭话、调用工具、跨多轮思考**的数字伙伴。
>
> A truly *livestockable* desktop AI companion — not just animated and voiced, but one that **remembers you, speaks proactively, calls tools, and thinks across multiple turns**.

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)](https://www.python.org)
[![PyQt5](https://img.shields.io/badge/UI-PyQt5%2BWebEngine-41CD52?logo=qt&logoColor=white)](https://riverbankcomputing.com)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-325%2B%20passing-brightgreen?logo=pytest)](tests/)
[![Verify](https://img.shields.io/badge/verify_features-94%2B%20items-blueviolet)](scripts/verify_features.py)
[![Live2D Demo](https://img.shields.io/badge/🎬_Live2D_Demo-Try%20Online-6c5ce7?style=flat&logo=githubpages&logoColor=white)](https://255856.github.io/Smart-Desktop-Pet/)

[中文](#中文) · [English](#english) · [🎬 **Live2D Demo**](https://255856.github.io/Smart-Desktop-Pet/) · [文档 / Docs](docs/) · [快速开始 / Quick Start](#快速开始--quick-start)

---

## ✨ 它能做什么？ / What can it do?

|  **多模态表现**<br/>Multimodal Presence |  **真正能思考的大脑**<br/>A Brain That Actually Thinks |
|---|---|
| 双渲染架构：**Live2D Cubism 4**（QWebEngineView，唇形/呼吸/动作）与 **PNG 帧动画** 自由切换；Live2D 不可用自动 fallback | 手写 **ReAct Agent 循环**，可选 **LangChain** 后端；3 层抗幻觉：<br/>① `force_tool_use` 第一轮强制调工具<br/>② `force_retry` 跨多轮持续到 `max_turns`<br/>③ `force_final` 工具结果 → 直接总结，不再二次套娃 |

|  **31 个工具随时调用**<br/>31 Tools On Call |  **真的会记住你**<br/>It Actually Remembers You |
|---|---|
| 时间 / 提醒 / 记忆 / 计算 / 单位换算 / 文件 / 截图 / 农历 / 系统状态 / 应用启动 / 网页搜索（Tavily + DuckDuckGo 双源）/ 快捷指令…… | **TF-IDF ↔ sentence-transformers** 可插拔后端；**重要性评分 + 时间衰减 + 冲突检测**；**长期记忆演化**（合并/升级/降级/淘汰）；跨会话持久 |
|  **3 引擎 TTS + 流式同步**<br/>3 TTS Engines, Stream-Synced |  **按住说话**<br/>Push-to-Talk ASR |
| `edge-tts`（默认，云端）/ `GPT-SoVITS`（本地克隆）/ `MiniMax`（云端克隆）；**每句后台 prepare** + 主线程等到合成完毕才显示文字 | `faster-whisper` 本地语音识别；流式文字与发音精准对齐，不抢话、不复读 |

---

##  现场演示 / Demo at a Glance

```
┌─ 桌宠本体（无边框、可拖拽、贴边自动半透明）──────┐
│  ╭─────────╮                                       ││
│  │  (◕‿◕)  │   "主人，上次你说的那个模型我帮你      ││
│  │  /| |\  │    跑了一遍，结果放在 data/works.json" ││
│  ╰─────────╯                                       ││
└────────────────────────────────────────────────────┘┘

┌─ 聊天窗 ────────────────────────────────────────────┐
│ 你：无职转生最新一集讲了什么？                        │
│ 桌宠：[搜索] → 拉取第 24 集剧评 → [整理] →         │
│       "第 24 集鲁迪乌斯与父亲和解……"               │
│  *（同时 TTS 流式朗读）*                            │
└────────────────────────────────────────────────────┘

┌─ Agent Trace Dashboard（FastAPI :8765）─────────────┐
│ turn │ tool       │ latency │ result                │
│  1   │ web_search │  820 ms │ 5 raw hits            │
│  2   │ summarize  │  340 ms │ final answer          │
│ [SQLite] traces.db                                 │
└────────────────────────────────────────────────────┘
```

>  截图见 [`docs/architecture.md`](docs/architecture.md)；Trace Dashboard 启动后访问 `http://127.0.0.1:8765`。

---

##  设计哲学 / Design Philosophy

- **可观测优先 / Observability-first**：每一次 Agent 决策落 SQLite，可视化、可回放、可重放调参
- **抗幻觉是工程问题，不是 prompt 问题 / Hallucination is engineering, not prompting** —— 用三层强制机制兜底，而不是堆砌"请你务必"
- **本地优先 / Local-first**：默认依赖已离线打包在 `.local-packages/`；TTS/ASR/Memory 可全本地跑
- **降级而非崩溃 / Degrade, never crash**：Live2D 挂了自动切 sprite；GPT-SoVITS 挂自动切 edge-tts；LLM 报错有友好兜底回复
- **协议开放 / Open protocols**：MCP stdio server 内置（filesystem），任何 MCP 兼容 server 可外挂

---

# 中文

## 🐳 桌面宠物

把一个"会思考的数字生物"养在你的桌面上。它不是又一个聊天机器人壳子，而是一只会**主动搭话、调用工具、多轮推理、长期记忆**的桌宠。

###  核心特性

####  智能层
- **手写 ReAct Agent**：单循环实现「思考→行动→观察」；3 层抗幻觉
- **可观测 Trace**：所有 LLM/工具调用落 SQLite（`data/traces.db`）
- **FastAPI Dashboard**：本机 `http://127.0.0.1:8765` 可视化 Trace、回放、调参
- **主动搭话**：空闲 N~M 分钟后桌宠主动冒泡（可关）
- **多角色 YAML**：每个角色独立的 system prompt / 情绪映射 / 声线

####  表现层
- **双渲染**：Live2D（Cubism 4 + pixi-live2d-display + QWebEngineView）/ PNG 帧动画
- **Live2D 特性**：唇形同步 / 呼吸 / 随机眨眼 / 动作队列；模型缺失自动 fallback sprite
- **流式 TTS**：`edge-tts` / `GPT-SoVITS`（本地克隆）/ `MiniMax`（云端克隆）
- **TTS 同步**：每句话后台 `prepare()` 合成完毕才显示文字，杜绝「抢话」
- **TTS 自愈**：缓存命中失败 / 网络超时 / 引擎宕机自动切换备用引擎
- **ASR**：`faster-whisper` 按住说话（默认 F 键 / 自定义热键）

####  工具层（31 个，按 9 个分组）
| 分组 | 数量 | 典型工具 |
|---|---|---|
| `_time` | 5 | `get_time`、`get_lunar`、`countdown` |
| `_reminder` | 4 | `add_reminder`、`list_reminders`、`cancel_reminder` |
| `_memory` | 4 | `remember_fact`、`search_memory`、`forget_memory`、`evolve_memory` |
| `_pet` | 6 | `set_emotion`、`switch_outfit`、`feed_food`、`trigger_action` |
| `_math` | 7 | `calc`、`unit_convert`、`random_choice` |
| `_file` | 3 | `read_file`、`write_file`、`list_dir` |
| `_system` | 10 | `system_info`、`screenshot`、`open_app`、`web_search`（Tavily + DDG）|
| `_shortcuts` | 8 | 自定义快捷指令 / 工作流 |
| `_core` | 6 | 内部编排（chat_store / dashboard / 等）|

完整目录 → [docs/tool-catalog.md](docs/tool-catalog.md)

####  记忆层
- **可插拔后端**：`TF-IDF`（零依赖）/ `sentence-transformers`（语义）
- **重要性评分**：写入时人工/自动打分
- **时间衰减**：旧记忆自动降权
- **冲突检测**：写入新记忆时检测与已有记忆冲突，给出合并/升级/降级建议
- **长期记忆演化**：定期任务合并重复、降级过期、淘汰噪声

#### 🔌 协议层
- **MCP stdio JSON-RPC** 内置（filesystem server 示例）
- 可外挂任何 MCP 兼容 server
- **多 backend**：`OpenAI compatible` / `Ollama` / 自定义 base_url

---

##  快速开始 / Quick Start

### 1️⃣ 准备依赖 / Install

```powershell
# 依赖已离线打包在 .local-packages/，无网络也能跑
cd E:\study\desktop-pet
pip install -r requirements.txt   # 若 .local-packages 不够用再装
pip install PyQtWebEngine         # Live2D 渲染依赖
```

### 2️⃣ 准备配置 / Configure

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

### 3️⃣ 启动 / Launch

```powershell
# 方式 A：直接启动
python main.py

# 方式 B：桌面快捷方式（推荐）
# 双击 C:\Users\<你>\Desktop\DesktopPet.lnk
# （会自动启动 GPT-SoVITS + Ollama 等后台服务）

# 方式 C：仅启动 Dashboard
python -m app.web.dashboard
# 浏览器打开 http://127.0.0.1:8765
```

> 第一次启动会引导你选 **Live2D 模型** 与 **TTS 引擎**；资源下载见 [docs/资源下载说明.md](docs/资源下载说明.md)。

---

##  快速验证 / Verify

```powershell
python -m pytest tests/ -q                 # 325+ 测试
python scripts/verify_features.py          # 94+ 项功能冒烟
python main.py                             # 启动桌宠本体
```

---

##  文件结构 / File Layout

```
desktop-pet/
├── main.py                     # 启动壳（import app.main）
├── config.example.yaml         # 配置示例
├── requirements.txt
├── app/
│   ├── main.py                 # 真正的入口（横幅 + Ollama 探测 + 装配）
│   ├── core/                   # config / settings / save / tray
│   ├── brain/                  # agent / llm_client / memory / proactive / trace
│   │   └── _legacy/            # v3.0 Plan-Execute-Reflect 路线（仅 tests 引用）
│   ├── animation/              # sprite + live2d 双渲染 + factory
│   ├── voice/                  # TTS（3 引擎）+ ASR + 角色情绪
│   ├── engine/                 # state / tools(31) / chat_store / reminder / dashboard
│   │   └── tools/              # _core _file _math _memory _pet _reminder _search _shortcuts _system _time
│   ├── ui/                     # 桌宠本体 + 聊天窗 + 设置面板 + UI 控制器
│   ├── mcp/                    # MCP 协议 + filesystem server
│   ├── agents/                 # Sub-agent 抽象基类（base.py）
│   ├── web/                    # FastAPI Dashboard（:8765，Agent Trace 可视化）
│   └── eval/                   # Agent Eval 套件（cases.py）
├── assets/
│   ├── sprites/                # 帧动画（gitignore）
│   ├── live2d_profiles/        # Live2D 模型 profile（bingtang / 超频猫猫）
│   ├── icons/
│   └── food/                   # 投喂素材（rmbg-1 ~ rmbg-10）
├── characters/                 # 多角色 YAML（jingyuniang / …）
├── voice/                      # GPT-SoVITS 训练音频（gitignore，仅 .gitkeep）
├── GPT-SoVITS-v2pro-*/         # 整合包（gitignore，仅 .gitkeep）
├── docs/                       # 详细文档
├── scripts/                    # run.py / verify_features.py
├── tests/                      # 325+ 测试
└── tools/                      # clone_voice.py + _legacy/（历史脚本归档）
```

---

## 详细文档 / Detailed Docs

| 文档 | 内容 |
|---|---|
| 🎬 [**Live2D 演示页**](docs/demo/) | 浏览器独立体验 Live2D 渲染（**不含版权模型**，需自备） |
| [docs/quickstart.md](docs/quickstart.md) | 5 分钟跑起来 |
| [docs/architecture.md](docs/architecture.md) | 启动时序 + Live2D 渲染子图 + Agent 流程图 |
| [docs/live2d-integration.md](docs/live2d-integration.md) | `.model3.json` + `*.model.yaml` 五段配置 + cubism-sdk 来源 + fallback 触发条件 |
| [docs/tts-integration.md](docs/tts-integration.md) | edge / gptsovits / minimax 三引擎 + 自愈逻辑 + 缓存格式嗅探 |
| [docs/config-reference.md](docs/config-reference.md) | `config.yaml` 全字段（默认/范围/说明）|
| [docs/tool-catalog.md](docs/tool-catalog.md) | 31 个工具完整说明（输入/输出/示例）|
| [docs/emotion-system.md](docs/emotion-system.md) | 11 个 Emotion 枚举 + 标签解析 + 关键词兜底 |
| [docs/dev-testing.md](docs/dev-testing.md) | pytest 跑法 + verify_features 解读 + CI 接入 |
| [docs/troubleshooting.md](docs/troubleshooting.md) | FAQ 展开 |
| [docs/资源下载说明.md](docs/资源下载说明.md) | voice / sprites / GPT-SoVITS / Live2D 模型下载 |

---

## FAQ

- **桌宠启动后看不到？** 看右下角系统托盘，单击显隐；或右键 → 退出后重启
- **聊天窗闪退？** 看 `crash.log` 末尾错误；删 `data/chat_history.json` 重试
- **LLM 报错？** 检查 `config.yaml` 的 `llm.api_key` / `base_url`；可切 Ollama（`base_url: http://127.0.0.1:11434/v1`）
- **切换 Live2D？** `config.yaml` → `pet.renderer: live2d` + `pet.live2d.model_dir`；不可用自动 fallback sprite
- **切换 TTS？** `config.yaml` → `character.tts_engine: edge | gptsovits | minimax`；切换后会自动嗅探缓存格式
- **GPT-SoVITS 启动失败？** 看 `data/tts_api.log`；确认 `voice/<角色>/` 下有参考音频
- **想加自己的工具？** 参考 [docs/tool-catalog.md](docs/tool-catalog.md) 末尾的"如何添加新工具"
- **想加自己的角色？** 在 `characters/` 下复制 `jingyuniang.yaml`，改 `system_prompt` / `tts_engine` / `live2d_model_dir`

---

## 贡献 / Contributing

PR 欢迎，但请先阅读：
- [docs/dev-testing.md](docs/dev-testing.md) — 测试约定
- [docs/architecture.md](docs/architecture.md) — 模块边界
- `.gitignore` — 哪些目录**不能**提交（版权资源）

---

## 协议 / License

MIT

版权资源（Live2D 模型、参考音频）受上游版权约束，**不随仓库分发**，仅在 `assets/` / `voice/` / `GPT-SoVITS-*/` 留 `.gitkeep` 骨架。

---

---

# English

## 🐳 Desktop Pet

Raise a **thinking digital being** on your desktop. Not just another chatbot shell — this is a companion that **speaks proactively, calls tools, reasons across turns, and remembers you long-term**.

### Core Features

#### Intelligence Layer
- **Hand-rolled ReAct Agent** — single-loop "think → act → observe"; 3-layer anti-hallucination
- **Observable Traces** — every LLM/tool call lands in SQLite (`data/traces.db`)
- **FastAPI Dashboard** — `http://127.0.0.1:8765` for visualization, replay, tuning
- **Proactive Chatter** — pet pops up after N~M idle minutes (toggleable)
- **Multi-character YAML** — each character has independent prompt / emotion map / voice

#### Presentation Layer
- **Dual renderer** — Live2D (Cubism 4 + pixi-live2d-display + QWebEngineView) / PNG sprite; auto-fallback if Live2D fails
- **Live2D goodies** — lip-sync / breathing / random blink / motion queue
- **Streaming TTS** — `edge-tts` (default, cloud) / `GPT-SoVITS` (local clone) / `MiniMax` (cloud clone)
- **TTS sync** — every sentence pre-synthesized before text renders; no stutter, no repeats
- **TTS self-healing** — cache miss / timeout / engine crash → auto-switch to fallback engine
- **ASR** — `faster-whisper` push-to-talk (default `F` key / rebindable)

#### Tooling (31 tools in 9 groups)
| Group | Count | Examples |
|---|---|---|
| `_time` | 5 | `get_time`, `get_lunar`, `countdown` |
| `_reminder` | 4 | `add_reminder`, `list_reminders`, `cancel_reminder` |
| `_memory` | 4 | `remember_fact`, `search_memory`, `forget_memory`, `evolve_memory` |
| `_pet` | 6 | `set_emotion`, `switch_outfit`, `feed_food`, `trigger_action` |
| `_math` | 7 | `calc`, `unit_convert`, `random_choice` |
| `_file` | 3 | `read_file`, `write_file`, `list_dir` |
| `_system` | 10 | `system_info`, `screenshot`, `open_app`, `web_search` (Tavily + DDG) |
| `_shortcuts` | 8 | custom shortcuts / workflows |
| `_core` | 6 | orchestration (chat_store / dashboard / …) |

Full catalog → [docs/tool-catalog.md](docs/tool-catalog.md)

#### Memory Layer
- **Pluggable backend** — `TF-IDF` (zero-dep) / `sentence-transformers` (semantic)
- **Importance scoring** — manual / auto at write time
- **Time decay** — old memories auto-down-weighted
- **Conflict detection** — detects contradictions on write, suggests merge/upgrade/downgrade
- **Long-term evolution** — periodic task merges duplicates, demotes stale, retires noise

#### Protocol Layer
- **MCP stdio JSON-RPC** built-in (filesystem server sample)
- Plug any MCP-compatible server
- **Multi-backend** — `OpenAI compatible` / `Ollama` / custom `base_url`

---

## Quick Start

### 1️⃣ Install

```powershell
cd E:\study\desktop-pet
pip install -r requirements.txt
pip install PyQtWebEngine         # Live2D renderer dependency
```

### 2️⃣ Configure

```powershell
copy config.example.yaml config.yaml
# Edit config.yaml, set llm.api_key (or use Ollama)
```

Minimal config (Ollama local, zero-cost):
```yaml
llm:
  base_url: http://127.0.0.1:11434/v1
  api_key: ollama
  model: qwen2.5:7b
```

### 3️⃣ Launch

```powershell
python main.py                    # direct
# or double-click DesktopPet.lnk on your desktop (auto-starts GPT-SoVITS + Ollama)
# or: python -m app.web.dashboard  # dashboard only → http://127.0.0.1:8765
```

First launch walks you through **Live2D model** + **TTS engine** selection. See [docs/资源下载说明.md](docs/资源下载说明.md) for resource downloads.

---

## Verify

```powershell
python -m pytest tests/ -q                 # 325+ tests
python scripts/verify_features.py          # 94+ smoke checks
python main.py                             # launch the pet
```

---

## FAQ

- **Pet not visible after launch?** Check system tray (bottom-right); click to show/hide.
- **Chat window crashes?** See tail of `crash.log`; try deleting `data/chat_history.json`.
- **LLM errors?** Check `llm.api_key` / `base_url` in `config.yaml`; or switch to Ollama.
- **Switch to Live2D?** Set `pet.renderer: live2d` + `pet.live2d.model_dir`; auto-fallback to sprite if missing.
- **Switch TTS?** Set `character.tts_engine: edge | gptsovits | minimax`; cache format is auto-sniffed.
- **GPT-SoVITS fails to start?** Check `data/tts_api.log`; ensure `voice/<character>/` has reference audio.
- **Add your own tool?** See "Adding new tools" at the end of [docs/tool-catalog.md](docs/tool-catalog.md).
- **Add your own character?** Copy `characters/jingyuniang.yaml` and edit `system_prompt` / `tts_engine` / `live2d_model_dir`.

---

## Contributing

PRs welcome. Please first read:
- [docs/dev-testing.md](docs/dev-testing.md) — testing conventions
- [docs/architecture.md](docs/architecture.md) — module boundaries
- `.gitignore` — directories that **must not** be committed (copyrighted assets)

---

## License

MIT

Copyrighted assets (Live2D models, reference audio) are restricted by upstream licenses and **are not distributed with this repo** — only `.gitkeep` skeletons remain in `assets/` / `voice/` / `GPT-SoVITS-*/`.
