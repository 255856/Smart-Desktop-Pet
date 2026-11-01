"""工具风险等级表 (创新点 2 - 桌宠养成状态对 Agent 决策的耦合机制)。

把每个 Function Calling 工具按 5 档风险定级:

  0 SAFE          纯读 / 纯计算 / 纯查询, 无副作用
  1 LOW           UI 反馈 / 桌面 GUI 启动 / 自己状态小幅改, 可逆
  2 MEDIUM        本地文件改 / 系统音量 + 亮度 / 剪贴板 / 长时间静音
  3 HIGH          网络 / 蓝牙 / 系统通知 / 邮件外发 / 本地脚本执行
  4 IRREVERSIBLE  锁屏 / 杀进程 / 物理不可逆, 主人立刻被影响

设计原则:
  - 该表是 **单源真相**, `_RISKY_TOOLS` frozenset (hallucination_guard.py
    用) 改为派生于 _risk, 避免两套数据漂移。
  - 表里**只列工具名**, 数字 0-4。description / 文档全部写在 _core.py +
    各 register 文件 docstring 里, 不要在这里重复。
  - 新加工具时, 主人 / reviewer 必须在 PR review 时**显式给出 risk 等级**,
    没填默认 MEDIUM (2) 而不是 SAFE, 强制让 reviewer 想一下。

后续 companion_policy.py / RiskyToolConfirm / hallucination_guard.prompt 都
从 TOOL_RISK_LEVEL 取数, 不再硬编码工具名。
"""
from __future__ import annotations

# 5 档风险常量, 让其它模块 import 时不直接看 magic number
SAFE: int = 0
LOW: int = 1
MEDIUM: int = 2
HIGH: int = 3
IRREVERSIBLE: int = 4

# 风险等级的人类可读标签 (注入 prompt 给 LLM 看)
RISK_LABELS: dict[int, str] = {
    SAFE: "SAFE",
    LOW: "LOW",
    MEDIUM: "MEDIUM",
    HIGH: "HIGH",
    IRREVERSIBLE: "IRREVERSIBLE",
}

# 主体分类: 类别说明, 仅供 prompt 内部参考, 不用来决策
RISK_RATIONALE: dict[int, str] = {
    SAFE:         "纯读 / 纯计算 / 纯查询, 完全可回退, 不影响桌面状态",
    LOW:          "本地 UI 改动 / 启动桌面应用 / 改自己内部状态, 撤销简单",
    MEDIUM:       "本地文件编辑 / 系统音量亮度 / 剪贴板 / 长时间静音, 影响中等",
    HIGH:         "网络开关 / 系统通知 / 邮件外发 / 沙箱内脚本执行, 影响显著",
    IRREVERSIBLE: "锁屏 / 杀进程 / 文件物理删除, 主人立刻被影响, 难撤销",
}

