# Architecture · 顶层架构

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
```

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

**关键点**：

- 内置 `ThreadingHTTPServer`（随机端口）绕过 Chromium CORS，让 PIXI 能直接 `fetch('model3.json')`
- HTTP handler 回传 `model3.json` 时**动态注入** `Motions`（idle / Zzz）与 `Expressions` 段 —— 原模型文件不动，桥端自动循环待机动作
- `*.model.yaml`（`assets/live2d_profiles/`）按五段声明每个模型的能力：emotions / actions / sleep / watermark / random_expression

## Agent 调用流程

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
    G -->|幻觉（无工具步骤）| I[force_retry]
    I --> E
    H --> J[final answer]
    D --> J
    J --> K[sanitize_text<br/>剥离推理/英文段]
    K --> L[流式 + TTS 同步]
```

**抗幻觉 3 层**：
1. **意图驱动 force_tool_use**：检测「打开XX/提醒XX/记住XX/查询」类意图，第一轮请求自动带 `tool_choice="required"`
2. **强制重试 force_retry**：模型仍只回文字 → 注入强提示重试一次
3. **强制 final 阶段**：连续多轮纯调工具无文字 → 去掉 tools 强制模型给出 final answer

详见 [dev-testing.md](dev-testing.md) 的 `tests/test_anti_hallucination*.py` 回归测试。

## 文件分层

```
┌─────────────────────────────────────────────────┐
│  UI: app/ui/{pet_window, chat_window, settings_window, ui_controller} │
├─────────────────────────────────────────────────┤
│  Animation: app/animation/{sprite_renderer, live2d_renderer, factory} │
├─────────────────────────────────────────────────┤
│  Brain: app/brain/{agent, llm_client, memory, proactive, langchain_agent} │
├─────────────────────────────────────────────────┤
│  Voice: app/voice/{voice, gptsovits_tts, minimax_tts, asr, character} │
├─────────────────────────────────────────────────┤
│  Engine: app/engine/{state, tools/*, chat_store, reminder, dashboard} │
├─────────────────────────────────────────────────┤
│  Core: app/core/{config, settings_store, save, tray, app_registry} │
├─────────────────────────────────────────────────┤
│  Infra: MCP, Web Dashboard, Eval, Agents (sub-agents) │
└─────────────────────────────────────────────────┘
```