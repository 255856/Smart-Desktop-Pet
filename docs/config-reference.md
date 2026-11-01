# config.yaml 全字段

复制 `config.example.yaml` 为 `config.yaml` 后填 `llm.api_key` 即可启动。详细字段含义见下。

## llm（大模型 API）

| 字段 | 默认 | 说明 |
|------|------|------|
| `base_url` | `https://api.deepseek.com/v1` | OpenAI 兼容协议地址。Ollama 用 `http://127.0.0.1:11434/v1` |
| `api_key` | `PUT-YOUR-API-KEY-HERE` | Ollama 时填任意非空字符串 |
| `model` | `deepseek-chat` | 推荐中文：`deepseek-chat` / `MiniMax-M3` / `abab6.5s-chat` / `qwen3.5:4b` |
| `stream` | `true` | 流式输出（关闭会丢流式体验） |
| `temperature` | `0.8` | 0~1，越高越发散 |
| `max_tokens` | `1024` | 单次回复上限 |
| `timeout` | `60` | 秒，请求超时 |
| `reasoning_effort` | —（不传） | 仅 OpenAI o1/o3 等推理模型支持：`low` / `medium` / `high` |
| `project_dirs` | `[]` | 沙箱工具额外放行的项目目录白名单（默认只给用户家目录） |

## character（角色）

| 字段 | 默认 | 说明 |
|------|------|------|
| `name` | `小汐` | 桌宠名字（影响 system prompt） |
| `persona` | 长字符串 | 人设，会拼到 system prompt |
| `character_file` | `characters/xiaoxi.yaml` | 角色文件路径（相对项目根）；角色的 name / persona 从这里读 |
| `tts_enabled` | `true` | 是否朗读 |
| `tts_voice` | `zh-CN-XiaoxiaoNeural` | edge-tts 中文女声 |
| `tts_engine` | `edge` | `edge` / `gptsovits` / `minimax` |
| `gptsovits_*` | — | GPT-SoVITS 引擎配置，见 [tts-integration.md](tts-integration.md) |
| `minimax_*` | — | MiniMax 引擎配置 |
| `voices` | `[]` | 可切换音色列表（`name` / `ref_audio` / `prompt_text` / `edge_voice`） |
| `current_voice` | —（空 = `voices[0]`） | 当前默认音色 |

## window（桌宠窗口）

| 字段 | 默认 | 说明 |
|------|------|------|
| `start_x` / `start_y` | `200` / `200` | 启动位置（左上角偏移） |
| `scale` | `0.6` | 缩放，0.4~0.8 推荐 |
| `always_on_top` | `true` | 桌宠置顶 |
| `show_in_taskbar` | `false` | 是否在任务栏显示 |

## reminder（提醒）

| 字段 | 默认 | 说明 |
|------|------|------|
| `enabled` | `true` | 是否启用 |
| `data_file` | `reminders.json` | 持久化路径 |

## app（启动）

| 字段 | 默认 | 说明 |
|------|------|------|
| `open_chat_on_start` | `false` | 启动时自动开聊天窗 |
| `start_minimized` | `false` | 启动时最小化到托盘 |
| `log_level` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |

## sprite（PNG 帧动画）

| 字段 | 默认 | 说明 |
|------|------|------|
| `directory` | `assets/sprites` | 帧动画目录 |
| `fallback` | `assets/sprites/body_front.png` | 找不到动画时的兜底图 |

## asr（语音输入）

| 字段 | 默认 | 说明 |
|------|------|------|
| `enabled` | `false` | 是否启用 ASR（默认关闭：启动慢、占内存、识别卡主线程） |
| `model_size` | `base` | `tiny` / `base` / `small` / `medium` / `large-v3` |
| `language` | `zh` | `zh` / `en` |

## brain（智能中枢）

| 字段 | 默认 | 说明 |
|------|------|------|
| `backend` | `lightweight` | `lightweight`（手写 ReAct，零依赖）/ `standard`（LangChain，带创新点 2 决策耦合） |
| `tools_enabled` | `true` | 允许模型调用工具 |
| `memory_file` | `data/memory.json` | **已弃用**：长期记忆走后端 sidecar 进程（`app/brain/memory_client.py`，默认 `http://127.0.0.1:8788`，卡片落 `data/memory_sidecar/cards.json`），这个字段现在没人读 |
| `proactive_enabled` | `true` | 空闲主动搭话 |
| `proactive_min_minutes` | `25` | 主动搭话随机间隔下限 |
| `proactive_max_minutes` | `45` | 主动搭话随机间隔上限 |
| `tavily_api_key` | — | `web_search` 的 Tavily key（留空降级 DuckDuckGo） |
| `agent.*` | — | V2 Plan-Execute-Reflect 配置（`mode` / `max_turns` / `max_replans` / `reflector_mode`；当前生产代码不读这些字段） |
| `langchain.*` | — | LangChain 后端参数（`backend=standard` 时生效） |

