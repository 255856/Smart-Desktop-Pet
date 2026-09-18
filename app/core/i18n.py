"""基础国际化支持：当前支持中文，预留英文/日文扩展。"""
from __future__ import annotations

# 当前语言
_LOCALE = "zh"

# 翻译表
_TRANSLATIONS = {
    "zh": {
        "chat_title": "和{name}的对话",
        "send": "发送",
        "stop": "停止",
        "clear": "清空上下文",
        "settings_title": "桌宠设置",
        "quit": "退出",
        "show_pet": "显示桌宠",
        "hide_pet": "隐藏桌宠",
        "open_chat": "打开聊天",
        "open_settings": "设置...",
        "status_tab": "状态",
        "fps_tab": "帧时长",
        "visual_tab": "视觉",
        "control_tab": "控制",
        "strength": "体力",
        "food": "饱食",
        "drink": "口渴",
        "feeling": "心情",
        "health": "健康",
        "likability": "好感",
        "chat_history": "对话轮次",
        "input_placeholder": "输入消息，回车发送……",
        "voice_button": "📢",
    },
    "en": {
        "chat_title": "Chat with {name}",
        "send": "Send",
        "stop": "Stop",
        "clear": "Clear History",
        "settings_title": "Pet Settings",
        "quit": "Quit",
        "show_pet": "Show Pet",
        "hide_pet": "Hide Pet",
        "open_chat": "Open Chat",
        "open_settings": "Settings...",
        "status_tab": "Status",
        "fps_tab": "FPS",
        "visual_tab": "Visual",
        "control_tab": "Controls",
        "strength": "Strength",
        "food": "Food",
        "drink": "Drink",
        "feeling": "Feeling",
        "health": "Health",
        "likability": "Affinity",
        "chat_history": "History",
        "input_placeholder": "Type a message...",
        "voice_button": "🎤 Hold to Talk",
    },
}


def set_locale(locale: str) -> None:
    """设置当前语言（'zh' / 'en'）。"""
    global _LOCALE
    if locale in _TRANSLATIONS:
        _LOCALE = locale


def get_locale() -> str:
    """获取当前语言。"""
    return _LOCALE


def tr(key: str, **kwargs) -> str:
    """翻译一个 key，支持参数替换。"""
    table = _TRANSLATIONS.get(_LOCALE, _TRANSLATIONS["zh"])
    text = table.get(key, key)
    if kwargs:
        text = text.format(**kwargs)
    return text


def available_locales() -> list[str]:
    """返回所有可用的语言代码。"""
    return list(_TRANSLATIONS.keys())
