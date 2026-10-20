"""LangChain 后端的运行参数（轻量模块）。

单独拆出来是为了**不拖慢启动**：这个 dataclass 本身零依赖，
但它原来住在 app/brain/langchain_agent.py 里，而那个模块在 import 时会连带
拉起 langchain.agents / langchain_openai / langgraph
（实测 langchain_openai 1.84 s + langchain.agents 1.00 s）。

聊天窗在模块级 import 了 LangChainAgentConfig（哪怕用 lightweight 后端、
根本不会碰 LangChain），于是每次启动桌宠都要白付这 ~1.1~1.8 秒。

现在聊天窗从本模块拿配置（零依赖），LangChainAgent 本体改成延迟导入。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LangChainAgentConfig:
    """LangChain 后端运行参数（从 brain.langchain 段读取）。"""
    enable_checkpointer: bool = True
    checkpoint_db: str = "data/langchain_checkpoints.db"
    max_iterations: int = 10
    return_intermediate_steps: bool = False


__all__ = ["LangChainAgentConfig"]