`backend=standard` 才走创新点 2 全套机制（决策查表 / 状态注入 / post-LLM 拦截器）。`backend=lightweight` 是手写 ReAct，无中间件，无状态耦合。

## pet（渲染器）

| 字段 | 默认 | 说明 |
|------|------|------|
| `renderer` | `sprite` | `sprite` / `live2d` |
| `live2d.model_dir` | `""`（代码默认空；示例配置写 `assets/live2d/bingtang/bingtang`） | Live2D 模型根目录 |
| `live2d.hide_watermark` | `true` | 是否隐藏模型自带水印（公开分发建议保持 `false`） |
| `live2d.random_expression` | `true` | 挂机随机表情 |
| `live2d.random_expression_min_s` / `max_s` | `25` / `70` | 随机间隔秒数 |
| `live2d.max_fps` | `24` | 渲染帧率上限 |
| `live2d.idle_fps` | `12` | 静置（没人看）时的降帧上限 |

详细 Live2D 配置见 [live2d-integration.md](live2d-integration.md)。

## companion（创新点 2 决策表）

不在 `config.yaml` 里，走独立 yaml `app/core/companion_config.yaml`：

| 段 | 作用 |
|---|---|
| `satiety` / `mood` / `level` / `favor` | 4 维状态 (satiety/mood/favor/level) 的 minimum/maximum/initial |
| `quantize` | 三档离散化阈值（LOW/MID/HIGH 分界 + hysteresis 防抖） |
| `leveling` | 升级公式 `base * level^exp` |
| `time_decay` | 时间驱动衰减（每小时减饱食度等） |
| `event_hooks` | per-run outcome→state 钩子权重（success/failure/rejected/accepted 各自的 mood/satiety/favor/exp 增量） |
| `favor_rules` | 好感维度额外规则（好感 ≥80 / <30 时对 confirm 的影响） |
| `persistence` | 数值持久化（落 `data/pet_state.json`，写入防抖 5s） |
| `trace` | 决策 trace 日志（落 `data/companion_log.txt`） |
| `tool_unlock` | 战令式工具解锁（Lv1 默认解锁 ~35 个基础工具，MCP 工具默认锁到 Lv8） |
| `tool_allowlist` | 执行期白名单（`exact` 精确名 + `prefixes` 前缀桶，覆盖全部 75 个工具） |
| `cost_budget` | 每轮 token 预算与告警节流（`input_tokens_per_turn` / 告警最短间隔） |
| `context_compaction` | 上下文压缩（用量到 80% 触发摘要，保留最近 16% 原文 + 最新 8 条） |
| `voice` | 语音行为（`auto_read` 是否自动朗读 / `mcp_enabled` 是否注册 voice MCP server / 产物目录 / TTS 地址） |
| `reply_pacing` | 逐句出气泡的间隔（`min_gap_ms` ~ `max_gap_ms`） |
| `output_budget` | 输出档位预算（`short` / `medium` / `long`，下限 `floor`） |

改数值公式 / 阈值 / 钩子权重全在 yaml 里，不用动代码。详见 [innovation-2-companion-policy.md](innovation-2-companion-policy.md)。

## MCP servers（mcp_servers.yaml）

仓库根的 `mcp_servers.yaml` 控制外部 MCP server 启停：

| name | 默认 | 说明 |
|------|------|------|
| `fs` | enabled | stdio JSON-RPC，`-m app.mcp.filesystem_server`（文件沙箱，3 个工具） |
| `voice` | enabled | 本机语音（念一段话 / 按旋律唱 / 查本机音色），`-m app.mcp.voice_server`（3 个工具） |
| `aw` | disabled（示例默认关） | ActivityWatch 活动感知，`-m app.mcp.activitywatch_server`（4 个工具），详见 [activitywatch-integration.md](activitywatch-integration.md) |

要扩到第四台 server 在 `mcp_servers.yaml` 加一段 `- name: ...` 即可。

## data/ 运行时数据

| 文件 | 用途 | 何时创建 |
|------|------|----------|
| `data/memory_sidecar/cards.json` | 长期记忆卡片（sidecar 唯一真值；同目录 `chroma/` 是派生索引） | 首次 `remember_fact` |
| `data/traces.db` | Agent Trace（SQLite） | 首次 Agent 调用 |
| `data/chat_history.db` | 聊天历史（SQLite） | 启动后首次聊天 |
| `data/companion_log.txt` | 创新点 2 决策 JSON-line 落盘 | 首次 `lookup_decision` |
| `data/dashboard.log` | Dashboard 子进程日志 | 启动过 Dashboard |
| `data/eval_report.md` | Eval 报告 | 跑 `verify_features.py` 后 |
| `data/feature_check.txt` | 功能验证明细 | 同上 |
| `data/pet_state.json` | 桌宠状态存档（等级/金币/好感/心情/饱食） | 启动后自动 |
| `data/live2d_scenes/<model>.json` | Live2D 18 场景配置覆盖 | 自定义后 |

**备份整个项目** = `data/` + `settings.json` + `mcp_servers.yaml` + `app/core/companion_config.yaml`。
