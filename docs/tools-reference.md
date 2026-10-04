# Tools Reference · 桌宠 52 个工具全集

> 模型通过 **Function Calling** 协议（OpenAI 格式）调用这些工具。  
> 用户口语 → 模型选工具 → `ToolRegistry.execute(name, args_json)` → 字符串结果回给模型 → 模型组织自然语言回复。

---

## 总览

| 类别 | 工具数 | 危险 |
|------|--------|------|
| 一、信息查询 | 4 | — |
| 二、时间与提醒 | 6 | — |
| 三、搜索与记忆 | 4 | — |
| 四、计算与转换 | 3 | — |
| 五、应用与文件 | 11 | 2 |
| 七、系统控制 | 20 | 5 |
| **合计** | **52** | **7** |

> 六、桌宠自身（5 个）和 八、语音唤醒（非工具）独立章节。

---

## 一、信息查询（4）

### `get_current_time`
- **触发**："几点了 / 现在时间 / 现在几号"
- **作用**：返回当前时间（带日期、星期、上下文）

### `get_pet_status`
- **触发**："你怎么样 / 你的状态 / 你饿不饿 / 心情如何"
- **作用**：返回桌宠自身的数值状态（饱食/口渴/体力/心情/健康/好感/金币/等级）

### `system_info`
- **触发**："电脑配置 / 系统信息 / CPU 内存 / 用了多久"
- **作用**：系统信息（CPU / 磁盘 / 内存 / 操作系统 / 开机时长）

### `date_info`
- **触发**："今天几号 / 这周五是几号 / 农历"
- **作用**：返回任意日期的星期、农历、节气、距离今天的天数

---

## 二、时间与提醒（6）

> `add_reminder` 是分钟级精度；`countdown` 是秒级精度（最长 1 小时）。

### `add_reminder`
- **触发**："5 分钟后提醒我喝水 / 提醒我明天下午开会"
- **作用**：创建一次性提醒（`text` + `delay_minutes` 或 `at_time`）

### `list_reminders`
- **触发**："我有哪些提醒 / 列出待办"
- **作用**：列出所有待办提醒

### `delete_reminder`
- **触发**："取消提醒 3 / 删掉那个提醒"
- **作用**：按 `reminder_id` 删除指定提醒

### `countdown` *(秒级)*
- **触发**："10 秒后叫我 / 煮面 90 秒后叫我 / 3 分钟后提醒我"
- **作用**：秒级倒计时，到点桌宠主动说话 + Windows 系统通知
- **参数**：`seconds: float (0.1~3600)`，`message: str`（空则默认「时间到啦~」）

### `list_countdowns`
- **触发**："我有哪些倒计时"
- **作用**：列出当前所有活跃倒计时（id + 剩余秒数 + 标签）

### `cancel_countdown`
- **触发**："取消倒计时 5"
- **作用**：按 `counter_id` 取消指定倒计时

---

## 三、搜索与记忆（4）

### `web_search`
- **触发**："搜一下 / 搜索 / 查一下 / 帮我找 / 最新 XXX"
- **作用**：网络搜索，返回 Markdown 格式结果（标题/摘要/URL）
- **后端**：Tavily 主用（需 key）→ DuckDuckGo 降级
- **参数**：`query: str`，`max_n: int (1~10, 默认 5)`

### `remember_fact`
- **触发**："记住 XXX / 记一下"
- **作用**：写入一条长期记忆（带类别、重要性、访问热度）
- **参数**：`content: str`，`category: str` (fact/preference/person/event/routine)，`importance: float (0~1)`

### `recall_memory`
- **触发**："我之前让你记过 XXX 吗 / 我喜欢什么"
- **作用**：语义检索长期记忆（按 综合分 = 相关度 × 类别权重 × 时新度 × 重要性 排序）
- **参数**：`keyword: str`，`category: str`（空则全部）

### `forget_memory`
- **触发**："忘掉 XXX / 忘记那条"
- **作用**：删除关键词命中的全部记忆
- **参数**：`keyword: str`

---

## 四、计算与转换（3）

### `calculate`
- **触发**："算一下 / 等于多少 / sqrt(144)+25"
- **作用**：数学表达式求值（sympy 后端，支持 sqrt/log/三角/复数/求导）
- **参数**：`expression: str`

