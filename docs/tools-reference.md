# Tools Reference · 桌宠 77 个工具全集

> 模型通过 **Function Calling** 协议（OpenAI 格式）调用这些工具。  
> 用户口语 → 模型选工具 → `ToolRegistry.execute(name, args_json)` → 字符串结果回给模型 → 模型组织自然语言回复。

---

## 总览

| 类别 | 工具数 | 危险 |
|------|--------|------|
| 一、信息查询 | 4 | — |
| 二、时间与提醒 | 6 | — |
| 三、搜索、记忆与黑话 | 7 | — |
| 四、计算与转换 | 3 | — |
| 五、应用与文件 | 15 | 0 |
| 六、桌宠自身 | 6 | — |
| 七、系统控制 | 20 | 5 |
| 八、MCP 外部工具（ActivityWatch 4 + 文件系统 3 + 语音 3） | 10 | — |
| 九、生成 / 委派 | 4 | 0 |
| **合计** | **77** | **5** |

> 十、语音唤醒（非工具）独立章节。

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

## 三、搜索、记忆与黑话（7）

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

### `query_slang`
- **触发**："XX 是什么意思" / 想用某个梗但没把握
- **作用**：查**已收录**（confirmed）的黑话 / 口头禅表 —— 主人确认过的词，模型可以直接用
- **参数**：`keyword: str`（留空 = 列出全部）、`limit: int`（默认 10）、`status: str`（confirmed / candidate / rejected / all，默认 confirmed）
- **存储**：`data/slang.json`（**词表**，不走记忆 sidecar）
- **风险**：SAFE（只读）

### `submit_slang`
- **触发**：主人反复在用、而模型不认识的词（黑话 / 口头禅 / 网络用语）
- **作用**：把新词提交成**候选**（candidate）。**候选要主人确认才算收录**，确认前模型不许用这个词
- **该提交**：同一个词在对话里反复出现（≥ 2 次），而且模型确实没把握
- **不该提交**：打错的字、只出现一次的临时说法、人名 / @ / 表情包 / 纯标点、含义本来就清楚的普通词
- **参数**：`content: str`（必填）、`meaning: str` / `usage: str` / `example: str` / `risk: str` / `evidence: str`（均可选）
- **风险**：MEDIUM（写本地文件 `data/slang.json`）

### `browser`

浏览器自动化的**网关工具**（playwright 那 25 个工具不原生下发，只经它按需调用）。

- 风险等级：**MEDIUM**（危险动作 `browser_run_code_unsafe` 已在网关里直接拒绝 ✓）
- 用法：`browser(action="browser_navigate", args={"url": "https://…"})`
- `action` 取 playwright 的动作名（`browser_navigate` / `browser_click` / `browser_type` / `browser_snapshot` / `browser_take_screenshot` / `browser_evaluate` / `browser_press_key` …）
- 为什么走网关：那 25 个工具的 schema ≈ **9.5k token**（占全部工具的 41%）→ 原生下发会让每段对话的首轮多付 9.5k，工具集一变缓存就失效 ✓
- 解锁等级：**Lv9**（跟 `mcp_levels.playwright` 一致）
### MCP 外部工具（playwright / fetch）

