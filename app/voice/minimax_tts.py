"""MiniMax TTS + 声音克隆（方案 A）——自定义音色，无需本地 GPU。

使用流程（手动）：
    1. python tools/clone_voice.py --voice-id my_voice --samples 样本1.mp3 ...
    2. config.yaml:
           character:
             tts_engine: minimax
             minimax_voice_id: my_voice
       （minimax_api_key 留空则复用 llm.api_key，同账号）

自动克隆：在 config.yaml 配 samples 目录后，桌宠启动时会自动执行克隆
（样本指纹未变化时跳过，不会重复执行），见 ensure_voice_cloned()。

接口：POST /v1/files（上传）、/v1/voice_clone（克隆）、/v1/t2a_v2（合成，
返回 hex 编码的 mp3）。
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from app.voice.voice import TTS, _strip_emojis

log = logging.getLogger(__name__)

# 克隆支持的音频扩展名
SAMPLE_EXTS = {".wav", ".mp3", ".m4a", ".flac"}


def collect_samples(src: str | Path, limit: int = 6) -> list[Path]:
    """样本来源 → 音频文件列表（最多 limit 个）。

    src 支持：目录（取其中音频文件，均匀抽样）或逗号分隔的文件列表。
    均匀抽样覆盖数据集不同段落，避免只取同一批。
    """
    src = str(src or "").strip()
    if not src:
        return []
    parts = [s.strip() for s in src.split(",") if s.strip()]
    paths: list[Path] = []
    if len(parts) == 1:
        p = Path(parts[0])
        if p.is_dir():
            paths = sorted(x for x in p.iterdir()
                           if x.suffix.lower() in SAMPLE_EXTS)
        elif p.is_file():
            paths = [p]
    else:
        paths = [Path(x) for x in parts if Path(x).is_file()]
    if len(paths) > limit:
        step = len(paths) / limit
        paths = [paths[int(i * step)] for i in range(limit)]
    return paths


def samples_fingerprint(paths: list[Path]) -> str:
    """样本指纹（文件名+大小+修改时间）：变了才重新克隆。"""
    h = hashlib.md5()
    for p in paths:
        try:
            st = p.stat()
            h.update(f"{p.name}:{st.st_size}:{int(st.st_mtime)}".encode("utf-8"))
        except OSError:
            continue
    return h.hexdigest()


def _concat_wavs(paths: list[Path], out_path: Path) -> Optional[Path]:
    """把多段同参数 wav 拼接成一个（克隆时单文件上传）。参数不一致返回 None。"""
    import wave
    try:
        w0 = wave.open(str(paths[0]), "rb")
        params = (w0.getnchannels(), w0.getsampwidth(), w0.getframerate())
        out = wave.open(str(out_path), "wb")
        out.setnchannels(params[0]); out.setsampwidth(params[1])
        out.setframerate(params[2])
        total = 0
        for p in paths:
            try:
                w = wave.open(str(p), "rb")
                if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != params:
                    w.close()
                    continue
                out.writeframes(w.readframes(w.getnframes()))
                total += w.getnframes() / params[2]
                w.close()
            except Exception:  # noqa: BLE001
                continue
        out.close()
        log.info("TTS 克隆: 样本已拼接为单文件（%.0f 秒）", total)
        return out_path
    except Exception as e:  # noqa: BLE001
        log.warning("TTS 克隆: 样本拼接失败，改用第一段: %s", e)
        return paths[0] if paths else None


def normalize_voice_id(vid: str) -> str:
    """voice_id 长度合规化（MiniMax 要求最少 8 个字符）。"""
    vid = (vid or "").strip() or "pet_voice"
    while len(vid) < 8:
        vid += "_clone"
    return vid[:256]


def clone_voice(api_key: str, voice_id: str, sample_paths: list[Path],
                base_url: str = "https://api.minimaxi.com/v1",
                group_id: str = "") -> str:
    """同步执行 声音克隆（上传样本 + 注册 voice_id）。失败抛异常。

    MiniMax 的 voice_clone 只收单个 file_id，多段样本先拼接成一个 wav；
    voice_id 长度不合规时自动补后缀。返回最终生效的 voice_id。
    """
    import httpx
    voice_id = normalize_voice_id(voice_id)
    base = base_url.rstrip("/")
    gid = f"?GroupId={group_id}" if group_id else ""
    headers = {"Authorization": f"Bearer {api_key}"}

    upload = sample_paths[0]
    if len(sample_paths) > 1:
        # 拼接临时文件放缓存目录，不污染用户样本目录
        concat = Path("assets/tts_cache/_pet_clone_concat.wav")
        concat.parent.mkdir(parents=True, exist_ok=True)
        upload = _concat_wavs(sample_paths, concat) or upload

    with httpx.Client(timeout=120) as client:
        log.info("TTS 克隆: 上传 %s ...", upload.name)
        resp = client.post(
            f"{base}/files/upload{gid}",
            headers=headers,
            data={"purpose": "voice_clone"},
            files={"file": (upload.name, upload.read_bytes(),
                            "application/octet-stream")},
        )
        data = resp.json()
        base_resp = data.get("base_resp") or {}
        if base_resp.get("status_code", 0) != 0:
            raise RuntimeError(f"样本上传失败: {base_resp}")
        fid = (data.get("file") or {}).get("file_id")
        if not fid:
            raise RuntimeError(f"上传响应无 file_id: {str(data)[:200]}")

        log.info("TTS 克隆: 注册 voice_id=%s（file_id=%s）...", voice_id, fid)
        resp = client.post(
            f"{base}/voice_clone{gid}",
            headers=headers,
            json={"file_id": int(fid), "voice_id": voice_id},
        )
        data = resp.json()
        base_resp = data.get("base_resp") or {}
        if base_resp.get("status_code", 0) != 0:
            raise RuntimeError(f"voice_clone 失败: {base_resp}")


def ensure_voice_cloned(api_key: str, voice_id: str, samples_src: str,
                        base_url: str, group_id: str,
                        marker_store) -> bool:
    """启动时自动克隆：样本指纹与上次一致则跳过；成功写标记。

    marker_store: SettingsStore 实例（持久化指纹标记）。
    返回是否执行了克隆。任何失败抛异常（调用方决定回退策略）。
    """
    paths = collect_samples(samples_src)
    if not paths or not voice_id:
        return False
    fp = samples_fingerprint(paths)
    if (marker_store.get("voice_clone_voice_id") == voice_id
            and marker_store.get("voice_clone_fingerprint") == fp):
        log.info("TTS 克隆: 音色「%s」已克隆过（样本未变），跳过", voice_id)
        return False
    log.info("TTS 克隆: 开始（voice_id=%s，%d 段样本，共 %.0f 秒）",
             voice_id, len(paths),
             sum(p.stat().st_size for p in paths) / 1024 / 44.1 / 2)
    clone_voice(api_key, voice_id, paths, base_url, group_id)
    marker_store.set("voice_clone_voice_id", voice_id)
    marker_store.set("voice_clone_fingerprint", fp)
    log.info("TTS 克隆: 完成")
    return True


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
