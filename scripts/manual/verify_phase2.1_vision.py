"""手动验证 Phase 2.1 Vision 后端(图片 → multimodal message)。

用法:
    cd E:\study\desktop-pet
    $env:PYTHONPATH="E:\study\desktop-pet"
    python scripts\manual\verify_phase2.1_vision.py

输出:
    - 准备一张测试图片(运行时生成纯色 PNG)
    - image_path_to_data_url 转 base64 data URL
    - build_multimodal_content 拼成 OpenAI 格式
    - LLMClient.build_vision_messages 把 multimodal content 放到 user 位
    - LLMClient._build_payload 序列化后仍是 list(关键:不能丢结构)

不消耗 token,纯本地。
"""
import asyncio
import base64
import os
import struct
import sys
import tempfile
import zlib
from pathlib import Path

# 脚本在 scripts/manual/ 下 → 往上两层才是项目根（app 包所在）
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from app.brain.llm_client import LLMClient, ChatMessage
from app.brain.llm_client import LLMConfig as LLMConfig
from app.brain.vision import (
    build_multimodal_content,
    encode_local_images,
    image_path_to_data_url,
)


def _make_tiny_png(path: Path, color=(255, 200, 100)):
    """手搓一个最小的 1x1 PNG,避免依赖 PIL。"""
    sig = b"\x89PNG\r\n\x1a\n"

    def chunk(typ: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(typ + data) & 0xffffffff
        return struct.pack(">I", len(data)) + typ + data + struct.pack(">I", crc)

    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    raw = b"\x00" + bytes(color)   # filter byte + RGB
    idat = chunk(b"IDAT", zlib.compress(raw))
    iend = chunk(b"IEND", b"")
    path.write_bytes(sig + ihdr + idat + iend)


def main():
    print("=== Phase 2.1 Vision 后端 手动验证 ===\n")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        png = td / "test.png"
        _make_tiny_png(png, color=(255, 200, 100))
        print(f"[setup] 生成测试图片 {png} ({png.stat().st_size} bytes)")

        print("\n[step 1] image_path_to_data_url 转 base64")
        url = image_path_to_data_url(png)
        print(f"  - URL 前 60 字符: {url[:60]}...")
        assert url.startswith("data:image/png;base64,")
        b64 = url.split(",", 1)[1]
        assert base64.b64decode(b64) == png.read_bytes()
        print(f"  - base64 解码后字节数跟原文一致 ({len(b64)} chars)")

        print("\n[step 2] build_multimodal_content 拼成 OpenAI 格式")
        parts = build_multimodal_content("看这张图", [url])
        print(f"  - parts 数: {len(parts)}")
        print(f"  - parts[0]: {parts[0]}")
        print(f"  - parts[1]['image_url'] 前 50 字符: {str(parts[1])[:60]}...")
        assert parts[0] == {"type": "text", "text": "看这张图"}
        assert parts[1]["type"] == "image_url"

        print("\n[step 3] LLMClient.build_vision_messages 拼 history + multimodal")
        client = LLMClient(LLMConfig(), system_prompt="sys")
        msgs = LLMClient.build_vision_messages(
            "看这张图", [url],
            history=[
                ChatMessage(role="user", content="hi"),
                ChatMessage(role="assistant", content="hello"),
            ],
        )
        print(f"  - 消息数: {len(msgs)}")
        assert len(msgs) == 3
        assert msgs[0].role == "user"
        assert msgs[0].content == "hi"   # history 原样保留
        assert msgs[1].role == "assistant"
        assert msgs[2].role == "user"
        assert isinstance(msgs[2].content, list)   # multimodal 是 list
        print(f"  - 最后一条 user.content[0]: {msgs[2].content[0]}")
        print(f"  - 最后一条 user.content[1] type: {msgs[2].content[1]['type']}")

        print("\n[step 4] LLMClient._build_payload 序列化后仍是 list")
        payload = client._build_payload(msgs)
        user_msg = payload["messages"][-1]
        print(f"  - payload['messages'][-1]['role']: {user_msg['role']}")
        print(f"  - payload['messages'][-1]['content'] 是 list: {isinstance(user_msg['content'], list)}")
        assert isinstance(user_msg["content"], list)
        assert user_msg["content"][0]["type"] == "text"
        assert user_msg["content"][1]["type"] == "image_url"

        print("\n[step 5] LangChain HumanMessage 接受 multimodal list")
        from langchain_core.messages import HumanMessage
        lc_msg = HumanMessage(content=msgs[2].content)
        print(f"  - HumanMessage.content 是 list: {isinstance(lc_msg.content, list)}")
        assert isinstance(lc_msg.content, list)

        print("\n[step 6] encode_local_images 批量 + 跳过失败")
        nonexistent = td / "missing.png"
        urls = encode_local_images([png, nonexistent])
        print(f"  - 拿到 {len(urls)} 个 URL(应 1,坏的被跳过)")
        assert len(urls) == 1

        print("\n=== 全部通过 ===")
        print("\n[note] 真实图片支持:PNG / JPG / JPEG / GIF / BMP / WEBP。")
        print("       size cap 6 MiB raw(超过会 ValueError)。")
        print("       当前 chat_window 还没接 drag/drop 处理,需要:")
        print("       1) Qt widget 接 dropEvent / pasteEvent,拿到 QPixmap / path")
        print("       2) 转 data_url + build_vision_messages")
        print("       3) 调 agent.run(messages_with_images)")


if __name__ == "__main__":
    main()