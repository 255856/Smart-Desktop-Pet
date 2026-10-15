"""多模态 providers 包。

2026-10-15 v11: 主人实测要求 ——
"重点是解决不同模型 API 与对应多模态工具之间的适配问题"。
本目录每个文件代表一个 API provider (OpenAI / MiniMax / 智谱 / 阿里百炼 / ...),
里面提供 gen_image / gen_video / gen_audio handler, 启动时 register 到
multimodal_router。主人 /api/<name> 切换 provider, multimodal tool 自动
路由到对应 handler。

新增 provider: 写一个 .py 跟 openai.py / MiniMax.py 同样的 shape, 然后在本
__init__.py 的 `_register_all()` 里加一行 import + setup()。
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


_registered = False


def setup_all() -> None:
    """启动桌宠时调一次, 把所有 provider 装到 router。"""
    global _registered
    if _registered:
        return
    # 用 importlib 显式小写模块名加载 (Windows 文件系统大小写不敏感,
    # 但 Python 模块名按 import 时的字符串固化, 别用 from . import MiniMax
    # 这种容易踩坑的写法)
    import importlib
    for mod_name in ("openai", "minimax"):
        full = f"app.brain.multimodal_providers.{mod_name}"
        try:
            mod = importlib.import_module(full)
        except Exception:  # noqa: BLE001
            log.exception("multimodal_providers: 加载 %s 失败", full)
            continue
        setup_fn = getattr(mod, "setup", None)
        if callable(setup_fn):
            try:
                setup_fn()
            except Exception:  # noqa: BLE001
                log.exception("multimodal_providers: %s.setup() 失败", full)
    _registered = True
    log.info("multimodal_providers: setup_all 跑完, 当前注册: %r",
             list(_name for _name in __all__ if False) or "见 router")
    # 顺手打一下已注册 provider (调试用)
    try:
        from app.brain.multimodal_router import list_providers
        log.info("multimodal_providers: 实际注册列表: %r", list_providers())
    except Exception:
        pass


def reset_for_tests() -> None:
    """测试用: 清掉所有 provider + 激活状态。"""
    from app.brain import multimodal_router
    multimodal_router.clear()
    global _registered
    _registered = False


__all__ = ["setup_all", "reset_for_tests"]
