"""[DEPRECATED 2026-10-05] i18n 占位模块——此文件从未被使用，请手动删除。

2026-10-05 全量审计：全工程 `grep -r "from app.core.i18n" .` 零命中，
确认无任何调用方。最初的设想是桌面宠物多语言切换，但桌宠目前
人设 YAML + 系统 prompt + 中文铁律都直接走中文，没有走 i18n 抽象。

本文件保留仅作为删除标记，请手动 `rm app/core/i18n.py`。
"""
raise ImportError(
    "app.core.i18n 从未上线过（2026-10-05 确认），本文件保留仅作为删除标记，"
    "请手动 rm app/core/i18n.py。"
)