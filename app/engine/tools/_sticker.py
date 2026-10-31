"""表情包工具：send_sticker —— 把一张表情包当**一条消息**发进聊天。

为什么单独一个文件：表情包是"桌宠自己的表情"（跟 change_pet_emotion 一家），
但它发出去的是**聊天里的一条消息**，不是动画 —— 两者别混。

这个工具**很便宜**：只查本地素材目录（assets/sprites/Emotion_*），
不调模型、不联网、不写文件。
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.engine.stickers import (catalogue_digest, emotion_list, get_sticker,
                                normalize_emotion, pick_sticker,
                                sticker_marker)

from ._core import ToolRegistry
from ._decorator import tool

log = logging.getLogger(__name__)


def _ids_text() -> str:
    """可选项清单（给错误文案用）：happy(开心): happy_1 happy_2 …"""
    return catalogue_digest() or "（素材目录里没有 Emotion_* 帧）"


def _emotions_text() -> str:
    """可选情绪清单（给错误文案用）。"""
    return "、".join(emotion_list()) or "（素材目录里没有 Emotion_* 帧）"


def register(reg: ToolRegistry) -> None:
    """注册 send_sticker（只需要注册表，别的不依赖）。"""

    @tool(
        description=(
            "给主人发一张表情包（图片）：会作为**独立的一条消息**出现在聊天里，"
            "主人点一下还能看大图。可选参数："
            "sticker_id（指定哪一张）或 emotion（按情绪挑一张："
            "happy 开心 / sad 难过 / anger 生气 / fear 害怕 / "
            "doubt 狐疑 / pride 骄傲 / shy 害羞 / surprise 惊讶）；"
            "两个都不给就随手发一张。"
            "**一轮最多发一张**，别连着发好几张。"
            f"可选 id：{_ids_text()}。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "sticker_id": {"type": "string",
                               "description": "表情包 id（可选，形如 happy_1）"},
                "emotion": {"type": "string",
                            "description": "情绪（可选，happy/sad/anger/fear/doubt/"
                                           "pride/shy/surprise）"},
            },
            "required": [],
        },
    )
    def send_sticker(sticker_id: str = "", emotion: str = "") -> str:
        """发一张表情包（本地素材，不联网）。"""
        sid = (sticker_id or "").strip().lower()
        emo_raw = (emotion or "").strip()
        emo = normalize_emotion(emo_raw)

        st = None
        if sid:
            # 明确点名了：必须认识，不认识就**如实报错**（不糊弄）
            st = get_sticker(sid)
            if st is None and emo:
                st = pick_sticker(emotion=emo)
            if st is None:
                return (f"错误：没有叫「{sticker_id}」的表情包，这次没发出表情包。"
                        f"可选：{_ids_text()}")
        elif emo_raw:
            if not emo:
                return (f"错误：没有「{emo_raw}」这种情绪，这次没发出表情包。"
                        f"可选情绪：{_emotions_text()}")
            st = pick_sticker(emotion=emo)
        else:
            st = pick_sticker()

        if st is None:
            return ("错误：表情包库是空的（素材目录里找不到 Emotion_* 帧），"
                    "这次没发出表情包。")
        try:
            exists = Path(st.file).is_file()
        except OSError:
            exists = False
        if not exists:
            # 图没了就**不画气泡**（界面靠 exists 判断），并如实说没发出去
            return (f"错误：表情包「{st.label}」的图不在了（{Path(st.file).name}），"
                    "这次没发出表情包。")
        return f"已发送表情包「{st.label}」。{sticker_marker(st.id)}"

    reg.register(send_sticker)


__all__ = ["register"]