### `convert_units`
- **触发**："100 美元多少人民币 / 1 公里多少米 / 摄氏 38 转华氏"
- **作用**：单位换算（长度/质量/温度/数据/速度/时间/面积/体积/功率）
- **参数**：`value: float`，`from_unit: str`，`to_unit: str`

### `get_weather`
- **触发**："天气怎么样 / 北京今天冷吗 / 明天上海下雨吗"
- **作用**：当前天气 + 1~7 天预报（温度/湿度/风速/降水概率/天气码）
- **后端**：open-meteo（无需 key，host 白名单 `api.open-meteo.com`）
- **参数**：`location: str`（空则默认北京），`days: int (1~7)`

---

## 五、应用与文件（10）

### `open_app` ⚠️ 危险
- **触发**："打开 QQ / 启动 Chrome / 计算器"
- **作用**：查找并启动 Windows 应用（内置注册表 → 已装应用扫描 → 路径猜测 三层降级）
- **查找顺序**：内置映射（如"计算器"→`calc.exe`）→ 已装应用相似度搜索 → 完整路径
- **危险点**：可能启动非预期应用；需用户在弹窗里点确认

### `open_website`
- **触发**："打开 baidu.com / 访问 https://github.com/xxx"
- **作用**：调默认浏览器打开 URL（自动补 `https://`、空格转搜索）
- **host 校验**：走 `is_safe_url`（拒绝 localhost / 私有 IP）

### `open_file_explorer`
- **触发**："打开 D 盘 / 看下桌面 / 资源管理器"
- **作用**：打开文件管理器（可指定 `path` 参数）
- **参数**：`path: str`（空则桌面）

### `open_terminal`
- **触发**："打开终端 / PowerShell / cmd"
- **作用**：启动 PowerShell

### `open_notepad`
- **触发**："打开记事本 / 帮我记 XXX（可附文本）"
- **作用**：打开记事本（可传 `text` 参数自动填入）

### `list_installed_apps`
- **触发**："我装了什么 / 列出软件 / 看看有没有 Photoshop"
- **作用**：列出已装应用（缓存 5 分钟，可按 `query` 模糊过滤）
- **参数**：`query: str`，`limit: int (1~50, 默认 20)`

### `read_text_file`
- **触发**："读一下 /tmp/xxx / 看看那个文件内容"
- **作用**：读取纯文本（UTF-8，沙箱 = 家目录，>1 MB 拒，>max_chars 截断）
- **参数**：`path: str`，`max_chars: int (200~50000, 默认 8000)`
- **沙箱**：路径必须在用户主目录下

### `read_file` *(多格式)*
- **触发**："读一下 PDF / 看看那个 docx / 帮我看下这份报告"
- **作用**：按扩展名分发解析（PDF / Word / Excel / CSV / 图片 OCR / Markdown / HTML / 纯文本）
- **格式支持矩阵**：
  - `.pdf` → pypdf（pip install pypdf）
  - `.docx/.doc` → python-docx
  - `.xlsx/.xls` → openpyxl（只读 + data_only）
  - `.csv` → csv 模块（>200 行截断）
  - `.png/.jpg/.jpeg/.bmp/.gif/.webp` → OCR（复用 _ocr）
  - `.md/.markdown` → 纯文本
  - `.html/.htm` → bs4 抽正文
  - `.txt/.json/.yaml/.yml/.tsv` → 纯文本
- **沙箱**：路径必须在用户主目录下
- **参数**：`path: str`（空则按 `question` 在家目录搜），`question: str`

### `list_desktop_files`
- **触发**："桌面上有什么 / 桌面文件清单"
- **作用**：列出桌面文件（名称 + 类型 + 大小）

### `search_files`
- **触发**："找名字含 report 的文件 / 搜下 ~/Documents 里 report"
- **作用**：在指定目录递归搜文件名含 `keyword` 的文件
- **沙箱**：路径必须在用户主目录下
- **参数**：`keyword: str`，`directory: str`（空则桌面），`limit: int (1~500, 默认 50)`

