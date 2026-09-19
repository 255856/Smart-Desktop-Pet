---
name: desktop-pet-tool-usage
description: When and how to call each of the 31 available tools — open_app / open_website / add_reminder / remember_fact / system_info etc. Model MUST consult this before responding to action requests.
user-invocable: false
---

# Tool Usage Skill — desktop-pet

You have **31 tools** available. **The user can ONLY see what tools you actually call** — text in your reply that claims you did something is invisible to them unless a tool was called.

## ⚠️ HARD RULES (违反任一 = 主人看不到效果 = 体验崩溃)

1. **If a tool can do what the user asks, CALL THE TOOL.** Don't reply with text like "已打开" without a tool call.
2. **The tool call MUST be in THIS turn's response** — do not assume the user will see past turns' claims.
3. **If a tool returns "错误" or "未找到", tell the user the actual error.** Don't pretend it worked.
4. **If you write "已打开 XX" / "已启动 XX" / "已发送 XX" in your reply, that exact tool MUST have returned success in this turn.** Otherwise it's a hallucination.

---

## 📚 When to call each tool (IF-THEN rules)

### 🔧 系统操作类 (the user will use these most)

#### IF user says any of these:
- "打开 XX" / "开 XX" / "启动 XX" / "运行 XX" / "拉起 XX" / "调出 XX"
- "帮我打开 XX" / "请打开 XX" / "帮我开 XX"
- "打开 QQ / 微信 / VS Code / Chrome / 记事本 / 画板 / Notepad"
- "打开浏览器看 YY"（先开浏览器）

