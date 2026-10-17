"""TTS 语音合成：Microsoft edge-tts（免费，无需 API key）。"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import logging
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# 用于从 TTS 文本中移除的字符模式：
# - 所有 Unicode emoji（含变体选择符）
# - 所有装饰性符号和分隔符
def _build_emoji_pattern() -> re.Pattern:
    """构建 emoji 正则模式（用 chr() 避免转义问题）。"""
    ranges = [
        (0x1F000, 0x1FAFF),   # emoji
        (0x2600, 0x27BF),     # misc symbols & dingbats
        (0x1F1E6, 0x1F1FF),   # regional indicators
        (0x1FB00, 0x1FBFF),   # symbols supplement
        (0x2190, 0x21FF),     # arrows
        (0x200D, 0x200D),     # zero-width joiner
        (0xFE0F, 0xFE0F),     # variation selector-16
        (0xFE0E, 0xFE0E),     # variation selector-15
        (0xE0020, 0xE007F),   # supplemental symbols
    ]
    parts = []
    for lo, hi in ranges:
        if lo == hi:
            parts.append(chr(lo))
        else:
            parts.append(chr(lo) + "-" + chr(hi))
    return re.compile("[" + "".join(parts) + "]+")


_EMOJI_RE = _build_emoji_pattern()


def _strip_emojis(text: str) -> str:
    """从文本中移除所有 emoji 和装饰性符号，返回纯文本。

    TTS 引擎会朗读 emoji 的字符名称（如 "smiling face"），
    所以朗读前必须移除。保留中文、英文、数字、标点。
    """
    cleaned = _EMOJI_RE.sub("", text)
    # 移除多个连续空白
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _cache_key(text: str, tag: str = "") -> str:
    """生成进程重启稳定、且区分音色的缓存 key。

    用 MD5（不需要加密，只要稳定 + 低碰撞），并把音色 tag 一并纳入：
    否则同一段文本切换音色后会命中旧缓存、播错声音。
    """
    return hashlib.md5((tag + "\x00" + text).encode("utf-8")).hexdigest()


# pygame.mixer 是进程级单例，**只 init 一次**，否则每次 init/quit 抖动大且会和
# 现有播放任务打架。下面用一个 module-level 标志位 + lock。
_MIXER_READY = False
_MIXER_LOCK = threading.Lock()


def _ensure_mixer() -> bool:
    """惰性初始化 pygame.mixer；返回是否成功。

    SDL audio backend 选错（Win11 常见 DirectSound 失败）是桌宠播放无声的最大原因。
    显式指定 frequency=32000 + 单声道（匹配 GPT-SoVITS 输出），并设
    SDL_AUDIODRIVER=wasapi 让 SDL2 走 Win 11 现代音频 API，避开 DirectSound 的坑。
    """
    global _MIXER_READY
    with _MIXER_LOCK:
        if _MIXER_READY:
            return True
        try:
            import pygame  # 延迟 import
            # SDL backend：wasapi 在 Win10+ 稳定，directsound 经常没声
            os.environ.setdefault("SDL_AUDIODRIVER", "wasapi")
            # 32000Hz + 单声道 + 16-bit 匹配 GPT-SoVITS 输出（实测单声道 wav 在
            # 双声道 mixer 上会"只有左/右一个声道有声音"，看起来像没声音；
            # init 直接传参比 pre_init 可靠——pygame 1.9.6+ pre_init 会被
            # 后续的 init() 覆盖，pre_init 失败时 mixer init 用默认参数）
            pygame.mixer.init(frequency=32000, size=-16, channels=1, buffer=1024)
            log.info("pygame.mixer 初始化成功：%s", pygame.mixer.get_init())
            _MIXER_READY = True
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("pygame.mixer.init 失败：%s（可尝试设 SDL_AUDIODRIVER=directsound）", e)
            return False


def _normalize_wav_loudness(path: Path, target_rms: float = 0.30, max_gain: float = 8.0) -> None:
    """原地把 wav 振幅归一化到 target_rms（基于 RMS，不是 peak）。

    背景：GPT-SoVITS 输出的 wav RMS 经常只有 0.05-0.10（-26 ~ -20 dB），
    Windows Media Player 播出来极小声；pygame/winsound/.NET SoundPlayer 也是「能播但
    几乎听不到」。这里检测 RMS，< 0.15 时等比放大到 target_rms。

    幂等：已经归一化过的 wav（peak >= 0.7）会跳过。
    """
    try:
        import wave
        import numpy as np
    except ImportError:
        return
    try:
        with wave.open(str(path), "rb") as w:
            params = w.getparams()
            n = w.getnframes()
            raw = w.readframes(n)
        sw = params.sampwidth
        if sw != 2:                       # 只处理 16-bit（GPT-SoVITS 默认输出）
            return
        arr = np.frombuffer(raw, dtype=np.int16).astype(np.float64)
        if arr.size == 0:
            return
        # 取整段（不区分声道）：GPT-SoVITS 输出单声道 + 多声道都用全采样
        peak = float(np.abs(arr).max()) / 32768.0
        rms = float(np.sqrt(np.mean(arr * arr))) / 32768.0
        if peak >= 0.95:
            return                          # 已经接近满幅，强行放大只会削顶
        if rms >= target_rms:
            return                          # 目标音量已满足
        if rms < 0.001:
            return                          # 全静 wav，放大只会放底噪，跳过
        gain = min(target_rms / rms, max_gain)
        arr = np.clip(arr * gain, -32768, 32767).astype(np.int16)
        with wave.open(str(path), "wb") as w:
            w.setparams(params)
            w.writeframes(arr.tobytes())
        log.info("TTS 归一化：%s RMS %.3f -> %.3f (gain=%.1fx)",
                 path.name, rms, min(target_rms, rms * gain), gain)
    except Exception as e:                  # noqa: BLE001
        log.warning("TTS wav 归一化失败（%s）: %s", path.name, e)


def _cache_format_matches(path: Path, expected_ext: str) -> bool:
    """检查缓存文件实际字节格式与期望扩展名是否一致。

    历史遗留：GPT-SoVITS 子类早期没设置 cache_ext=''.wav''，所有缓存被写成 .mp3 但实际是 RIFF/WAV，
    pygame mixer 加载会报 `music_drmp3: corrupt mp3 file`。这里嗅探前 12 字节，对得上 RIFF/WAV
    视作 WAV，对得上 ID3/'\\xff\\xfb' 视作 MP3；都不像视作损坏 → 删除重合成。
    """
    try:
        with open(path, "rb") as fp:
            head = fp.read(12)
    except Exception:
        return False
    if not head:
        return False
    is_riff_wav = head[:4] == b"RIFF" and head[8:12] == b"WAVE"
    is_mp3 = (head[:3] == b"ID3"
              or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0))
    if expected_ext == ".wav":
        return is_riff_wav
    if expected_ext == ".mp3":
        return is_mp3
    return is_riff_wav or is_mp3


class TTS:
    """TTS 包装：异步合成 + 异步播放（独立线程，不阻塞 Qt 主循环）。

    使用前确保：
        pip install edge-tts pygame
    """

    def __init__(self, voice: str = "zh-CN-XiaoxiaoNeural",
                 cache_dir: str | Path = "assets/tts_cache"):
        self.voice = voice
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._enabled = True
        # 口型同步钩子：播放开始/结束时被调用（UIController 接到桌宠嘴巴）
        self.on_speak_start = None
        self.on_speak_end = None
        # 串行化：不要并发调用 speak() 抢同一个 mixer channel
        self._play_lock = threading.Lock()
        # 句子队列：所有 speak() 入队，由一个 daemon worker 顺序消费
        import queue as _queue
        self._speak_queue: _queue.Queue = _queue.Queue()
        self._speak_worker: Optional[threading.Thread] = None
        self._speak_worker_started = False
        self._speak_worker_lock = threading.Lock()
        # 启动时清理过期缓存（7 天前的 mp3）
        self._clean_expired_cache()

    def _clean_expired_cache(self, max_age_days: int = 7,
                             max_total_mb: int = 80) -> None:
        """清理 TTS 缓存。两道闸：
          1) 删 max_age_days 天前的 mp3（旧的同一句话也基本不会被再用到）
          2) 总大小超 max_total_mb 时按 mtime 删最老的一半，LRU 风格截断
        """
        try:
            import time as _t
            cutoff = _t.time() - max_age_days * 86400
            removed = 0
            for f in self.cache_dir.glob("*.mp3"):
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            if removed:
                log.info("TTS 缓存清理：删除 %d 个过期文件", removed)
            # 总大小截断：超 max_total_mb 时砍掉最老的一半
            files = sorted(self.cache_dir.glob("*.mp3"), key=lambda p: p.stat().st_mtime)
            total = sum(p.stat().st_size for p in files)
            if total > max_total_mb * 1024 * 1024 and len(files) > 4:
                keep = len(files) // 2    # 砍掉一半最老的，保留 50%
                for p in files[:-keep]:
                    p.unlink()
                    removed += 1
                log.info("TTS 缓存清理：超 %dMB 截断，删 %d 个最老文件", max_total_mb, len(files) - keep)
        except Exception as e:  # noqa: BLE001
            log.warning("TTS 缓存清理失败：%s", e)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, v: bool) -> None:
        self._enabled = v

    # 缓存文件后缀：edge-tts 默认 mp3；GPT-SoVITS / MiniMax 返回 wav 需覆盖为 .wav
    cache_ext: str = ".mp3"

    def speak(self, text: str) -> None:
        """把 text 按当前默认音色入朗读队列；worker 顺序合成 + 播放。"""
        if not self._enabled or not text.strip():
            return
        self._speak_queue.put((text, None))
        self._ensure_speak_worker()

    def speak_as(self, text: str, profile=None) -> None:
        """用指定音色（VoiceProfile）朗读；profile=None 即当前默认音色。"""
        if profile is None or not self._enabled or not text.strip():
            return self.speak(text)
        self._speak_queue.put((text, profile))
        self._ensure_speak_worker()

    def _ensure_speak_worker(self) -> None:
        """懒启动唯一 daemon worker 消费队列。"""
        with self._speak_worker_lock:
            if self._speak_worker_started:
                return
            self._speak_worker_started = True
        t = threading.Thread(target=self._speak_worker_loop, daemon=True,
                             name="tts-worker")
        self._speak_worker = t
        t.start()

    def _speak_worker_loop(self) -> None:
        import queue as _queue
        while True:
            try:
                item = self._speak_queue.get(timeout=0.5)
            except _queue.Empty:
                continue
            text, profile = item
            try:
                self._speak_blocking(text, profile)
            except Exception as e:  # noqa: BLE001
                log.warning("TTS worker 单句失败：%s", e)
            finally:
                self._speak_queue.task_done()

    def prepare(self, text: str, timeout_s: float = 30.0) -> bool:
        """同步合成（仅缓存，不播放）。返回是否成功。

        用于「聊天窗回复等语音准备好后一起显示」：调用方在文本 emit 前同步
        等到音频文件 ready，避免用户看到文本先于声音出现。

        Args:
            text: 待朗读文本（已 sanitize）
            timeout_s: 最长等待时间；超时返回 False（不致命，UI 仍继续）

        行为：
            - 已缓存且格式正确 → 立即返回 True
            - 未缓存 → 调 _synthesize 写到缓存 → 返回 True
            - 缓存格式不对 → unlink 后重合成 → 返回 True
        """
        clean_text = _strip_emojis(text or "")
        if not clean_text:
            return False
        cache_name = _cache_key(clean_text, self._voice_tag()) + self.cache_ext
        cache_path = self.cache_dir / cache_name
        try:
            if not cache_path.is_file():
                log.info("TTS: prepare 合成 → %s (引擎=%s)",
                         cache_name, type(self).__name__)
                asyncio.run(self._synthesize(text, cache_path, None))
            if not cache_path.is_file():
                log.warning("TTS: prepare 合成后文件不存在 %s", cache_path)
                return False
            if not _cache_format_matches(cache_path, self.cache_ext):
                log.warning("TTS: prepare 缓存格式不匹配 %s，删除重合成",
                            cache_path.name)
                try:
                    cache_path.unlink()
                except (OSError, ValueError, KeyError, TypeError) as e:
                    log.debug("ignored: %s", e)
                asyncio.run(self._synthesize(text, cache_path, None))
                if not cache_path.is_file():
                    return False
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("TTS: prepare 失败：%s", e)
            return False

    def _voice_tag(self) -> str:
        """当前默认音色的缓存区分标记（edge 用音色名；GPT-SoVITS 子类覆盖）。"""
        return str(self.voice or "")

    def _profile_tag(self, profile) -> str:
        """指定音色的缓存标记。"""
        if profile is None:
            return self._voice_tag()
        return str(getattr(profile, "ref_audio", "")
                   or getattr(profile, "name", "") or "")

    def set_voice(self, profile=None) -> None:
        """运行时切换默认音色（edge 用 edge_voice；GPT-SoVITS 子类覆盖）。"""
        if profile is not None and getattr(profile, "edge_voice", ""):
            self.voice = profile.edge_voice

    def _speak_blocking(self, text: str, profile=None) -> None:
        tag = self._profile_tag(profile)
        try:
            # 移除 emoji 和装饰性符号，避免 TTS 朗读字符名称
            clean_text = _strip_emojis(text)
            if not clean_text:
                log.info("TTS: text 被 sanitize 为空，跳过朗读")
                return
            cache_name = _cache_key(clean_text, tag) + self.cache_ext
            cache_path = self.cache_dir / cache_name
            if not cache_path.is_file():
                log.info("TTS: 合成并缓存 → %s (引擎=%s)", cache_name, type(self).__name__)
                asyncio.run(self._synthesize(text, cache_path, profile))
            if not cache_path.is_file():
                log.warning("TTS: 合成后文件不存在 %s", cache_path)
                return
            # 校验缓存文件与引擎格式一致：避免历史 .mp3 文件名实际是 wav 内容
            if not _cache_format_matches(cache_path, self.cache_ext):
                log.warning("TTS: 缓存 %s 与引擎格式 %s 不匹配，删除重合成",
                            cache_path.name, self.cache_ext)
                try:
                    cache_path.unlink()
                except (OSError, ValueError, KeyError, TypeError) as e:
                    log.debug("ignored: %s", e)
                log.info("TTS: 重新合成 → %s", cache_name)
                asyncio.run(self._synthesize(text, cache_path, profile))
                if not cache_path.is_file():
                    return
            # 口型同步：播放开始（工作线程回调，UI 层自行保证线程安全）
            if self.on_speak_start:
                try:
                    self.on_speak_start()
                except Exception:  # noqa: BLE001
                    pass
            # 串行排队，避免和上一段语音抢 mixer
            with self._play_lock:
                self._play(cache_path)
            if self.on_speak_end:
                try:
                    self.on_speak_end()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001
            # 友好提示：edge-tts 联网失败、minimax 鉴权/网络、gptsovits 服务未起等
            log.warning("TTS 失败：%s", e)
            log.warning("  ▸ 当前引擎=%s；可在设置面板切换或参考 docs/资源下载说明.md",
                        type(self).__name__)

    async def _synthesize(self, text: str, out_path: Path, profile=None) -> None:
        # 延迟 import，避免启动时无 pygame/edge-tts 也能跑程序
        import edge_tts
        voice = self.voice
        if profile is not None and getattr(profile, "edge_voice", ""):
            voice = profile.edge_voice
        communicate = edge_tts.Communicate(text, voice=voice)
        await communicate.save(str(out_path))

    def _play(self, mp3_path: Path) -> None:
        # GPT-SoVITS 输出 RMS 经常只有 0.05-0.10（-26dB），不归一化听起来几乎无声。
        # 幂等：已经大声的 wav（peak>=0.95 或 RMS>=0.30）会直接跳过。
        _normalize_wav_loudness(mp3_path)
        # Windows 优先用 .NET System.Media.SoundPlayer 通过 PowerShell 调：
        #  - 不依赖 SDL/SDL2（pygame mixer 的 DirectSound / wasapi backend 选错坑）
        #  - 不受 PyCharm run config 注入的环境变量影响
        #  - 支持任意 PCM 采样率（GPT-SoVITS 输出 32kHz；winsound.PlaySound 只支持
        #    22.05/44.1kHz 静默拒绝 32kHz，所以之前 winsound 路径调用成功但无声）
        #  - 异步 Play()，powershell 进程随音频播完退出
        if os.name == "nt":
            # 播放路径优先级（每个 path 都接受 32kHz mono wav）：
            #  1. ffplay (ffmpeg)              → 走 DirectSound/WASAPI 任意 backend，**不
            #                                   依赖桌宠进程的音频设备继承**，所以最稳
            #  2. PowerShell .NET SoundPlayer  → 走 winmm/MCI，会继承桌宠进程
            #                                   的默认音频设备（可能错/静音）
            #  3. winsound                      → 走 winmm（不支持 32kHz 多数时候无声）
            # 全部 fire-and-forget，不阻塞 worker 线程
            sdl_driver = os.environ.get("SDL_AUDIODRIVER", "(unset)")
            log.info("TTS: 准备播放 %s (%d 字节)  SDL_AUDIODRIVER=%s",
                     mp3_path.name, mp3_path.stat().st_size, sdl_driver)

            # Path 1: ffplay（最稳）—— **同步等播完**避免多句 LLM 回复并行
            # 触发 Popen 多句同时播导致声音重叠（_speak_blocking 串行，
            # 但 Popen 不阻塞 → 多 ffplay 进程同时持有 waveOut → 声音叠在一起）
            try:
                ffplay = shutil.which("ffplay")
                if ffplay:
                    import wave as _wave
                    try:
                        with _wave.open(str(mp3_path), "rb") as _w:
                            _dur = _w.getnframes() / max(_w.getframerate(), 1)
                    except Exception:
                        _dur = 10.0
                    log.info("TTS: ffplay 播放 → %s (%.1fs)", mp3_path.name, _dur)
                    proc = subprocess.Popen(
                        [ffplay, "-nodisp", "-autoexit", "-loglevel", "error",
                         "-hide_banner", "-t", str(_dur + 0.5), str(mp3_path)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=(subprocess.CREATE_NO_WINDOW
                                       if hasattr(subprocess, "CREATE_NO_WINDOW") else 0),
                    )
                    try:
                        proc.wait(timeout=_dur + 3)   # 同步等播完
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                    return
                log.warning("ffplay 不在 PATH，跳过")
            except Exception as e:
                log.warning("ffplay 路径失败：%s", e)

            # Path 2: PowerShell + .NET SoundPlayer（**PlaySync 同步**避免重叠）
            try:
                safe_path = str(mp3_path).replace("'", "''")
                # PlaySync 阻塞到播完（不是 Play 异步），所以多句不会重叠
                ps_cmd = f"(New-Object Media.SoundPlayer '{safe_path}').PlaySync()"
                log_stderr_path = mp3_path.parent / "_ps_stderr.log"
                stderr_file = open(log_stderr_path, "wb")
                proc = subprocess.Popen(
                    ["powershell", "-NoProfile", "-Command", ps_cmd],
                    stdout=stderr_file, stderr=stderr_file,
                    creationflags=(subprocess.CREATE_NO_WINDOW
                                   if hasattr(subprocess, "CREATE_NO_WINDOW") else 0),
                )
                try:
                    proc.wait(timeout=15)   # PlaySync 最长等 15s（防 .NET 卡死）
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                stderr_file.flush()
                with open(log_stderr_path, "rb") as f:
                    err = f.read()
                if err:
                    log.warning("PowerShell stderr: %s",
                                err.decode("utf-8", errors="ignore").strip()[:300])
                return
            except Exception as e:
                log.warning("PS SoundPlayer 启动失败：%s", e)

            # Path 3: winsound（兜底）
            try:
                import winsound
                winsound.PlaySound(
                    str(mp3_path),
                    winsound.SND_FILENAME | winsound.SND_ASYNC
                    | winsound.SND_NODEFAULT,
                )
                return
            except Exception as e:
                log.warning("winsound 也失败：%s", e)
        # 跨平台 fallback：pygame（macOS / Linux + Windows 兜底）
        try:
            import pygame
            if not _ensure_mixer():
                log.warning("pygame.mixer 未就绪，跳过播放 %s", mp3_path.name)
                return
            sdl_driver = os.environ.get("SDL_AUDIODRIVER", "(unset)")
            log.info("TTS: pygame 播放 → %s (%d 字节)  SDL_AUDIODRIVER=%s  mixer=%s",
                     mp3_path.name, mp3_path.stat().st_size,
                     sdl_driver, pygame.mixer.get_init())
            pygame.mixer.music.stop()
            pygame.mixer.music.load(str(mp3_path))
            pygame.mixer.music.play()
            import time as _t
            _t.sleep(0.1)
            log.info("TTS: 播放 100ms 后 get_busy=%s", pygame.mixer.music.get_busy())
            while pygame.mixer.music.get_busy():
                pygame.time.wait(100)
        except Exception as e:  # noqa: BLE001
            log.warning("播放 TTS 失败：%s（文件=%s）", e, mp3_path.name)