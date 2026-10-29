# 顶层架构

## 启动时序

```mermaid
sequenceDiagram
    participant U as User
    participant M as main.py (shell)
    participant A as app/main.py
    participant P as PetWindow
    participant B as BrainController
    participant T as TTS
    participant L as LLM
    participant AW as ActivityWatch

    U->>M: python main.py
    M->>M: sys.path.insert(.local-packages)
    M->>A: from app.main import main()
    A->>A: load config.yaml
    A->>A: Ollama 探测 (11434)
    A->>T: start_tts_api_subprocess (if gptsovits)
    A->>P: create_renderer (sprite/live2d)
    A->>B: BrainController(root, cfg, state, reminders)
    A->>A: UIController 装配
    A->>U: 桌宠窗口显示 + 托盘图标
    U->>P: 双击 / 聊天
    P->>B: 发送消息
    B->>L: chat_stream(messages)
    L-->>B: text_delta / tool_call / done
    B->>T: 合成朗读 + 口型同步
    B-->>P: 流式回流（气泡 + 表情）
    Note over B,AW: mcp_aw_* 工具调 aw-server (5600) 拉活动窗口
```

`backend=standard` 时 B 内部多走一条 LangChain 链路：`_ensure_agent` 构造带 4 个 middleware 的 graph，模型响应经 `PostLLMStateGuard` 拦截改写，工具调用经 `RiskyToolConfirm` 查表。

## Live2D 渲染子图

```mermaid
graph LR
    subgraph "PetWindow (QWebEngineView)"
        HTML[live2d_bridge.html]
        PIXI[pixi-live2d-display]
        CUBISM[cubism-sdk/live2dcubismcore.min.js]
    end
    subgraph "Live2DRenderer (Python)"
        HTTP[ThreadingHTTPServer: 随机端口]
        MODEL[model3.json 动态注入]
        BRIDGE[QWebChannel]
    end
    PIXI --> CUBISM
    HTML --> PIXI
    HTML -- "QWebChannel.messageReceived" --> HTTP
    HTTP -- "set_expression / play_motion / focus(x,y)" --> MODEL
    BRIDGE -- "bridge.on('motion_finished')" --> MODEL
```

内置 `ThreadingHTTPServer`（随机端口）绕过 Chromium CORS，让 PIXI 能直接 `fetch('model3.json')`。HTTP handler 回传 `model3.json` 时动态注入 `Motions`（idle / Zzz）与 `Expressions` 段，原模型文件不动，桥端自动循环待机动作。

`*.model.yaml`（`assets/live2d_profiles/`）按五段声明每个模型的能力：emotions / actions / sleep / watermark / random_expression。

## Agent 调用流程

`backend=lightweight`（默认，手写 ReAct）：

```mermaid
flowchart TD
    A[用户输入] --> B[检测意图 force_tool_use]
    B -->|需要工具| C[tool_choice=required]
    B -->|纯聊天| D[stream chat]
    C --> E[模型产出 tool_call]
    E --> F[ToolRegistry 执行]
    F --> G{Reflector<br/>启发式评估}
    G -->|ok| H[继续/出 final]
    G -->|retry| F
    G -->|幻觉| I[force_retry]
    I --> E
    H --> J[final answer]
    D --> J
    J --> K[sanitize_text<br/>剥离推理/英文段]
    K --> L[流式 + TTS 同步]
```

抗幻觉 3 层：

1. 意图驱动 `force_tool_use`：检测「打开XX/提醒XX/记住XX/查询」类意图，第一轮请求自动带 `tool_choice="required"`
2. 强制重试 `force_retry`：模型仍只回文字 → 注入强提示重试一次
3. 强制 final 阶段：连续多轮纯调工具无文字 → 去掉 tools 强制模型给出 final answer

`backend=standard`（LangChain）走 `langchain_agent.py`：