| 工具 | 说明 |
|---|---|
| `mcp_fetch_fetch` | 网页正文抓取 → Markdown |
| `mcp_playwright_browser_click` | Playwright 浏览器自动化（用系统 Edge） |
| `mcp_playwright_browser_close` | Playwright 浏览器自动化（用系统 Edge） |
| `mcp_playwright_browser_console_messages` | Playwright 浏览器自动化（用系统 Edge） |
| `mcp_playwright_browser_drag` | Playwright 浏览器自动化（用系统 Edge） |
| `mcp_playwright_browser_drop` | Playwright 浏览器自动化（用系统 Edge） |
| `mcp_playwright_browser_emulate_media` | Playwright 浏览器自动化（用系统 Edge） |
| `mcp_playwright_browser_evaluate` | Playwright 浏览器自动化（用系统 Edge） |
| `mcp_playwright_browser_file_upload` | Playwright 浏览器自动化（用系统 Edge） |
| `mcp_playwright_browser_fill_form` | Playwright 浏览器自动化（用系统 Edge） |
| `mcp_playwright_browser_find` | Playwright 浏览器自动化（用系统 Edge） |
| `mcp_playwright_browser_handle_dialog` | Playwright 浏览器自动化（用系统 Edge） |
| `mcp_playwright_browser_hover` | Playwright 浏览器自动化（用系统 Edge） |
| `mcp_playwright_browser_navigate` | Playwright 浏览器自动化（用系统 Edge） |
| `mcp_playwright_browser_navigate_back` | Playwright 浏览器自动化（用系统 Edge） |
| `mcp_playwright_browser_network_request` | Playwright 浏览器自动化（用系统 Edge） |
| `mcp_playwright_browser_network_requests` | Playwright 浏览器自动化（用系统 Edge） |
| `mcp_playwright_browser_press_key` | Playwright 浏览器自动化（用系统 Edge） |
| `mcp_playwright_browser_resize` | Playwright 浏览器自动化（用系统 Edge） |
| `mcp_playwright_browser_run_code_unsafe` | Playwright 浏览器自动化（用系统 Edge） |
| `mcp_playwright_browser_select_option` | Playwright 浏览器自动化（用系统 Edge） |
| `mcp_playwright_browser_snapshot` | Playwright 浏览器自动化（用系统 Edge） |
| `mcp_playwright_browser_tabs` | Playwright 浏览器自动化（用系统 Edge） |
| `mcp_playwright_browser_take_screenshot` | Playwright 浏览器自动化（用系统 Edge） |
| `mcp_playwright_browser_type` | Playwright 浏览器自动化（用系统 Edge） |
| `mcp_playwright_browser_wait_for` | Playwright 浏览器自动化（用系统 Edge） |

> **等级**：playwright 的 25 个工具 schema 约 **9.5k token**（占全部工具的 41%），
> 默认锁 **Lv9**（`companion_config.yaml` 的 `mcp_levels.playwright`），平时不进 prompt；
> 想让它一直可用就把它改成 `1` —— 代价是每轮多 ~9.5k token。
> fetch 只有 1 个工具，跟默认一样 Lv5 解锁。
### `use_skill`

取一个**技能**（行为覆盖包）来用。`skills/` 目录里每个 `.md` 就是一个技能
（例如 `domain-modeling` 建模、`systematic-debugging` 排障、`poem` 用诗回话）。
模型觉得任务对味就自己调它，工具把这轮该守的规矩原样返回。

- 风险等级：**LOW**（只读 `skills/*.md`，不改任何东西）
- 不带名字调用 → 返回可用技能清单
- 主人手动激活仍然支持：`/skills/<名字>`（聊天开头输入即可）
### `update_self`
- **触发**：她想调整**自己**（而不是记主人的事）—— 「我是谁 / 我在乎什么 / 我做事的套路」变了
- **作用**：改自己的三个**记忆分区**（跟 话 / 话题 / 事件 / 事实·偏好 / 印象 平级）：
  - `identity` = 自我认知（我是谁、我跟主人什么关系）
  - `value` = 价值观（level：底线 / 偏好 / 风格）
  - `procedural` = 程序性（做某事的固定套路、行事风格与目标）
- **参数**：`region: str`（identity/value/procedural，必填）、`content: str`（必填）、
  `action: str`（add / revise / remove，默认 add）、`target: str`（revise/remove 的目标 id）、
  `level: str`（仅 value：bottom_line/preference/style）、`reason: str`（为什么改，给人看）
- **改写**：用 `revise` + 旧 id → 旧条目标记过时（**不是删**），主人能在面板上看到演变过程
- **限速**：每个区域**每天最多改 2 次** —— 人格是慢慢长出来的，别每轮重写自己
- **怎么进会话**：身份 / 价值进 **system prompt 稳定段**（内容不变就逐字节相同，不掉前缀缓存）；
  程序性条目多 → 走**易变段按需召回**
