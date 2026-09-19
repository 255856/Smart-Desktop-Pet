# 🐳 桌面宠物 (Desktop Pet) — 多智能体桌宠

一个 **本地多智能体 (Multi-Agent) 桌面助理**，不只是聊天桌宠：

- **ReAct Plan-Execute-Reflect** 自主决策
- **本地 RAG 长期记忆**（向量检索 + 重要性衰减）
- **MCP (Model Context Protocol)** 工具生态，可外挂任意 server
- **Sub-agent 专家分工**（Life / Research / Code）+ 主调度
- **Agent Trace + FastAPI Dashboard** 全链路可观测
- **离线可跑的 Agent 评估**（Eval 套件）

参考 VPet Simulator 的动画系统设计，接入任何 **OpenAI 兼容协议** 的大模型 API（DeepSeek / MiniMax / OpenAI / 硅基流动 / 本地中转 / **Ollama 本地**）。

>  详细架构演进：[`ARCHITECTURE.md`](ARCHITECTURE.md)
>  功能清单验证：`python scripts/verify_features.py`

---

## ⚡ 快速验证（30 秒）

```powershell
cd E:\study\desktop-pet
python -m pytest tests/ -q                        # 单元测试 304 个，应全过
python scripts/verify_features.py                  # 功能验证 94 项，应全过
python main.py --with-dashboard --no-banner        # 启动 + 自动开 Dashboard
```

`verify_features.py` 输出类似：

```
总计：94 OK, 0 FAIL, 94 项
```

---

## 🛡️ 抗幻觉机制（v3.2+）

「主人说『帮我打开 QQ』但桌宠只回了文字、QQ 没真的打开」是国产 LLM 的常见坑。本项目有 **3 层防御纵深 + 真正的 ReAct 智能体**：

| 层级 | 位置 | 作用 |
|------|------|------|
| **第 1 层 · 意图驱动 force_tool_use** | `agent.py` + `llm_client.py` | 检测到「打开XX/提醒XX/记住XX/查询」类意图时，第一轮请求自动带 `tool_choice="required"`，强制模型必须调工具；服务端不支持时降级为 user-prompt 强制 |
| **第 2 层 · 强制重试** | `agent.py` | 第一轮 force 后模型仍只回文字 → 注入强提示重试一次（force_retry meta 事件），给模型第二次机会 |
| **第 3 层 · 强制 final 阶段** | `agent.py` | 连续多轮纯调工具无文字 → 去掉 tools 强制模型给出 final answer（避免「调工具上瘾」） |

**真正的智能体**：lightweight backend 直接用 `AgentLoop`（手写 ReAct 循环）—— 模型自主决定调什么工具、调几次、什么时候给 final answer。这与 LangChain `create_agent` 语义一致，零依赖。

**两种后端**：
- `backend: lightweight`（默认）—— 手写 ReAct，零依赖，3 层抗幻觉
- `backend: standard` —— LangChain 1.0+ `create_agent` + `langgraph-checkpoint-sqlite`（生产 / 生态对接）

**抗推理污染**：推理模型（MiniMax-M3 / DeepSeek-R1 / Qwen3.5 等）的 SSE 流会在 `content` 字段之前先输出 `reasoning_content` 思考痕迹。LLMClient 严格区分：
- `delta.content` → yield 为 `text` 事件（UI 显示）
- `delta.reasoning_content` / `delta.reasoning` → yield 为 `meta{"event":"reasoning_delta"}`（仅 Trace 记录，UI 不显示）
- 若 content 内含 `<think>...</think>` 标签，sanitize_text 会剥掉

详见：`app/brain/agent.py`（真正的 ReAct + 3 层抗幻觉）、
`app/brain/llm_client.py`（force_tool_use + 推理分离）、
`app/brain/langchain_agent.py`（LangChain 后端）、
`tests/test_react_loop.py` + `tests/test_anti_hallucination*.py` + `tests/test_reasoning_separation.py`（回归测试）。

---

## 数据存储位置

