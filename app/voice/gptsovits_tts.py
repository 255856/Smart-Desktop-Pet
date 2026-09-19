"""GPT-SoVITS 本地 TTS 引擎（方案 B）——完全免费的自定义音色。

前提：本地部署 GPT-SoVITS（官方整合包）并启动 api_v2.py：
    runtime\\python api_v2.py -a 127.0.0.1 -p 9880
    （整合包里可先用 WebUI 微调流萤数据集——你们的数据集自带 .lab 文本标注，
      也可以不做微调，直接用零样本模式：给一段参考音频 + 它的文本）

config.yaml（character: 段）：
    tts_engine: gptsovits
    gptsovits_url: "http://127.0.0.1:9880"
    gptsovits_ref_audio: "流萤某段 wav 的路径（服务端本地可访问）"
    gptsovits_prompt_text: "该参考音频对应的文本（.lab 文件里的内容）"

播放/缓存/口型同步钩子全部复用基类 TTS。
"""
from __future__ import annotations

import logging
from pathlib import Path

from app.voice.voice import TTS, _strip_emojis

log = logging.getLogger(__name__)


class GPTSoVITSTTS(TTS):
    """调用本地 GPT-SoVITS api_v2 的 /tts 接口，返回 wav 音频。"""

    def __init__(self, url: str = "http://127.0.0.1:9880",
                 ref_audio: str = "", prompt_text: str = "",
                 text_lang: str = "zh", prompt_lang: str = "zh",
                 cache_dir: str | Path = "assets/tts_cache"):
        super().__init__(voice="gptsovits", cache_dir=cache_dir)
        self.url = url.rstrip("/")
        self.ref_audio = ref_audio
        self.prompt_text = prompt_text
        self.text_lang = text_lang
        self.prompt_lang = prompt_lang

    async def _synthesize(self, text: str, out_path: Path) -> None:
        import httpx
        clean = _strip_emojis(text)
        if not clean:
            raise ValueError("empty text")
        if not self.ref_audio:
            raise RuntimeError("未配置 gptsovits_ref_audio（参考音频路径）")
        payload = {
            "text": clean,
            "text_lang": self.text_lang,
            "ref_audio_path": self.ref_audio,
            "prompt_text": self.prompt_text,
            "prompt_lang": self.prompt_lang,
            "text_split_method": "cut5",
            "speed_factor": 1.0,
        }
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(f"{self.url}/tts", json=payload)
            resp.raise_for_status()
            audio = resp.content
        if len(audio) < 100:
            raise RuntimeError(f"GPT-SoVITS 返回异常（{len(audio)} 字节），"
                               "请确认 api_v2 已启动且模型加载成功")
        out_path.write_bytes(audio)
