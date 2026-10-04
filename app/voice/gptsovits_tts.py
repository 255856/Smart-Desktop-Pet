"""GPT-SoVITS 本地 TTS 引擎（方案 B）——完全免费的自定义音色。"""
from __future__ import annotations

import logging
from pathlib import Path

from app.voice.voice import TTS, _strip_emojis

log = logging.getLogger(__name__)


class GPTSoVITSTTS(TTS):
    """调用本地 GPT-SoVITS api_v2 的 /tts 接口，返回 wav 音频。"""

    # api_v2 默认返回 wav；缓存后缀必须为 .wav，否则 pygame mixer 当 mp3 解析会报 bad stream
    cache_ext: str = ".wav"

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

    def _resolve_ref(self, ref: str) -> str:
        """参考音频相对路径 → 绝对路径（服务端按其自身工作目录解析，须给绝对路径）。"""
        if not ref:
            return ref
        p = Path(ref)
        if p.is_absolute():
            return str(p)
        root = Path(__file__).resolve().parent.parent.parent
        return str((root / ref).resolve())

    def _voice_tag(self) -> str:
        """缓存区分：GPT-SoVITS 的 voice 名是固定的，要用当前参考音频区分音色。"""
        return str(self.ref_audio or "")

    def set_voice(self, profile=None) -> None:
        """运行时切换默认音色：换参考音频 + 参考文本。"""
        if profile is not None:
            if getattr(profile, "ref_audio", ""):
                self.ref_audio = profile.ref_audio
            if getattr(profile, "prompt_text", ""):
                self.prompt_text = profile.prompt_text

    async def _synthesize(self, text: str, out_path: Path, profile=None) -> None:
        import httpx
        clean = _strip_emojis(text)
        if not clean:
            raise ValueError("empty text")
        ref_audio = self.ref_audio
        prompt_text = self.prompt_text
        if profile is not None:
            ref_audio = getattr(profile, "ref_audio", "") or ref_audio
            prompt_text = getattr(profile, "prompt_text", "") or prompt_text
        if not ref_audio:
            raise RuntimeError("未配置参考音频路径（gptsovits ref_audio）")
        payload = {
            "text": clean,
            "text_lang": self.text_lang,
            "ref_audio_path": self._resolve_ref(ref_audio),
            "prompt_text": prompt_text,
            "prompt_lang": self.prompt_lang,
            "text_split_method": "cut5",
            "speed_factor": 1.0,
        }
        # 第一次连接失败：尝试自愈（拉起 api_v2）+ 重试一次
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=120) as client:
                    resp = await client.post(f"{self.url}/tts", json=payload)
                    resp.raise_for_status()
                    audio = resp.content
                if len(audio) < 100:
                    raise RuntimeError(f"GPT-SoVITS 返回异常（{len(audio)} 字节），"
                                       "请确认 api_v2 已启动且模型加载成功")
                out_path.write_bytes(audio)
                return
            except (httpx.ConnectError, httpx.ReadError, OSError) as e:
                if attempt == 0:
                    log.warning("GPT-SoVITS 连接失败（%s），尝试自动重启 api_v2…", e)
                    try:
                        from app.main import start_tts_api_subprocess
                        from pathlib import Path
                        start_tts_api_subprocess(
                            Path(__file__).resolve().parent.parent.parent,
                            port=9880)
                        import asyncio
                        await asyncio.sleep(8)
                        continue
                    except Exception as e2:  # noqa: BLE001
                        raise RuntimeError(f"GPT-SoVITS 自愈失败: {e2}") from e
                raise
