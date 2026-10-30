r"""语音 MCP server：桌宠**自己决定**要不要发语音 / 唱歌。

工具
----
  speak   念一段话（可选情绪）      -> 本地工具名 mcp_voice_speak
  sing    按旋律唱一段歌词          -> 本地工具名 mcp_voice_sing
  status  报服务在不在、有哪些音色可用 -> 本地工具名 mcp_voice_status

三条铁律（主人定的）
--------------------
1. **只复用本机已有的模型**（本机 GPT-SoVITS 服务 + 本机 RVC 权重），
   **绝不下载任何模型权重**；本机没有的音色就如实说「音色不在本地」。
2. **诚实**：做了什么、产物在哪、哪一步失败，都要说清楚；
   没产出文件就**不许**说「已发送」。
3. **语音条前面不加方括号标签**（「[翻唱-清唱版]」那种标签主人嫌出戏），
   要说话就另外发一条普通文字消息。

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
import subprocess
import sys
import time
import urllib.error
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
            "用本机 RVC 按旋律唱一段歌词（同样不下载模型，用本机已有的音色权重）。"
            "和 mcp_voice_speak 的区别：speak 是平读（念白），sing 会给每个字配一个音高、"
            "并把音高在字内保持住，所以听起来是在唱而不是在念。"
            "歌词直接给文本就行，不要写曲谱或音符。"
            "规矩：①唱的是**按这段词现编的调**，不是原创歌曲——要这么跟主人说明，"
            "别自称原创；②语音条前面**不要加方括号标签**，要说话就另外发一条普通文字消息；"
            "③语音是打断，不是每轮都要用，大部分回合用文字就好；"
            "④失败就如实说失败（哪一步没成），**不要假装唱过了**。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "lyrics": {"type": "string",
                           "description": "要唱的歌词。中文短句或一段副歌最好，太长会显得平。"},
                "speed": {"type": "number",
                          "description": "可选：语速，0.85 左右更像唱，1.0 以上更接近说话。"},
                "seed": {"type": "integer",
                         "description": "可选：随机种子。换一个数就是另一条旋律；不给就每次随机。"},
                "base": {"type": "number",
                         "description": "可选：旋律基准音高 Hz（默认 329.63）。"
                                        "调高整体更亮，调低更沉。"},
            },
            "required": ["lyrics"],
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
    return _abs(cfg.get("voice_dir") or "voice/大飞鱼") / "emotions"


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
    vdir = _abs(cfg.get("voice_dir") or "voice/大飞鱼")
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
                     % _abs(cfg.get("voice_dir") or "voice/大飞鱼"))

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
# voice_sing：走本机 RVC（复用 E:\SnowLuma\rvc_sing.py）
# ---------------------------------------------------------------------------

def handle_sing(args: dict) -> dict:
    """voice_sing：用本机 RVC 按旋律唱一段歌词。"""
    lyrics = str(args.get("lyrics") or args.get("text") or "").strip()
    if not lyrics:
        return _fail("歌词不能为空。")
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
