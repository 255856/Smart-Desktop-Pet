"""视觉输入(vision)支持的测试。

覆盖:
- image_path_to_data_url: PNG / JPG / 大文件 / 不存在
- bytes_to_data_url
- build_multimodal_content: text + 多图、空 text、多图
- encode_local_images: 批量,跳过失败
- LLMClient.build_vision_messages: 拼 history + multimodal user
- LLMClient._build_payload 在 content 是 list 时仍正确序列化
- LangChain HumanMessage 接受 multimodal content list
"""
from __future__ import annotations

import base64
from pathlib import Path

import pytest

from app.brain import vision


# -------------------- image_path_to_data_url --------------------

class TestImagePathToDataUrl:
    def test_png(self, tmp_path):
        f = tmp_path / "x.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fake_png_data")
        url = vision.image_path_to_data_url(f)
        assert url.startswith("data:image/png;base64,")
        b64 = url.split(",", 1)[1]
        assert base64.b64decode(b64) == f.read_bytes()

    def test_jpg(self, tmp_path):
        f = tmp_path / "x.jpg"
        f.write_bytes(b"\xff\xd8\xff\xe0fake_jpg")
        url = vision.image_path_to_data_url(f)
        assert url.startswith("data:image/jpeg;base64,")

    def test_jpeg_extension(self, tmp_path):
        f = tmp_path / "x.jpeg"
        f.write_bytes(b"\xff\xd8\xff")
        url = vision.image_path_to_data_url(f)
        assert "image/jpeg" in url

    def test_webp(self, tmp_path):
        f = tmp_path / "x.webp"
        f.write_bytes(b"RIFF\xff\xffWEBPfake")
        url = vision.image_path_to_data_url(f)
        assert "image/webp" in url

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            vision.image_path_to_data_url(tmp_path / "nope.png")

    def test_oversize_file_raises(self, tmp_path, monkeypatch):
        # 把 cap 调小以便测试
        from app.brain import vision as v
        monkeypatch.setattr(v, "MAX_IMAGE_B64_BYTES", 100)
        f = tmp_path / "big.png"
        f.write_bytes(b"X" * 200)
        with pytest.raises(ValueError, match="too large"):
            vision.image_path_to_data_url(f)


# -------------------- bytes_to_data_url --------------------

class TestBytesToDataUrl:
    def test_default_mime(self):
        url = vision.bytes_to_data_url(b"\x89PNG...")
        assert url.startswith("data:image/png;base64,")

    def test_custom_mime(self):
        url = vision.bytes_to_data_url(b"data", mime="image/jpeg")
        assert url.startswith("data:image/jpeg;base64,")

    def test_oversize_raises(self, monkeypatch):
        from app.brain import vision as v
        monkeypatch.setattr(v, "MAX_IMAGE_B64_BYTES", 50)
        with pytest.raises(ValueError, match="too large"):
            vision.bytes_to_data_url(b"X" * 100)


# -------------------- build_multimodal_content --------------------

class TestBuildMultimodalContent:
    def test_text_only(self):
        parts = vision.build_multimodal_content("hello", [])
        assert len(parts) == 1
        assert parts[0] == {"type": "text", "text": "hello"}

    def test_text_and_one_image(self):
        url = "data:image/png;base64,ABC"
        parts = vision.build_multimodal_content("look", [url])
        assert parts == [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": url}},
        ]

    def test_text_and_multiple_images(self):
        urls = [f"data:image/png;base64,{i}" for i in range(3)]
        parts = vision.build_multimodal_content("multi", urls)
        assert len(parts) == 4   # 1 text + 3 images
        image_parts = [p for p in parts if p["type"] == "image_url"]
        assert len(image_parts) == 3

    def test_empty_text_and_image(self):
        parts = vision.build_multimodal_content("", ["data:image/png;base64,A"])
        assert len(parts) == 1   # 只有 image,text 空字符串被跳过

    def test_empty_inputs_falls_back(self):
        """text 空 + 无图 → 给个空 text,避免 content=[]。"""
        parts = vision.build_multimodal_content("", [])
        assert parts == [{"type": "text", "text": ""}]


# -------------------- encode_local_images --------------------

class TestEncodeLocalImages:
    def test_batch_with_one_bad(self, tmp_path):
        good = tmp_path / "ok.png"
        good.write_bytes(b"\x89PNGfake")
        bad = tmp_path / "missing.png"
        urls = vision.encode_local_images([good, bad])
        assert len(urls) == 1   # 坏的跳过
        assert "image/png" in urls[0]

    def test_all_missing_returns_empty(self, tmp_path):
        urls = vision.encode_local_images([tmp_path / "x.png", tmp_path / "y.jpg"])
        assert urls == []


