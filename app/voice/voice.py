"""TTS 语音合成 —— 使用 Microsoft edge-tts（免费，无需 API key）。

生成 mp3 后用 pygame 播放。本设计是「边生成边播放」的体验不必要，
所以走的是「回复完成后整段播一次」路线。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
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


def _cache_key(text: str) -> str:
    """生成**进程重启稳定**的缓存 key。

    原实现用 ``hash(text)``，受 PYTHONHASHSEED 影响，每次启动结果不同，
    缓存命中率会被打掉。这里换成 MD5（不需要加密，只要稳定 + 低碰撞）。
    """
    return hashlib.md5(text.encode("utf-8")).hexdigest()


# pygame.mixer 是进程级单例，**只 init 一次**，否则每次 init/quit 抖动大且会和
# 现有播放任务打架。下面用一个 module-level 标志位 + lock。
_MIXER_READY = False
_MIXER_LOCK = threading.Lock()


def _ensure_mixer() -> bool:
    """惰性初始化 pygame.mixer；返回是否成功。"""
    global _MIXER_READY
    with _MIXER_LOCK:
        if _MIXER_READY:
            return True
        try:
            import pygame  # 延迟 import
            pygame.mixer.init()
            _MIXER_READY = True
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("pygame.mixer.init 失败：%s", e)
            return False


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
        # 启动时清理过期缓存（7 天前的 mp3）
        self._clean_expired_cache()

    def _clean_expired_cache(self, max_age_days: int = 7) -> None:
        """清理超过 max_age_days 天的 TTS 缓存文件。"""
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
        """把 text 朗读出来；独立线程跑，不阻塞 Qt。"""
        if not self._enabled or not text.strip():
            return
        threading.Thread(target=self._speak_blocking, args=(text,),
                         daemon=True, name="tts").start()

    def _speak_blocking(self, text: str) -> None:
        try:
            # 移除 emoji 和装饰性符号，避免 TTS 朗读字符名称
            clean_text = _strip_emojis(text)
            if not clean_text:
                return
            cache_name = _cache_key(clean_text) + self.cache_ext
            cache_path = self.cache_dir / cache_name
            if not cache_path.is_file():
                asyncio.run(self._synthesize(text, cache_path))
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

    async def _synthesize(self, text: str, out_path: Path) -> None:
        # 延迟 import，避免启动时无 pygame/edge-tts 也能跑程序
        import edge_tts
        communicate = edge_tts.Communicate(text, voice=self.voice)
        await communicate.save(str(out_path))

    def _play(self, mp3_path: Path) -> None:
        try:
            import pygame
            if not _ensure_mixer():
                return
            # 复位 + 加载 + 播
            pygame.mixer.music.stop()
            pygame.mixer.music.load(str(mp3_path))
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                pygame.time.wait(100)
        except Exception as e:  # noqa: BLE001
            log.warning("播放 TTS 失败：%s", e)
        # 不再 quit(): mixer 留着进程级复用