| 文件 | 位置 | 用途 | 何时创建 |
|------|------|------|---------|
| `data/memory.json` | 项目目录 | 长期记忆（重要性 + 向量） | 首次 `remember_fact` 时 |
| `data/traces.db` | 项目目录 | Agent Trace（SQLite） | 首次 Agent 调用时 |
| `data/chat_history.json` | 项目目录 | 聊天历史（持久化） | 启动后首次聊天 |
| `data/foods.json` | 项目目录 | 食物库 | 项目自带 |
| `data/works.json` | 项目目录 | 打工系统 | 项目自带 |
| `data/app_registry.json` | 项目目录 | 应用启动映射 | 项目自带 |
| `data/reminders.json` | 项目目录 | 提醒列表 | **首次启动自动建空文件** |
| `data/dashboard.log` | 项目目录 | Dashboard 子进程日志 | 启动过 Dashboard 后 |
| `data/eval_report.md` | 项目目录 | Eval 报告 | 跑 `verify_features.py` 后 |
| `data/feature_check.txt` | 项目目录 | 功能验证明细 | 跑 `verify_features.py` 后 |
| `settings.json` | 项目目录 | 窗口位置/缩放/不透明度 | 启动后自动保存 |
| **`~/.desktop-pet/save.json`** | **用户目录** | **宠物状态存档**（等级/金币/好感） | 启动后自动保存 |
| **`~/.desktop-pet/save.bak.json`** | **用户目录** | 上一份存档备份（崩溃回退用） | 每次保存时备份 |

**备份整个项目** = 复制 `data/` + `settings.json` + `~/.desktop-pet/save.json` 三个地方。

---

## 核心能力

| 模块 | 能力 |
|------|------|
| **桌宠本体** | 透明无边框、置顶、可拖动、idle bob 微动效 |
| **帧动画系统** | VPet 同款：`Frame(duration_ms)` + 多循环变体 + LOOP/ONCE/PINGPONG |
| **表情切换** | 8 种情绪 × 多种动画变体，按 LLM 回复自动切 |
| **触摸热区** | 头部/身体/拖动区分别触发不同反应 |
| **移动系统** | 桌宠自动沿屏幕走动/爬行/边缘隐藏 |
| **LLM 聊天** | 流式输出，自动朗读（TTS），多行输入 + `/` 命令补全 |
| **情绪驱动** | 模型回复末尾 `[happy]` 等标签驱动表情 |
| **智能提醒** | 「30 分钟后提醒我喝水」自动识别 + 后台轮询 |
| **语音合成** | edge-tts 免费，无需 API Key |
| **语音输入（ASR）** | faster-whisper，按住说话自动识别 |
| **系统托盘** | 右键菜单：显示/隐藏/打开聊天/设置/调试面板/查看记忆/退出 |

### 智能体方向（v3.0 新增）

| 模块 | 能力 | 文件 |
|------|------|------|
| **ReAct Planner** | 把用户目标拆解为 JSON 步骤（含 thought / kind / arguments） | `app/brain/planner.py` |
| **Plan Executor** | **并行执行** Plan 步骤（`parallel_group` 同组 gather）+ 重试 | `app/brain/executor.py` |
| **Reflector** | 启发式 / LLM 双重评估每步结果，决定 ok / retry / replan | `app/brain/reflector.py` |
| **向量长期记忆** | TF-IDF（默认）/ sentence-transformers（可选）/ 子串（兜底），重要性评分 + 时间衰减 + 冲突检测 | `app/brain/memory.py` |
| **MCP 客户端** | stdio JSON-RPC，自动桥接 MCP tools 到 OpenAI Schema | `app/mcp/protocol.py` |
| **MCP Filesystem Server** | 沙箱文件访问（限制根目录）示例实现 | `app/mcp/filesystem_server.py` |
| **Sub-agent 框架** | `BaseAgent` + `LifeAgent` / `ResearchAgent` / `CodeAgent` + `Orchestrator` 分派 | `app/agents/base.py` |
| **Agent Trace** | 每次运行完整 trace 落 SQLite | `app/brain/trace.py` |
| **FastAPI Dashboard** | run 列表 / run 详情 / stats / memory 可视化（端口 8765） | `app/web/dashboard.py` |
| **Evaluator** | Mock LLM 驱动的 Eval 套件 + Markdown 报告 | `app/eval/cases.py` |

---

## 快速开始

### 1. 安装依赖（已完成，可跳过）

```powershell
cd E:\study\desktop-pet
# 依赖已装好在 .local-packages/
```

### 2. 下载桌宠动画文件
```
链接：
```

### 3. 启动桌宠

```powershell
python main.py
```

启动横幅会显示：
- 配置加载状态（角色名、模型）
- **Ollama 本地模型探测结果**（如果 11434 端口在跑）
- 数据文件落盘路径

### 切换 Live2D 模型（可选，v3.1+）

默认用 PNG 帧动画（sprite）。如果你装了 Live2D 模型（如 Cubism 4 `.model3.json`），可以在 `config.yaml` 切换：

```yaml
pet:
  renderer: live2d              # sprite | live2d
  live2d:
    model_dir: "E:/your/path/to/live2d_model"
    scale: 0.5
```

**依赖**：Live2D 模式需要在系统 Python 装 `PyQtWebEngine`：

```powershell
pip install PyQtWebEngine
```