### `fetch_url_text`
- **触发**："这个网页说的啥 / 抓一下 github.com/xxx/yyy"
- **作用**：抓取 URL 正文（readability-lxml 提取主文，无则降级去 HTML 标签）
- **host 校验**：IP 字面量走 `is_safe_url`；域名二次解析 IP 后再判断私有段（防 DNS rebinding）
- **参数**：`url: str`，`max_chars: int (200~20000, 默认 4000)`

---

## 六、桌宠自身（5）

### `feed_self`
- **触发**："喂你 / 给你吃苹果 / 投食 / 吃 XXX"
- **作用**：喂食物（消耗金币，按数值涨状态；冷却 30 秒）
- **参数**：`food_name: str`（食物库 `data/foods.json` 中的名称）

### `play_animation`
- **触发**："跳一下 / 转圈圈 / 伸懒腰"
- **作用**：播放一次动画（按渲染器能力匹配）

### `change_pet_emotion`
- **触发**："开心点 / 害羞 / 切到快乐"
- **作用**：切换表情（持久叠加状态）

### `say_to_user`
- **触发**："跟主人说 XXX / 帮我转告他"
- **作用**：让桌宠主动说话（不依赖对话流）

### `take_screenshot`
- **触发**："截图 / 截个屏 / 看看我在干嘛"
- **作用**：截全屏保存到 `data/screenshots/`，返回路径

---

## 七、系统控制（20）

### 7.1 TTS 与语音（4）

#### `set_tts_mute_until`
- **触发**："静音 / 闭嘴 / 别说话 / 安静一下"
- **作用**：静音 TTS N 分钟（最长 8 小时；回复只显示气泡，不朗读）
- **参数**：`minutes: float (0.1~480, 0 = 默认 30)`
- **典型场景**：开会 / 自习 / 不想被打扰

#### `unmute_tts`
- **触发**："取消静音 / 说话吧"
- **作用**：立即恢复 TTS

#### `get_tts_mute_status`
- **触发**："现在静音了吗"
- **作用**：查询静音状态 + 剩余分钟数

#### `ocr_image`
- **触发**："图里写的啥 / 识别这张图 / 桌面那段文字是啥"
- **作用**：OCR 识别图片中的文字
- **后端**：Windows Media.Ocr 优先 → Tesseract 兜底（需 `pip install pytesseract`）
- **参数**：`image_path: str`（空则自动截屏 + OCR）

### 7.2 音量与亮度（4）

#### `set_volume`
- **触发**："把音量调到 30 / 静音 / 大声点 / 30%"
- **作用**：系统主音量（0~100）
- **参数**：`level: int (0~100)`

#### `get_volume`
- **触发**："现在音量多少"
- **作用**：查询当前系统音量（百分比）

#### `set_brightness`
- **触发**："屏幕太亮 / 调暗一点"
- **作用**：屏幕亮度（Windows WMI，需管理员）
- **参数**：`level: int (0~100)`

#### `get_brightness`
- **触发**："现在亮度多少"
- **作用**：查询当前屏幕亮度

### 7.3 电源与硬件（3）

#### `get_battery`
- **触发**："笔记本还有多少电 / 电池状态"
- **作用**：Win32_Battery 当前电量 + 状态 + 续航（分钟）

#### `lock_screen` ⚠️ 危险
- **触发**："锁屏 / 帮我锁一下 / 我走了锁一下"
- **作用**：`rundll32 user32.dll,LockWorkStation`
- **危险点**：需用户在弹窗里点确认

#### `clear_bubble`
- **触发**："别说了 / 收起来 / 隐藏气泡"
- **作用**：隐藏桌宠头顶气泡（hook 注入实现）

### 7.4 进程与剪贴板（4）

#### `list_processes`
- **触发**："什么程序在跑 / 内存谁占最多 / 看下 chrome 进程"
- **作用**：进程列表（PID + 内存 + 名称，按 RSS 倒序）
- **参数**：`filter_name: str`（空则全部），`limit: int (1~200, 默认 30)`

#### `kill_process` ⚠️ 危险
- **触发**："结束 XXX / 杀进程 1234 / 关掉那个卡死的"
- **作用**：按 `pid` 或 `name` 杀进程
- **参数**：`pid: int`，`name: str`（二选一）
- **危险点**：可能杀掉关键进程；需用户确认