- **风险**：MEDIUM（写长期记忆）

> **已收录词条会进 system prompt**（【已收录黑话】段，**只有 confirmed**）：
> 模型按语境自然用这些词，不确定就别硬塞，没收录的不用。
> 候选（candidate）**不进**提示词。主人确认的办法：把 `data/slang.json` 里
> 那条的 `status` 改成 `confirmed`。

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

## 五、应用与文件（15）

### `open_app` ⚠️ LOW
- **触发**："打开 QQ / 启动 Chrome / 计算器"
- **作用**：查找并启动 Windows 应用（内置注册表 → 已装应用扫描 → 路径猜测 三层降级）
- **查找顺序**：内置映射（如"计算器"→`calc.exe`）→ 已装应用相似度搜索 → 完整路径
- **危险点**：可能启动非预期应用（LOW 档，不弹确认框）

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

### `edit_file`
- **触发**：把 xxx.txt 里的 foo 改成 bar
- **作用**：精确替换文件里的一段文本（原子写；old_string 默认必须唯一匹配，防误伤）
- **参数**：`path: str`，`old_string: str`，`new_string: str`（`replace_all: bool` 全替换，`dry_run: bool` 只看 diff 不写盘）
- **沙箱**：路径必须在用户主目录下

### `multi_edit`
- **触发**：把这几处都改一下
- **作用**：对同一个文件做多处精确替换（事务式：全成功或全失败，任一错就回滚）
- **参数**：`path: str`，`edits: list`（每项含 old_string + new_string，可选 replace_all），`dry_run: bool`
- **沙箱**：路径必须在用户主目录下

### `list_desktop_files`
- **触发**："桌面上有什么 / 桌面文件清单"
- **作用**：列出桌面文件（名称 + 类型 + 大小）

### `search_files`
- **触发**："找名字含 report 的文件 / 搜下 ~/Documents 里 report"
- **作用**：在指定目录递归搜文件名含 `keyword` 的文件
- **沙箱**：路径必须在用户主目录下
- **参数**：`keyword: str`，`directory: str`（空则桌面），`limit: int (1~500, 默认 50)`

### `grep_files`
- **触发**：在项目里搜一下 xxx
- **作用**：递归按正则匹配文件内容，输出 `file:行号:内容`（比逐个 read_file 快得多）
- **参数**：`pattern: str`（Python re 语法），`directory: str`（空则桌面），`include: str`（文件名 glob 过滤，如 *.py），`max_results: int`，`case_insensitive: bool`
- **沙箱**：限定在用户主目录下；超过 2 MiB 的文件自动跳过

### `glob_files`
- **触发**：找一下所有 *.png
- **作用**：按 glob 路径模式（如 `src/**/*.py`）找文件，按修改时间倒序（比 search_files 更精确）
- **参数**：`pattern: str`（支持 ** 递归），`directory: str`（空则桌面），`max_results: int`，`include_dirs: bool`
- **沙箱**：限定在用户主目录下

### `fetch_url_text`
- **触发**："这个网页说的啥 / 抓一下 github.com/xxx/yyy"
- **作用**：抓取 URL 正文（readability-lxml 提取主文，无则降级去 HTML 标签）
- **host 校验**：IP 字面量走 `is_safe_url`；域名二次解析 IP 后再判断私有段（防 DNS rebinding）
- **参数**：`url: str`，`max_chars: int (200~20000, 默认 4000)`

---

## 六、桌宠自身（6）

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

