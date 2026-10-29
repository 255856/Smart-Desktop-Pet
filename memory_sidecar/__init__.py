# -*- coding: utf-8 -*-
"""桌宠记忆 sidecar：独立进程持有记忆（JSON 记忆卡 + Chroma 向量 + BM25 关键词 + 精排）。

用法::

    python -m memory_sidecar init              # 建数据目录 + 空卡库（幂等）
    python -m memory_sidecar upsert --file cards.json
    python -m memory_sidecar search --query "我不能吃什么"
    python -m memory_sidecar delete --ids a,b
    python -m memory_sidecar stats
    python -m memory_sidecar serve --port 8788  # 常驻 HTTP（模型只加载一次）
"""
from .server import DEFAULT_HOST, DEFAULT_PORT, MemorySidecarServer, serve
from .store import (
    DEFAULT_DATA_DIR,
    EMBED_MODEL,
    RERANK_MODEL,
    RERANK_MODEL_CPU,
    RERANK_MODEL_ENV,
    RERANK_MODEL_GPU,
    MemoryStore,
    resolve_rerank_model,
    tokenize,
)

__all__ = [
    "MemoryStore",
    "MemorySidecarServer",
    "serve",
    "tokenize",
    "DEFAULT_DATA_DIR",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "EMBED_MODEL",
    # RERANK_MODEL 是环境变量的值：空字符串 = 没设，按设备自动选
    # （resolve_rerank_model：有 CUDA → v2-m3，否则 → base）
    "RERANK_MODEL",
    "RERANK_MODEL_ENV",
    "RERANK_MODEL_GPU",
    "RERANK_MODEL_CPU",
    "resolve_rerank_model",
]

__version__ = "1.0.0"
