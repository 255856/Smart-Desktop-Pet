# -*- coding: utf-8 -*-
"""自我记忆（Self Memory）：身份 / 价值 / 程序性 —— **记忆库里的三个分区**。

为什么不是「提示词常量」
----------------------
上一版把它们写死在 §data/*.json§ + system prompt 的常量段里：模型看得见，
但**改不了**、也不在向量库里 —— 那不叫「分区」，那叫「写死的设定」。

现在这三个区域是**记忆卡的一种 kind**，跟 话 / 话题 / 事件 / 事实·偏好 / 印象 平级：

| kind | 是什么 | 谁写 | 怎么进会话 |
|---|---|---|---|
| §identity§ | 自我认知（我是谁、我跟主人什么关系） | 模型自己（§update_self§）+ 主人 | **稳定段**（条数少、变化慢 → 指纹缓存，不掉前缀缓存） |
| §value§ | 自我价值观（底线 / 偏好 / 风格） | 同上 | **稳定段** |
| §procedure§ | 程序性（做某事的固定套路、我的行事风格与目标） | 同上 | **易变段按需召回**（条目多、变化快） |

文件（§data/identity.json§ 等）保留两件事：① 一次性迁移源；② sidecar 没跑时的兜底。
**真值在记忆库里**。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


#: 三个自我区域（= 记忆卡的 kind）
SELF_KINDS = ("identity", "value", "procedural")

#: 区域的标题（进提示词时用）
REGION_HEADERS = {
    "identity": "【我是谁】",
    "value": "【我的价值】",
    "procedural": "【我怎么做事】",
}

#: 价值分三层：底线 → 偏好 → 风格
VALUE_LEVELS = ("bottom_line", "preference", "style")
VALUE_LEVEL_LABELS = {"bottom_line": "底线", "preference": "偏好",
                      "style": "风格"}

#: 卡片 key 的前缀（§self.<region>.<level>.<slug>§）
KEY_PREFIX = "self."

#: 每天每个区域最多改几条 —— 她的人格是**慢慢长**出来的，不是每轮重写一遍
MAX_EDITS_PER_DAY = 2

#: 编辑流水（防止她一天里把自己改八百遍）
_EDIT_LOG_FILE = "data/self_edit_log.json"

_lock = threading.Lock()


def _client(client):
    """拿到记忆客户端（没注入就用默认客户端，指向本机 sidecar）。"""
    if client is None:
        try:
            from app.engine.tools._memory import default_client
            return default_client()
        except Exception:  # noqa: BLE001
            return None
    return client


def slug(text: str, limit: int = 24) -> str:
    """内容 → key 里的短标识（中文取前几个字 + 哈希尾巴，避免撞 key）。"""
    t = str(text or "").strip()
    if not t:
        return "note"
    import hashlib
    h = hashlib.sha1(t.encode("utf-8")).hexdigest()[:6]
    head = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", t)[:limit]
    return ("%s-%s" % (head, h)) if head else h


def make_key(region: str, text: str, level: str = "") -> str:
    """自我记忆卡的 key（稳定 → 再写一次就是**改写**同一条，不是堆新卡）。"""
    region = str(region or "").strip()
    level = str(level or "").strip()
    if region == "value" and level not in VALUE_LEVELS:
        level = "preference"
    parts = [KEY_PREFIX.rstrip("."), region]
    if level:
        parts.append(level)
    parts.append(slug(text))
    return ".".join(parts)


def level_from_key(key: str) -> str:
    """从 key 反推层级（§self.value.<level>.<slug>§ → level）。"""
    parts = [p for p in str(key or "").split(".") if p]
    if len(parts) >= 3 and parts[0] == "self" and parts[1] == "value":
        if parts[2] in VALUE_LEVELS:
            return parts[2]
    return ""


def load_region(client, kind: str, limit: int = 80) -> list:
    """这个区域当前生效的卡片（active，按更新时间倒序）。"""
    c = _client(client)
    if c is None:
        return []
    try:
        rows = c.list_cards(limit=limit) or []
    except Exception:  # noqa: BLE001
        log.debug("读自我区域 %s 失败", kind, exc_info=True)
        return []
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        if str(r.get("kind") or "") != kind:
            continue
        if str(r.get("status") or "active") != "active":
            continue
        text = str(r.get("text") or "").strip()
        if not text:
            continue
        key = str(r.get("key") or "")
        out.append({
            "id": str(r.get("id") or ""),
            "key": key,
            "text": text,
            "level": level_from_key(key),
            "importance": r.get("importance"),
            "updated_at": r.get("updatedAt") or r.get("updatedAtMs") or 0,
        })
    out.sort(key=lambda x: x["updated_at"] or 0, reverse=True)
    return out


def _legacy_items(region: str) -> list:
    """旧文件里的内容（sidecar 没跑 / 还没迁移时的兜底）。"""
    try:
        from app.brain import memory_partitions as mp
    except Exception:  # noqa: BLE001
        return []
    try:
        if region == "identity":
            out = []
            for f in mp.load_identity():
                text = str(f.get("text") or "").strip()
                if text:
                    out.append({"id": "", "key": "", "text": text,
                                "level": "", "importance": 0.7})
            return out
        if region == "value":
            out = []
            for v in mp.load_values():
                text = str(v.get("content") or "").strip()
                if text:
                    out.append({"id": "", "key": "", "text": text,
                                "level": str(v.get("level") or ""),
                                "importance": 0.7})
            return out
    except Exception:  # noqa: BLE001
        log.debug("读旧文件分区失败", exc_info=True)
    return []


def identity_block(client=None) -> str:
    """【我是谁】—— 身份分区（稳定段）。"""
    items = load_region(client, "identity")
    if not items:
        items = _legacy_items("identity")
    if not items:
        return ""
    lines = [REGION_HEADERS["identity"]]
    for it in items:
        lines.append("- " + it["text"])
    return "\n".join(lines)
    return []
    if not items:
        return ""
    lines = [REGION_HEADERS["identity"]]
    for it in items:
        lines.append("- " + it["text"])
    return "\n".join(lines)


def value_block(client=None) -> str:
    """【我的价值】—— 价值观分区（稳定段；按底线 → 偏好 → 风格排）。"""
    items = load_region(client, "value")
    if not items:
        items = _legacy_items("value")
    if not items:
        return ""
    def _order(it):
        lv = it.get("level") or ""
        return VALUE_LEVELS.index(lv) if lv in VALUE_LEVELS else len(VALUE_LEVELS)
    lines = [REGION_HEADERS["value"]]
    for it in sorted(items, key=_order):
        label = VALUE_LEVEL_LABELS.get(it.get("level") or "", "")
        lines.append(("- %s：%s" % (label, it["text"])) if label
                     else ("- " + it["text"]))
    return "\n".join(lines)


def procedural_items(client=None) -> list:
    """程序性分区的条目（**易变段按需召回**用）。"""
    return load_region(client, "procedural")


#: 稳定段块的缓存（指纹一样就不重读 sidecar → system 串逐字节不变）
_block_cache: dict = {}
_cache_ts: float = 0.0
CACHE_TTL_S = 20.0


def stable_blocks(client=None) -> list:
    """身份 + 价值两块（**常量**：内容没变就逐字节相同）。"""
    global _cache_ts
    now = time.time()
    with _lock:
        if _cache_ts and (now - _cache_ts) < CACHE_TTL_S and _block_cache:
            return list(_block_cache.values())
    ident = identity_block(client)
    value = value_block(client)
    with _lock:
        _block_cache.clear()
        _block_cache["identity"] = ident
        _block_cache["value"] = value
        _cache_ts = now
    return [ident, value]


def invalidate_cache() -> None:
    """改了自我区域后调用（下一轮就重读）。"""
    global _cache_ts
    with _lock:
        _block_cache.clear()
        _cache_ts = 0.0


#: 每天每区域的编辑次数（§update_self§ 用）
def _edit_log_path(root: Optional[Path] = None) -> Path:
    if root is not None:
        return Path(root) / _EDIT_LOG_FILE
    return Path(_EDIT_LOG_FILE)




def migrate_legacy_files(client=None) -> int:
    """把旧文件分区（§data/identity.json§ 等）导成记忆卡 —— 一次性的。

    之后**以记忆库为准**（文件只留作 sidecar 没跑时的兜底）。
    已经导过的区域会跳过（幂等）。返回导入条数。
    """
    c = _client(client)
    if c is None:
        return 0
    try:
        from app.brain import memory_partitions as mp
    except Exception:  # noqa: BLE001
        return 0
    total = 0
    plans = (
        ("identity", mp.load_identity, "text", ""),
        ("value", mp.load_values, "content", "level"),
    )
    for region, loader, text_key, level_key in plans:
        try:
            if load_region(c, region):
                continue          # 记忆库里已经有了 → 不重复导
            items = loader() or []
        except Exception:  # noqa: BLE001
            continue
        for it in items:
            text = str(it.get(text_key) or "").strip()
            if not text:
                continue
            level = str(it.get(level_key) or "").strip() if level_key else ""
            try:
                written = c.add(text, key=make_key(region, text, level),
                               kind=region, importance=0.75,
                               category=level or "self")
            except Exception:  # noqa: BLE001
                continue
            if written:
                total += 1
    # 程序性：旧文件里是「任务 + 步骤」
    try:
        if not load_region(c, "procedural"):
            for pr in mp.load_procedures() or []:
                task = str(pr.get("task") or "").strip()
                steps = [str(s).strip() for s in (pr.get("steps") or [])
                         if str(s).strip()]
                if not task:
                    continue
                text = ("%s：%s" % (task, " → ".join(steps))) if steps else task
                try:
                    if c.add(text, key=make_key("procedural", text),
                            kind="procedural", importance=0.7,
                            category="self"):
                        total += 1
                except Exception:  # noqa: BLE001
                    continue
    except Exception:  # noqa: BLE001
        pass
    if total:
        invalidate_cache()
    return total

def edits_today(region: str, day: str = "") -> list:
    """今天这个区域改过几次。"""
    import datetime
    day = day or datetime.datetime.now().strftime("%Y-%m-%d")
    path = _edit_log_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(data, list):
        return []
    return [e for e in data
            if isinstance(e, dict) and str(e.get("date") or "") == day
            and str(e.get("region") or "") == region]


def record_edit(region: str, card_id: str = "", now=None) -> None:
    """记一次编辑（给「每天最多 N 次」用）。"""
    import datetime
    now = now or datetime.datetime.now()
    path = _edit_log_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            data = []
    except Exception:  # noqa: BLE001
        data = []
    data.append({"date": now.strftime("%Y-%m-%d"), "region": region,
                 "id": card_id, "at": now.isoformat(timespec="seconds")})
    data = data[-200:]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    except Exception:  # noqa: BLE001
        log.debug("写自我编辑流水失败", exc_info=True)