```mermaid
flowchart TD
    A[用户输入] --> B[LangChainAgent._ensure_agent]
    B --> C[middleware 链: orphan_pruner + dynamic_prompt + RiskyToolConfirm + PostLLMStateGuard]
    C --> D[create_agent → LLM]
    D --> E[ModelResponse]
    E --> F[PostLLMStateGuard.run<br/>state 差 + preemptive → 改写]
    F --> G[AIMessage 流到 UI]
    G --> H{用户操作}
    H -->|工具调用| I[RiskyToolConfirm 中间件<br/>查表 lookup_decision]
    I -->|ALLOW| J[handler 放行]
    I -->|REJECT| K[返回 rejected ToolMessage]
    I -->|CONFIRM| L[弹窗 + 等主人]
    J --> M[ToolMessage 回流]
    K --> M
    L --> M
    M --> N[任务结束 _finish_run_with_outcome]
    N --> O[apply_outcome → PetState.feeling/food/favor/exp]
```

Post-LLM 拦截器（`PostLLMStateGuard`）跟 prompt-time 的 `grounding_dynamic_prompt` 互为冗余。详见 [innovation-2-companion-policy.md](innovation-2-companion-policy.md) 第 4 节。

## 决策耦合（创新点 2）

```mermaid
flowchart LR
    A[PetState] -->|quantize| B[mood/sat/favor/level<br/>LOW/MID/HIGH]
    B --> C[lookup_decision<br/>RULES_TABLE 40 + 13]
    C --> D{决策}
    D -->|ALLOW| E[工具直接执行]
    D -->|CONFIRM| F[弹窗]
    D -->|REJECT| G[拒绝 ToolMessage]
    D -->|REFUSE| H[任务级拒绝]
    E --> I[apply_outcome]
    F --> I
    G --> I
    H --> I
    I -->|mood/sat/favor/exp Δ| A
    C -.->|trace_decision JSON-line| J[data/companion_log.txt]
```

`S` 量化 → 查表 → 决策 → 工具执行 → outcome 反馈写回 `S`，闭环。决策每条 JSON-line 落 `data/companion_log.txt` 供 Dashboard `/api/companion` 读。

## 活动感知（ActivityWatch）

```mermaid
sequenceDiagram
    participant U as User
    participant P as Pet (LLM)
    participant M as mcp__aw__* 工具
    participant S as aw_server (5600)
    participant W as aw_watcher

    U->>P: 过去 5 分钟在做什么？
    P->>M: mcp_aw_get_recent_activities
    M->>S: HTTP GET /api/0/buckets
    S-->>M: buckets 列表
    M->>S: HTTP GET /api/0/.../events
    S-->>M: window 事件
    M-->>P: 字符串结果
    P-->>U: 主人过去 5 分钟在 VSCode

    Note over W,S: aw_watcher_window + aw_watcher_afk + aw_watcher_web 后台持续写 bucket
```

`mcp_aw_*` 4 个工具：`get_recent_activities` / `current_window` / `query` / `buckets`。3 种错误信息（server 没跑 / 没装 watcher / watcher 没在记）让 LLM 能告诉主人怎么修。详见 [activitywatch-integration.md](activitywatch-integration.md)。

## 文件分层

```
┌──────────────────────────────────────────────────────────┐
│  UI: app/ui/{pet_window, chat_window, ui_controller} │
├──────────────────────────────────────────────────────────┤
│  Animation: app/animation/{sprite_renderer, live2d_renderer, factory} │
├──────────────────────────────────────────────────────────┤
│  Brain: app/brain/{agent, llm_client, memory, proactive, langchain_agent, companion_policy, _companion_registry, _companion_log, hallucination_guard} │
├──────────────────────────────────────────────────────────┤
│  Voice: app/voice/{voice, gptsovits_tts, minimax_tts, asr, character} │
├──────────────────────────────────────────────────────────┤
│  Engine: app/engine/{state, tools/*, chat_store, reminder, dashboard} │
├──────────────────────────────────────────────────────────┤
│  Core: app/core/{config, settings_store, save, tray, app_registry, companion_config.yaml} │
├──────────────────────────────────────────────────────────┤
│  MCP: app/mcp/{filesystem_server, activitywatch_server, protocol} │
├──────────────────────────────────────────────────────────┤
│  Infra: Web Dashboard, Eval, Agents (sub-agents) │
└──────────────────────────────────────────────────────────┘
```

`app/brain/` 跟 v21 比新增了 4 个文件（`companion_policy` / `_companion_registry` / `_companion_log` / `hallucination_guard.PostLLMStateGuard`）。`app/core/companion_config.yaml` 独立承载创新点 2 数值公式 + 阈值 + 钩子。
