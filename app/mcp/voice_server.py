r"""语音 MCP server：桌宠**自己决定**要不要发语音 / 唱歌。

工具
----
  speak   念一段话（可选情绪）      -> 本地工具名 mcp_voice_speak
  sing    按旋律唱一段歌词 **或翻唱指定的某首歌** -> 本地工具名 mcp_voice_sing
  status  报服务在不在、有哪些音色可用 -> 本地工具名 mcp_voice_status

四条铁律（主人定的）
--------------------
1. **只复用本机已有的模型**（本机 GPT-SoVITS 服务 + 本机 RVC 权重），
   **绝不下载任何模型权重**；本机没有的音色就如实说「音色不在本地」。
2. **诚实**：做了什么、产物在哪、哪一步失败，都要说清楚；
   没产出文件就**不许**说「已发送」。
3. **语音条前面不加方括号标签**（「[翻唱-清唱版]」那种标签主人嫌出戏），
   要说话就另外发一条普通文字消息。
4. **翻唱不许造假**（主人投诉：「叫桌宠唱某某歌曲她并不会搜索这个歌并且调用 rvc
   翻唱，导致直接念自己编的词」）——唱《XXX》必须去找**真的那首歌**，
   找不到就**如实说找不到**，绝不允许把自己编的词当成那首歌唱给主人。

参考实现：E:\study\qq-bridge-main\qq-bridge-main\src\mcp-local-media.js
（local_speak / local_sing / local_media_status）。那边是「发到 QQ 会话」，
这边是「在本机放给主人听 + 落盘」，其余契约（措辞规则、失败语义、
绝不下载）保持一致。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

#: 项目根（app/mcp/voice_server.py → 上两级）
PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: 情绪参考库：本地情绪名 ← 主人/模型可能说的各种叫法
#: 键是本地 voice/<voice_dir>/emotions/<名字>.wav 的文件名（不含扩展名）。
EMOTION_ALIASES: dict[str, str] = {
    "中性": "neutral", "平静": "neutral", "普通": "neutral", "neutral": "neutral",
    "愤怒": "anger", "生气": "anger", "恼火": "anger", "anger": "anger", "angry": "anger",
    "惊讶": "surprise", "吃惊": "surprise", "surprise": "surprise", "surprised": "surprise",
    "悲伤": "sad", "难过": "sad", "伤心": "sad", "sad": "sad",
    "紧张": "nervous", "不安": "nervous", "nervous": "nervous",
    "恐惧": "fear", "害怕": "fear", "fear": "fear",
    "轻声": "soft", "温柔": "soft", "soft": "soft",
    "耳语": "whisper", "低语": "whisper", "whisper": "whisper",
}

#: 情绪名 → 中文标签（给 status 看）
EMOTION_LABELS: dict[str, str] = {
    "neutral": "中性", "anger": "愤怒", "surprise": "惊讶", "sad": "悲伤",
    "nervous": "紧张", "fear": "恐惧", "soft": "轻声", "whisper": "耳语",
}


# ---------------------------------------------------------------------------
# MCP 工具定义
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "speak",
        "description": (
            "用本机音色把一句话念出来（用的就是本机已经装好的语音模型，不下载任何东西）。"
            "适合：一句很短的提醒、哼一声、撒娇的一句吐槽、念句台词。"
            "不适合：长篇解释——那种用文字回就行。"
            "规矩：①语音条**前面不要加方括号标签**（例如「[语音]」「[朗读]」），"
            "主人嫌那玩意儿像在读任务卡；要说话就另外发一条普通文字消息；"
            "②语音是打断，不是每轮都要用，大部分回合用文字就好；"
            "③失败就如实告诉主人哪一步失败，**不要假装已经发出**。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string",
                         "description": "要念的话。中文一两句最好，太长会显得像念稿。"},
                "emotion": {"type": "string",
                            "description": "可选：情绪。可选值见 mcp_voice_status 的情绪参考列表"
                                           "（中性/愤怒/惊讶/悲伤/紧张/恐惧/轻声/耳语）。"
                                           "不填就用默认音色。"},
                "speed": {"type": "number",
                          "description": "可选：语速 0.5~2.0，默认 1.0。"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "sing",
        "description": (
            "用本机 RVC 唱歌。两种模式（song 和 lyrics **二选一**）：\n"
            "① **翻唱**（主人说「唱《XXX》」「翻唱某某歌」「来一首某某」）：传 song（歌名，可选 artist）。"
            "本机会上网找这首歌的**音频直链**，下载 → 分离人声 → 用「大肥鱼」的音色做音转音 → "
            "把原伴奏混回去（mix 决定混不混）→ 放给主人听。\n"
            "② **按旋律唱一段词**（主人说「唱一句」「唱段歌词」「哼一段」）：传 lyrics。"
            "和 mcp_voice_speak 的区别：speak 是平读（念白），sing 会给每个字配一个音高、"
            "并把音高在字内保持住，所以听起来是在唱而不是在念。"
            "旋律是**按这段词现编的调**，不是原创歌曲——要这么跟主人说明，别自称原创；"
            "歌词直接给文本就行，不要写曲谱或音符。\n"
            "**主人点了具体歌名就一定走 ①**：把歌名当 lyrics 唱出来 = 自己编词冒充那首歌，"
            "主人最烦这个（他原话：「叫桌宠唱某某歌曲她并不会搜索这个歌并且调用 rvc 翻唱，"
            "导致直接念自己编的词」）。\n"
            "规矩：①翻唱必须是**真的那首歌** —— 找不到就如实说「没找到这首歌」，"
            "**绝不许**把自己编的词当成那首歌唱，宁可说不唱；"
            "②翻唱出来的是**翻唱**（大肥鱼的嗓子唱原曲、伴奏是原曲的），不是原唱，要这么说明；"
            "伴奏被静音（清唱版）时也要说明；"
            "③找歌 / 下载 / 人声分离 / 音转音哪一步失败，就说**是哪一步**失败，不要含糊；"
            "④语音条前面**不要加方括号标签**，要说话就另外发一条普通文字消息；"
            "⑤语音是打断，不是每轮都要用，大部分回合用文字就好；"
            "⑥失败就如实说失败（哪一步没成），**不要假装唱过了**。\n"
            "耗时：整首翻唱要跑几分钟（下载 + 人声分离 + 音转音），耐心等一会儿；"
            "想快点就用 start/end 只唱一段（比如副歌）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "song": {"type": "string",
                         "description": "要翻唱的歌名（例：「月亮代表我的心」）。"
                                        "给了就走翻唱流程：找歌 → 下载 → 分离人声 → RVC 音转音 → 播放。"},
                "artist": {"type": "string",
                           "description": "可选：歌手名（例：「邓丽君」）。同名歌多的时候用它消歧。"},
                "url": {"type": "string",
                        "description": "可选：已知的音频直链（http/https 开头）。"
                                       "给了就跳过搜索，直接下载这一段（自己搜到直链时用这个）。"},
                "lyrics": {"type": "string",
                           "description": "按旋律唱的歌词。和 song 二选一："
                                          "主人没点具体某首歌、要的是「唱点什么」时才用它。"},
                "start": {"type": "number",
                          "description": "可选：只取歌曲的某一段（起始秒）。比如只唱副歌。"},
                "end": {"type": "number",
                        "description": "可选：片段结束秒（秒）。不填就到结尾。"},
                "mix": {"type": "string",
                        "description": "可选：原伴奏要不要混回去。"
                                       "auto=有伴奏就混（默认，完整翻唱）；on=必须带伴奏；"
                                       "off=只要人声（清唱版，伴奏被静音）。"},
                "mix_volume": {"type": "number",
                               "description": "可选：伴奏音量倍数，默认 0.75（压在人声下面）。"},
                "pitch": {"type": "integer",
                          "description": "可选：变调半音数，-12 ~ 12。0 表示不改调。"},
                "speed": {"type": "number",
                          "description": "可选：语速（只对 lyrics 模式生效），0.85 左右更像唱。"},
                "seed": {"type": "integer",
                         "description": "可选：随机种子（只对 lyrics 模式生效）。"
                                        "换个数就是另一条旋律；不给就每次随机。"},
                "base": {"type": "number",
                         "description": "可选：旋律基准音高 Hz（只对 lyrics 模式生效，默认 329.63）。"
                                        "调高整体更亮，调低更沉。"},
            },
        },
    },
    {
        "name": "status",
        "description": (
            "查看本机语音服务是否就绪：TTS 合成服务在线与否、有哪些音色/情绪参考在本机。"
            "只读探测，**不会下载任何模型**；本机没有的会写明「音色不在本地」。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _ok(text: str) -> dict:
    """成功结果（MCP content 格式）。"""
    return {"content": [{"type": "text", "text": text}]}


def _fail(text: str) -> dict:
    """失败结果。前缀「失败：」是本项目的工具失败契约（见 app/engine/tools/_core.py）。"""
    return {"content": [{"type": "text", "text": "失败：" + text}], "isError": True}


def _cfg() -> dict:
    """读 companion_config.yaml 的 voice 段。"""
    try:
        from app.voice.voice import load_voice_settings
        return load_voice_settings() or {}
    except Exception as e:  # noqa: BLE001
        log.warning("读 voice 配置失败：%s", e)
        return {}


def _abs(path_str: str, base: Optional[Path] = None) -> Path:
    """相对路径按项目根（可指定 base）解析成绝对路径。"""
    p = Path(str(path_str)).expanduser()
    if p.is_absolute():
        return p
    return ((base or PROJECT_ROOT) / p)


def _out_dir(cfg: dict) -> Path:
    """语音产物目录（不存在就建）。"""
    d = _abs(cfg.get("output_dir") or "data/voice_out")
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:  # noqa: BLE001
        log.warning("建语音产物目录失败 %s：%s", d, e)
    return d


def _stamp() -> str:
    """文件名用的时间戳。"""
    return time.strftime("%Y%m%d-%H%M%S")


def _wav_info(path: Path) -> tuple[int, float]:
    """(字节数, 秒数)。读不出时长就返回 0.0。"""
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    dur = 0.0
    try:
        import wave
        with wave.open(str(path), "rb") as w:
            rate = w.getframerate() or 1
            dur = w.getnframes() / float(rate)
    except Exception:  # noqa: BLE001
        dur = 0.0
    return size, dur


def _human_kb(size: int) -> str:
    return "%.1f KB" % (size / 1024.0)


# ---------------------------------------------------------------------------
# 语音产物的**结构化旁路文件**（给界面用，不给模型看）
# ---------------------------------------------------------------------------
#
# 为什么不在工具结果文本里塞路径 / 时长的结构化字段：
#   工具结果文本是**给模型看的**，必须保持人话原样（模型看见机器标记
#   就会被带偏，主人也会看到一串乱码）。所以结构化信息走**旁路文件**：
#   语音产物落盘时，顺手在同一个目录写一份 <产物名>.voice.json，
#   里面是准确的路径 / 时长 / 体积。模型永远看不见它，界面拿得到。
#
# 约定（生产端写、界面端读，两边必须一致）：
#   · 文件名 = 产物文件名 + VOICE_META_SUFFIX
#   · 内容 = {"kind", "path", "file_name", "duration", "size", "created", "text"}

#: 语音产物旁路文件的后缀。
VOICE_META_SUFFIX = ".voice.json"


def voice_out_dir(cfg: Optional[dict] = None) -> Path:
    """语音产物目录（绝对路径）。

    生产端和界面端共用这一个函数 —— 两边各自算目录一定会算歪，
    界面就找不到语音产物了。
    """
    return _out_dir(cfg if cfg is not None else _cfg())


def write_voice_meta(wav_path: Path, kind: str, duration: float,
                     size: int, text: str = "") -> Optional[Path]:
    """给语音产物写一份结构化旁路文件（.voice.json）。

    写失败**不影响语音本身**：语音已经合成、已经放给主人听了，
    旁路文件只是给界面补一份准确信息，写不动就当没有（界面会退化成
    普通工具行，不会编造路径）。
    """
    try:
        meta = {
            "kind": str(kind or ""),
            "path": str(wav_path),
            "file_name": wav_path.name,
            "duration": round(float(duration), 3),
            "size": int(size),
            "created": time.time(),
            "text": str(text or "")[:200],
        }
        out = wav_path.with_name(wav_path.name + VOICE_META_SUFFIX)
        out.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("写语音旁路文件失败 %s：%s", wav_path, e)
        return None


# ---------------------------------------------------------------------------
# 本机资产盘点（绝不下载）
# ---------------------------------------------------------------------------

def _rvc_cfg(cfg: dict) -> dict:
    return (cfg.get("rvc") or {}) if isinstance(cfg.get("rvc"), dict) else {}


def _rvc_weight_path(cfg: dict) -> Path:
    """本机 RVC 权重文件的绝对路径（RVC 根/assets/weights/<model>）。"""
    rvc = _rvc_cfg(cfg)
    model = str(rvc.get("model") or "da_fei_yu.pth")
    if os.path.isabs(model):
        return Path(model)
    root = _abs(rvc.get("root") or "")
    return root / "assets" / "weights" / model


def _local_rvc_models(cfg: dict) -> list[str]:
    """本机已有的 RVC 音色权重文件名（列目录，不下载）。"""
    rvc = _rvc_cfg(cfg)
    root = _abs(rvc.get("root") or "")
    wdir = root / "assets" / "weights"
    if not wdir.is_dir():
        return []
    out = []
    for p in sorted(wdir.glob("*.pth")):
        try:
            out.append("%s（%s）" % (p.name, _human_kb(p.stat().st_size)))
        except OSError:
            out.append(p.name)
    return out


def _emotion_dir(cfg: dict) -> Path:
    return _abs(cfg.get("voice_dir") or "voice/大肥鱼") / "emotions"


def _local_emotions(cfg: dict) -> list:
    """本机情绪参考库：[(本地名, wav 路径, 参考文本)]。"""
    edir = _emotion_dir(cfg)
    out = []
    if not edir.is_dir():
        return out
    for wav in sorted(edir.glob("*.wav")):
        lab = wav.with_suffix(".lab")
        prompt = ""
        if lab.is_file():
            try:
                prompt = lab.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                prompt = ""
        out.append((wav.stem, wav, prompt))
    return out


def _probe_tts(url: str, timeout: float = 4.0) -> tuple[bool, str]:
    """探活 TTS 服务。返回 (在线?, 说明)。"""
    base = url.rstrip("/")
    try:
        req = urllib.request.Request(base + "/", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = resp.status
        # api_v2 的根路径返回 404，但能连上就说明服务在
        return True, "在线（HTTP %d）" % code
    except urllib.error.HTTPError as e:
        return True, "在线（HTTP %d）" % e.code
    except Exception as e:  # noqa: BLE001
        return False, "离线（%s）" % (str(e) or type(e).__name__)


# ---------------------------------------------------------------------------
# voice_speak：走本机 GPT-SoVITS
# ---------------------------------------------------------------------------

def _resolve_ref(cfg: dict, emotion: Optional[str]):
    """挑参考音频。返回 (wav 路径 or None, 参考文本, 本地情绪名, 备注)。

    - 情绪能对上本地情绪库 → 用对应的参考音频（连韵律一起换）
    - 情绪对不上 → 退回默认音色，备注里说清楚（不编，也不下载）
    - 本机一个参考音频都没有 → 返回 None（调用方报「音色不在本地」）
    """
    edir = _emotion_dir(cfg)
    note = ""
    name = ""
    if emotion:
        name = EMOTION_ALIASES.get(str(emotion).strip(), "")
        if not name:
            note = "本机没有「%s」这个情绪参考，用默认音色念了" % str(emotion).strip()
    if not name:
        default = str(cfg.get("default_emotion") or "neutral")
        name = EMOTION_ALIASES.get(default, "neutral")
        if emotion and not note:
            note = "情绪「%s」不在本机情绪库里，用默认音色念了" % str(emotion).strip()

    wav = edir / ("%s.wav" % name)
    lab = edir / ("%s.lab" % name)
    if wav.is_file():
        prompt = ""
        if lab.is_file():
            try:
                prompt = lab.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                prompt = ""
        return wav, prompt, name, note

    # 没有情绪库 → 退回音色目录下的主参考音频
    vdir = _abs(cfg.get("voice_dir") or "voice/大肥鱼")
    for cand in sorted(vdir.glob("*_ref.wav")):
        prompt = ""
        lab2 = cand.with_suffix(".lab")
        if lab2.is_file():
            try:
                prompt = lab2.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                prompt = ""
        return cand, prompt, name, note
    return None, "", name, note


def handle_speak(args: dict) -> dict:
    """voice_speak：用本机 GPT-SoVITS 念一段话。"""
    text = str(args.get("text") or "").strip()
    if not text:
        return _fail("要念的话不能为空。")
    cfg = _cfg()
    url = str(cfg.get("tts_url") or "http://127.0.0.1:9880")

    ref, prompt, emo_name, note = _resolve_ref(cfg, args.get("emotion"))
    if ref is None:
        return _fail("音色不在本地：%s 下没有可用的参考音频（本工具不会下载任何模型）。"
                     % _abs(cfg.get("voice_dir") or "voice/大肥鱼"))

    speed = args.get("speed")
    try:
        speed = float(speed) if speed is not None else 1.0
    except (TypeError, ValueError):
        speed = 1.0
    speed = max(0.5, min(2.0, speed))

    payload = {
        "text": text,
        "text_lang": "zh",
        "ref_audio_path": str(ref),
        "prompt_text": prompt,
        "prompt_lang": "zh",
        "media_type": "wav",
        "streaming_mode": False,
        "text_split_method": "cut5",
        "speed_factor": speed,
    }
    try:
        req = urllib.request.Request(
            url.rstrip("/") + "/tts",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            audio = resp.read()
    except Exception as e:  # noqa: BLE001
        return _fail("合成失败：TTS 服务 %s 没响应（%s）。"
                     "本机没装好这个服务时**不会**去下载模型。" % (url, str(e) or type(e).__name__))
    if not audio or len(audio) < 100:
        return _fail("合成失败：TTS 服务返回了 %d 字节，不是音频。" % len(audio or b""))

    out = _out_dir(cfg) / ("speak_%s.wav" % _stamp())
    try:
        out.write_bytes(audio)
    except OSError as e:
        return _fail("写文件失败：%s（%s）" % (out, e))
    size, dur = _wav_info(out)
    if size <= 0:
        return _fail("写文件失败：%s 是空的。" % out)

    # 结构化旁路文件：给界面做语音条用（模型看不到它）
    write_voice_meta(out, "speak", dur, size, text)
    played = _play(out)
    lines = ["念完了：%s（%s，%.1f 秒）。" % (text, _human_kb(size), dur)]
    if emo_name:
        lines.append("音色：%s。" % EMOTION_LABELS.get(emo_name, emo_name))
    if note:
        lines.append("（%s）" % note)
    if played:
        lines.append("已经放给主人听了。")
    else:
        lines.append("（本机没放出来，音频在 %s）" % out)
    return _ok(" ".join(lines))


def _play(path: Path) -> bool:
    """把音频放给主人听。失败不抛（调用方如实写进结果）。"""
    try:
        from app.voice.voice import play_audio_file
        return bool(play_audio_file(path))
    except Exception as e:  # noqa: BLE001
        log.warning("播放失败 %s：%s", path, e)
        return False


# ---------------------------------------------------------------------------
# 翻唱（cover）：找歌 → 下载 → 分离人声 → RVC 音转音 → 混伴奏 → 播放
# ---------------------------------------------------------------------------
#
# 这是 qq-bridge 的 local_fetch_audio + local_voice_convert 的本机版：
#   1. 找歌：先走**项目自己的 web_search 后端**（Tavily / DuckDuckGo），
#      找不到音频直链再退到免 key 的公开音源（网易云）；
#   2. 下载 + 人声分离：复用 E:\SnowLuma\rvc_fetch.py（**脚本不改**），
#      产物落进本地语音库 <voice_lib>/incoming/；
#   3. 音转音：复用本机 RVC 的 infer.cli，把干声换成「大肥鱼」的嗓子；
#   4. 混伴奏：ffmpeg 把原伴奏混回人声（mix=auto/on/off）。
#
# 三条硬规矩（跟文件头的铁律一样）：
#   · **绝不下载模型**：分离模型 / 音色权重必须本地已有，缺了就报错。
#     （rvc_fetch.py 里的 pymss 在模型缺失时会**自动下载**，所以这里
#      **先检查再调用**，绝不把 "--separate" 交给一台没有模型的机器。）
#   · **找不到歌就说找不到**：绝不拿自己编的词冒充那首歌。
#   · **哪一步失败说哪一步**：下载 / 分离 / 音转音 / 混音分开报。

#: 音频直链的扩展名（用来把「播放页」和「音频直链」分开）
_AUDIO_EXT_RE = re.compile(r"\.(mp3|m4a|flac|wav|aac|ogg|opus)(?:$|\?)", re.I)

#: 找歌 / 下载用的 UA（音源 CDN 认这个）
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

#: 网易云公开搜索 / 取直链的地址（免 key）
_NETEASE_SEARCH = "https://music.163.com/api/search/get/web?csrf_token="
_NETEASE_OUTER = "http://music.163.com/song/media/outer/url?id=%d.mp3"


def _norm_text(s: str) -> str:
    """歌名 / 歌手名归一化：去掉括号内容、空白和标点，只留字母数字汉字。"""
    s = re.sub(r"[（(\[【《].*?[）)\]】》]", "", str(s or ""))
    return re.sub(r"[\s\W_]+", "", s, flags=re.UNICODE).lower()


def _safe_stem(text: str, limit: int = 24) -> str:
    """歌名 → 文件名安全片段（保留中文和字母数字）。"""
    s = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", str(text or "").strip(), flags=re.UNICODE)
    return (s.strip("_") or "song")[:limit]


def _is_direct_audio(url: str) -> bool:
    """这个 URL 看起来是不是**音频直链**（而不是播放页）。"""
    return bool(_AUDIO_EXT_RE.search(str(url or "").split("#", 1)[0]))


def _http_text(url: str, data: Optional[bytes] = None,
               headers: Optional[dict] = None, timeout: float = 20.0) -> str:
    """发一个 HTTP 请求并返回文本（失败抛异常，调用方自己接）。"""
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _probe_audio(url: str, timeout: float = 20.0) -> Optional[dict]:
    """探测一个 URL 是不是真的音频（只取前面几个字节，不下整个文件）。

    返回 {"type": content-type, "size": 长度} 或 None（不是音频 / 抓不到）。
    """
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": _UA, "Range": "bytes=0-2047"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            resp.read(2048)
            return {"type": ctype, "size": resp.headers.get("Content-Length")}
    except Exception as e:  # noqa: BLE001
        log.debug("探测音频直链失败 %s：%s", url, e)
        return None


def _tavily_key() -> str:
    """项目自己的搜索后端（Tavily）用的 key —— 只取用，**不打印**。

    优先 config.yaml 的 brain.tavily_api_key，其次环境变量 TAVILY_API_KEY。
    """
    try:
        from app.core.config import load_config
        cfg = load_config()
        key = getattr(getattr(cfg, "brain", None), "tavily_api_key", "") or ""
        if key:
            return str(key)
    except Exception as e:  # noqa: BLE001
        log.debug("读 Tavily key 失败：%s", e)
    return str(os.environ.get("TAVILY_API_KEY") or "")


def _project_web_search(query: str, limit: int = 8) -> tuple:
    """用**项目自己的** web_search 后端（Tavily / DuckDuckGo）搜。

    返回 (结果列表, 说明)。两个后端都不可用时返回 ([], 为什么) ——
    **如实说搜不了**，绝不假装搜过。
    """
    try:
        from app.engine.tools import _search as s
    except Exception as e:  # noqa: BLE001
        return [], "本机没装项目的搜索后端（%s）" % (str(e) or type(e).__name__)
    key = _tavily_key()
    problems = []
    if key:
        try:
            res = s._tavily_search(query, limit, key, 15.0)
            if res:
                return res, "Tavily"
            problems.append("Tavily 没搜到结果")
        except Exception as e:  # noqa: BLE001
            problems.append("Tavily 失败（%s）" % (str(e) or type(e).__name__))
    else:
        problems.append("没配 Tavily key")
    try:
        res = s._ddg_search(query, limit, 15.0)
        if res:
            return res, "DuckDuckGo"
        problems.append("DuckDuckGo 没搜到结果")
    except Exception as e:  # noqa: BLE001
        problems.append("DuckDuckGo 不可用（%s）" % (str(e) or type(e).__name__))
    return [], "；".join(problems)


def _search_direct_links(song: str, artist: str = "") -> tuple:
    """走项目自己的搜索后端，找这首歌的**音频直链**。

    返回 (直链 or None, 用的哪个后端, 过程说明)。
    """
    notes = []
    want = _norm_text(song)
    base = ("%s %s" % (song, artist)).strip()
    queries = [base + " mp3", base + " 音频 下载 直链"]
    for q in queries:
        res, backend = _project_web_search(q, 8)
        notes.append("「%s」→ %s" % (q, backend))
        if not res:
            continue
        cands = []
        for r in res:
            url = str(r.get("url") or "")
            if not _is_direct_audio(url):
                continue
            score = 1
            if want and want in _norm_text(url):
                score = 3
            elif want and want in _norm_text(r.get("title") or ""):
                score = 2
            cands.append((score, url))
        cands.sort(key=lambda x: -x[0])
        for _score, url in cands[:3]:
            probe = _probe_audio(url)
            if probe and (probe["type"].startswith("audio/") or _is_direct_audio(url)):
                notes.append("直链 %s（%s）" % (url[:80], probe["type"] or "未知类型"))
                return url, backend, notes
    return None, "", notes


def _netease_candidates(song: str, artist: str = "", limit: int = 8) -> list:
    """网易云公开搜索：找同名歌。返回原始 song 列表。"""
    q = ("%s %s" % (song, artist)).strip()
    body = urllib.parse.urlencode(
        {"s": q, "type": "1", "offset": "0", "total": "true", "limit": str(limit)}
    ).encode("utf-8")
    text = _http_text(_NETEASE_SEARCH, data=body,
                       headers={"User-Agent": _UA, "Referer": "https://music.163.com/"},
                       timeout=20)
    obj = json.loads(text)
    return (obj.get("result") or {}).get("songs") or []


def _netease_pick(song: str, artist: str = "") -> Optional[dict]:
    """在网易云结果里挑最像的那首（歌名对得上、且拿得到下载直链）。"""
    want = _norm_text(song)
    want_artist = _norm_text(artist)
    best = None
    best_score = 0
    for s in _netease_candidates(song, artist):
        name = str(s.get("name") or "")
        artists = "/".join(str(a.get("name") or "") for a in (s.get("artists") or []))
        n = _norm_text(name)
        if not n or not want:
            continue
        if n == want:
            score = 4
        elif n.startswith(want) or want.startswith(n):
            score = 3
        elif want in n:
            score = 2
        else:
            continue
        if want_artist and want_artist in _norm_text(artists):
            score += 2
        # fee=1 是 VIP：网易云不给下载直链（会跳 404），跳过
        try:
            fee = int(s.get("fee") or 0)
        except (TypeError, ValueError):
            fee = 0
        if fee == 1:
            continue
        if score > best_score:
            best_score = score
            best = {"id": s.get("id"), "name": name, "artists": artists}
    return best


def _netease_direct_url(song_id: int) -> Optional[str]:
    """网易云歌曲 → 音频直链（CDN 上的 mp3）。拿不到就 None。"""
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(
        _NETEASE_OUTER % int(song_id),
        headers={"User-Agent": _UA, "Referer": "https://music.163.com/"})
    try:
        resp = opener.open(req, timeout=20)
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        resp.read(1024)
        if ctype.startswith("audio/"):
            return resp.geturl()
        return None
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location") or ""
        if e.code in (301, 302, 303, 307) and _is_direct_audio(loc):
            return loc
        return None
    except Exception as e:  # noqa: BLE001
        log.debug("取网易云直链失败：%s", e)
        return None


def _find_audio_url(song: str, artist: str = "") -> tuple:
    """找一首歌的音频直链。

    返回 (直链 or None, 音源说明, 过程说明)。
    找不到就返回 (None, "", 说明) —— 调用方必须**如实说没找到**，
    绝不拿编的词冒充这首歌。
    """
    notes = []
    url, backend, n = _search_direct_links(song, artist)
    notes.extend(n)
    if url:
        return url, "搜索后端（%s）找到的音频直链" % backend, "；".join(notes)
    # 退到免 key 的公开音源（网易云）
    try:
        pick = _netease_pick(song, artist)
    except Exception as e:  # noqa: BLE001
        pick = None
        notes.append("网易云搜索失败（%s）" % (str(e) or type(e).__name__))
    if pick:
        direct = _netease_direct_url(pick["id"])
        if direct:
            notes.append("网易云匹配到《%s》-%s" % (pick["name"], pick["artists"]))
            return (direct,
                    "网易云公开直链（《%s》-%s）" % (pick["name"], pick["artists"]),
                    "；".join(notes))
        notes.append("网易云匹配到《%s》-%s，但这首拿不到下载直链（多半是 VIP）"
                     % (pick["name"], pick["artists"]))
    return None, "", "；".join(notes)


# ---------------------------------------------------------------------------
# 翻唱的三步：下载+分离（rvc_fetch.py）/ 音转音（infer.cli）/ 混伴奏（ffmpeg）
# ---------------------------------------------------------------------------

def _num(value, default: float = 0.0) -> float:
    """把参数里的数字取出来（取不到就用默认值）。"""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _fetch_script_path(cfg: dict) -> Path:
    """下载 + 人声分离脚本（E:\\SnowLuma\\rvc_fetch.py）。"""
    return _abs(_rvc_cfg(cfg).get("fetch_script") or "")


def _separate_model(cfg: dict) -> tuple:
    """本机已有的**人声分离模型**。

    返回 (模型路径 or None, pymss 模型根目录 or None)。
    找不到就返回 (None, None) —— 调用方必须报错，**绝不下载**。

    查找的地方跟 tools/pymss/model_registry.py 一致：
    PYMSS_MODEL_DIR → <RVC 根>/all_models → ~/.cache/pymss/models，
    目录里再按分类名（experimental_general）放的那一份也认。
    """
    name = str(_rvc_cfg(cfg).get("separate_model")
               or "bs_roformer_anvuew_sdr_12.45.ckpt")
    roots = []
    env = os.environ.get("PYMSS_MODEL_DIR")
    if env:
        roots.append(Path(env))
    rvc_root = _abs(_rvc_cfg(cfg).get("root") or "")
    if rvc_root.is_dir():
        roots.append(rvc_root / "all_models")
    roots.append(Path.home() / ".cache" / "pymss" / "models")
    for root in roots:
        for cand in (root / name, root / "experimental_general" / name):
            if cand.is_file():
                return cand, root
    return None, None


def _voice_lib_dir(cfg: dict) -> Path:
    """翻唱用的本地语音库目录（rvc_fetch.py 往这儿放下载+分离好的音频）。"""
    lib = str(_rvc_cfg(cfg).get("voice_lib") or "")
    d = _abs(lib) if lib else (_out_dir(cfg) / "voice_lib")
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:  # noqa: BLE001
        log.warning("建语音库目录失败 %s：%s", d, e)
    return d


def _ffmpeg_path(cfg: dict) -> str:
    """ffmpeg 可执行文件（混伴奏用）。配置里没配就找 PATH 上的。"""
    raw = str(_rvc_cfg(cfg).get("ffmpeg") or "").strip()
    if raw:
        p = Path(raw)
        if p.is_file():
            return str(p)
    return shutil.which("ffmpeg") or "ffmpeg"


def _fetch_stage(stdout: str) -> str:
    """从 rvc_fetch.py 自己的打印判断它卡在哪一步（好如实说哪一步失败）。"""
    if "已下载" not in stdout:
        return "下载"
    if "已转码" not in stdout:
        return "转码"
    if "分离产物" not in stdout:
        return "人声分离"
    return "落盘"


def _run_fetch(cfg: dict, url: str, lib_dir: Path, start: float = 0.0,
               end: float = 0.0, sep_model: Optional[Path] = None,
               sep_model_dir: Optional[Path] = None) -> dict:
    """调 rvc_fetch.py 下载（+ 可选分离人声）。

    返回 {"ok": bool, "payload": {...}, "stage": 阶段, "error": 原因}。
    """
    rvc = _rvc_cfg(cfg)
    py_path = _abs(rvc.get("python") or "")
    script = _fetch_script_path(cfg)
    rvc_root = _abs(rvc.get("root") or "")
    cmd = [str(py_path), str(script), "--url", url, "--lib-dir", str(lib_dir)]
    if start > 0:
        cmd += ["--start", str(start)]
    if end > 0:
        cmd += ["--end", str(end)]
    if sep_model is not None:
        cmd += ["--separate", "--model", sep_model.name]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    if sep_model_dir is not None:
        # 告诉 pymss 模型在哪儿 —— 免得它以为自己没模型就去下载
        env["PYMSS_MODEL_DIR"] = str(sep_model_dir)
    timeout = float(rvc.get("fetch_timeout_s") or 1800)
    try:
        proc = subprocess.run(
            cmd, cwd=str(rvc_root) if rvc_root.is_dir() else None,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return {"ok": False, "payload": {}, "stage": "下载+分离",
                "error": "超过 %d 秒还没结束，已放弃" % int(timeout)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "payload": {}, "stage": "下载+分离",
                "error": "跑不动下载脚本（%s）" % (str(e) or type(e).__name__)}
    out = proc.stdout or ""
    blob = out + "\n" + (proc.stderr or "")
    payload = None
    for line in reversed([x.strip() for x in out.splitlines() if x.strip()]):
        if line.startswith("{"):
            try:
                payload = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    if payload and payload.get("ok"):
        return {"ok": True, "payload": payload, "stage": "", "error": ""}
    detail = ""
    if payload and payload.get("error"):
        detail = str(payload["error"])
    else:
        tail = [x for x in blob.splitlines() if x.strip()]
        detail = tail[-1][:300] if tail else "（脚本没有输出）"
    return {"ok": False, "payload": {}, "stage": _fetch_stage(out),
            "error": detail}


def _run_rvc(cfg: dict, src: Path, out: Path, pitch: int = 0) -> tuple:
    """跑一次 RVC 音转音（infer.cli）。

    src 可以是单个文件，也可以是一个**目录**（目录模式 = 一次模型加载跑多块）。
    返回 (成功?, 失败原因)。
    """
    rvc = _rvc_cfg(cfg)
    py_path = _abs(rvc.get("python") or "")
    rvc_root = _abs(rvc.get("root") or "")
    weight = _rvc_weight_path(cfg)
    index_rate = _num(rvc.get("index_rate"), 0.75)
    timeout = float(rvc.get("convert_timeout_s") or 900)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"

    def _run(rate: float):
        cmd = [str(py_path), "-m", "infer.cli",
               "--model", str(weight),
               "--input", str(src),
               "--output", str(out),
               "--f0-method", str(rvc.get("f0_method") or "rmvpe"),
               "--protect", str(rvc.get("protect") or 0.33),
               "--index-rate", str(rate),
               "--overwrite"]
        if pitch:
            cmd += ["--pitch", str(pitch)]
        return subprocess.run(
            cmd, cwd=str(rvc_root) if rvc_root.is_dir() else None,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=env)

    try:
        proc = _run(index_rate)
    except subprocess.TimeoutExpired:
        return False, "RVC 推理超过 %d 秒还没结束，已放弃" % int(timeout)
    except Exception as e:  # noqa: BLE001
        return False, "跑不动 RVC 脚本（%s）" % (str(e) or type(e).__name__)
    blob = (proc.stderr or "") + "\n" + (proc.stdout or "")
    if proc.returncode != 0 or not (out.is_file() or out.is_dir()):
        # 索引缺失只是「检索增强」没了 —— 退化成无索引再跑一次
        if index_rate > 0 and ("index" in blob.lower() or "索引" in blob):
            try:
                proc = _run(0)
                blob = (proc.stderr or "") + "\n" + (proc.stdout or "")
            except Exception as e:  # noqa: BLE001
                return False, ("无索引重跑也失败（%s）"
                               % (str(e) or type(e).__name__))
        else:
            tail = [x for x in blob.splitlines() if x.strip()]
            detail = tail[-1][:300] if tail else "（没有输出）"
            return False, "RVC 返回码 %s，最后一行：%s" % (proc.returncode, detail)
    if out.is_file() and out.stat().st_size <= 0:
        tail = [x for x in blob.splitlines() if x.strip()]
        detail = tail[-1][:300] if tail else "（没有输出）"
        return False, "RVC 没有产出音频（%s）" % detail
    return True, ""


def _run_convert(cfg: dict, src: Path, out: Path, pitch: int = 0) -> tuple:
    """RVC 音转音：把干声换成「大飞鱼」的嗓子。

    长音频**分块**跑（一次模型加载 + N 块，最后拼起来）：
    整段一次喂进去显存吃太狠，本机 GPU 一满就慢到没法用
    （本机实测 180 秒：整段 >8 分钟，分成 6 块只要 39 秒）。

    返回 (成功?, 失败原因)。
    """
    chunk_s = _num(_rvc_cfg(cfg).get("convert_chunk_s"), 30.0)
    src_dur = _wav_info(src)[1]
    if chunk_s <= 0 or src_dur <= chunk_s * 1.2:
        return _run_rvc(cfg, src, out, pitch)

    work = out.parent / (out.stem + "_chunks")
    out_dir = out.parent / (out.stem + "_chunks_out")
    for d in (work, out_dir):
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as e:  # noqa: BLE001
            return False, "建分块目录失败 %s（%s）" % (d, e)
    # ① 切块
    split = subprocess.run(
        [_ffmpeg_path(cfg), "-y", "-v", "error", "-i", str(src),
         "-f", "segment", "-segment_time", str(chunk_s),
         "-c", "copy", str(work / "chunk_%03d.wav")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300)
    chunks = sorted(work.glob("chunk_*.wav"))
    if split.returncode != 0 or not chunks:
        return False, "切块失败（RVC 一次吃不下这么长的音频）"
    # ② 一次模型加载跑完所有块
    ok, err = _run_rvc(cfg, work, out_dir, pitch)
    if not ok:
        return False, err
    pieces = sorted(out_dir.glob("chunk_*.wav"))
    if len(pieces) != len(chunks):
        return False, "分块音转音只出了 %d/%d 块" % (len(pieces), len(chunks))
    # ③ 拼回去
    listing = work / "concat.txt"
    try:
        listing.write_text("".join("file '%s'\n" % p.as_posix()
                                   for p in pieces), encoding="utf-8")
    except OSError as e:  # noqa: BLE001
        return False, "写拼接清单失败（%s）" % e
    merged = subprocess.run(
        [_ffmpeg_path(cfg), "-y", "-v", "error", "-f", "concat", "-safe", "0",
         "-i", str(listing), "-c", "copy", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=300)
    if merged.returncode != 0 or not out.is_file() or out.stat().st_size <= 0:
        tail = [x for x in (merged.stderr or "").splitlines() if x.strip()]
        return False, "拼接失败：%s" % (tail[-1][:200] if tail else "（没有输出）")
    # 分块用完就删 —— 别在产物目录里堆几十兆垃圾
    for d in (work, out_dir):
        shutil.rmtree(d, ignore_errors=True)
    return True, ""


def _mix_with_instrumental(cfg: dict, vocal: Path, instrumental: Path,
                            out: Path, volume: float = 0.75) -> tuple:
    """把原伴奏混回翻唱的人声里（ffmpeg）。

    返回 (成功?, 失败原因)。
    """
    cmd = [_ffmpeg_path(cfg), "-y", "-v", "error",
           "-i", str(vocal), "-i", str(instrumental),
           # 两路先统一到 48k（人声是 RVC 的 48k，伴奏是下载时的 44.1k）。
           # normalize=0 必须给：amix 默认会把和再除以 2，白白小一档。
           "-filter_complex",
           "[0:a]aresample=48000,volume=1.0[v];[1:a]aresample=48000,volume=%s[m];"
           "[v][m]amix=inputs=2:duration=longest:normalize=0[out]" % volume,
           "-map", "[out]", "-ac", "1", str(out)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=300)
    except subprocess.TimeoutExpired:
        return False, "ffmpeg 混音超过 300 秒还没结束"
    except Exception as e:  # noqa: BLE001
        return False, "跑不动 ffmpeg（%s）" % (str(e) or type(e).__name__)
    if not out.is_file() or out.stat().st_size <= 0:
        tail = [x for x in (proc.stderr or "").splitlines() if x.strip()]
        return False, tail[-1][:200] if tail else "ffmpeg 没有产出文件"
    return True, ""


def handle_cover(args: dict) -> dict:
    """voice_sing(song=...)：翻唱一首歌。

    找歌 → 下载 →（可选）分离人声 → RVC 音转音 →（可选）混伴奏 → 播放。
    """
    song = str(args.get("song") or "").strip()
    artist = str(args.get("artist") or "").strip()
    url = str(args.get("url") or "").strip()
    if not song and not url:
        return _fail("要翻唱就得给歌名（song），或者给一个音频直链（url）。")
    t0 = time.time()
    cfg = _cfg()
    rvc = _rvc_cfg(cfg)

    # ① 本机资产盘点 —— 缺了就如实说，**绝不下载**
    py_path = _abs(rvc.get("python") or "")
    rvc_root = _abs(rvc.get("root") or "")
    weight = _rvc_weight_path(cfg)
    fetch_script = _fetch_script_path(cfg)
    if not py_path.is_file():
        return _fail("RVC 解释器不在本机：%s（本工具不会下载或安装任何东西）。" % py_path)
    if not rvc_root.is_dir():
        return _fail("RVC 项目不在本机：%s（本工具不会下载任何东西）。" % rvc_root)
    if not weight.is_file():
        return _fail("音色不在本地：%s（不会自动下载模型权重）。" % weight)
    if not fetch_script.is_file():
        return _fail("下载脚本不在本机：%s（本工具不会下载任何东西）。" % fetch_script)

    sep_model, sep_model_dir = _separate_model(cfg)
    separated = sep_model is not None
    sep_name = str(rvc.get("separate_model") or "bs_roformer_anvuew_sdr_12.45.ckpt")

    # ② 找音源：先项目自己的搜索后端，再退到免 key 的公开音源
    if url:
        source = "调用方给的音频直链"
    else:
        url, source, notes = _find_audio_url(song, artist)
        if not url:
            return _fail(
                "没找到《%s》%s的音频直链%s。**不会**用自己编的词冒充这首歌唱 —— "
                "换个说法（加上歌手名）再试一次，或者直接把音频直链给我（url 参数）。"
                % (song, ("（%s）" % artist) if artist else "",
                   ("（%s）" % notes) if notes else ""))

    # ③ 下载 +（可选）分离人声
    lib_dir = _voice_lib_dir(cfg)
    start = _num(args.get("start"), 0.0)
    end = _num(args.get("end"), 0.0)
    trimmed = ""
    max_s = _num(rvc.get("cover_max_seconds"), 180.0)
    if end <= 0 and max_s > 0:
        # 整首太长会顶穿 MCP 调用超时（voice server 注册的是 240 秒）——
        # 截一段，并且**如实告诉主人**截了。
        end = start + max_s
        trimmed = "（整首太长，这次只取了前 %d 秒）" % int(max_s)
    fetched = _run_fetch(cfg, url, lib_dir, start, end,
                         sep_model if separated else None,
                         sep_model_dir if separated else None)
    if not fetched["ok"]:
        return _fail("%s失败：%s" % (fetched["stage"], fetched["error"]))
    payload = fetched["payload"]
    vocal = lib_dir / str(payload.get("name") or "")
    if not vocal.is_file():
        return _fail("下载成功，但语音库里没有这个文件：%s" % vocal)
    instrumental = None
    if payload.get("instrumental"):
        cand = lib_dir / str(payload["instrumental"])
        if cand.is_file():
            instrumental = cand
    # 歌本来没那么长 → 不用提「截了一段」
    if trimmed:
        _vdur = _wav_info(vocal)[1]
        if _vdur and _vdur < max_s - 1.0:
            trimmed = ""

    # ④ 音转音（换成「大肥鱼」的嗓子）
    out_dir = _out_dir(cfg)
    stem = "cover_%s_%s" % (_safe_stem(song or "song"), _stamp())
    conv_out = out_dir / (stem + "_vocal.wav")
    try:
        pitch = int(args.get("pitch") or 0)
    except (TypeError, ValueError):
        pitch = 0
    ok, err = _run_convert(cfg, vocal, conv_out, pitch)
    if not ok:
        return _fail("音转音失败：%s" % err)

    # ⑤ 混伴奏（mix=auto/on/off）
    mix_mode = str(args.get("mix") or "auto").strip().lower()
    mix_volume = max(0.0, min(2.0, _num(args.get("mix_volume"),
                                          _num(rvc.get("mix_volume"), 0.75))))
    final = conv_out
    mixed = False
    notes_out = []
    if mix_mode == "off":
        notes_out.append("这次是清唱版（原伴奏被静音）。")
    elif instrumental is not None:
        mix_out = out_dir / (stem + ".wav")
        ok, err = _mix_with_instrumental(cfg, conv_out, instrumental,
                                         mix_out, mix_volume)
        if ok:
            final = mix_out
            mixed = True
        else:
            notes_out.append("混伴奏失败（%s），这次给的是清唱版。" % err)
    elif mix_mode == "on":
        return _fail("要求带伴奏，但这段素材没有伴奏轨（没分离出伴奏），只能给清唱版。")
    else:
        notes_out.append("这段素材没有伴奏轨，这次给的是清唱版。")
    if not separated:
        notes_out.append("本机没有 %s 人声分离模型，这次是整段音频直接换调，"
                         "伴奏也跟着变了调。" % sep_name)

    size, dur = _wav_info(final)
    if size <= 0:
        return _fail("音转音没有产出音频文件。")
    write_voice_meta(final, "sing", dur, size,
                     "%s%s" % (song, (" - " + artist) if artist else ""))
    played = _play(final)
    elapsed = time.time() - t0
    kind = "大肥鱼的嗓子 + 原曲伴奏" if mixed else "只有大肥鱼的人声"
    lines = [
        "翻唱完了：%s（%s，%.1f 秒，%s）。" % (song, kind, dur, _human_kb(size)),
        "音源：%s。这是对原曲的**翻唱**，不是原唱。" % source,
        "音频在 %s。" % final,
        "全程花了 %.1f 秒。" % elapsed,
    ]
    lines.extend(notes_out)
    if trimmed:
        lines.append(trimmed)
    if played:
        lines.append("已经放给主人听了。")
    else:
        lines.append("（本机没放出来，音频在 %s）" % final)
    return _ok(" ".join(lines))

# ---------------------------------------------------------------------------
# voice_sing：走本机 RVC（复用 E:\SnowLuma\rvc_sing.py）
# ---------------------------------------------------------------------------

def handle_sing(args: dict) -> dict:
    """voice_sing：用本机 RVC 唱歌。

    给了 song / url → **翻唱**（找歌 → 下载 → 分离人声 → 音转音 → 播放）；
    只给 lyrics → 按旋律唱一段词（旋律是现编的调）。
    """
    if str(args.get("song") or "").strip() or str(args.get("url") or "").strip():
        return handle_cover(args)
    lyrics = str(args.get("lyrics") or args.get("text") or "").strip()
    if not lyrics:
        return _fail("要唱歌得给歌名（song，翻唱那首歌）或歌词（lyrics，按旋律唱一段）。")
    cfg = _cfg()
    rvc = _rvc_cfg(cfg)

    py_path = _abs(rvc.get("python") or "")
    script_path = _abs(rvc.get("sing_script") or "")
    rvc_root = _abs(rvc.get("root") or "")
    weight = _rvc_weight_path(cfg)

    # 逐项检查本机资产 —— 缺了就如实说「音色不在本地」，**绝不下载**
    if not py_path.is_file():
        return _fail("RVC 解释器不在本机：%s（本工具不会下载或安装任何东西）。" % py_path)
    if not script_path.is_file():
        return _fail("唱歌脚本不在本机：%s（本工具不会下载任何东西）。" % script_path)
    if not weight.is_file():
        return _fail("音色不在本地：%s（不会自动下载模型权重）。" % weight)

    try:
        speed = args.get("speed")
        speed = float(speed) if speed is not None else float(rvc.get("sing_speed") or 0.92)
    except (TypeError, ValueError):
        speed = 0.92
    speed = max(0.5, min(1.5, speed))

    try:
        seed = args.get("seed")
        seed = int(seed) if seed is not None else random.randint(1, 10 ** 6)
    except (TypeError, ValueError):
        seed = random.randint(1, 10 ** 6)

    try:
        base = args.get("base")
        base = float(base) if base is not None else float(rvc.get("sing_base") or 329.63)
    except (TypeError, ValueError):
        base = 329.63

    out = _out_dir(cfg) / ("sing_%s.wav" % _stamp())
    cmd = [str(py_path), str(script_path),
           "--text", lyrics,
           "--out", str(out),
           "--model", str(rvc.get("model") or "da_fei_yu.pth")]
    cmd += ["--speed", str(speed)]
    cmd += ["--seed", str(seed)]
    cmd += ["--base", str(base)]
    # 参考音频用**项目配置里那个音色目录**（rvc_sing.py 里写死的那条路径
    # 在本机不存在 —— 目录名是「大肥鱼」，脚本里写的是「飞鱼」，
    # TTS 那边会直接 400）。这里显式传本机真实存在的那份。
    ref_wav = _abs(cfg.get("voice_dir") or "voice/大飞鱼") / "dfy_ref.wav"
    if ref_wav.is_file():
        cmd += ["--ref-audio", str(ref_wav)]
        lab = ref_wav.with_suffix(".lab")
        if lab.is_file():
            try:
                prompt = lab.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                prompt = ""
            if prompt:
                cmd += ["--ref-text", prompt]

    timeout = float(rvc.get("timeout_s") or 300)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(rvc_root) if rvc_root.is_dir() else None,
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return _fail("唱歌失败：RVC 推理超过 %d 秒还没结束，已放弃。" % int(timeout))
    except Exception as e:  # noqa: BLE001
        return _fail("唱歌失败：跑不动 RVC 脚本（%s）。" % (str(e) or type(e).__name__))

    if not out.is_file() or out.stat().st_size <= 0:
        tail = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip().splitlines()
        detail = tail[-1][:300] if tail else "（没有输出）"
        return _fail("唱歌失败：没有产出音频。RVC 返回码 %s，最后一行：%s" % (proc.returncode, detail))
    if proc.returncode != 0:
        tail = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip().splitlines()
        detail = tail[-1][:300] if tail else "（没有输出）"
        return _fail("唱歌失败：RVC 返回码 %s，最后一行：%s" % (proc.returncode, detail))

    size, dur = _wav_info(out)
    # 结构化旁路文件：给界面做语音条用（模型看不到它）
    write_voice_meta(out, "sing", dur, size, lyrics)
    played = _play(out)
    lines = ["唱完了（%s，%.1f 秒）。" % (_human_kb(size), dur),
             "旋律是按这段词现编的调（seed=%d，基准 %.0f Hz）。" % (seed, base),
             "音频在 %s。" % out]
    if played:
        lines.append("已经放给主人听了。")
    else:
        lines.append("（本机没放出来，音频在 %s）" % out)
    return _ok(" ".join(lines))


# ---------------------------------------------------------------------------
# voice_status：只读盘点
# ---------------------------------------------------------------------------

def handle_status(args=None) -> dict:
    """voice_status：报本机语音服务 / 音色盘点。"""
    cfg = _cfg()
    url = str(cfg.get("tts_url") or "http://127.0.0.1:9880")
    lines = ["本机语音服务状态："]

    tts_up, tts_detail = _probe_tts(url)
    lines.append("- TTS 合成服务（%s）：%s" % (url, tts_detail))

    rvc = _rvc_cfg(cfg)
    py_path = _abs(rvc.get("python") or "")
    script_path = _abs(rvc.get("sing_script") or "")
    weight = _rvc_weight_path(cfg)
    lines.append("- RVC 解释器（%s）：%s" % (py_path, "就绪" if py_path.is_file() else "不在本机"))
    lines.append("- RVC 唱歌脚本（%s）：%s" % (script_path, "就绪" if script_path.is_file() else "不在本机"))
    if weight.is_file():
        try:
            lines.append("- 本机音色权重：%s（%s）"
                         % (weight.name, _human_kb(weight.stat().st_size)))
        except OSError:
            lines.append("- 本机音色权重：%s" % weight.name)
    else:
        lines.append("- 本机音色权重：音色不在本地（%s），**不会自动下载**。" % weight)

    models = _local_rvc_models(cfg)
    if models:
        lines.append("- 本机可用音色（共 %d 个）：%s"
                     % (len(models), "、".join(m.split("（")[0] for m in models)))
    else:
        lines.append("- 本机可用音色：音色不在本地（%s 下没有 .pth）"
                     % (_abs(rvc.get("root") or "") / "assets" / "weights"))

    emotions = _local_emotions(cfg)
    if emotions:
        lines.append("- 情绪参考（%s）：%s"
                     % (_emotion_dir(cfg),
                        "、".join(EMOTION_LABELS.get(n, n) for n, _w, _p in emotions)))
    else:
        lines.append("- 情绪参考：不在本地（%s）" % _emotion_dir(cfg))

    # 翻唱链路的本机资产（找歌 → 下载 → 分离 → 音转音 → 混伴奏）
    fetch_script = _fetch_script_path(cfg)
    sep_model, _sep_dir = _separate_model(cfg)
    lines.append("- 翻唱下载脚本（%s）：%s"
                 % (fetch_script, "就绪" if fetch_script.is_file() else "不在本机"))
    if sep_model is not None:
        try:
            lines.append("- 人声分离模型（%s）：就绪（%s）"
                         % (sep_model.name, _human_kb(sep_model.stat().st_size)))
        except OSError:
            lines.append("- 人声分离模型（%s）：就绪" % sep_model.name)
    else:
        lines.append("- 人声分离模型：不在本地（%s），**不会自动下载**；"
                     "没有它就分不开人声和伴奏（翻唱会退化成整段换调）。"
                     % (rvc.get("separate_model") or "bs_roformer_anvuew_sdr_12.45.ckpt"))
    lines.append("- 翻唱语音库：%s" % _voice_lib_dir(cfg))
    lines.append("- 搜索后端：%s" % ("Tavily（已配置 key）"
                                     if _tavily_key() else "Tavily 没配 key，"
                                                          "DuckDuckGo 没装 ddgs 时只能靠公开音源"))

    lines.append("- 语音产物目录：%s" % _out_dir(cfg))
    lines.append("不可用的服务对应的工具会失败，其余工具不受影响。"
                 "本工具只读探测，**不会下载任何模型**。")
    return _ok("\n".join(lines))


# ---------------------------------------------------------------------------
# MCP 主循环
# ---------------------------------------------------------------------------

def handle_call(name: str, arguments: dict) -> dict:
    """处理 tools/call，返回 MCP 格式 result。"""
    arguments = arguments or {}
    try:
        if name == "speak":
            return handle_speak(arguments)
        if name == "sing":
            return handle_sing(arguments)
        if name == "status":
            return handle_status(arguments)
    except Exception as e:  # noqa: BLE001
        log.exception("voice tool %s 异常", name)
        return _fail("%s 执行出错：%s" % (name, str(e) or type(e).__name__))
    return _fail("未知工具：%s" % name)


def _force_utf8_stdio() -> None:
    """把 stdin/stdout 强制成 UTF-8（MCP stdio 传输规定 UTF-8）。"""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass


def send(req_id, result=None, error=None) -> None:
    msg = {"jsonrpc": "2.0", "id": req_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def run_server() -> None:
    """主循环：stdin 一行行读 JSON-RPC，stdout 输出。"""
    log.info("MCP voice_server 启动（project_root=%s）", PROJECT_ROOT)
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        req_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params", {}) or {}

        if method == "initialize":
            send(req_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "voice_server", "version": "1.0.0"},
            })
            continue
        if method == "notifications/initialized":
            continue
        if method == "tools/list":
            send(req_id, {"tools": TOOLS})
            continue
        if method == "tools/call":
            name = params.get("name", "")
            args = params.get("arguments", {}) or {}
            send(req_id, handle_call(name, args))
            continue
        if req_id is not None:
            send(req_id, None, error={"code": -32601, "message": "未知方法：%s" % method})


def main():
    _force_utf8_stdio()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true",
                    help="只跑一次 voice_status 就退出（自检用）")
    args = ap.parse_args()
    if args.probe:
        print(handle_status()["content"][0]["text"])
        return
    run_server()


if __name__ == "__main__":
    main()