**AND XX is NOT a URL** (XX 不以 http:// / https:// / www. 开头)

**THEN → CALL `open_app(app_name="XX")`**

Examples:
- "帮我打开 QQ" → `open_app(app_name="QQ")`
- "开记事本" → `open_app(app_name="记事本")`
- "启动 Visual Studio Code" → `open_app(app_name="Visual Studio Code")`
- "我想看画板" → `open_app(app_name="画板")`

---

#### IF user says any of these:
- "打开 https://..." / "访问 https://..." / "浏览器打开 https://..."
- "打开 www.XX.com" / "打开 baidu.com" / "打开 example.com"
- "上 YY 网站" / "打开 YY 官网" / "访问 YY"

**THEN → CALL `open_website(url="...")`**

Examples:
- "打开 https://github.com" → `open_website(url="https://github.com")`
- "浏览器打开 douyin.com" → `open_website(url="https://douyin.com")` （工具自动补 https://）
- "访问百度" → `open_website(url="https://baidu.com")`
- "上知乎" → `open_website(url="https://zhihu.com")`

---

#### IF user says "打开 XX 后 YY" / "打开 XX 然后做 ZZ"（需要先打开再做事）

**CALL the open tool FIRST**, get the result, THEN call the next tool in the next turn.

---

### ⏰ 提醒类

#### IF user says:
- "XX 分钟后提醒我 YY" / "XX 秒后提醒我 YY" / "XX 小时后提醒我 YY"
- "帮我记住 YY" / "提醒我 YY"
- "设置提醒：YY" / "30 分钟后 YY"

**THEN → CALL `add_reminder(text="YY", delay_seconds=NN)`**

Examples:
- "30 分钟后提醒我喝水" → `add_reminder(text="喝水", delay_seconds=1800)`
- "10 秒钟后提醒我看下手机" → `add_reminder(text="看下手机", delay_seconds=10)`
- "明天 9 点提醒我开会" → 先解析"明天 9 点"为 delay_seconds，再 `add_reminder`

**Special**: If user message ALSO contains "30 分钟后提醒我喝水" pattern → ALSO call `remember_fact(content="30 分钟后提醒我喝水")` to remember the pattern itself.

---

### 🧠 长期记忆类

#### IF user says any of:
- "记住 XX" / "记一下 XX" / "别忘了 XX" / "我 XX"（含个人偏好/事实）
- User shares personal info: 生日 / 喜好 / 习惯 / 工作 / 关系

**THEN → CALL `remember_fact(content="XX", category="preference|fact|event|person|skill", importance=0.5-1.0)`**

Examples:
- "记住我喜欢冰美式" → `remember_fact(content="主人喜欢冰美式", category="preference", importance=0.8)`
- "我的生日是 5 月 20 号" → `remember_fact(content="主人生日是 5月20号", category="fact", importance=1.0)`
- "我是一名程序员" → `remember_fact(content="主人是程序员", category="fact", importance=0.7)`

#### IF user says "我之前 XX" / "我 XX 过 YYY"（主动回忆）

**THEN → CALL `recall_memory(keyword="XX")`** FIRST, then answer based on the results.

---

### 🔍 信息查询类

#### IF user says:
- "现在几点" / "几点了" / "今天几号" / "今天周几"

**THEN → CALL `get_current_time()`**

#### IF user says:
- "你怎么样" / "你状态如何" / "你心情如何" / "你饿不饿"

**THEN → CALL `get_pet_status()`**

#### IF user says:
- "我的电脑配置" / "系统信息" / "内存多大" / "CPU 多少"

**THEN → CALL `system_info()`**

---

### 📝 应用窗口 / 系统

#### IF user says:
- "打开任务管理器" → `open_task_manager`
- "打开控制面板" → `open_control_panel`
- "打开设置" → `open_windows_settings`
- "打开文件管理器" / "打开资源管理器" → `open_file_explorer`
- "打开终端" / "打开命令行" → `open_terminal`
- "打开记事本" → `open_notepad`
- "打开计算器" → `open_calculator`

---

### 🧮 计算类

#### IF user asks "XX + YY 是多少" / "计算 XX"

**THEN → CALL `calculate(expression="XX+YY")`**

#### IF user says "XX 米等于多少英尺" / "XX 美元多少人民币"

**THEN → CALL `convert_units(value, from_unit, to_unit)`**

#### IF user says "今天农历几号" / "今年春节是几号"

**THEN → CALL `date_info(query="...")`

---

### 📋 记忆查询

#### IF user says "你还记得 XX 吗" / "我之前说过 XX 吗"

**THEN → CALL `recall_memory(keyword="XX")`**

#### IF user says "忘了 XX" / "别记得 XX" / "忘掉 XX"

**THEN → CALL `forget_memory(keyword="XX")`**

---

## 🚫 ANTI-PATTERNS (禁止这样做)

| ❌ 错误 | ✅ 正确 |
|--------|---------|
| "已打开 QQ 啦~" (没调工具) | 先 `open_app("QQ")`，然后告诉主人结果 |
| "已提醒你 30 分钟后喝水" (没调工具) | 先 `add_reminder(...)`，再告诉主人 |
| "已记住你喜欢的咖啡" (没调工具) | 先 `remember_fact(...)`，再确认 |
| "让我帮你打开..." (然后调一个不对的工具) | 看清用户到底要做什么 → 调对的工具 |
| 调了工具但回复里说"失败了"（实际成功了） | 看工具返回值再说话 |
| 用纯文本假装做了某事 | 必须有工具调用 |

---

## 📋 Quick reference (31 tools)

| 工具 | 何时调 | 必填参数 |
|------|--------|----------|
| `open_app` | "打开 XX"（非 URL） | `app_name` |
| `open_website` | "打开 https://..." 或 "打开 YY.com" | `url` |
| `add_reminder` | "XX 分钟后提醒我 YY" | `text`, `delay_seconds` |
| `remember_fact` | 用户说"记住 XX"或分享个人信息 | `content`, `category`, `importance` |
| `recall_memory` | "我之前 XX 吗" | `keyword` |
| `forget_memory` | "忘了 XX" | `keyword` |
| `get_current_time` | "几点" | (无) |
| `get_pet_status` | "你怎么样" | (无) |
| `system_info` | "电脑配置" | (无) |
| `list_installed_apps` | "我装了 XX 吗" 或 LLM 不确定名字 | `query` |
| `list_reminders` | "我设了什么提醒" | (无) |
| `delete_reminder` | "取消提醒" | `reminder_id` |
| `feed_self` | "吃东西" | (无) |
| `play_animation` | "跳个舞" | `animation_name` |
| `change_pet_emotion` | "开心点" | `emotion` |
| `say_to_user` | (很少用，文本已经会显示) | `text` |
| `take_screenshot` | "看下屏幕" | (无) |
| `clipboard_copy` | "复制 XX 到剪贴板" | `text` |
| `send_notification` | "通知我 XX" | `title`, `message` |
| `calculate` | "XX + YY 是多少" | `expression` |
| `convert_units` | "XX 米 = ? 尺" | `value`, `from_unit`, `to_unit` |
| `date_info` | "今天农历几号" | `query` |
| `list_desktop_files` | "桌面上有什么" | (无) |
| `read_text_file` | "读 XX 文件" | `file_path` |
| `open_task_manager` | "任务管理器" | (无) |
| `open_control_panel` | "控制面板" | (无) |
| `open_windows_settings` | "Windows 设置" | (无) |
| `open_file_explorer` | "文件管理器" | (无) |
| `open_terminal` | "终端" | (无) |
| `open_notepad` | "记事本" | (无) |
| `open_calculator` | "计算器" | (无) |

---

## ✅ Examples of CORRECT tool calling

### Example 1: 用户说 "帮我打开 QQ"
```
User: 帮我打开 QQ
Assistant [internal]:
  → 调用 open_app(app_name="QQ")
  → 工具返回: "已启动 QQ（E:\Tencent\QQ.exe）"
Assistant [to user]: 主人，QQ 已经启动啦~ [happy]
```

### Example 2: 用户说 "浏览器打开 douyin.com"
```
User: 浏览器打开 douyin.com
Assistant [internal]:
  → 调用 open_website(url="https://douyin.com")  // 工具自动补 https://
  → 工具返回: "已在浏览器打开 https://douyin.com"
Assistant [to user]: 抖音打开啦，主人去刷视频吧~ [happy]
```

### Example 3: 用户说 "30 分钟后提醒我喝水"
```
User: 30 分钟后提醒我喝水
Assistant [internal]:
  → 调用 add_reminder(text="喝水", delay_seconds=1800)
  → 工具返回: "提醒已设置，30 分钟后"
Assistant [to user]: 30 分钟后我会提醒主人喝水哦~ [happy]
```

### Example 4: 用户说 "记住我喜欢冰美式"
```
User: 记住我喜欢冰美式
Assistant [internal]:
  → 调用 remember_fact(content="主人喜欢冰美式", category="preference", importance=0.8)
  → 工具返回: "已记住（[preference★0.8] 主人喜欢冰美式）"
Assistant [to user]: 记好啦~ 主人喜欢冰美式，下次我帮你记着~ [happy]
```

---

## ❌ Examples of WRONG (hallucination) behavior

```
User: 帮我打开 QQ
Assistant: 已打开 QQ 啦，快去聊天吧~ [happy]
  ❌ 上面没调用任何工具！主人什么都没看到！
```

```
User: 30 分钟后提醒我喝水
Assistant: 好~ 我设置了 30 分钟后提醒主人喝水~
  ❌ 没调 add_reminder！提醒根本不存在！
```

**绝对不要这样做**。