# -------------------- LLMClient.build_vision_messages --------------------

class TestBuildVisionMessages:
    def test_empty_history(self):
        from app.brain.llm_client import ChatMessage, LLMClient
        msgs = LLMClient.build_vision_messages(
            "what?", ["data:image/png;base64,AAA"]
        )
        assert len(msgs) == 1
        assert msgs[0].role == "user"
        # content 是 multimodal list
        assert isinstance(msgs[0].content, list)
        assert msgs[0].content[0] == {"type": "text", "text": "what?"}
        assert msgs[0].content[1]["type"] == "image_url"

    def test_with_history(self):
        from app.brain.llm_client import ChatMessage, LLMClient
        history = [
            ChatMessage(role="user", content="hi"),
            ChatMessage(role="assistant", content="hello"),
        ]
        msgs = LLMClient.build_vision_messages("看这张", ["data:..."], history=history)
        assert len(msgs) == 3
        assert msgs[0].role == "user"
        assert msgs[0].content == "hi"
        assert msgs[1].role == "assistant"
        # 最后一条 user 是 multimodal
        assert isinstance(msgs[2].content, list)

    def test_history_not_mutated(self):
        from app.brain.llm_client import ChatMessage, LLMClient
        history = [ChatMessage(role="user", content="hi")]
        msgs = LLMClient.build_vision_messages("看图", ["data:..."], history=history)
        # 调用方拿到的是新 list(history 被浅拷贝,加了 multimodal 进来)
        assert msgs is not history
        assert len(history) == 1   # 原始 history 没变


# -------------------- _build_payload 接受 multimodal content --------------------

class TestBuildPayloadAcceptsMultimodal:
    def test_payload_serializes_list_content(self):
        from app.brain.llm_client import ChatMessage, LLMClient, LLMConfig
        client = LLMClient(LLMConfig(), system_prompt="sys")
        msgs = LLMClient.build_vision_messages(
            "描述", ["data:image/png;base64,XYZ"]
        )
        payload = client._build_payload(msgs)
        # system + 1 user
        assert len(payload["messages"]) == 2
        user_msg = payload["messages"][1]
        assert user_msg["role"] == "user"
        # content 仍是 list (JSON 序列后还原)
        assert isinstance(user_msg["content"], list)
        assert user_msg["content"][0]["type"] == "text"
        assert user_msg["content"][1]["type"] == "image_url"

    def test_payload_passes_through_string_content_unchanged(self):
        """非 multimodal 路径仍要工作 —— 字符串 content 不能被改动。"""
        from app.brain.llm_client import ChatMessage, LLMClient, LLMConfig
        client = LLMClient(LLMConfig(), system_prompt="sys")
        msgs = [ChatMessage(role="user", content="hi")]
        payload = client._build_payload(msgs)
        assert payload["messages"][1]["content"] == "hi"


# -------------------- LangChain HumanMessage multimodal --------------------

class TestLangChainHumanMessageMultimodal:
    def test_human_message_accepts_list(self):
        from langchain_core.messages import HumanMessage
        msg = HumanMessage(content=[
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:..."}},
        ])
        assert isinstance(msg.content, list)
        assert msg.content[0]["type"] == "text"


# -------------------- LangChainAgent.run 接受 multimodal messages --------------------

class TestLangChainAgentRunWithMultimodal:
    """LangChainAgent.run 拿到 messages[-1].content 是 list 时,
    HumanMessage 正确构造(端到端验证,不需要真实 LLM)。"""

    def test_run_passes_list_to_human_message(self):
        from app.brain.langchain_agent import LangChainAgent, LangChainAgentConfig
        from app.brain.llm_client import LLMConfig
        from app.engine.tools import ToolRegistry

        agent = LangChainAgent(
            llm_cfg=LLMConfig(),
            registry=ToolRegistry(),
            cfg=LangChainAgentConfig(),
            persona="test",
            thread_id="t_vision",
        )
        # _to_lc_history 必须把 multimodal content 透传,不能让 HumanMessage 拿到 list
        history = agent._to_lc_history([
            {"role": "user", "content": [
                {"type": "text", "text": "看"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
            ]},
        ])
        assert len(history) == 1
        assert isinstance(history[0].content, list), (
            "HumanMessage.content 应该保留 multimodal 列表,不能被转成字符串"
        )