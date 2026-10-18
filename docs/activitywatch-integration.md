# ActivityWatch 集成

> 状态: 已落地 (v25+)，mcp_aw_* 4 个工具 (get_recent_activities / current_window / query / buckets) 走 MCP stdio 桥接 aw-server HTTP API。

## 1. 一句话概括

桌宠通过 [ActivityWatch](https://activitywatch.net/) 读主人最近在做什么 —— 当前窗口 / 过去 5 分钟的活动 / 浏览器 tab 标题 / 自定义 query，LLM 可以主动汇报或被问及时回答。

## 2. 为什么需要 ActivityWatch？

桌宠本体 (Python) 没有权限直接读 Windows / macOS / Linux 的活动窗口 API。ActivityWatch 是一个开源跨平台的时间追踪工具 ([GitHub](https://github.com/ActivityWatch/activitywatch))，自带的 **aw-server** 在本地 5600 端口开 HTTP API，多个 **aw-watcher** 后台进程持续采集系统活动写入 bucket。

桌宠通过 MCP stdio 起一个 Python 桥接进程 (`app/mcp/activitywatch_server.py`)，这个桥接到 aw-server 拉数据，对 LLM 暴露 4 个 mcp_aw_* 工具。桌宠**不需要**写平台特定的窗口 API 调用。

## 3. 一次性安装流程

### 3.1 装 ActivityWatch 桌面客户端

下载安装: <https://activitywatch.net/downloads/>

按平台选:
- **Windows**: `ActivityWatch-x.x.x-windows-x86_64.msi` (默认装到 `C:\Program Files\ActivityWatch\`)
- **macOS**: `ActivityWatch-x.x.x-macos.dmg`
- **Linux**: AppImage / deb / 各自包管理器

装完会自动启动 aw-server (5600 端口) + 1 个 watcher。打开浏览器访问 `http://localhost:5600` 看到 ActivityWatch Web UI 即说明 aw-server 跑起来了。

### 3.2 装 watcher (后台采集进程)

主程序自带 aw-watcher-window + aw-watcher-afk, 但有几个 watcher 需要单独装:

```bash
# 1) aw-watcher-window — 当前活动窗口 (主进程常驻, 自动从系统 API 取)
#    Windows / macOS 自带; Linux 需 aw-watcher-window 单独装:
pip install aw-watcher-window

# 2) aw-watcher-afk — 鼠标键盘空闲检测 (主进程常驻, 算 AFK 时长)
#    Windows / macOS 自带; Linux 需:
pip install aw-watcher-afk

# 3) aw-watcher-web — 浏览器 tab title / url 采集
#    装对应浏览器扩展 (Chrome / Firefox), 不是 Python 包:
#    https://activitywatch.net/downloads/  → "Web Watcher" 段
```

### 3.3 启用 mcp_servers.yaml

仓库根目录的 `mcp_servers.yaml` 里 `aw` server 默认 `enabled: true` (line 29-33)。桌宠启动时自动起 `python -m app.mcp.activitywatch_server` 子进程, 这个子进程在 5600 端口连 aw-server。

如果你改了 aw-server 端口或装到其他机器:

```yaml
# mcp_servers.yaml
- name: aw
  description: "ActivityWatch 活动查询"
  command: python
  args: ["-m", "app.mcp.activitywatch_server", "--aw-url", "http://192.168.1.100:5600"]
  enabled: true
```

或设环境变量 `AW_SERVER_URL=http://...`。

## 4. 工具清单

| MCP 工具 | 用途 | 例子 |
|---|---|---|
| `mcp_aw_get_recent_activities` | 过去 N 分钟的活动窗口 + 应用名 + 时长 | "主人过去 5 分钟在做什么?" |
| `mcp_aw_get_current_window` | 现在的活动窗口 | "主人现在在写代码吗?" |
| `mcp_aw_query` | 自定义 query (类似 ActivityWatch Web 里的 Query Builder) | "主人今天在 VSCode 待了多久?" |
| `mcp_aw_buckets` | 列出所有 bucket (watcher 写入的原始数据) | 调试用 |

工具**自带 3 种报错信息** (2026-10-15 v25 改):

- **aw-server 没运行**: 「ActivityWatch server 未运行, 请启动 aw-server (默认 5600 端口)」
- **没装 watcher**: 「ActivityWatch 已运行, 但未检测到 aw-watcher-window / afk 数据, 请安装对应 watcher」
- **watcher 装了但没在记**: 「watcher 在但最近 10 分钟无活动, 可能是空闲或 watcher 卡住」

LLM 能根据这 3 种提示告诉主人怎么修。

## 5. 验证

启动桌宠后, 打开任意聊天窗口问:

> 主人过去 5 分钟在做什么?

桌宠会调 `mcp_aw_get_recent_activities` 拉数据, 告诉你: 「主人过去 5 分钟在 [应用名], 待了 [时长]」。

如果回 "ActivityWatch server 未运行" → 装桌面客户端或起 aw-server。
如果回 "未检测到 aw-watcher" → 装对应 watcher。

## 6. 决策耦合 (跟创新点 2 联动)

mcp_aw_* 工具不在 `_RISKY_TOOLS` 里 (只是只读查询), 不会被 post-LLM 拦截器改写, 跟其他 SAFE 工具一样正常走 confirm / ALLOW 路径。
