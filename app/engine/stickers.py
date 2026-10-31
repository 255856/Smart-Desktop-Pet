"""表情包库：把**已有的** assets/sprites/Emotion_* 帧索引成一张表情包目录。

规矩（跟本仓库其它"素材"约定一致）：

* **只读索引** —— 不下载、不生成、不新增任何素材文件。表情包就是桌宠
  本来就在演的那几套情绪动画（Emotion_happy / sad / anger / fear /
  doubt / pride / shy / surprise），每套 81 帧 rmbg-*.png。
* 目录形状：**id -> {file, emotion, label, frames}**。id 稳定
  （happy_1 这种），界面和工具都靠 id 找图，不靠路径。
* **解析不出来就返回空** —— 界面退化成"没有气泡"，绝不画坏图
  （文件被删 / id 不认识 / 结果被篡改，一概不画）。

对外主要三个口：

* catalogue() —— 目录本体（带缓存）。
* pick_sticker() —— 工具侧选图：按 id / 按情绪 / 随便来一个。
* resolve_sticker_artifact() —— 界面侧解析：工具结果 -> 表情包信息。
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

#: 工具名（界面和工具共用一个常量，别各写各的）
STICKER_TOOL_NAME = "send_sticker"


def sprites_root() -> Path:
    """表情包素材根目录（assets/sprites）。"""
    return Path(__file__).resolve().parents[2] / "assets" / "sprites"


#: 情绪 key -> 中文标签（模型和界面都看得懂）
EMOTION_LABELS: dict[str, str] = {
    "happy": "开心",
    "sad": "难过",
    "anger": "生气",
    "fear": "害怕",
    "doubt": "狐疑",
    "pride": "骄傲",
    "shy": "害羞",
    "surprise": "惊讶",
}

#: 模型可能说的别名 -> 情绪 key（目录名是 Emotion_anger，模型爱说 angry）
EMOTION_ALIASES: dict[str, str] = {
    "happy": "happy", "joy": "happy", "cheer": "happy",
    "开心": "happy", "高兴": "happy", "愉快": "happy",
    "sad": "sad", "sadness": "sad", "cry": "sad",
    "难过": "sad", "伤心": "sad", "不开心": "sad",
    "anger": "anger", "angry": "anger", "mad": "anger",
    "生气": "anger", "愤怒": "anger", "火大": "anger",
    "fear": "fear", "afraid": "fear", "scared": "fear",
    "害怕": "fear", "恐惧": "fear", "怕": "fear",
    "doubt": "doubt", "doubtful": "doubt", "suspicious": "doubt",
    "狐疑": "doubt", "怀疑": "doubt", "疑惑": "doubt",
    "pride": "pride", "proud": "pride",
    "骄傲": "pride", "得意": "pride", "神气": "pride",
    "shy": "shy", "embarrassed": "shy",
    "害羞": "shy", "不好意思": "shy",
    "surprise": "surprise", "shock": "surprise", "amazed": "surprise",
    "惊讶": "surprise", "吃惊": "surprise", "惊喜": "surprise",
}

#: 素材目录名 Emotion_xxx -> 情绪 key
_EMOTION_DIR_RE = re.compile(r"^Emotion_(.+)$")
#: 帧文件名 rmbg-12.png
_FRAME_RE = re.compile(r"^rmbg-(\d+)\.png$", re.IGNORECASE)

#: 每个情绪切几段（每段 = 一张表情包）
SLICES_PER_EMOTION = 4
#: 每张表情包带几帧（frames：留给以后做动图）
FRAMES_PER_STICKER = 4

#: 单张图片 inline 进聊天的体积上限（超过就别 base64 了）
MAX_INLINE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class Sticker:
    """一张表情包。

    Attributes:
        id: 稳定 id（happy_1），工具和界面都靠它找图。
        emotion: 情绪 key（happy / sad / …）。
        label: 中文标签（"开心 1"）。
        file: 代表帧（气泡里显示的就是它）。
        frames: 动画帧序列（同情绪的若干帧）。
    """

    id: str
    emotion: str
    label: str
    file: Path
    frames: tuple[Path, ...] = ()

    def to_dict(self) -> dict:
        """转成普通 dict（id -> {file, emotion, label, frames}）。"""
        return {
            "id": self.id,
            "emotion": self.emotion,
            "label": self.label,
            "file": str(self.file),
            "frames": [str(f) for f in self.frames],
        }


# ---------------------------------------------------------------------------
# 目录
# ---------------------------------------------------------------------------

def _scan_frames(emotion_dir: Path) -> list[Path]:
    """列出一个情绪目录里的帧（按 rmbg-N.png 的 N 排好序）。"""
    out: list[tuple[int, Path]] = []
    try:
        entries = list(emotion_dir.iterdir())
    except OSError:
        return []
    for p in entries:
        m = _FRAME_RE.match(p.name)
        if m and p.is_file():
            out.append((int(m.group(1)), p))
    out.sort(key=lambda x: x[0])
    return [p for _n, p in out]


def _split_sticker_frames(frames: list[Path]) -> list[list[Path]]:
    """把一串帧切成若干"表情包"的帧组。"""
    n = len(frames)
    if n == 0:
        return []
    if n <= SLICES_PER_EMOTION:
        return [[f] for f in frames]
    width = n // SLICES_PER_EMOTION
    windows = [frames[i * width:(i + 1) * width]
               for i in range(SLICES_PER_EMOTION)]
    windows[-1].extend(frames[SLICES_PER_EMOTION * width:])
    out: list[list[Path]] = []
    for win in windows:
        if not win:
            continue
        if len(win) <= FRAMES_PER_STICKER:
            out.append(list(win))
            continue
        # 窗口里**均匀**取几帧（别全挤在开头，那几帧长得一样）
        step = (len(win) - 1) / float(FRAMES_PER_STICKER - 1)
        idx = sorted({int(round(i * step)) for i in range(FRAMES_PER_STICKER)})
        out.append([win[i] for i in idx])
    return out


def build_catalogue(root: Optional[Path] = None) -> dict[str, Sticker]:
    """扫素材目录，建一张表情包目录（id -> Sticker）。

    素材目录不在（打包漏了 / 单测环境）时返回空 dict —— 调用方要**如实**
    告诉主人"没有表情包"，不能假装有。
    """
    base = Path(root) if root is not None else sprites_root()
    out: dict[str, Sticker] = {}
    if not base.is_dir():
        log.warning("表情包素材目录不存在：%s", base)
        return out
    try:
        dirs = sorted(p for p in base.iterdir() if p.is_dir())
    except OSError:
        log.warning("读表情包素材目录失败：%s", base)
        return out
    for d in dirs:
        m = _EMOTION_DIR_RE.match(d.name)
        if not m:
            continue
        emotion = m.group(1).strip().lower()
        frames = _scan_frames(d)
        if not frames:
            continue
        label_cn = EMOTION_LABELS.get(emotion, emotion)
        groups = _split_sticker_frames(frames)
        for i, group in enumerate(groups, start=1):
            sid = f"{emotion}_{i}"
            out[sid] = Sticker(
                id=sid,
                emotion=emotion,
                label=f"{label_cn} {i}",
                file=group[0],
                frames=tuple(group),
            )
    return out


_CATALOGUE_CACHE: dict[str, dict[str, Sticker]] = {}


def catalogue(root: Optional[Path] = None) -> dict[str, Sticker]:
    """表情包目录（进程内缓存；同一份素材只扫一次）。"""
    key = str(Path(root).resolve()) if root is not None else str(sprites_root())
    cached = _CATALOGUE_CACHE.get(key)
    if cached is None:
        cached = build_catalogue(root)
        _CATALOGUE_CACHE[key] = cached
        log.info("表情包目录：%d 张（素材 %s）", len(cached), key)
    return cached


def get_sticker(sticker_id: str, root: Optional[Path] = None) -> Optional[Sticker]:
    """按 id 找一张表情包；找不到返回 None。"""
    sid = (sticker_id or "").strip().lower()
    if not sid:
        return None
    return catalogue(root).get(sid)


def normalize_emotion(emotion: str) -> str:
    """情绪名归一（中文 / 别名 -> 情绪 key）；认不出来返回空串。"""
    raw = (emotion or "").strip().lower()
    if not raw:
        return ""
    if raw in EMOTION_ALIASES:
        return EMOTION_ALIASES[raw]
    # "Emotion_happy" / "happy_1" 这种写法也认
    raw2 = raw.replace("emotion_", "")
    if raw2 in EMOTION_ALIASES:
        return EMOTION_ALIASES[raw2]
    return ""


#: 轮转指针：同一情绪连续发时不至于每次都是同一张
_ROTATE = {"n": 0}


def pick_sticker(sticker_id: str = "", emotion: str = "",
                 root: Optional[Path] = None,
                 rotate: bool = True) -> Optional[Sticker]:
    """选一张表情包。

    优先级：**指定 id** > **指定情绪**（轮着来）> 随便一张（轮着来）。

    Args:
        sticker_id: 表情包 id（happy_1）；认不出来就当没给。
        emotion: 情绪（中文 / 英文别名都认）。
        rotate: 同一情绪有多张时轮着挑（False = 永远第一张）。
    """
    cat = catalogue(root)
    if not cat:
        return None
    sid = (sticker_id or "").strip().lower()
    if sid and sid in cat:
        return cat[sid]
    emo = normalize_emotion(emotion)
    pool = [s for s in cat.values() if s.emotion == emo] if emo else []
    if not pool:
        pool = list(cat.values())
    if not pool:
        return None
    if not rotate:
        return pool[0]
    idx = _ROTATE["n"] % len(pool)
    _ROTATE["n"] = idx + 1
    return pool[idx]


def catalogue_digest(root: Optional[Path] = None) -> str:
    """目录摘要（一行一个情绪），给工具描述 / 提示词用。"""
    cat = catalogue(root)
    if not cat:
        return ""
    by_emotion: dict[str, list[str]] = {}
    for st in cat.values():
        by_emotion.setdefault(st.emotion, []).append(st.id)
    parts = []
    for emotion in sorted(by_emotion):
        label = EMOTION_LABELS.get(emotion, emotion)
        ids = " ".join(sorted(by_emotion[emotion]))
        parts.append(f"{emotion}({label}): {ids}")
    return "；".join(parts)


def emotion_list(root: Optional[Path] = None) -> list[str]:
    """目录里有哪些情绪 key（给工具描述用）。"""
    return sorted({st.emotion for st in catalogue(root).values()})


# ---------------------------------------------------------------------------
# 结果里的标记：界面靠它知道"发的是哪一张"
# ---------------------------------------------------------------------------

#: 工具结果里的标记：[sticker:happy_1]
STICKER_MARKER_RE = re.compile(r"\[sticker:([A-Za-z0-9_]+)\]")


def sticker_marker(sticker_id: str) -> str:
    """结果文本里的标记（界面解析用）。"""
    return f"[sticker:{sticker_id}]"


def parse_sticker_marker(result: str) -> str:
    """从工具结果文本里取出表情包 id；没有就返回空串。"""
    m = STICKER_MARKER_RE.search(result or "")
    return m.group(1) if m else ""


def _args_dict(args: str) -> dict:
    """工具参数（JSON 文本）-> dict；解析不了就空 dict。"""
    if isinstance(args, dict):
        return dict(args)
    raw = (args or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def resolve_sticker_artifact(name: str, args: str, result: str,
                             root: Optional[Path] = None) -> dict:
    """工具事件 -> 表情包信息（界面渲染气泡用）。

    返回 {"id", "emotion", "label", "path", "exists"}；
    **解析不出来就返回空 dict** —— 界面这时不画气泡（绝不画坏图）。

    解析顺序：结果里的标记 > 参数里的 sticker_id > 参数里的 emotion。
    """
    if name != STICKER_TOOL_NAME:
        return {}
    # 结果里点名了哪一张，就用哪一张 —— 点名了却不认识 = 解析不出来（不画）
    sid = parse_sticker_marker(result)
    if sid:
        st = get_sticker(sid, root)
        if st is None:
            return {}
    else:
        a = _args_dict(args)
        raw_sid = str(a.get("sticker_id") or "").strip().lower()
        if raw_sid:
            st = get_sticker(raw_sid, root)
            if st is None:
                return {}
        else:
            emo = str(a.get("emotion") or "")
            # 情绪认不出来就别瞎挑一张（那不是主人/模型要的那张）
            st = (pick_sticker(emotion=emo, root=root, rotate=False)
                  if normalize_emotion(emo) else None)
    if st is None:
        return {}
    path = Path(st.file)
    try:
        exists = path.is_file()
    except OSError:
        exists = False
    return {
        "id": st.id,
        "emotion": st.emotion,
        "label": st.label,
        "path": str(path),
        "file": str(path),
        "exists": exists,
    }


# ---------------------------------------------------------------------------
# 本地图片 -> data: URL（QTextBrowser 稳定显示自包含图片）
# ---------------------------------------------------------------------------

_DATA_URL_CACHE: dict[tuple, str] = {}
_DATA_URL_CACHE_MAX = 16


def sticker_data_url(path: str | Path) -> str:
    """本地 PNG -> data:image/png;base64,...；读不到就返回空串。

    按 (路径, mtime, size) 缓存 —— 流式期间消息会反复重渲染，
    每次重新 base64 一张 400 KB 的图太浪费。
    """
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return ""
    if not p.is_file() or st.st_size <= 0 or st.st_size > MAX_INLINE_BYTES:
        return ""
    key = (str(p), st.st_mtime_ns, st.st_size)
    cached = _DATA_URL_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        raw = p.read_bytes()
    except OSError:
        return ""
    url = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
    if len(_DATA_URL_CACHE) >= _DATA_URL_CACHE_MAX:
        _DATA_URL_CACHE.clear()
    _DATA_URL_CACHE[key] = url
    return url


__all__ = [
    "STICKER_TOOL_NAME",
    "EMOTION_LABELS",
    "Sticker",
    "build_catalogue",
    "catalogue",
    "catalogue_digest",
    "emotion_list",
    "get_sticker",
    "normalize_emotion",
    "parse_sticker_marker",
    "pick_sticker",
    "resolve_sticker_artifact",
    "sticker_data_url",
    "sticker_marker",
    "sprites_root",
]