# 工具风险等级主表（65 个内置工具 + MCP 工具共 69 条）
TOOL_RISK_LEVEL: dict[str, int] = {
    # ---- SAFE (0): 纯读 / 纯计算 / 纯查询 ----
    "get_current_time":          SAFE,
    "get_pet_status":            SAFE,
    "get_weather":               SAFE,
    "date_info":                 SAFE,
    "calculate":                 SAFE,
    "convert_units":             SAFE,
    "web_search":                SAFE,
    "fetch_url_text":            SAFE,
    "system_info":               SAFE,
    "get_battery":               SAFE,
    "list_countdowns":           SAFE,
    "list_reminders":            SAFE,
    "list_processes":            SAFE,
    "list_desktop_files":        SAFE,
    "list_installed_apps":       SAFE,
    "search_files":              SAFE,
    "grep_files":                SAFE,
    "glob_files":                SAFE,
    "read_text_file":            SAFE,
    "read_file":                 SAFE,
    "get_tts_mute_status":       SAFE,
    "get_volume":                SAFE,
    "get_brightness":            SAFE,
    "get_clipboard":             SAFE,
    "ocr_image":                 SAFE,
    "understand_image":          SAFE,
    "recall_memory":             SAFE,
    "query_slang":               SAFE,   # 只读: 查已收录的黑话表
    "send_sticker":              SAFE,   # 只读本地表情包素材 + 在聊天里发一条图
    # ---- LOW (1): UI 改动 / 桌面启动 / 内部状态小幅动 ----
    "clear_bubble":              LOW,
    "countdown":                 LOW,
    "cancel_countdown":          LOW,
    "say_to_user":               LOW,
    "feed_self":                 LOW,
    "play_animation":            LOW,
    "change_pet_emotion":        LOW,
    "take_screenshot":           LOW,   # tool name is take_screenshot_tool
    "take_screenshot_tool":      LOW,   # alias, 真实 _core 注册是这个名字
    "open_terminal":             LOW,
    "open_notepad":              LOW,
    "open_file_explorer":        LOW,
    "open_website":              LOW,
    "open_app":                  LOW,
    "unmute_tts":                LOW,
    # ---- MEDIUM (2): 本地文件改 / 系统音量亮度 / 剪贴板 ----
    "add_reminder":              MEDIUM,
    "delete_reminder":           MEDIUM,
    "edit_file":                 MEDIUM,
    "multi_edit":                MEDIUM,
    "remember_fact":             MEDIUM,
    "submit_slang":              MEDIUM, # 写本地词库文件 (data/slang.json)
    "update_self":                MEDIUM, # 改自己的身份/价值/程序性（长期记忆）
    "use_skill":                  LOW,    # 只读 skills/*.md（行为覆盖包），不改任何东西
    "forget_memory":             MEDIUM,
    "set_volume":                MEDIUM,
    "set_brightness":            MEDIUM,
    "clipboard_copy":            MEDIUM,
    "set_tts_mute_until":        MEDIUM,
    "Task":                      MEDIUM,   # 派发子 agent, 资源消耗中等
    "generate_image":            MEDIUM,
    "generate_audio":            MEDIUM,
    # ---- HIGH (3): 网络开关 / 通知 / 邮件 / 脚本执行 ----
    "send_notification":         HIGH,
    "send_email":                HIGH,
    "set_wifi":                  HIGH,
    "set_bluetooth":             HIGH,
    "run_script":                HIGH,
    "generate_video":            HIGH,   # 异步 + 计费高
    # ---- IRREVERSIBLE (4): 主人立刻被影响, 难撤销 ----
    "kill_process":              IRREVERSIBLE,
    "lock_screen":               IRREVERSIBLE,
    # 下面两个是**预留名**(工具函数还没实现, live 注册表里查不到):
    # shutdown_computer 已经在 agent.py 的 DANGEROUS_TOOLS 里, RISK_RATIONALE[4] 也点名了
    # 「文件物理删除」—— 等级先定死, 模型真调这两个名字时一律走 confirm 流程。
    "shutdown_computer":         IRREVERSIBLE,   # 关机 / 重启: 物理不可逆
    "delete_file":               IRREVERSIBLE,   # 文件物理删除: 不可逆
}


def risk_for_tool(name: str, default: int = MEDIUM) -> int:
    """查一个工具的风险等级; 工具不在表里返 default (默认 MEDIUM 而非 SAFE)。

    用 MEDIUM 作为默认是因为: 加新工具时, 主人 / reviewer 还没显式定
    级的话, 默认要让其走 confirm 路径, 强制让 reviewer 重新评估。这比
    默认 SAFE 更稳 (主人不会因为新工具被默认放行而惊到)。
    """
    return TOOL_RISK_LEVEL.get(name, default)


def is_risky(name: str, threshold: int = MEDIUM) -> bool:
    """name 风险 >= threshold 视为 risky, 默认门槛 MEDIUM (2)。

    默认 MEDIUM 是兼容原 hallucination_guard._RISKY_TOOLS 9 条的集合:
    lock_screen / kill_process / set_wifi / set_bluetooth / send_notification /
    run_script / set_volume / set_brightness / send_email 全部 ≥ MEDIUM (≥2),
    is_risky(name) == True。生成视频 generate_video 也 ≥MEDIUM (3=HIGH)。

    如只想看"主人立刻被打断"那批 (≥ HIGH), 显式传 threshold=HIGH。
    如想"任何非纯读" (含 LOW), 显式传 threshold=LOW。
    """
    return risk_for_tool(name) >= threshold


# 风险等级 int → 名称 (供 UI 显示)
RISK_NAME: dict[int, str] = {
    SAFE: "SAFE",
    LOW: "LOW",
    MEDIUM: "MEDIUM",
    HIGH: "HIGH",
    IRREVERSIBLE: "IRREVERSIBLE",
}


def risk_name(risk_int) -> str:
    """int 风险等级 → 名称。None / 未知值返 'UNKNOWN'。

    MCP 外部工具不在 TOOL_RISK_LEVEL 主表里,调用方 get() 拿到 None,
    这里必须容忍而不是抛 TypeError。
    """
    if risk_int is None:
        return "UNKNOWN"
    try:
        return RISK_NAME.get(int(risk_int), "UNKNOWN")
    except (TypeError, ValueError):
        return "UNKNOWN"


__all__ = [
    "SAFE", "LOW", "MEDIUM", "HIGH", "IRREVERSIBLE",
    "RISK_LABELS", "RISK_RATIONALE",
    "TOOL_RISK_LEVEL", "risk_for_tool", "is_risky", "RISK_NAME", "risk_name",
]
