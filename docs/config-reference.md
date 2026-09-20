# Config Reference · `config.yaml` 全字段

> 复制 `config.example.yaml` 为 `config.yaml` 后填 `llm.api_key` 即可启动。详细字段含义见下。

## `llm`（大模型 API）

| 字段 | 默认 | 说明 |
|------|------|------|
| `base_url` | `https://api.deepseek.com/v1` | OpenAI 兼容协议地址。Ollama 用 `http://127.0.0.1:11434/v1` |
| `api_key` | `PUT-YOUR-API-KEY-HERE` | Ollama 时填任意非空字符串 |
| `model` | `deepseek-chat` | 推荐中文：`deepseek-chat` / `MiniMax-M3` / `abab6.5s-chat` / `qwen3.5:4b` |
| `stream` | `true` | 流式输出（关闭会丢流式体验） |
| `temperature` | `0.8` | 0~1，越高越发散 |
| `max_tokens` | `1024` | 单次回复上限 |
| `timeout` | `60` | 秒，请求超时 |

## `character`（角色）

| 字段 | 默认 | 说明 |
|------|------|------|
| `name` | `小深` | 桌宠名字（影响 system prompt） |
| `persona` | 长字符串 | 人设，会拼到 system prompt |
| `tts_enabled` | `true` | 是否朗读 |
| `tts_voice` | `zh-CN-XiaoxiaoNeural` | edge-tts 中文女声 |
| `tts_engine` | `edge` | `edge` / `gptsovits` / `minimax` |
| `gptsovits_*` | — | GPT-SoVITS 引擎配置，见 [tts-integration.md](tts-integration.md) |
| `minimax_*` | — | MiniMax 引擎配置 |

## `window`（桌宠窗口）

| 字段 | 默认 | 说明 |
|------|------|------|
| `start_x` / `start_y` | `200` / `200` | 启动位置（左上角偏移） |
| `scale` | `0.4` | 缩放，0.4~0.8 推荐 |
| `always_on_top` | `true` | 桌宠置顶 |
| `show_in_taskbar` | `false` | 是否在任务栏显示（桌宠一般 false） |

## `reminder`（提醒）

| 字段 | 默认 | 说明 |
|------|------|------|
| `enabled` | `true` | 是否启用 |
| `data_file` | `reminders.json` | 持久化路径 |

## `app`（启动）

| 字段 | 默认 | 说明 |
|------|------|------|
| `open_chat_on_start` | `false` | 启动时自动开聊天窗 |
| `start_minimized` | `false` | 启动时最小化到托盘 |
| `log_level` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

## `sprite`（PNG 帧动画）

| 字段 | 默认 | 说明 |
|------|------|------|
| `directory` | `assets/sprites` | 帧动画目录（含 `Default/` `Sleep/` 等子目录） |
| `fallback` | `assets/sprites/body_front.png` | 找不到动画时的兜底图 |

## `asr`（语音输入）

| 字段 | 默认 | 说明 |
|------|------|------|
| `enabled` | `true` | 是否启用 ASR |
| `model_size` | `base` | `tiny` / `base` / `small` / `medium` / `large-v3` |
| `language` | `zh` | `zh` / `en` |

## `brain`（智能中枢）

| 字段 | 默认 | 说明 |
|------|------|------|
| `backend` | `lightweight` | `lightweight`（手写 ReAct，零依赖）/`standard`（LangChain） |
| `tools_enabled` | `true` | 允许模型调用工具 |
| `memory_file` | `data/memory.json` | 长期记忆落盘 |
| `proactive_enabled` | `true` | 空闲主动搭话 |
| `proactive_min_minutes` | `25` | 主动搭话随机间隔下限 |
| `proactive_max_minutes` | `45` | 主动搭话随机间隔上限 |
| `agent.*` | — | V2 Plan-Execute-Reflect 配置（生产未挂载，仅 `_legacy/` 测试用） |
| `langchain.*` | — | LangChain 后端参数（`backend=standard` 时生效） |

## `pet`（渲染器）

| 字段 | 默认 | 说明 |
|------|------|------|
| `renderer` | `sprite` | `sprite` / `live2d` |
| `live2d.model_dir` | `assets/live2d/bingtang/bingtang` | Live2D 模型根目录（含 `*.model3.json`） |
| `live2d.scale` | `0.5` | PIXI 模型显示缩放 |
| `live2d.random_expression` | `true` | 挂机随机表情 |
| `live2d.random_expression_min_s` / `max_s` | `25` / `70` | 随机间隔秒数 |
| `live2d.max_fps` | `30` | 渲染帧率上限 |

详细 Live2D 配置见 [live2d-integration.md](live2d-integration.md)。

## `data/` 运行时数据

| 文件 | 用途 | 何时创建 |
|------|------|----------|
| `data/memory.json` | 长期记忆（向量 + 重要性） | 首次 `remember_fact` |
| `data/traces.db` | Agent Trace（SQLite） | 首次 Agent 调用 |
| `data/chat_history.json` | 聊天历史（持久化） | 启动后首次聊天 |
| `data/dashboard.log` | Dashboard 子进程日志 | 启动过 Dashboard |
| `data/eval_report.md` | Eval 报告 | 跑 `verify_features.py` 后 |
| `data/feature_check.txt` | 功能验证明细 | 同上 |
| `~/.desktop-pet/save.json` | 宠物状态存档（等级/金币/好感） | 启动后自动 |

**备份整个项目** = `data/` + `settings.json` + `~/.desktop-pet/save.json`。