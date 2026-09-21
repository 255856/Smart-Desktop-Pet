# 🐳 桌面宠物 · Smart Desktop Pet

> **一个真正能"养"的桌面 AI 宠物** —— 不只是会动、会说话，而是会**记住你、主动搭话、调用工具、跨多轮思考**，还能**陪你下五子棋、玩狼人杀**的数字伙伴。
>
> A truly *livestockable* desktop AI companion — remembers you, speaks proactively, calls tools, reasons across turns, and plays Gomoku & Werewolf with you.

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)](https://www.python.org)
[![PyQt5](https://img.shields.io/badge/UI-PyQt5%2BWebEngine-41CD52?logo=qt&logoColor=white)](https://riverbankcomputing.com)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-382%20passed%20%2F%206%20skipped-brightgreen?logo=pytest)](tests/)
[![Verify](https://img.shields.io/badge/verify_features-93%2F94-blueviolet)](scripts/verify_features.py)
[![Live2D Demo](https://img.shields.io/badge/Live2D%20Demo-Try%20Online-6c5ce7?logo=githubpages&logoColor=white)](https://255856.github.io/Smart-Desktop-Pet/)

[中文](#-5-分钟跑起来) · [English](#-5-min-quick-start) · [**Live2D 在线 Demo**](https://255856.github.io/Smart-Desktop-Pet/) · [文档 / Docs](docs/)

---

## 它能做什么？

- **真智能**：手写 **ReAct Agent** 循环 + **3 层抗幻觉**（强制调工具 → 跨轮持续到 max_turns → 工具结果直接总结），可选 **LangChain** 后端
- **32个工具**：时间 / 提醒 / 记忆 / 计算 / 文件 / 截图 / 系统 / 应用 / 网页搜索（Tavily + DuckDuckGo）/ 快捷指令...
- **真记忆**：`TF-IDF` / `sentence-transformers` 可插拔；重要性评分 + 时间衰减 + 冲突检测 + 长期演化
- **真说话**：**3 引擎 TTS**（edge-tts / GPT-SoVITS 本地克隆 / MiniMax 云端）+ 浏览器 SpeechRecognition **ASR 按住说话** + 嘴型同步
- **真模样**：**双渲染**（Live2D Cubism 4 + pixi-live2d-display / PNG 帧动画），模型缺失自动 fallback
- **真玩法**：**五子棋**（3 档 AI 难度）+ **9 人狼人杀**（你 + 8 个独立 NPC Agent + 桌宠主持，无 API key 也能离线玩）+ 每日签到 + 喂食 + 5 项状态养成
- **真开放**：内置 **MCP stdio JSON-RPC** + filesystem server，可外挂任何 MCP 兼容 server
- **可观测**：所有 LLM/工具调用落 SQLite，**FastAPI Dashboard** (`localhost:8766`) 可视化 Trace、回放、调参

---

## 5 分钟跑起来

> **目标**：克隆 → 安装 → 配置 → 启动桌宠 → 看 UI
> **依赖**：Python 3.10+、Windows 4GB 内存

### 第 1 步：克隆仓库

```bash
git clone https://github.com/255856/Smart-Desktop-Pet.git
cd Smart-Desktop-Pet
```

### 第 2 步：安装 Python 依赖

```bash
# 推荐用虚拟环境
python -m venv .venv
source .venv/bin/activate        # Windows PowerShell: .\.venv\Scripts\Activate.ps1

pip install -r requirements.txt
pip install PyQtWebEngine        # Live2D 渲染依赖（GUI 必需）
```

> **依赖说明**：基础栈（httpx + edge-tts + pygame + Pillow + PyYAML）即可跑通桌宠本体。
> **LangChain 后端**（`requirements.txt` 末尾）是 **可选** 的，`brain.backend: standard` 时才用。

### 第 3 步：配置 LLM API Key

桌宠需要 LLM 才能"思考"。两种方式任选：

**方式 A：使用 OpenAI / DeepSeek / 通义等云端 API**（推荐起步）

```bash
cp config.example.yaml config.yaml
```

编辑 `config.yaml`：
```yaml
llm:
  base_url: https://api.openai.com/v1        # 或 https://api.deepseek.com/v1
  api_key: sk-xxxxxxx                       # 你的 API Key
  model: gpt-4o-mini                        # 或 deepseek-chat / qwen-turbo
```

**方式 B：本地 Ollama**（零成本、零配置）

```bash
# 1. 安装 Ollama（https://ollama.com）
# 2. 拉模型
ollama pull qwen2.5:7b
# 3. config.yaml 改：
llm:
  base_url: http://127.0.0.1:11434/v1
  api_key: ollama
  model: qwen2.5:7b
```

### 第 4 步：启动桌宠

```bash
python main.py
```

**首次启动会引导你**：
1. 选择 Live2D 模型（可暂时选 sprite 跳过）
2. 选择 TTS 引擎（默认 edge-tts，无需 Key）
3. 桌宠出现在桌面右下角，**右键托盘图标**可打开聊天窗、设置、Agent Trace 等

### 第 5 步：（可选）启动 Agent Trace Dashboard

```bash
python -m app.web.dashboard
# 浏览器打开 http://127.0.0.1:8766
```

可视化每一次 Agent 决策的工具调用链。

### ✅ 验证安装（独立步骤，确认环境 OK）

```bash
python -m pytest tests/ -q              # 382 个测试通过，6 个 skip（约 30s）
python scripts/verify_features.py --no-gui    # 93 项功能冒烟通过
python docs/demo/serve.py --port 8765  # 浏览器 http://127.0.0.1:8765 看 Live2D Demo
```

---

## 项目结构

```
Smart-Desktop-Pet/
├── main.py                  # 启动壳（import app.main）
├── config.example.yaml      # 配置示例（复制为 config.yaml）
├── requirements.txt         # Python 依赖
├── pytest.ini
│
├── app/                     # 94 个源文件（10 个子模块）
│   ├── main.py              # 真正入口（横幅 + Ollama 探测 + 装配）
│   ├── core/                # config / settings / save / tray / qt_compat
│   ├── brain/               # agent / llm_client / memory / proactive / trace
│   │   └── _legacy/         # Plan-Execute-Reflect 路线遗产（tests 仍引用）
│   ├── animation/           # Live2D + sprite 双渲染
│   │   └── cubism-sdk/      # 桌面 SDK（QWebEngineView 内嵌用）
│   ├── voice/               # TTS 3 引擎 + ASR + 角色情绪
│   ├── engine/              # state / tools(32) / chat_store / reminder / screenshot
│   │   └── tools/           # 32 个工具按 _time _reminder _memory _pet _math _file _system _search _shortcuts 分组
│   ├── games/               # gomoku + werewolf + werewolf_agents + director
│   ├── ui/                  # 桌宠本体 + 聊天窗 + 设置 + 5 子游戏窗口 + UI 控制器
│   ├── mcp/                 # MCP stdio JSON-RPC 协议 + filesystem server
│   ├── agents/              # Sub-agent 抽象基类（LifeAgent / ResearchAgent / CodeAgent / Orchestrator）
│   ├── web/                 # FastAPI Dashboard（Agent Trace）
│   └── eval/                # Eval 框架（cases.py）
│
├── assets/                  # 应用图标 + 投喂素材 + Live2D profile（live2d 模型 gitignore 不入库）
├── characters/              # 角色 YAML（jingyuniang.yaml 是示例；克隆后直接可用）
├── docs/                    # 详细文档（10 篇 1000+ 行）
├── data/                    # 运行时数据（memory / reminders / traces.db 等，gitignore）
├── scripts/                 # run.py / verify_features.py（513 行验证脚本）
├── tests/                   # 33 个测试文件，382 个用例（git tracked）
├── voice/                   # GPT-SoVITS 训练音频（gitignore，仅 .gitkeep）
└── GPT-SoVITS-v2pro-*/      # 整合包（gitignore，仅 .gitkeep）
```

---

## 文档导航

| 文档 | 看什么 |
|---|---|
| **[Live2D 在线 Demo](https://255856.github.io/Smart-Desktop-Pet/)** | 浏览器直接看 Hiyori 真实渲染（含 TTS/ASR/主动搭话/Trace） |
| [docs/quickstart.md](docs/quickstart.md) | 5 分钟极简版（跳过 README 的炫技部分） |
| [docs/architecture.md](docs/architecture.md) | 启动时序 + Live2D 渲染子图 + Agent 流程图 |
| [docs/config-reference.md](docs/config-reference.md) | `config.yaml` 全部字段（默认/范围/说明） |
| [docs/tool-catalog.md](docs/tool-catalog.md) | 32 个工具完整说明 + 如何加新工具 |
| [docs/tts-integration.md](docs/tts-integration.md) | edge / gptsovits / minimax 三引擎 + 自愈逻辑 |
| [docs/live2d-integration.md](docs/live2d-integration.md) | `.model3.json` + profile YAML + cubism-sdk 来源 + fallback 触发 |
| [docs/emotion-system.md](docs/emotion-system.md) | 11 个 Emotion 枚举 + 标签解析 + 关键词兜底 |
| [docs/dev-testing.md](docs/dev-testing.md) | pytest 跑法 + verify_features 解读 + CI 接入 |
| [docs/troubleshooting.md](docs/troubleshooting.md) | FAQ 展开 |
| [docs/demo/](docs/demo/README.md) | 纯静态 Demo 本地预览 + 部署到 GitHub Pages |
| [docs/资源下载说明.md](docs/资源下载说明.md) | voice / sprites / GPT-SoVITS / Live2D 模型 下载 |

---

## FAQ

- **桌宠启动后看不到？** 看右下角系统托盘，单击显隐，或右键菜单 → 显示桌宠
- **聊天窗闪退？** 看 `crash.log` 末尾错误；删 `data/chat_history.json` 重试
- **LLM 报错？** 检查 `config.yaml` 的 `llm.api_key` / `base_url`；可切 Ollama（`http://127.0.0.1:11434/v1`）
- **怎么玩五子棋 / 狼人杀？** 桌宠右键菜单 → "小游戏" → 进入；五子棋选难度获胜得金币，狼人杀由桌宠主持、其余 8 位玩家由 AI 扮演
- **切换 Live2D？** `config.yaml` → `pet.renderer: live2d` + `pet.live2d.model_dir`；不可用自动 fallback sprite
- **切换 TTS？** `config.yaml` → `character.tts_engine: edge | gptsovits | minimax`
- **GPT-SoVITS 启动失败？** 看 `data/tts_api.log`；确认 `voice/<角色>/` 有参考音频
- **想加自己的工具？** 参考 [docs/tool-catalog.md](docs/tool-catalog.md) 末尾"如何添加新工具"
- **想加自己的角色？** 复制 `characters/jingyuniang.yaml`，改 `system_prompt` / `tts_engine` / `live2d_model_dir`

---

## License

**MIT** — 代码部分。

版权资源（**Live2D 官方模型**、**GPT-SoVITS 整合包**、**参考音频**）受上游版权约束，**不随仓库分发**。仅在 `assets/live2d_profiles/`、`voice/`、`GPT-SoVITS-*/` 留 `.gitkeep` 骨架；纯静态 Demo 所用的官方样例模型（Hiyori / Miara）只部署在 **gh-pages 分支**。

---

## 贡献

PR 欢迎！请先读：
- [docs/dev-testing.md](docs/dev-testing.md) — 测试约定
- [docs/architecture.md](docs/architecture.md) — 模块边界
- `.gitignore` — **不能**提交的内容（版权资源 / 运行时数据 / 计划文档）

---

# 5-Min Quick Start

> **Goal**: clone → install → configure → launch → see the pet
> **Deps**: Python 3.10+, Windows/macOS/Linux, 4GB RAM

### 1. Clone

```bash
git clone https://github.com/255856/Smart-Desktop-Pet.git
cd Smart-Desktop-Pet
```

### 2. Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install PyQtWebEngine
```

### 3. Configure LLM

```bash
cp config.example.yaml config.yaml
# Edit config.yaml — set llm.api_key (or use Ollama locally)
```

### 4. Run

```bash
python main.py
# → pet appears in the system tray; right-click for chat/settings/games
```

### 5. (Optional) Agent Trace

```bash
python -m app.web.dashboard
# → http://127.0.0.1:8766
```

### ✅ Verify

```bash
python -m pytest tests/ -q                 # 382 passed, 6 skipped
python scripts/verify_features.py --no-gui # 93/94 OK
```

For details see [`docs/`](docs/) or try the **zero-install [Live2D demo](https://255856.github.io/Smart-Desktop-Pet/)**.
