# 桌面宠物 · Smart Desktop Pet

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue?logo=python&logoColor=white)](https://www.python.org)
[![PyQt5](https://img.shields.io/badge/UI-PyQt5%2BWebEngine-41CD52?logo=qt&logoColor=white)](https://riverbankcomputing.com)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-2400%20passed%20%2F%2015%20skipped-brightgreen?logo=pytest)](tests/)

[Live2D 在线 Demo](https://255856.github.io/Smart-Desktop-Pet/) · [完整文档](docs/)

> 一个住在桌面上的 AI 伙伴：会记住你、主动搭话、调用工具、跟你下棋。

## 它能做什么

| | |
|---|---|
| **聊天** | 双后端（LangChain / 自研 ReAct）· 逐句上屏 · 语音条 · 表情包 |
| **记忆** | 独立 sidecar（向量 + BM25 + 精排）· 自我认知 / 价值观 / 程序性分区 |
| **主动** | 定时冒泡：按记忆 / 最近对话 / 你在做什么 / 自己的念头轮换选话题 |
| **工具** | 75 个（提醒 / 文件 / 应用 / 搜索 / 天气 / 截图 / 语音 / 记忆…） |
| **养成** | 等级 / 心情 / 饱食 / 好感度 —— 状态会真的影响它怎么答你 |
| **游戏** | 五子棋 / 中国象棋 / 狼人杀 |

## 跑起来（5 分钟）

```bash
git clone <repo> && cd desktop-pet
pip install -r requirements.txt
cp config.example.yaml config.yaml     # Windows: copy config.example.yaml config.yaml
# 填 llm.api_key（MiniMax / OpenAI 兼容），然后：
python main.py
```

启动会顺带拉起**记忆 sidecar**（:8788）与 **控制台**（:8766）。

可选：

- **Live2D 渲染**：装 `PyQtWebEngine`（约 250MB），否则退回 sprite 渲染；
- **联网搜索**：在 `config.yaml` 填 `brain.tavily_api_key`（不给就走 DuckDuckGo 降级）；
- **ActivityWatch 活动感知**：装 ActivityWatch，它就能知道你正在忙什么。

## 配置

`config.yaml`（照着 `config.example.yaml` 改）常用几项：

| 键 | 说明 |
|---|---|
| `llm.api_key` / `llm.model` | 模型与 key |
| `character.character_file` | 人设：`characters/xiaoxi.yaml`（小汐）/ `xiaoxiao.yaml`（小晓）/ `dfy.yaml`（大肥鱼） |
| `brain.tavily_api_key` | 联网搜索用 Tavily |
| `window.scale` | 桌宠大小 |
| `brain.backend` | `standard`（LangChain）或 `lightweight`（ReAct） |

完整字段见 [docs/config-reference.md](docs/config-reference.md)。

## 目录结构

```
main.py              启动入口
app/
  brain/             大脑：agent / 提示词 / 记忆 / 主动搭话
  engine/            引擎：工具 / 状态 / 贴纸
  ui/                界面：桌宠 / 聊天窗 / 面板
  core/              核心：配置 / 宠物控制 / 第三方集成
  animation/         动画：Live2D / sprite
  voice/             语音
  mcp/               MCP 服务（活动感知 / 文件 / 语音）
  web/               控制台（FastAPI + 前端）
  games/  agents/  eval/   小游戏 / 子智能体 / 评测
characters/          人设与行为层
memory_sidecar/      长期记忆服务（独立进程）
scripts/             工具脚本（live2d/ 与 manual/ 已分组）
docs/                文档
tests/               测试
packaging/           打包（build.py / PyInstaller spec / hooks）
```

## 常用命令

```bash
python -m pytest -q                      # 跑全部测试（约 4 分钟）
python scripts/verify_features.py         # 特性自检（82 项）
python scripts/check_tool_docs.py         # 工具清单与文档对账
python scripts/measure_turn_latency.py    # 真 API 端到端计时
python scripts/eval_memory_recall.py      # 记忆召回评测
python packaging/build.py                 # 打包成 exe
```

## 文档

| | |
|---|---|
| 快速上手 | [docs/quickstart.md](docs/quickstart.md) |
| 架构总览 | [docs/architecture.md](docs/architecture.md) |
| 工具清单（75 个） | [docs/tools-reference.md](docs/tools-reference.md) |
| 长期记忆 | [docs/memory-sidecar.md](docs/memory-sidecar.md) |
| 配置字段 | [docs/config-reference.md](docs/config-reference.md) |
| Live2D 接入 | [docs/live2d-integration.md](docs/live2d-integration.md) |
| 语音接入 | [docs/tts-integration.md](docs/tts-integration.md) |
| 活动感知 | [docs/activitywatch-integration.md](docs/activitywatch-integration.md) |
| 排错 | [docs/troubleshooting.md](docs/troubleshooting.md) |
| 开发与测试 | [docs/dev-testing.md](docs/dev-testing.md) |
| 与 qq-bridge 的对照 | [docs/comparison-with-qq-bridge.md](docs/comparison-with-qq-bridge.md) |

## 已知限制

- **联网搜索**要 Tavily key 才稳（免 key 的 DuckDuckGo 在部分网络下连不通）；
- **语音**依赖本机 GPT-SoVITS 服务（:9880），未启动时只出文字；
- **Live2D** 需要 `PyQtWebEngine`；没有就退回 sprite。

## 许可

[MIT](LICENSE)
