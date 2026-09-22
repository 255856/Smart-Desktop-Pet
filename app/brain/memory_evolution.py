"""记忆自动进化 / 整理（兼容旧入口，内部委托 MemoryCurator）。"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


# 旧 API stub：直接 re-export
from app.brain.memory import MemoryCurator  # noqa: E402,F401

# 旧类名向后兼容
MemoryEvolution = MemoryCurator  # type: ignore[misc]
