# 桌面宠物 · Smart Desktop Pet

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)](https://www.python.org)
[![PyQt5](https://img.shields.io/badge/UI-PyQt5%2BWebEngine-41CD52?logo=qt&logoColor=white)](https://riverbankcomputing.com)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-2393%20passed%20%2F%2024%20skipped-brightgreen?logo=pytest)](tests/)
[![Verify](https://img.shields.io/badge/verify_features-82%20checks-blueviolet)](scripts/verify_features.py)
[![Live2D Demo](https://img.shields.io/badge/Live2D%20Demo-Try%20Online-6c5ce7?logo=githubpages&logoColor=white)](https://255856.github.io/Smart-Desktop-Pet/)

---

[![中文](https://img.shields.io/badge/%E4%B8%AD%E6%96%87-1976D2?style=for-the-badge&logo=github&logoColor=white)](#zh-hans)
[![English](https://img.shields.io/badge/English-24292F?style=for-the-badge&logo=github&logoColor=white)](#english)

[Live2D 在线 Demo / Live2D Demo](https://255856.github.io/Smart-Desktop-Pet/) · [文档 / Docs](docs/)

---

<a name="zh-hans"></a>

# 桌面宠物

> **一个真正能"养"的桌面 AI 宠物** —— 不只是会动、会说话，而是会**记住你、主动搭话、调用工具、跨多轮思考**，还能**陪你下五子棋、下中国象棋、玩狼人杀**的数字伙伴。

## 它能做什么？

- **真智能**：手写 **ReAct Agent** 循环 + **3 层抗幻觉**（强制调工具 → 跨轮持续到 `max_turns` → 工具结果直接总结），可选 **LangChain** 后端
- **75 个工具**：时间 / 提醒 / 记忆 / 计算 / 文件 / 截图 / 系统 / 应用 / 网页搜索（Tavily + DuckDuckGo）/ 快捷指令 + **ActivityWatch 活动感知**（mcp_aw_aw_get_recent_activities / mcp_aw_aw_get_current_window / mcp_aw_aw_status / mcp_aw_aw_get_browser_tabs，桌宠能主动汇报主人最近在做什么）
- **真记忆**：**独立记忆进程** `memory_sidecar`（HTTP `127.0.0.1:8788`，桌宠自动拉起）：**Chroma 向量 + BM25 关键词 + 交叉编码器精排**，JSON 记忆卡是真值、索引随时能重建；重要度评分 + 时间衰减 + 冲突改写 + 长期演化
- **真说话**：**3 引擎 TTS**（edge-tts / GPT-SoVITS 本地克隆 / MiniMax 云端）+ 浏览器 SpeechRecognition **ASR 按住说话** + 嘴型同步
- **真模样**：**双渲染**（Live2D Cubism 4 + pixi-live2d-display / PNG 帧动画），模型缺失自动 fallback
- **真玩法**：**五子棋**（3 档 AI 难度）+ **中国象棋**（**走法生成 / 将军 / 将死 / 困毙 / 飞将全部走 [cchess](https://pypi.org/project/cchess/) 库**，MIT、PyPI 1.20+，我们自己只写 alpha-beta 搜索 + 估值；3 档 AI 难度）+ **9 人狼人杀**（你 + 8 个独立 NPC Agent + 桌宠主持，无 API key 也能离线玩）+ 每日签到 + 喂食 + 5 项状态养成
- **真养成 + 决策耦合（创新点 2）**：桌宠的 **心情 / 饱食 / 好感 / 等级** 4 维度不再只影响头顶表情，而是直接耦合 Agent 决策 —— 状态差时**强制 LLM 直接拒绝** lock_screen / kill_process / shutdown_computer 等高危工具（不走 confirm 弹窗），3 层防御（prompt 顶部 directive + 末尾 override + post-LLM 拦截器）+ 决策 trace 全留痕。详见 [创新点 2 文档](docs/innovation-2-companion-policy.md)
- **真开放**：内置 **MCP stdio JSON-RPC** + filesystem server + activitywatch server，可外挂任何 MCP 兼容 server
- **可观测**：所有 LLM / 工具调用落 SQLite，**FastAPI Dashboard**（`localhost:8766`）可视化 Trace、回放、调参；companion 决策每条 JSON-line 落 `data/companion_log.txt` 供 grep 排查

## 5 分钟跑起来

### 安装前置（适用于 macOS / Linux）

```bash
# Python 3.10+
python3 --version
pip3 install -r requirements.txt

# 可选：Live2D 渲染 + Chromium 内核（建议 250MB+）
pip3 install PyQtWebEngine

# 启动（会顺带拉起记忆 sidecar 子进程 :8788，不用手动开）
python3 main.py
```

### 安装前置（适用于 Windows）

```powershell
# Python 3.10+（勾选 Add Python to PATH）
python --version
pip install -r requirements.txt

# 可选：Live2D 渲染 + Chromium 内核（建议 250MB+）
pip install PyQtWebEngine

# 启动（会顺带拉起记忆 sidecar 子进程 :8788，不用手动开）
python main.py
```

> **桌宠启动时会多出一个进程**：长期记忆住在**独立进程** `memory_sidecar`（HTTP `127.0.0.1:8788`）里。
> 桌宠**自动拉起它**（健康检查 + 崩溃自动重启 + 退出时收干净），**你什么都不用做**；
> 想自己看看它：`python -m memory_sidecar stats`。细节见 [记忆 sidecar 文档](docs/memory-sidecar.md)。

### 可选：ActivityWatch 活动感知 (强烈推荐)

桌宠能告诉你「过去 5 分钟在做什么 / 现在在哪个窗口」，前提是装 [ActivityWatch](https://activitywatch.net/)：

1. 下载安装桌面客户端 (Windows / macOS / Linux) → aw-server 自动在 5600 端口跑
2. 浏览器想记 tab → 装 [aw-watcher-web](https://activitywatch.net/downloads/) Chrome/Firefox 扩展
3. `mcp_servers.yaml` 里 `aw` server 默认 `enabled: true`，桌宠启动时自动连
4. 不装也能用桌宠，mcp_aw_* 工具会回「server 未运行」并教你怎么装

详见 [ActivityWatch 集成](docs/activitywatch-integration.md)

### 第一次打开会怎样？

1. **默认自动开启内置模型 Hiyori**（Live2D 官方样例，零配置）
2. **桌宠主动打招呼**：检测当前时间段说「早上好 / 中午好 / 晚上好」
3. **任意键说话 / 按住说话键 ASR** → 桌宠回复 + 异步嘴型同步
4. **无 API key**？桌宠用 mock 回复兜底（功能演示完整，但不能调真实 Tavily 搜索 / 真 LLM）
5. **ActivityWatch 未运行**？桌宠启动时 log 提示 `MCP aw 未启用 (aw-server 未运行?)`，不影响其他功能，mcp_aw_* 工具会回「server 未运行, 请装 ActivityWatch」
6. **右键模型 → 「设置」** 配置 LLM / Tavily Key 启用真模型；设置面板 → 「控制 Tab → 主动关心」段可开 AW SQLite 落盘
7. **记忆 sidecar 在后台起来了**：桌宠自己拉起 `memory_sidecar`（:8788），**不阻塞桌宠启动**；它没就绪也不影响聊天，只是这一轮没有长期记忆
8. **记忆模型没下载**？语义召回（向量 + 精排）用不了，只剩 BM25 关键词腿；想开就跑一次 `python scripts/prefetch_models.py`（≈1.1 GB / 有 CUDA ≈2.2 GB），详见下一节
9. **桌宠不再逐句朗读了**。声音改成**它自己决定**要不要用：合适的时候会发一条短语音，或者哼唱两句。想恢复原来的逐句朗读，把 `app/core/companion_config.yaml` 里的 `voice.auto_read` 改成 `true`（也可以在 `data/companion_config.yaml` 里覆盖）—— **改完即时生效，不用重启**。

### 语音：变成 MCP 能力，由桌宠自己决定要不要说

三个 MCP 工具（按本项目的 `mcp_<server>_<tool>` 命名规范）：

| 工具 | 干什么 |
|---|---|
| `mcp_voice_speak` | 念一小段话（可选情绪） |
| `mcp_voice_sing` | 按旋律唱几句歌词（旋律是现编的调） |
| `mcp_voice_status` | 报本机语音服务在不在、有哪些音色可用 |

**全部复用你本机已有的东西**：念用本机 GPT-SoVITS 服务（`:9880`），唱用本机 RVC 音色权重。
**绝不下载任何模型权重** —— 本机没有的音色，`mcp_voice_status` 会写明「音色不在本地」，
工具也会**如实失败**，不会偷偷去下一个。

**措辞铁律**（写在工具描述里，模型看得见）：语音条**前面不加方括号标签**；自编的调**不冒充原创歌曲**；
工具失败了**不许假装已经发出**。语音是打断 —— 绝大多数回合用文字就好。

### 长期记忆：sidecar 进程 + 模型（第一次要下 ≈1.1 GB / 有 CUDA ≈2.2 GB）

长期记忆住在**独立进程** `memory_sidecar` 里（HTTP `127.0.0.1:8788`）：**JSON 记忆卡是真值**
（`data/memory_sidecar/cards.json`），Chroma（向量）和 BM25（关键词）只是**派生索引**，
随时可以 `POST /rebuild` 从卡片重建；最后还有一条**交叉编码器精排**，决定本轮真正注入哪几条记忆。

**进程是桌宠拉起来的，模型不是**：模型下载被做成一个**显式动作** —— 想开语义召回，就自己跑一次
下面的脚本（脚本幂等，模型已在缓存里就跳过）。
**运行时绝不会偷偷下载**：sidecar 加载模型时用 `local_files_only=True`，只读本地缓存、不联网；
模型不在缓存里就**快速失败**并提示你跑这个脚本（离线机器上不会卡在重试上）：

```powershell
# 1) 下载 + 校验模型（幂等：下过一次再跑就是 no-op）
python scripts/prefetch_models.py

# 只看模型在不在（不下载、不加载）
python scripts/prefetch_models.py --check
```

| 模型 | 大小 | 干什么 |
|---|---|---|
| `BAAI/bge-small-zh-v1.5` | 91.9 MB | 向量模型（512 维）：找语义相近的记忆 |
| `BAAI/bge-reranker-v2-m3` | ≈2.1 GB | 精排：GPU 上 16 对 41 ms、分离度 2.7x（**有 CUDA 才选它**） |
| `BAAI/bge-reranker-base` | ≈1.1 GB | 精排：CPU 上 16 对 291 ms（**没有 CUDA 选它**） |

精排模型**按设备自动选**（有 CUDA → v2-m3，没有 → base）；想自己定就设环境变量
`DESKTOP_PET_MEMORY_RERANK`。首次下载合计 **≈1.15 GB（纯 CPU）/ ≈2.2 GB（有 CUDA）**。

模型落在 HuggingFace 缓存目录（Windows：`%USERPROFILE%\.cache\huggingface\hub`），
**不进仓库**（仓库的 `data/` 始终是干净的）。

**没跑预取会怎样**：向量腿和精排腿降级（精排退回粗排，向量退回 BM25 关键词腿），召回只剩
关键词匹配 —— 桌宠照样聊天、照样记东西，只是想不起「意思相近、字面不重合」的那条。

**记忆服务没起来会怎样**（sidecar 挂了 / 起不来）：桌宠**照常聊天**，只是这一轮没有长期记忆
（记忆块是空的）；`remember_fact` / `recall_memory` 会如实回「错误：记忆服务不可用…」，
不会假装记住。sidecar 连续重试 3 次还起不来，桌宠会冒泡告诉你「长期记忆暂时用不了了」。

接口就这几个：`GET /health /stats /list`、`POST /search /upsert /delete /supersede /rebuild`。
端点细节、卡片格式、降级矩阵、命令行用法都在 [记忆 sidecar 文档](docs/memory-sidecar.md)。

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

控制台里能调 **视觉 / 帧时长 / 行为 / Live2D / 语音 / 模型** 六组共 47 项；**ASR / 提醒**在 `config.yaml` 里改；**长期记忆**另有记忆面板（查看 / 搜索 / 分类 / 增删）+ sidecar 环境变量（端口 / 数据目录 / 精排模型）

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

### 3. 长期记忆（独立 sidecar 进程 + 重要度 + 冲突改写）

- **存储**：**独立进程** `memory_sidecar`（HTTP :8788）—— **JSON 记忆卡是真值**（`data/memory_sidecar/cards.json`），Chroma 向量索引 + BM25 关键词索引都只是**派生**的，随时能从卡片重建
- **检索**：向量 + 关键词两路粗排（RRF 融合）→ **交叉编码器精排**（模型按设备自动选，见上文）
- **重要度评分**（0–1）
- **时间衰减**（按 kind 的半衰期，记忆越久越靠后）
- **冲突改写**（事实变了用 `remember_fact(supersedes=旧id)` 改写，旧卡自动标记过时，不会新旧两条并存）
- **降级不阻塞**：sidecar 不可用 → 记忆块为空、工具如实报错，**桌宠照常聊天**

### 4. 状态养成对 Agent 决策的耦合

桌宠的 4 维状态（心情 / 饱食 / 好感 / 等级）不再只决定表情，而是直接耦合到 LLM 决策路径 —— 状态差时**强制拒绝**高危工具，状态好时**正常 confirm**。落地是「权限层 + 策略层 + 反馈层」三层 + 3 道 prompt 防线 + post-LLM 兜底。

#### 4.1 决策路径（核心命题 `Action = f(intent, tools, context, S)`）

```
用户消息
   │
   ▼
状态量化 (mood/satiety/favor/level: LOW/MID/HIGH)
   │
   ├─ 工具风险等级 (5 档: SAFE/LOW/MID/HIGH/IRREVERSIBLE)
   │
   ▼
查表 lookup_decision(state, tool)  →  ALLOW / CONFIRM / REJECT / REFUSE
   │
   ├─ ALLOW   → 直接放行 (即便高风险, 状态好时主人特权)
   ├─ CONFIRM → 弹窗确认
   ├─ REJECT  → 返回拒绝 ToolMessage (返回 REJECT 反馈钩子)
   └─ REFUSE  → 同 REJECT, 但任务级标 failure
   │
   ▼
任务完成 → apply_outcome 调钩子 (mood/sat/favor/exp 增减) → 落盘
```

#### 4.2 3 道防线

1. **prompt 顶部 directive** (`grounding_dynamic_prompt` 开头): 状态差时硬编码「【!!! 第一原则 !!!】禁止 preemptive 问 / 禁止 confirm 流程 / 直接拒绝」
2. **prompt 末尾 override**: 跟顶部 directive 互为冗余，确保 LLM 在任意位置都能看到
3. **post-LLM 拦截器** (`PostLLMStateGuard`): 状态差 + LLM 输出含 preemptive 措辞 → 改写 `ModelResponse.result[0].content = REFUSAL_TEXT` + 清 `tool_calls`，100% 兜底

完整设计与 60+ 个测试见 [docs/innovation-2-companion-policy.md](docs/innovation-2-companion-policy.md)

## 与桌宠本体不同

| 维度 | 桌面版 | 网页 Demo |
|---|---|---|
| 渲染 | Live2D / PNG / **Voice** | ✓ Live2D |
| 语音 | Speech / ASR / TTS / Voice | ✓ Speech / ASR / TTS / Voice |
| 大脑 | ReAct + LangChain + Multi-Agent | ✓ ReAct + Multi-Agent |
| 工具 | 75 个 | ✓ 24 个（browser sandbox） |
| 记忆 | sidecar 进程：向量 + 关键词 + 精排 / 持久化 | ✓ localStorage |
| 提醒 | 系统通知 + cron | ✓ Browser Notifications |
| 游戏 | 五子棋 / 象棋 / 狼人杀 | ✗（browser sandbox） |
| 养成 | 5 状态 / 喂食 / 签到 | ✗ |
| 主动搭话 | 时段上下文 / 习惯 | ✓ 时段上下文 / habit |
| Trace | SQLite + Dashboard | ✓ 内嵌 |

### 怎样补齐？

本仓库内网页 Demo 与桌面版**始终同步更新**——新增的桌面工具可零成本移植到 Web（browser sandbox 限制除外：截图 / 本地文件 / 系统调用 / 应用启动）。

## 它能怎么玩？

### 基础对话

- 「今天天气怎么样？」 → 自动搜索 → 真回答
- 「3 分钟后提醒我喝水」 → 系统通知 + TTS
- 「记住我最爱的颜色是蓝色」 → 写入长期记忆 → 下次自动想起
- 「过去 5 分钟我在做什么？」 → 调 ActivityWatch (需装桌面客户端) → 「主人过去 5 分钟在 VSCode, 待了 4 分 32 秒」

### 设置项

设置在**控制台**（浏览器面板）里，分 6 组共 47 项：

| 组 | 能调什么 |
|---|---|
| 视觉 | 渲染引擎（sprite / Live2D）/ 缩放 / 透明度 / 置顶 / 过渡动画 / 锁定第一张 idle / 隐藏水印 |
| 帧时长 | 帧间隔 / 帧率监控 / 渲染帧率上限 / **Live2D 静置帧率** |
| 行为 | 待机/走动/决策的间隔 / 主动搭话开关与间隔 / ActivityWatch 开关与服务器 / 开发者模式 |
| Live2D | 随机表情 / 随机贴纸 / 贴纸尺寸与时长 |
| 语音 | TTS 开关与引擎（edge / GPT-SoVITS / MiniMax）/ 音色 / 语速 / 参考音频 / 超时 |
| 模型 | base URL / API key / 模型名 / 温度 / max_tokens / 超时 / 流式 |

**下面这些不在控制台里**，要改 `config.yaml`：

| 段 | 内容 |
|---|---|
| `asr` | 语音输入开关 / 模型大小 / 语言 |
| `reminder` | 提醒开关 / 数据文件 |
| `character` | 当前人设文件（人设 / 名字 / 音色） |

长期记忆另有**记忆面板**（查看 / 搜索 / 分类 / 增删）+ sidecar 环境变量（端口 / 数据目录 / 精排模型）。

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
- [记忆 sidecar](docs/memory-sidecar.md) — 独立记忆进程：端点 / 卡片格式 / 降级矩阵 / 模型
- [Live2D 集成](docs/live2d-integration.md)
- [情绪系统](docs/emotion-system.md)
- [TTS 集成](docs/tts-integration.md)
- [ActivityWatch 集成](docs/activitywatch-integration.md) — 桌宠如何读你的活动窗口
- [状态对 Agent 决策的耦合](docs/innovation-2-companion-policy.md)
- [桌面版工具参考](docs/tools-reference.md) — 75 个工具详细说明

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

- **2400 passed** / 15 skipped
- 验证：82 项功能检查
- 19 个 AST 守卫 + 量化 / 钩子 / 决策一致性 / post-LLM 拦截器测试

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
- **75 tools**: time / reminder / calc / files / screenshot / system / apps / web search (Tavily + DuckDuckGo) / shortcuts + **ActivityWatch activity awareness** (mcp_aw_aw_get_recent_activities / mcp_aw_aw_get_current_window / mcp_aw_aw_status / mcp_aw_aw_get_browser_tabs — the pet proactively reports what the owner has been doing)
- **Real memory**: a **standalone sidecar process** (`memory_sidecar`, HTTP `127.0.0.1:8788`, auto-started by the pet): **Chroma vector + BM25 keyword + cross-encoder rerank**, with JSON memory cards as the source of truth (the indexes are derived and rebuildable); importance scoring + time decay + conflict rewriting + long-term evolution
- **Real voice**: **3 TTS engines** (edge-tts / GPT-SoVITS local cloning / MiniMax cloud) + browser SpeechRecognition **ASR hold-to-talk** + mouth-sync
- **Real look**: **dual renderer** (Live2D Cubism 4 + pixi-live2d-display / PNG frame animation), graceful fallback when the model is missing
- **Real games**: **Gomoku** (3 AI difficulty levels) + **Chinese Chess** (move generation / check / checkmate / stalemate / flying general all come from the **[cchess](https://pypi.org/project/cchess/)** library, MIT, PyPI 1.20+; we only write the alpha-beta search + evaluation, with 3 AI difficulty levels) + **9-player Werewolf** (you + 8 independent NPC agents + the pet as moderator, fully offline without any API key) + daily check-in + feeding + 5-stat nurturing
- **Real nurturing → decision coupling (Innovation #2)**: the pet's **mood / satiety / favor / level** no longer just drives the on-top expression — they directly couple to the Agent's decision path. When state is low, the LLM is **forced to refuse** risky tools like `lock_screen` / `kill_process` / `shutdown_computer` (no preemptive confirmation prompt, no confirm dialog). 3 layers of defense (top-of-prompt directive + bottom-of-prompt override + post-LLM interceptor) + every decision is JSON-line traced. See [Innovation #2 doc](docs/innovation-2-companion-policy.md)
- **Real extensibility**: built-in **MCP stdio JSON-RPC** + filesystem server + activitywatch server, attach any MCP-compatible server
- **Observable**: all LLM / tool calls land in SQLite; a **FastAPI Dashboard** (`localhost:8766`) visualizes Trace, replay, and runtime parameters; companion decisions land in `data/companion_log.txt` as JSON lines for grep-based debugging

## 5-Min Quick Start

### Prerequisites (macOS / Linux)

```bash
# Python 3.10+
python3 --version
pip3 install -r requirements.txt

# Optional: Live2D rendering + Chromium core (250MB+ recommended)
pip3 install PyQtWebEngine

# Run (also starts the memory sidecar process on :8788 — nothing to launch by hand)
python3 main.py
```

### Prerequisites (Windows)

```powershell
# Python 3.10+ (tick "Add Python to PATH")
python --version
pip install -r requirements.txt

# Optional: Live2D rendering + Chromium core (250MB+ recommended)
pip install PyQtWebEngine

# Run (also starts the memory sidecar process on :8788 — nothing to launch by hand)
python main.py
```

> **One more process shows up on startup**: long-term memory lives in a **standalone sidecar process**
> (`memory_sidecar`, HTTP `127.0.0.1:8788`). The pet **starts it for you** (health check,
> restart on crash, cleaned up on exit) — **no action needed**. Peek at it with
> `python -m memory_sidecar stats`; details in the [memory sidecar doc](docs/memory-sidecar.md).

### Optional: ActivityWatch activity awareness (strongly recommended)

The pet can tell you "what you did in the last 5 minutes / which window you're in" once you install [ActivityWatch](https://activitywatch.net/):

1. Install the desktop client (Windows / macOS / Linux) → aw-server starts on port 5600
2. To track browser tabs, install the [aw-watcher-web](https://activitywatch.net/downloads/) Chrome / Firefox extension
3. `mcp_servers.yaml` ships with `aw` server `enabled: true`, the pet connects automatically on startup
4. The pet works fine without it — `mcp_aw_*` tools return "server not running, please install ActivityWatch"

See [ActivityWatch integration](docs/activitywatch-integration.md)

### What happens on first launch?

1. **The bundled model Hiyori is enabled by default** (an official Live2D sample, zero configuration)
2. **The pet greets you first**: it detects the time of day and says "Good morning / afternoon / evening"
3. **Type to chat / hold-to-talk for ASR** → the pet replies with async mouth-sync
4. **No API key?** The pet falls back to mock replies (full feature demo, but no real Tavily search / real LLM)
5. **ActivityWatch not running?** Startup log shows `MCP aw not enabled (aw-server not running?)` — everything else keeps working, `mcp_aw_*` tools return "server not running, please install ActivityWatch"
6. **Right-click the model → "Settings"** to configure LLM / Tavily keys; Settings → "Control Tab → Proactive Care" enables AW SQLite persistence
7. **The memory sidecar comes up in the background**: the pet starts `memory_sidecar` (:8788) without blocking its own startup; if it isn't ready yet the pet still chats — that turn simply has no long-term memory
8. **Models not downloaded?** Semantic recall (vector + rerank) is unavailable and only the BM25 keyword leg remains; run `python scripts/prefetch_models.py` once to enable it (≈1.1 GB, ≈2.2 GB with CUDA) — see the section below
9. **The pet no longer reads every reply aloud.** Voice is now something it *chooses*: it may send a
   short voice message, or sing a line, when that fits. If you want the old behaviour back, set
   `voice.auto_read: true` in `app/core/companion_config.yaml` (or in `data/companion_config.yaml`
   to override it) — it takes effect immediately, no restart.

### Voice as an MCP capability (the pet decides when to speak)

Three MCP tools, following the project's `mcp_<server>_<tool>` convention:

| tool | what it does |
|---|---|
| `mcp_voice_speak` | say a short line out loud (optional emotion) |
| `mcp_voice_sing` | sing a few lines to a melody it makes up |
| `mcp_voice_status` | report which local voices/services are actually available |

**Everything runs on what you already have**: speech goes through your local GPT-SoVITS service
(`:9880`), singing goes through your local RVC voice model. **No model weight is ever downloaded** —
if a voice is not on this machine, `mcp_voice_status` says 「音色不在本地」 and the tool fails
honestly instead of fetching something.

**Wording rules the model has to follow** (they are in the tool descriptions): no `[voice]`-style
label in front of a voice message, never claim a self-written melody is an original song, and never
claim a voice was sent when it failed. Voice is an interruption — most turns should stay text.

### Long-term memory models (≈1.1 GB on first run, ≈2.2 GB with CUDA)

Long-term memory lives in a **standalone sidecar process** (`memory_sidecar`, HTTP `127.0.0.1:8788`):
**JSON memory cards are the source of truth** (`data/memory_sidecar/cards.json`), while Chroma (vector)
and BM25 (keyword) are **derived indexes** that can be rebuilt from the cards at any time
(`POST /rebuild`); a **cross-encoder reranker** then decides which memories actually get injected.

**The pet starts the process, not the models**: downloading the models is an explicit step — run
the script yourself once (idempotent: models already in the cache are skipped):

```powershell
# 1) Download + verify the models (idempotent: a second run is a no-op)
python scripts/prefetch_models.py

# Just report whether they are cached (no download, no loading)
python scripts/prefetch_models.py --check
```

| Model | Size | What it does |
|---|---|---|
| `BAAI/bge-small-zh-v1.5` | 91.9 MB | Embeddings (512-dim): finds semantically similar memories |
| `BAAI/bge-reranker-v2-m3` | ≈2.1 GB | Reranker: 41 ms per 16 pairs on GPU, 2.7x separation (**picked when CUDA is available**) |
| `BAAI/bge-reranker-base` | ≈1.1 GB | Reranker: 291 ms on CPU (**picked when there is no CUDA**) |

The reranker is **auto-selected by device**; override it with the `DESKTOP_PET_MEMORY_RERANK`
environment variable. First-run download: **≈1.15 GB (CPU only) / ≈2.2 GB (with CUDA)**.
Models land in the HuggingFace cache (Windows: `%USERPROFILE%\.cache\huggingface\hub`),
**not in the repo**.

**Without them**: the vector and rerank legs degrade (rerank → coarse ranking, vector → BM25
keyword leg) — the pet still chats and still remembers, it just can't recall memories that mean
the same without sharing words. **Without the sidecar**: the pet still chats, that turn simply has
no long-term memory, and the memory tools honestly answer "记忆服务不可用…".

Endpoints, card format, the degradation matrix and the CLI live in the
[memory sidecar doc](docs/memory-sidecar.md).

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

LLM / TTS / ASR / reminders / shortcuts — all adjustable; **long-term memory** has its own panel (view / search / filter / add / delete) plus sidecar environment variables (port / data dir / rerank model).

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

### 3. Long-term memory (standalone sidecar process + importance + conflict rewriting)

- **Storage**: a **standalone process** (`memory_sidecar`, HTTP :8788) — **JSON memory cards are the source of truth**; the Chroma vector index and the BM25 keyword index are **derived** and can be rebuilt from the cards at any time
- **Retrieval**: vector + keyword coarse ranking fused with RRF → **cross-encoder rerank** (the model is auto-selected per device, see above)
- **Importance score** (0–1)
- **Time decay** (per-kind half-life: older memories rank lower)
- **Conflict rewriting** (a changed fact is rewritten with `remember_fact(supersedes=<old id>)` — the old card is marked as superseded instead of keeping both versions)
- **Degradation never blocks**: if the sidecar is unavailable the memory block is empty and the tools answer honestly, but **the pet still chats**

### 4. Innovation #2: pet state couples into Agent decisions

The pet's 4-dimensional state (mood / satiety / favor / level) no longer just decides the on-top expression — it directly couples into the LLM decision path. When state is low, **risky tools are force-rejected**; when state is good, normal `confirm` flow continues. Implementation: a "permission layer + strategy layer + feedback layer" three-tier design, with 3 layers of prompt defense plus a post-LLM safety net.

#### 4.1 Decision path (paper core proposition `Action = f(intent, tools, context, S)`)

```
User message
   │
   ▼
State quantize (mood/satiety/favor/level: LOW/MID/HIGH)
   │
   ├─ Tool risk level (5 tiers: SAFE/LOW/MID/HIGH/IRREVERSIBLE)
   │
   ▼
Lookup lookup_decision(state, tool) → ALLOW / CONFIRM / REJECT / REFUSE
   │
   ├─ ALLOW   → pass through (owner privilege even for risky tools)
   ├─ CONFIRM → dialog confirmation
   ├─ REJECT  → return rejected ToolMessage (fires REJECT feedback hook)
   └─ REFUSE  → like REJECT, but task-level marks as failure
   │
   ▼
Task done → apply_outcome fires hooks (mood/sat/favor/exp deltas) → persist
```

#### 4.2 Three layers of defense

1. **Top-of-prompt directive** (`grounding_dynamic_prompt` head): when state is low, hard-code `[!!! First Principle !!!] forbid preemptive questions / forbid confirm flow / direct refusal`
2. **Bottom-of-prompt override**: redundant with the top directive so the LLM sees the state-check at any reading position
3. **Post-LLM interceptor** (`PostLLMStateGuard`): state low + LLM output contains preemptive phrasing → rewrite `ModelResponse.result[0].content = REFUSAL_TEXT` and clear `tool_calls`, a 100% runtime safety net

Full design and 19+ tests in [docs/innovation-2-companion-policy.md](docs/innovation-2-companion-policy.md).

## How it's different from the desktop app

| Aspect | Desktop | Web Demo |
|---|---|---|
| Renderer | Live2D / PNG / **Voice** | ✓ Live2D |
| Voice | ✅ Speech / ASR / TTS / Voice | ✓ Speech / ASR / TTS / Voice |
| Brain | ✅ ReAct + LangChain + Multi-Agent | ✓ ReAct + Multi-Agent |
| Tools | ✅ 52 | ✓ 18 (browser sandbox) |
| Memory | ✅ sidecar process: vector + keyword + rerank / persistent | ✓ localStorage |
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
- "What did I do in the last 5 minutes?" → calls ActivityWatch (requires desktop client) → "You were in VSCode for 4m32s"

### Settings

| Category | Items |
|---|---|
| LLM | Model name / base URL / API key / system prompt |
| TTS | Engine (edge / GPT-SoVITS / MiniMax) / speed / pitch / test |
| ASR | Language / auto-send |
| Reminder | Default 5 / 10 / 30 minutes |
| Memory | Memory panel (search / filter / add / delete) + sidecar env vars (port / data dir / rerank model) |
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
- [Memory sidecar](docs/memory-sidecar.md) — the standalone memory process: endpoints / card format / degradation matrix / models
- [Live2D integration](docs/live2d-integration.md)
- [Emotion system](docs/emotion-system.md)
- [TTS integration](docs/tts-integration.md)
- [ActivityWatch integration](docs/activitywatch-integration.md) — how the pet reads your activity windows
- [Innovation #2: state-coupling Agent decision](docs/innovation-2-companion-policy.md)
- [Tools reference](docs/tools-reference.md) — detailed description of all 68 tools

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

- **2400 passed** / 15 skipped
- Verification: 82 feature checks
- Innovation #2: 19 AST guards + quantize / hook / decision-consistency / post-LLM interceptor tests

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