装好后 `pip install` 后重启桌宠就生效。如果没装或模型目录缺 `model3.json`，**自动 fallback 到 sprite 渲染**（不会崩）。
- 自动启动 Dashboard（如果 `--with-dashboard`）

### 3. 启动 Dashboard（3 种方式）

```powershell
# 方式 A：命令行启动时自动开
python main.py --with-dashboard

# 方式 B：托盘菜单 → 📊 调试面板 (Dashboard)
# 方式 C：聊天窗标题栏 → 📊 按钮，或输入 /调试

# 手动启动（独立进程）
python -m app.web.dashboard --port 8765
# 浏览器打开 http://127.0.0.1:8765
```

### 4. 跑功能验证 + Eval

```powershell
python scripts/verify_features.py    # 70 项功能检查 + 跑 ReAct Eval
# 报告：data/feature_check.txt
# Eval：data/eval_report.md
```

### 5. 跑单元测试

```powershell
python -m pytest tests/ -q            # 125 个测试
```

---

## 配置 `config.yaml`

```yaml
llm:
  base_url: "https://api.deepseek.com/v1"   # 或 http://127.0.0.1:11434/v1（Ollama）
  api_key: "sk-..."                          # Ollama 不用填，但需要 model
  model: "deepseek-chat"                     # 或 qwen3.5:4b
  stream: true
  temperature: 0.8
  max_tokens: 1024

character:
  name: "鲸鱼娘"
  persona: "..."                    # 影响 LLM 风格
  tts_enabled: true
  tts_voice: "zh-CN-XiaoxiaoNeural"

window:
  scale: 0.6                        # 桌宠缩放（0.4~0.8）

brain:                              # v3.0 智能体配置
  tools_enabled: true
  memory_file: "data/memory.json"
  proactive_enabled: true
  proactive_min_minutes: 25
  proactive_max_minutes: 45
```

### 用 Ollama 本地模型

```yaml
llm:
  base_url: "http://127.0.0.1:11434/v1"
  api_key: "ollama"                          # 任意非空字符串
  model: "qwen3.5:4b"                        # 本地模型
```

启动时 main.py 会自动探测 `http://127.0.0.1:11434/api/tags` 并列出本地模型。

---

## 文件结构

```
desktop-pet/
├── main.py                 # 启动入口（含启动横幅 + Ollama 检测 + Dashboard）
├── config.yaml             # 配置
├── ARCHITECTURE.md         # 架构演进文档
├── README.md               # 本文件
├── requirements.txt
├── scripts/
│   └── verify_features.py  # 70 项功能验证脚本（推荐首次跑一下）
├── app/
│   ├── main.py                    # App 装配（含启动横幅 + Dashboard 子进程）
│   ├── core/                      # 基础设施
│   │   ├── config.py
│   │   ├── qt_compat.py
│   │   ├── i18n.py
│   │   ├── save.py                # ~/.desktop-pet/save.json 存档
│   │   ├── tray.py                # 系统托盘（含 Dashboard / 记忆入口）
│   │   ├── app_registry.py
│   │   └── settings_store.py
│   ├── ui/                        # UI 层
│   │   ├── pet_window.py          # 桌宠本体
│   │   ├── chat_window.py         # 聊天窗（多行输入 + 命令补全）
│   │   ├── settings_window.py
│   │   ├── ui_controller.py       # 信号连接
│   │   └── ui_style.py
│   ├── animation/                 # 动画系统
│   │   ├── animations.py          # Frame / Animation (LOOP/ONCE/PINGPONG)
│   │   ├── sprite_atlas.py
│   │   └── motion.py
│   ├── voice/                     # 语音
│   │   ├── voice.py               # edge-tts
│   │   ├── asr.py                 # faster-whisper
│   │   ├── character.py
│   │   └── characters.py
│   ├── engine/                    # 引擎
│   │   ├── state.py               # 数值衰减 + 被动回复
│   │   ├── state_manager.py       # 存档 + 提醒
│   │   ├── reminder.py            # 提醒（按需调度 + 自动建空文件）
│   │   ├── tools.py               # 30+ 内置工具
│   │   ├── chat_store.py
│   │   ├── works.py
│   │   └── screenshot.py
│   ├── brain/                     # 智能中枢 v3.0
│   │   ├── llm_client.py
│   │   ├── agent.py               # 旧版 Agent Loop（兼容）
│   │   ├── agent_v2.py            # 新 Agent Loop（react / single 切换）
│   │   ├── plan.py                # Plan / Step（带 parallel_group）
│   │   ├── planner.py
│   │   ├── executor.py            # 包含 gather 并行
│   │   ├── reflector.py
│   │   ├── memory.py              # 向量记忆（TF-IDF / sbert / substring）
│   │   ├── memory_evolution.py    # Memory Curator（兼容旧名）
│   │   ├── proactive.py
│   │   ├── brain_controller.py
│   │   └── trace.py               # SQLite Trace
│   ├── mcp/                       # MCP 协议
│   │   ├── protocol.py            # stdio JSON-RPC
│   │   └── filesystem_server.py
│   ├── agents/                    # Sub-agent 框架
│   │   └── base.py
│   ├── web/                       # FastAPI Dashboard
│   │   └── dashboard.py
│   └── eval/                      # Agent 评估
│       └── cases.py
├── assets/
│   └── sprites/                   # 静态兜底素材
├── tests/                         # 125 个测试
└── data/                          # 运行时数据
    ├── memory.json                # 长期记忆
    ├── traces.db                  # Agent Trace
    ├── chat_history.json          # 聊天历史
    ├── reminders.json             # 提醒
    ├── eval_report.md             # Eval 报告
    └── feature_check.txt          # 功能验证明细
```

