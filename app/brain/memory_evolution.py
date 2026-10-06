"""[DEPRECATED 2026-10-05] 内存进化兼容层——此文件已无实际功能，请手动删除。

历史：早期版本里 `MemoryEvolution` 是独立类（与 `MemoryCurator` 并存）。
2024 起迁并到 `app.brain.memory.MemoryCurator`，本文件仅保留 re-export 作为
向后兼容入口。2026-10-05 全量审计时全工程 `grep -r MemoryEvolution app/ tests/`
零命中（除本文件自身），确认无任何调用方，**可以删**。

迁移指引：
    from app.brain.memory_evolution import MemoryEvolution
                       ↓ 改为
    from app.brain.memory import MemoryCurator   # 即原 MemoryEvolution

`MemoryEvolution` 别名已不再导出，import 会立刻报错；这样 2026-10-05 之后
任何旧 import 都会被立刻发现，不会悄悄在新代码里继续走死路径。
"""
raise ImportError(
    "app.brain.memory_evolution 已废弃（2026-10-05），请改 import app.brain.memory.MemoryCurator。"
    "本文件保留仅作为删除标记，请手动 rm app/brain/memory_evolution.py。"
)