#### `get_clipboard`
- **触发**："我刚才复制了什么 / 读剪贴板"
- **作用**：读剪贴板当前内容（Windows CF_UNICODETEXT）

#### `clipboard_copy`
- **触发**："复制 XXX 到剪贴板"
- **作用**：写文本到剪贴板
- **参数**：`text: str`

### 7.5 无线开关（2）

#### `set_wifi` ⚠️ 危险
- **触发**："关掉 Wi-Fi / 开 Wi-Fi"
- **作用**：启用 / 禁用所有名称含 "Wi-Fi" 的网络适配器（NetAdapter cmdlet）
- **危险点**：系统级变更；需用户确认

#### `set_bluetooth` ⚠️ 危险
- **触发**："关掉蓝牙 / 开蓝牙"
- **作用**：通过 PnP 启用 / 禁用蓝牙类设备
- **危险点**：可能影响蓝牙鼠标 / 耳机；需用户确认

### 7.6 脚本执行（1）

#### `run_script` ⚠️ 危险
- **触发**："跑一下 ~/scripts/xxx.py / 执行我的那个脚本"
- **作用**：执行本地脚本（按扩展名派发解释器）
- **支持**：`*.py` (python) / `.ps1` (powershell) / `.bat/.cmd` (cmd) / `.sh` (bash)
- **沙箱**：脚本必须在用户主目录（含 `~/scripts/`）下；超时默认 60s（最多 300s）
- **参数**：`script_path: str`，`timeout_s: int (1~300)`，`args: str`（shlex 切分）
- **危险点**：任意 Python / Shell 执行；需用户确认

---

## 八、语音唤醒（非工具）

**模块名**：`WakeWordRecognizer`（`app/voice/wake_word.py`）

| 触发词 | 行为 |
|--------|------|
| "小汐" / "嗨小汐" / "hey 小汐" / "hey" | 桌宠冒泡"我在~"，接下来 8 秒录主人命令 |
| 8 秒内识别到命令 | 自动填入聊天框 + 自动发送给桌宠 |

**配置**：`settings.json` 里 `wake_word_enabled: true/false`（默认 True）

**实现**：
- 复用主 ASR 的 `faster-whisper` 模型（省内存）
- 后台线程持续录音，每 1.5s 切一片做唤醒检测
- CPU 开销约 5%~10%（base 模型 / int8）

**注意**：唤醒会让桌宠**立刻说话**回应"我在~"，静音场景下需要先关掉唤醒。

---

## 危险工具清单（7 个）

集中在 `app/brain/agent.py` 的 `DANGEROUS_TOOLS`：

```python
DANGEROUS_TOOLS = {
    "open_app", "open_website", "lock_screen", "kill_process",
    "run_script", "set_wifi", "set_bluetooth", "shutdown_computer",
}
```

执行流程：
1. AgentLoop 解析出 tool_calls
2. 若 tool_name ∈ DANGEROUS_TOOLS → 弹窗（由 `confirm_tool: Callable[[str, str], bool]` 注入）
3. 用户点"确认"才真执行；否则返回 "用户取消了此操作"

`shutdown_computer` 已在 DANGEROUS_TOOLS 集合里预留，本版本**未实现**对应工具函数（避免误触发），下一版再加。

---

## Host 校验（Mimosa 安全约束）

所有**对外发请求**的工具（`web_search` / `get_weather` / `fetch_url_text`）都走 `is_safe_url`：

- **协议**：仅允许 `http://` / `https://`（拒 `file://` / `ftp://` / `javascript:`）
- **白名单模式**：`allowed_hosts={...}` 时，host 不在白名单 → 直接拒
- **IP 字面量**：黑名单（`localhost` / `0.0.0.0` / `::1`）+ 段检查（私有 / 环回 / 保留 / link-local / multicast / unspecified）
- **域名**：先 `socket.gethostbyname` 解析，再判断 IP 段（防 DNS rebinding）

| 工具 | 校验策略 |
|------|---------|
| `web_search` | 硬编码 endpoint（Tavily / DDG），无 host 校验需要 |
| `get_weather` | 白名单 `api.open-meteo.com` / `geocoding-api.open-meteo.com` |
| `fetch_url_text` | 用户可控 URL：默认过 + 域名解析后段检查 |