---

## 测试

```powershell
# 全部单元测试（125 个）
python -m pytest tests/ -v

# 功能验证（70 项 + ReAct Eval）
python scripts/verify_features.py

# 单独跑某类测试
python -m pytest tests/test_planner.py -v   # Planner / Reflector / Executor
python -m pytest tests/test_mcp.py -v      # MCP
python -m pytest tests/test_agents.py -v   # Sub-agent
```

---

## 实测数据示例

跑 `scripts/verify_features.py` 后生成：

`data/eval_report.md`：
```
# Agent 评估报告
- 用例总数: 4
- 通过: 3
- 成功率: 75%
- 平均耗时: 0 ms

## 用例结果
| 用例 | 状态 | 工具调用 | 备注 |
| add_reminder_basic | ✅ | add_reminder({"text":"喝水","delay_minutes":25}) | |
| remember_fact | ✅ | remember_fact(...) | |
| chat_no_tool | ✅ | — | |
| calculate | ❌ | — | 未调用期望工具 calculate |
```

`data/feature_check.txt`：70 行 `[OK]`/`[FAIL]` 明细。

---

## FAQ

**桌宠启动后看不到？**
看右下角系统托盘有没有「鲸鱼娘」图标，单击显隐。

**聊天窗打不开 / 闪退？**
1. 看 `crash.log` 末尾的错误
2. 跑 `python scripts/verify_features.py` 看是哪步异常
3. 删除 `data/chat_history.json` 重试

**聊天报 `LLMError`？**
检查 `config.yaml` 里 `llm.api_key` 是否正确，或切到其他 OpenAI 兼容服务商。

**启用 sentence-transformers 本地嵌入？**
```powershell
pip install sentence-transformers
# 然后 config.yaml 里（需新增字段或改默认 backend）
```

**切换到 LangChain 标准后端？**
```powershell
pip install langchain langchain-openai langgraph langgraph-checkpoint-sqlite
```
然后 `config.yaml`：
```yaml
brain:
  backend: standard   # 默认 lightweight（手写 ReAct 零依赖）；standard = LangChain 1.0+
```
两种后端共用同一份 UI 和工具集，事件协议完全兼容。可在 `langchain:` 段配置 SqliteSaver 持久化、最大步数等。

**启动 Dashboard？**
- 命令行：`python main.py --with-dashboard`
- 托盘菜单：📊 调试面板 (Dashboard)
- 聊天窗：标题栏 📊 按钮 或 `/调试` 命令

**数据存在哪？** 见上方「📂 数据存储位置」表格。

**Ollama 本地模型？**
`config.yaml` 里 `base_url: "http://127.0.0.1:11434/v1"`，`model: "qwen3.5:4b"`。

---

## v3.0 升级亮点

1. **多智能体架构**：Planner / Executor / Reflector 三层 ReAct，含并行执行（`parallel_group` 同组 gather）；配合 Life / Research / Code 三个 Sub-agent + 主 Orchestrator
2. **本地 RAG 长期记忆**：TF-IDF / sentence-transformers 可插拔后端，含重要性评分 / 时间衰减 / 冲突检测
3. **MCP 协议实现**：完整 stdio JSON-RPC 客户端 + 沙箱 filesystem server，可对接任何 MCP 兼容工具
4. **Agent 可观测性**：每次运行落 SQLite trace，FastAPI Dashboard 提供实时回放 + 统计
5. **离线 Agent 评估**：Mock LLM 驱动的 Eval 套件 + 70 项功能验证脚本（CI 友好）
6. **工程化**：模块化 9 层架构、125 单元测试、Reminders/memory/traces 等数据文件可视化