### `send_sticker`
- **触发**："发个表情 / 发表情包 / 来张图" / 想表达情绪又不想写一大段话
- **作用**：把桌宠自己的表情当**一条消息**发进聊天 —— 聊天窗里是一张独立的表情气泡，主人点一下能看大图
- **参数**（都不给就随手发一张）：`sticker_id: str`（形如 `happy_1`）、`emotion: str`（happy 开心 / sad 难过 / anger 生气 / fear 害怕 / doubt 狐疑 / pride 骄傲 / shy 害羞 / surprise 惊讶）
- **素材**：只用仓库里**已有**的 `assets/sprites/Emotion_*` 帧（**不下载、不生成任何素材**）；共 8 种情绪 × 4 张 = 32 张
- **成本**：只查本地目录，不调模型、不联网
- **风险**：SAFE（只读素材 + 在聊天里发一条图）

### `take_screenshot_tool`
- **触发**："截图 / 截个屏 / 看看我在干嘛"
- **作用**：截全屏保存到 `data/screenshots/`，返回路径

---

## 七、系统控制（20）

### 7.1 TTS、语音与图像理解（5）

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

#### `understand_image`
- **触发**：这图里是什么 / 描述一下这张图
- **作用**：理解图片整体内容（主体 / 场景 / 含义），走多模态 chat completion
- **区别**：`ocr_image` 专提取图中文字，本工具不需要装 tesseract / winsdk
- **参数**：`image_path: str`（本地图片路径），`prompt: str`（要问的问题，默认「描述图片内容」）

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

### 7.4 进程、剪贴板与通知（5）

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

#### `send_notification`
- **触发**：弹个通知提醒我
- **作用**：发 Windows 系统通知（右下角弹窗）
- **参数**：`title: str`，`message: str`
- **依赖**：需要装 win10toast（`pip install win10toast`），没装会如实回报

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

## 八、MCP 外部工具（10）

> 这些不是内置工具，是**运行时**由 MCP（Model Context Protocol）server 桥接进来的，
> 命名规则 `mcp_<server 别名>_<工具名>`（app/mcp/protocol.py）。
> 三个 server：`aw`（ActivityWatch，4 个工具）+ `fs`（文件系统沙箱，3 个工具）
> + `voice`（本机语音，3 个工具）。

### 8.1 ActivityWatch 活动感知（4）

