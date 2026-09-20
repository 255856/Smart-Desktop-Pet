# Tool Catalog · 31 个工具分组

所有工具在 `app/engine/tools/` 下按域分文件注册：

```
app/engine/tools/
├── __init__.py     # ToolRegistry + 统一注册
├── _core.py        # 通用基础
├── _file.py        # 文件 / 桌面
├── _math.py        # 数学 / 单位换算 / 日期
├── _memory.py      # 长期记忆（记住 / 回忆 / 忘掉）
├── _pet.py         # 桌宠自身（吃饭 / 播动画 / 表情 / 说话）
├── _reminder.py    # 提醒（增 / 删 / 查）
├── _shortcuts.py   # Windows 快捷（任务管理器 / 控制面板 / 资源管理器 等）
├── _system.py      # 系统（开应用 / 开网页 / 截图 / 剪贴板 / 通知）
└── _time.py        # 时间 / 桌宠状态
```

## time / reminder / memory

| 工具 | 参数 | 说明 |
|------|------|------|
| `get_current_time` | `timezone: str` | 当前时间（默认本地） |
| `get_pet_status` | — | 桌宠等级 / 饱腹 / 心情 / 经验 |
| `add_reminder` | `text: str`, `delay_minutes: int` | 几分钟后提醒 |
| `list_reminders` | — | 列出全部提醒 |
| `delete_reminder` | `reminder_id: str` | 删除提醒 |
| `remember_fact` | `text: str`, `importance: float` | 长期记住 |
| `recall_memory` | `query: str` | 向量检索记忆 |
| `forget_memory` | `memory_id: str` | 忘掉一条 |

## pet（影响桌宠本体）

| 工具 | 参数 | 说明 |
|------|------|------|
| `feed_self` | `food: str` | 桌宠吃饭（扣饱腹、播动画） |
| `play_animation` | `anim_name: str` | 一次性动作（jump/stretch/spin/eat/file/swim） |
| `change_pet_emotion` | `emotion: str` | 切情绪表情（happy/sad/angry/...） |
| `say_to_user` | `text: str` | 桌宠主动说话 + TTS 朗读 |

## system

| 工具 | 参数 | 说明 |
|------|------|------|
| `open_website` | `url: str` | 浏览器开网页 |
| `open_app` | `name: str` | 启动已装应用（按 `installed_apps.py` 映射） |
| `list_installed_apps` | — | 列出可启动的应用 |
| `system_info` | — | CPU / 内存 / 磁盘 |
| `take_screenshot` | `path: str` | 截屏保存到 path |
| `clipboard_copy` | `text: str` | 写剪贴板 |
| `send_notification` | `title: str`, `body: str` | Windows 通知 |

## math / file / shortcuts

| 工具 | 参数 | 说明 |
|------|------|------|
| `calculate` | `expression: str` | 数学计算 |
| `convert_units` | `value: float`, `from_unit: str`, `to_unit: str` | 单位换算 |
| `date_info` | — | 当前日期 / 星期 / 是否节假日 |
| `list_desktop_files` | — | 桌面文件清单 |
| `read_text_file` | `path: str` | 读文本文件（沙箱内） |
| `open_task_manager` | — | 打开任务管理器 |
| `open_control_panel` | — | 打开控制面板 |
| `open_windows_settings` | — | 打开 Windows 设置 |
| `open_file_explorer` | — | 打开资源管理器 |
| `open_terminal` | — | 打开 cmd |
| `open_notepad` | — | 打开记事本 |
| `open_calculator` | — | 打开计算器 |

## 危险工具（需谨慎）

无 —— 项目策略是「LLM 调用前 force_tool_use」，危险工具在 system prompt 中显式列出，需要时另行加灰度开关。