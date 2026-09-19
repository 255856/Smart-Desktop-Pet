"""MiniMax TTS + 声音克隆（方案 A）——自定义音色，无需本地 GPU。

使用流程：
    1. python tools/clone_voice.py --voice-id my_voice --samples 样本1.mp3 ...
       （上传几段 10 秒以上的目标音色样本，得到自定义 voice_id）
    2. config.yaml:
           character:
             tts_engine: minimax
             minimax_voice_id: my_voice
       （minimax_api_key 留空则复用 llm.api_key，同账号）
    3. 启动即用克隆音色说话。

接口：POST /v1/t2a_v2（文本转语音，返回 hex 编码的 mp3）。
"""
from __future__ import annotations

import logging
from pathlib import Path

from app.voice.voice import TTS, _strip_emojis

log = logging.getLogger(__name__)


class MiniMaxTTS(TTS):
    """MiniMax t2a_v2 引擎：与 edge-tts 版 TTS 同接口（speak/set_enabled/voice），
    仅替换合成环节；播放/缓存/口型同步钩子全部复用基类。"""

    def __init__(self, api_key: str, voice_id: str,
                 base_url: str = "https://api.minimaxi.com/v1",
                 model: str = "speech-01-turbo",
                 group_id: str = "",
                 cache_dir: str | Path = "assets/tts_cache"):
        super().__init__(voice=voice_id or "female-shaonv",
                         cache_dir=cache_dir)
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.group_id = group_id

    async def _synthesize(self, text: str, out_path: Path) -> None:
        import httpx
        clean = _strip_emojis(text)
        if not clean:
            raise ValueError("empty text")
        url = f"{self.base_url}/t2a_v2"
        if self.group_id:
            url += f"?GroupId={self.group_id}"
        payload = {
            "model": self.model,
            "text": clean,
            "stream": False,
            "voice_setting": {
                "voice_id": self.voice,
                "speed": 1.0,
                "vol": 1.0,
                "pitch": 0,
            },
            "audio_setting": {
                "sample_rate": 32000,
                "bitrate": 128000,
                "format": "mp3",
                "channel": 1,
            },
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        base = data.get("base_resp") or {}
        if base.get("status_code", 0) != 0:
            raise RuntimeError(f"MiniMax TTS 失败: {base}")
        audio_hex = (data.get("data") or {}).get("audio", "")
        if not audio_hex:
            raise RuntimeError(f"MiniMax TTS 无音频返回: {str(data)[:200]}")
        out_path.write_bytes(bytes.fromhex(audio_hex))