> 需装 [ActivityWatch](https://activitywatch.net/) 桌面客户端（5600 端口）。详见 [activitywatch-integration.md](activitywatch-integration.md)。

### `mcp_aw_aw_get_recent_activities`
- **触发**：「过去 5 分钟我在做什么 / 刚才用了什么 app」
- **作用**：调 aw-server `/api/0/.../events` 拉最近 N 分钟 window 事件，按 app 聚合
- **参数**：`minutes: int (1~120)`，`limit: int (1~20)`
- **报错**：
  - server 没跑 → 「ActivityWatch server 未运行, 请启动 aw-server (默认 5600 端口)」
  - 没装 watcher → 「ActivityWatch 已运行, 但未检测到 aw-watcher-window / afk 数据, 请装 watcher」
  - watcher 卡住 → 「watcher 在但最近 10 分钟无活动」

### `mcp_aw_aw_get_current_window`
- **触发**：「我现在在做什么 / 主人在哪个窗口」
- **作用**：拉最近 1 个 window 事件
- **报错**：同上

### `mcp_aw_aw_get_browser_tabs`
- **触发**：「我最近看了什么网页 / 我刚逛了哪些网站」
- **作用**：拉最近 N 分钟访问过的浏览器标签页（标题 + URL + 时长）
- **参数**：`minutes: int`（默认 30），`limit: int`（默认 10）
- **报错**：同上

### `mcp_aw_aw_status`
- **触发**：「AW 还能用吗 / aw-server 在跑吗」
- **作用**：查 aw-server 是否在运行（健康检查，问 AW 之前先查它）
- **报错**：server 没跑时报同上

### 8.2 文件系统沙箱（3）

> 内置的 filesystem MCP server（`app/mcp/filesystem_server.py`），
> 只允许访问 `mcp_servers.yaml` 里 `--root` 指定的沙箱根目录（默认项目目录）。

### `mcp_fs_fs_read_file`
- **触发**：看下这个文件里写了什么
- **作用**：读一个文本文件（最多 2000 字符），仅沙箱目录内
- **参数**：`path: str`（沙箱内路径）

### `mcp_fs_fs_list_dir`
- **触发**：这个目录里有哪些文件
- **作用**：列出一个目录里的文件，仅沙箱目录内
- **参数**：`path: str`（沙箱内路径）

### `mcp_fs_fs_search_files`
- **触发**：沙箱里找一下所有 *.py
- **作用**：在沙箱内按 glob 模式搜索文件
- **参数**：`pattern: str`（glob 模式），`root: str`（沙箱内搜索根目录）

### 8.3 本机语音（3）

> 内置的 voice MCP server（`app/mcp/voice_server.py`）。**只复用本机已经装好的模型**：
> 合成走本机 GPT-SoVITS 服务（默认 `http://127.0.0.1:9880`），唱歌走本机 RVC 权重
> （默认 `da_fei_yu.pth`，配在 `app/core/companion_config.yaml` 的 `voice.rvc` 段）。
> **绝不下载任何模型**：本机没有的音色，工具会如实说「音色不在本地」。
>
> 语音是**桌宠自己决定**要不要发（默认不朗读每条回复；开关 `voice.auto_read`）。
> 措辞铁律：语音条前面不加方括号标签；唱的是按这段词现编的调，不许自称原创歌曲；
> 工具失败了就说失败，不许假装已经发出。

### `mcp_voice_speak`
- **触发**：「念给我听」「哼一声」/ 只想丢一句很短的俏皮话过去
- **作用**：用本机音色把一句话念出来，在本机播放并落盘到 `data/voice_out/`
- **参数**：`text: str`（必填，一两句最好）、`emotion: str`（可选，中性/愤怒/惊讶/悲伤/紧张/恐惧/轻声/耳语）、`speed: float`（0.5~2.0，默认 1.0）
- **报错**：
  - 本机没音色 → 「失败：音色不在本地：…（本工具不会下载任何模型）。」
  - TTS 服务没起 → 「失败：合成失败：TTS 服务 … 没响应（…）。」

### `mcp_voice_sing`
- **触发**：「唱一句」「唱一段」/ 主人点名要听歌
- **作用**：用本机 RVC 按旋律唱一段歌词（每个字配一个音高并在字内保持住，所以是唱不是念），在本机播放并落盘
- **参数**：`lyrics: str`（必填，给文本，别写曲谱）、`speed: float`（0.5~1.5，默认 0.92）、`seed: int`（可选，换一个数就是另一条旋律；不给就随机）、`base: float`（可选，旋律基准音高 Hz，默认 329.63）
- **报错**：
  - 本机没 RVC 资产 → 「失败：RVC 解释器不在本机：… / 音色不在本地：…（不会自动下载模型权重）。」
  - RVC 没产出音频 → 「失败：唱歌失败：没有产出音频。RVC 返回码 …，最后一行：…」

### `mcp_voice_status`
- **触发**：不确定本机语音能不能用 / 先查再唱
- **作用**：只读盘点 TTS 服务在线与否、RVC 解释器 / 脚本 / 音色权重、本机情绪参考库
- **参数**：无
- **报错**：不报错；缺的东西会直接写明「音色不在本地」并声明不会下载

ActivityWatch 与文件系统那 7 个工具**不在** `_RISKY_TOOLS` 里（只读查询），
不被 post-LLM 拦截器改写，跟其他 SAFE 工具一样正常走 confirm / ALLOW 路径；
语音那 3 个工具会在本机播放并写音频文件（只写 `data/voice_out/`），同样不在 `_RISKY_TOOLS` 里。

---

## 九、生成 / 委派（4）

### `generate_image`
- **触发**：画一张猫的图
- **作用**：按 prompt 生成图片，返回 http(s) URL（落盘后给本地 file:// 路径）
- **参数**：`prompt: str`，`size: str`（如 774x774）
- **后端**：config.yaml 的 `multimodal` 段（base_url / api_key / image_model）

### `generate_video`
- **触发**：生成一段 5 秒的短视频
- **作用**：按 prompt 生成短视频（异步任务 + 计费高 → 危险级）
- **参数**：`prompt: str`，`duration_seconds: int`
- **后端**：config.yaml 的 `multimodal` 段（base_url / api_key / video_model）

### `generate_audio`
- **触发**：把这段话做成语音 / 生成一段音乐
- **作用**：按 prompt 生成音频 / 音乐，返回 http(s) URL
- **参数**：`prompt: str`
- **后端**：config.yaml 的 `multimodal` 段（base_url / api_key / audio_model）

### `Task`
- **触发**：帮我搜一下 X 然后总结这类不想开新对话的活
- **作用**：派生子 agent 跑一个隔离的小任务，返回最终文字结果（独立上下文，不污染主对话）
- **参数**：`task: str`，`agent_type: str`（general / explore …，目前只是语义标签）

## 十、语音唤醒（非工具）

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

## 危险工具清单（25 个）

集中在 `app/engine/tools/_risk.py` 的 `TOOL_RISK_LEVEL`（5 档：SAFE / LOW / MEDIUM / HIGH / IRREVERSIBLE），自动派生 `_RISKY_TOOLS`（≥ MEDIUM）。`backend=lightweight` 走 `app/brain/agent.py` 的 `DANGEROUS_TOOLS` 弹窗拦截，`backend=standard` 走 `app/brain/hallucination_guard.RiskyToolConfirm` 中间件 + `PostLLMStateGuard` post-LLM 拦截器。

弹窗 confirm 的 6 个：

```python
DANGEROUS_TOOLS = {"lock_screen", "kill_process", "run_script",
                   "set_wifi", "set_bluetooth", "shutdown_computer"}
```

只打 WARNING log 不弹窗的 19 个：`add_reminder` / `clipboard_copy` / `delete_file` / `delete_reminder` / `edit_file` / `forget_memory` / `generate_audio` / `generate_image` / `generate_video` / `multi_edit` / `remember_fact` / `send_email` / `send_notification` / `set_brightness` / `set_tts_mute_until` / `set_volume` / `submit_slang` / `Task` / `update_self`（owner 自己可控，但 _RISKY_TOOLS 集合里）。

执行流程（`backend=lightweight`）：

1. AgentLoop 解析出 tool_calls
2. 若 tool_name ∈ DANGEROUS_TOOLS → 弹窗（由 `confirm_tool: Callable[[str, str], bool]` 注入）
3. 用户点"确认"才真执行；否则返回 "用户取消了此操作"

执行流程（`backend=standard` LangChain）：

1. `RiskyToolConfirm` 中间件走 `lookup_decision(state, tool)` 查表 → ALLOW / CONFIRM / REJECT / REFUSE
2. CONFIRM → 弹窗；REJECT → 拒绝 ToolMessage（不调工具）；ALLOW → 放行；REFUSE → 任务级标 failure
3. `PostLLMStateGuard` 在 LLM 输出后兜底：state 差 + LLM preemptive 问 → 改写 `ModelResponse.result[0].content = REFUSAL_TEXT` + 清 `tool_calls`

`shutdown_computer` 已预留，本版本**未实现**对应工具函数（避免误触发），下一版再加。完整机制详见 [innovation-2-companion-policy.md](innovation-2-companion-policy.md)。

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

- **77 个工具**（不含语音唤醒） = 67 个内置 + 10 个 MCP（4 个 ActivityWatch + 3 个 filesystem + 3 个语音）+ 1 个语音模块
- **6 个弹窗 confirm** + 19 个只 log 不弹窗 = 25 个 `_RISKY_TOOLS` 成员
- **9 个新工具模块**（`_tts_mute` / `_audio` / `_weather` / `_timer` / `_power` / `_filesearch` / `_webfetch` / `_ocr` / `_runner`）
- **2400 个测试**（含 19+ 个创新点 2 决策 / 注入 / post-LLM 拦截器守卫）