---

## 沙箱限定

| 工具 | 沙箱根 |
|------|--------|
| `search_files` | 用户家目录 |
| `read_text_file` / `read_file` | 用户家目录 |
| `list_desktop_files` | 桌面 |
| `run_script` | 用户家目录（含 `~/scripts/`） |

路径解析后若不在沙箱内 → 直接拒（"安全策略拒绝"）。

---

## 添加工具（开发者指南）

每个工具是一个独立 Python 文件，模板：

```python
# app/engine/tools/_xxx.py
"""简短说明。"""
from __future__ import annotations

import logging
from ._core import Tool, ToolRegistry

log = logging.getLogger(__name__)


def register(reg: ToolRegistry) -> None:
    """注册工具。"""

    def my_tool(arg1: str, arg2: int = 10) -> str:
        """docstring 会成为模型能看到的 description。"""
        # 业务逻辑
        return "结果（必须是 str，失败时返回 "错误：xxx"）"

    reg.register(Tool(
        name="my_tool",
        description="用户问XXX时调用",
        parameters={
            "type": "object",
            "properties": {
                "arg1": {"type": "string", "description": "..."},
                "arg2": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["arg1"],
        },
        fn=my_tool,
    ))


__all__ = ["register"]
```

### 危险工具

如果工具涉及"开 / 关硬件 / 杀进程 / 执行脚本"，加入 `app/brain/agent.py` 的 `DANGEROUS_TOOLS` 集合，会自动走确认弹窗。

### 接入 `build_default_tools`

```python
# app/engine/tools/__init__.py
from . import ..., _xxx    # 加在 import 行

def build_default_tools(...):
    ...
    _xxx.register(reg)    # 加在 register 调用行
```

### 加测试

`tests/test_xxx.py`，覆盖：
1. `register(reg)` 不抛异常
2. 函数返回 string 类型
3. 参数错误返回带 "错误：" 前缀

---

## 文件位置

```
app/engine/tools/
├── __init__.py     # ToolRegistry + 统一注册 + is_safe_url
├── _core.py        # Tool / ToolRegistry / is_safe_url（核心类型 + host 校验）
├── _time.py        # 时间 / 桌宠状态
├── _reminder.py    # 提醒（增 / 删 / 查）
├── _memory.py      # 长期记忆（记住 / 回忆 / 忘掉）
├── _pet.py         # 桌宠自身（吃饭 / 播动画 / 表情 / 说话）
├── _math.py        # 数学 / 单位换算 / 日期
├── _file.py        # 文件 / 桌面
├── _shortcuts.py   # Windows 快捷（资源管理器 / 终端 / 记事本）
├── _system.py      # 系统（开应用 / 开网页 / 截图 / 剪贴板 / 通知 / 进程）
├── _search.py      # 网络搜索（Tavily / DDG）
├── _audio.py       # 音量 / 亮度
├── _weather.py     # 天气
├── _timer.py       # 秒级倒计时
├── _power.py       # 电池 / 锁屏 / 进程 / 无线 / 剪贴板 / 清气泡
├── _filesearch.py  # 沙箱文件搜索
├── _webfetch.py    # URL 正文抓取（host 校验）
├── _ocr.py         # 图片 OCR
├── _runner.py      # 本地脚本执行（沙箱）
└── _tts_mute.py    # TTS 静音开关
```

| 内容 | 文件 |
|------|------|
| 工具核心类型（Tool / ToolRegistry / is_safe_url） | `app/engine/tools/_core.py` |
| 工具注册入口 | `app/engine/tools/__init__.py` |
| 各工具模块 | `app/engine/tools/_*.py` |
| 危险工具集合 | `app/brain/agent.py`（`DANGEROUS_TOOLS`） |
| 语音唤醒 | `app/voice/wake_word.py` |
| 单元测试 | `tests/test_*.py` |

## 统计

- **52 个工具**（不含语音唤醒） + 1 个语音模块
- **7 个危险**（需弹窗确认）
- **9 个新工具模块**（`_tts_mute` / `_audio` / `_weather` / `_timer` / `_power` / `_filesearch` / `_webfetch` / `_ocr` / `_runner`）
- **23 个新增测试用例**（`tests/test_new_tools.py`）