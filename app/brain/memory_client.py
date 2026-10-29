# -*- coding: utf-8 -*-
"""记忆 sidecar 的 HTTP 客户端（桌宠侧唯一入口）。

sidecar = **独立进程**（`python -m memory_sidecar serve --port 8788`）。这个模块只跟它
说 HTTP，不 import memory_sidecar 里的任何东西 —— 它可以单独重启 / 单独升级，
桌宠这边永远只是一个客户端。

红线（A3：sidecar 挂了桌宠照样能聊）
----------------------------------
**绝不把异常抛进聊天路径**：连不上 / 超时 / 500 / JSON 坏掉，一律返回空结果
（`[]` / `0` / `{}` / `""`），并把原因记进 `last_error`。
记忆没了只是「不记得事」，不是「不能说话」。

测试（A7：不需要真 sidecar / 网络）
----------------------------------
全部 IO 收在可注入的 `http(method, path, payload, timeout)` 里
（默认 urllib 实现），测试塞一个假的进去就行。

卡片 id 是**内容派生**的（sha1(kind|key|text)），所以同一条事实记两次是
幂等 upsert 到同一张卡，不会留下一堆重复行。
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

#: sidecar 默认端口（环境变量可覆盖；brain_controller 起的也是这个端口）
DEFAULT_PORT = 8788


def default_port() -> int:
    """sidecar 端口（默认 8788，环境变量 DESKTOP_PET_MEMORY_SIDECAR_PORT 可覆盖）。

    客户端、监督者（它起子进程时传 --port）、面板读的是**同一个函数** ——
    否则会出现「客户端连 8788、监督者起了另一个端口」这种对不上的情况。
    """
    raw = os.environ.get("DESKTOP_PET_MEMORY_SIDECAR_PORT") or DEFAULT_PORT
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_PORT


def default_base_url() -> str:
    """默认的 sidecar 地址（http://127.0.0.1:<port>，端口可被环境变量覆盖）。"""
    return "http://127.0.0.1:%d" % default_port()


#: sidecar 默认地址（app/brain/brain_controller.py 起的也是这个端口）
DEFAULT_BASE_URL = default_base_url()
#: 聊天路径上的硬超时（秒）。宁可这轮没有记忆，也不能卡住说话。
DEFAULT_TIMEOUT = 2.0
#: 每轮最多注入几条（与 app/brain/memory.py 的 DEFAULT_MAX_INJECT 一致）
DEFAULT_MAX_ROWS = 4
#: 写操作的超时（秒）。写请求走**工具路径**（不在聊天主线程上），而且
#: sidecar 刚起来、模型还在预热时第一次写入要排队等模型加载（本机实测最长
#: 19.5s：warmup 线程与请求线程串在同一把模型锁上）—— 所以给得比读宽。
#: 预热完成之后写入是毫秒级。
DEFAULT_WRITE_TIMEOUT = 25.0
#: 一次失败之后的冷却时间（秒）。
#: 为什么需要它：sidecar 不在时，一次连接尝试要耗满 timeout —— 每轮聊天都
#: 白等一个超时，桌宠说话就会一顿一顿的。冷却期内直接返回空结果（不试试不
#: 代表放弃：health 探测**不受冷却影响**，supervisor 照常能发现它回来了）。
FAILURE_COOLDOWN = 5.0

#: 记忆块标题 + 免责文案 —— 与 app/brain/memory.py 的 system_block **逐字一致**
#: （prompt 的形状不能因为换了后端就悄悄变，见任务说明）
MEMORY_BLOCK_HEADER = (
    "【你记得的事】（按你刚才这句话检索出来的，可能相关也可能不准；用不上就别硬扯）"
)
MEMORY_BLOCK_LEGEND = "（★ 是重要性 0-1）"

#: sidecar 认的生命周期分类（memory_sidecar/store.py 的 HALF_LIFE_DAYS 键）
SIDECAR_KINDS = ("thought", "topic", "event", "fact", "impression")
DEFAULT_KIND = "fact"

#: 界面 / 工具用的老 category（内容类型）→ sidecar kind（生命周期分类）。
#: 两份映射只有一份实现（工具层 app/engine/tools/_memory.py 也 import 这里），
#: 免得「面板记的类别」跟「工具记的类别」慢慢漂成两套。
CATEGORY_TO_KIND = {
    "preference": "impression",   # 偏好 = 对这个人的印象
    "person": "impression",
    "event": "topic",
    "shared": "topic",
    "fact": "fact",
    "skill": "fact",
    "other": "fact",
}
#: 反向映射：kind → 面板显示的类别（卡片自己没带 category 时的兜底）
KIND_TO_CATEGORY = {
    "impression": "preference",
    "topic": "event",
    "event": "event",
    "thought": "other",
    "fact": "fact",
}


def category_to_kind(category: str, kind: str = "") -> str:
    """老 category（+ 可选 kind）→ sidecar 的 kind。显式 kind 优先。"""
    k = (kind or "").strip()
    if k in SIDECAR_KINDS:
        return k
    return CATEGORY_TO_KIND.get((category or "").strip().lower(), DEFAULT_KIND)


class MemoryCard:
    """面板 / 主动发言读记忆用的**视图对象**（不是 sidecar 的存储结构）。

    属性名沿用老记忆条目的口径（id / content / category /
    importance / created_at），这样 app/ui/memory_panel.py 与
    app/brain/proactive.py 的读法一行都不用改。
    """

    __slots__ = ("id", "content", "category", "kind", "key", "importance",
                 "created_at", "updated_at", "hit_count", "superseded")

    def __init__(self, raw: dict) -> None:
        raw = raw if isinstance(raw, dict) else {}
        self.id = str(raw.get("id") or "")
        self.content = str(raw.get("text") or "")
        kind = str(raw.get("kind") or DEFAULT_KIND)
        self.kind = kind
        self.category = str(raw.get("category") or KIND_TO_CATEGORY.get(kind, "other"))
        self.key = str(raw.get("key") or "")
        try:
            self.importance = float(raw.get("importance", 0.5))
        except (TypeError, ValueError):
            self.importance = 0.5
        self.created_at = _ms_to_seconds(raw.get("createdAt"))
        self.updated_at = _ms_to_seconds(raw.get("updatedAt"))
        try:
            self.hit_count = int(raw.get("hitCount") or 0)
        except (TypeError, ValueError):
            self.hit_count = 0
        self.superseded = str(raw.get("status") or "active") != "active"

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "<MemoryCard %s [%s★%.1f] %s>" % (
            self.id, self.category, self.importance, self.content[:20])


def _ms_to_seconds(ts) -> float:
    """毫秒时间戳 → 秒（拿不到就是 0）。"""
    try:
        return float(ts or 0) / 1000.0
    except (TypeError, ValueError):
        return 0.0


#: 卡片 id 各段之间的分隔符（正文里不会出现的控制字符，避免拼接撞车）
_ID_SEP = chr(31)


def card_id(text: str, *, kind: str = DEFAULT_KIND, key: str = "") -> str:
    """内容派生的卡片 id（同一条事实 → 同一个 id → 幂等写入）。"""
    raw = _ID_SEP.join([kind or "", key or "", (text or "").strip()])
    return "mem_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _format_time(ts_ms) -> str:
    """时间戳（毫秒）→ 简短相对时间标记（照抄 memory.py 的口径）。"""
    try:
        dt = datetime.datetime.fromtimestamp(float(ts_ms) / 1000.0)
    except Exception:  # noqa: BLE001
        return "记不清了"
    delta = (datetime.datetime.now() - dt).days
    if delta <= 0:
        return "今天"
    if delta == 1:
        return "昨天"
    if delta < 7:
        return "%d天前" % delta
    if delta < 30:
        return "%d周前" % (delta // 7)
    if delta < 365:
        return "%d月前" % (delta // 30)
    return "%d年前" % (delta // 365)


def _urllib_http(method: str, url: str, payload, timeout: float):
    """默认 IO 实现：返回 (status, body_bytes)。**异常照抛**，由调用方收口。"""
    data = None
    headers = {"accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["content-type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(getattr(resp, "status", 200) or 200), resp.read()
    except urllib.error.HTTPError as e:  # 500 也是一个**回答**（body 里有 error）
        body = b""
        try:
            body = e.read()
        except Exception:  # noqa: BLE001
            pass
        return int(getattr(e, "code", 0) or 0), body


class MemoryClient:
    """记忆 sidecar 的客户端。

    base_url: sidecar 地址（默认 http://127.0.0.1:8788）
    timeout:  每次请求的超时（秒，默认 2.0 —— 聊天路径）
    http:     可注入的 IO（测试用）。签名 (method, url, payload, timeout)
              -> (status, body_bytes)，异常照抛。
    """

    def __init__(self, base_url: str = DEFAULT_BASE_URL,
                 timeout: float = DEFAULT_TIMEOUT, http=None,
                 cooldown: float = FAILURE_COOLDOWN, clock=None,
                 write_timeout: float = DEFAULT_WRITE_TIMEOUT) -> None:
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout = float(timeout or DEFAULT_TIMEOUT)
        self.write_timeout = float(write_timeout or DEFAULT_WRITE_TIMEOUT)
        self.cooldown = max(0.0, float(cooldown or 0.0))
        self._http = http or _urllib_http
        self._clock = clock or time.monotonic
        self._failed_at = 0.0
        #: 最近一次失败原因（成功一次就清空）—— 面板 / sidecar_status() 读它
        self.last_error = ""
        #: 最近一次调用成功了吗（None = 还没调用过）
        self.last_ok = None
        #: sidecar 有 /list 端点吗（第一次探测到 404 之后就记住，不再重复问）
        self._list_missing = False

    # -- 底层 ------------------------------------------------------------
    def _cooling(self) -> bool:
        """上一次失败还在冷却期里吗（冷却期不真的发请求）。"""
        return bool(self.cooldown and self._failed_at
                    and (self._clock() - self._failed_at) < self.cooldown)

    def _call(self, method: str, path: str, payload=None, timeout=None,
              breaker: bool = True, track_failure: bool = True):
        """发一次请求 → dict | None。**永不抛异常**（失败记 last_error）。

        track_failure=False：这次失败**不算 sidecar 挂了**（不记冷却时间）。
        只给「探测一个可能还不存在的端点」用 —— 比如 /list：老版本 sidecar
        回 404，那不该把接下来 5 秒的聊天记忆一起关掉。
        """
        if breaker and self._cooling():
            self.last_ok = False
            return None          # last_error 还是上一次的真实原因
        url = self.base_url + path
        try:
            status, body = self._http(
                method, url, payload,
                self.timeout if timeout is None else float(timeout),
            )
        except Exception as e:  # 连不上 / 超时 / DNS / socket 挂 —— 都只是「没有记忆」
            self.last_error = "连接失败：%s: %s" % (type(e).__name__, e)
            self.last_ok = False
            if track_failure:
                self._failed_at = self._clock()
            log.debug("记忆 sidecar 请求失败 %s %s：%s", method, path, e)
            return None
        try:
            data = json.loads(body.decode("utf-8") if isinstance(body, (bytes, bytearray))
                              else str(body or ""))
        except Exception as e:  # noqa: BLE001 - 返回了不是 JSON 的东西
            self.last_error = "响应不是合法 JSON（HTTP %s）：%s" % (status, e)
            self.last_ok = False
            return None
        if not isinstance(data, dict):
            self.last_error = "响应格式不对（HTTP %s）：不是 JSON 对象" % status
            self.last_ok = False
            return None
        if status >= 400 or data.get("ok") is False:
            self.last_error = str(data.get("error") or ("HTTP %s" % status))
            self.last_ok = False
            if status >= 400 and track_failure:
                self._failed_at = self._clock()
            return data          # 调用方决定怎么用（search 会拿到 results=[]）
        self.last_error = ""
        self.last_ok = True
        self._failed_at = 0.0
        return data

    def _post(self, path: str, payload, timeout=None):
        return self._call("POST", path, payload, timeout=timeout)

    def _get(self, path: str, timeout=None):
        return self._call("GET", path, None, timeout=timeout)

    # -- 端点 ------------------------------------------------------------
    def health(self, timeout=None) -> dict:
        """GET /health → {ok, cards, models, degraded, ...}（拿不到 → {}）。

        探测**不吃冷却**：supervisor 就是靠它发现 sidecar 回来了没有。
        """
        return self._call("GET", "/health", None, timeout=timeout, breaker=False) or {}

    def stats(self, timeout=None) -> dict:
        """GET /stats → stats 字典（拿不到 → {}）。"""
        data = self._get("/stats", timeout=timeout) or {}
        stats = data.get("stats")
        return stats if isinstance(stats, dict) else {}

    def graph(self, limit: int = 300, top_k: int = 4, timeout=None) -> dict:
        """GET /graph → 语义图 {ok, nodes, edges, ...}（拿不到 → {"ok": False}）。

        节点是记忆卡，位置按向量主成分排；边是余弦近邻。
        sidecar 没有向量时返回 ok=False —— 那是降级，不是错误。
        """
        data = self._call(
            "GET", "/graph?limit=%d&top_k=%d" % (int(limit), int(top_k)),
            None, timeout=timeout, track_failure=False,
        ) or {}
        if not data.get("ok"):
            return {"ok": False, "nodes": [], "edges": [],
                    "reason": str(data.get("reason") or data.get("error") or "")}
        nodes = data.get("nodes")
        edges = data.get("edges")
        return {
            "ok": True,
            "projection": data.get("projection") or "pca",
            "dim": data.get("dim"),
            "nodes": nodes if isinstance(nodes, list) else [],
            "edges": edges if isinstance(edges, list) else [],
        }

    def search(self, query: str, top_k: int = 8, mood: str | None = None,
               kind: str | None = None, key: str | None = None) -> list:
        """POST /search → 结果列表（失败 → []）。

        key: 只看某一张「分区」（工具层拿它保留老的 scope 语义：project / user）。
        """
        payload = {"query": query or "", "top_k": int(top_k or 8)}
        if mood:
            payload["mood"] = mood
        if kind:
            payload["kind"] = kind
        if key:
            payload["key"] = key
        data = self._post("/search", payload) or {}
        rows = data.get("results")
        return rows if isinstance(rows, list) else []

    def upsert(self, cards) -> int:
        """POST /upsert → 实际写入条数（失败 → 0）。"""
        cards = [c for c in (cards or []) if isinstance(c, dict)]
        if not cards:
            return 0
        data = self._post("/upsert", {"cards": cards},
                          timeout=self.write_timeout) or {}
        try:
            return int(data.get("upserted") or 0)
        except Exception:  # noqa: BLE001
            return 0

    def delete(self, ids) -> int:
        """POST /delete → 实际删除条数（失败 → 0）。"""
        ids = [str(i) for i in (ids or []) if str(i or "").strip()]
        if not ids:
            return 0
        data = self._post("/delete", {"ids": ids},
                          timeout=self.write_timeout) or {}
        try:
            return int(data.get("deleted") or 0)
        except Exception:  # noqa: BLE001
            return 0

    def supersede(self, old_id: str, card: dict) -> int:
        """POST /supersede → 写入条数（旧卡标 superseded，失败 → 0）。"""
        if not isinstance(card, dict) or not card:
            return 0
        data = self._post("/supersede", {"oldId": old_id or "", "card": card},
                          timeout=self.write_timeout) or {}
        try:
            return int(data.get("upserted") or 0)
        except Exception:  # noqa: BLE001
            return 0

    def rebuild(self) -> dict:
        """POST /rebuild → {'vector': n, 'bm25': n, 'errors': [...]}（失败 → {}）。"""
        data = self._post("/rebuild", {}, timeout=self.write_timeout) or {}
        out = data.get("rebuilt")
        return out if isinstance(out, dict) else {}

    # -- 方便工具用的高层封装 ---------------------------------------------
    def make_card(self, text: str, *, key: str = "", kind: str = DEFAULT_KIND,
                  importance: float = 0.5, mood: str = "",
                  category: str = "") -> dict:
        """造一张记忆卡（形状 = memory_sidecar/store.py 的 _normalize_card）。

        category：老的内容类型（preference/fact/event/shared/...）。sidecar 会
        原样保留调用方塞的额外字段，所以卡片里带着它 —— 面板按类别筛、
        主动发言找「共同经历」都靠它，不需要再猜 kind。
        """
        text = (text or "").strip()
        kind = (kind or DEFAULT_KIND).strip() or DEFAULT_KIND
        card = {
            "id": card_id(text, kind=kind, key=key or ""),
            "key": key or "",
            "kind": kind,
            "text": text,
            "mood": mood or "",
            "importance": max(0.0, min(1.0, float(importance))),
        }
        cat = (category or "").strip().lower()
        if cat:
            card["category"] = cat
        return card

    def add(self, text: str, *, key: str = "", kind: str = DEFAULT_KIND,
            importance: float = 0.5, mood: str = "",
            category: str = "") -> dict:
        """记一条 → 写进去的那张卡（失败 → 空 dict，原因在 last_error）。"""
        card = self.make_card(text, key=key, kind=kind, importance=importance,
                              mood=mood, category=category)
        if not card["text"]:
            self.last_error = "内容为空，没有写入"
            return {}
        if not self.upsert([card]):
            return {}
        return card

    def recall(self, query: str, top_k: int = DEFAULT_MAX_ROWS, mood: str | None = None,
               kind: str | None = None, key: str | None = None) -> list:
        """检索（空 query 在 sidecar 侧就是「无结果」，这里直接短路）。"""
        if not (query or "").strip():
            return []
        return self.search(query, top_k=top_k, mood=mood, kind=kind, key=key)

    def forget(self, ids) -> int:
        """按 id 删。"""
        return self.delete(ids)

    def remove(self, one_id: str) -> int:
        """按 id 删一条（面板 / 老的 remove 同名入口）。"""
        return self.delete([one_id])

    # -- 列表（面板 / 仪表盘 / 主动发言读它）---------------------------------
    def count(self) -> int:
        """记忆库里**活着**的卡片数（拿不到 → 0）。

        走 /health：它不加载模型、不抢索引锁，面板每秒刷都行。
        """
        health = self.health()
        if health:
            try:
                return int(health.get("cards") or 0)
            except (TypeError, ValueError):
                pass
        try:
            return int((self.stats() or {}).get("active") or 0)
        except (TypeError, ValueError):
            return 0

    def _cards_from_file(self) -> list:
        """兜底：直接读 sidecar 的真值文件 cards.json（**只读**）。

        为什么需要这一条：/list 端点还在落地中，现在线上 sidecar 只有
        /health /stats /search /upsert /delete /supersede /rebuild。
        路径由 /stats 的 cardsFile 给（不猜目录），读不到就返回 []。
        """
        stats = self.stats() or {}
        path = str(stats.get("cardsFile") or "")
        if not path:
            return []
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:  # noqa: BLE001 - 面板读不到就是空列表
            log.debug("读 sidecar 卡片文件失败 %s：%s", path, e)
            return []
        if isinstance(raw, dict):
            raw = raw.get("cards") or []
        return [c for c in raw or [] if isinstance(c, dict)]

    def list_cards(self, limit: int = 200,
                   include_superseded: bool = False,
                   with_category: bool = False) -> list:
        """记忆卡列表（按更新时间倒序；拿不到 → []）。

        先问 sidecar 的 /list，端点不存在（404）就退到读 cards.json ——
        面板要「看得见所有记忆」这件事不能因为版本差异就消失。
        探测失败**不记冷却**：老 sidecar 的 404 不是「记忆服务挂了」。

        with_category=True：/list 只回固定字段（id/key/kind/text/mood/
        importance/status/时间/命中数），**不含**我们写卡时塞的老 category
        （preference / shared / ...）。要按类别显示（面板）或者找「共同经历」
        （主动发言）时，就从 sidecar 的真值文件 cards.json 里按 id 补回来。
        """
        limit = max(1, int(limit or 200))
        rows = []
        if not self._list_missing:
            data = self._call("GET", "/list?limit=%d" % limit, None,
                              breaker=False, track_failure=False)
            got = (data or {}).get("cards")
            if isinstance(got, list):
                rows = [c for c in got if isinstance(c, dict)]
            else:
                self._list_missing = True
        if self._list_missing:
            rows = self._cards_from_file()
        if not include_superseded:
            rows = [c for c in rows
                    if str(c.get("status") or "active") == "active"]
        if with_category and any("category" not in c for c in rows):
            extra = {str(c.get("id") or ""): c for c in self._cards_from_file()}
            for row in rows:
                if "category" in row:
                    continue
                src = extra.get(str(row.get("id") or "")) or {}
                if src.get("category"):
                    row["category"] = src["category"]
        rows.sort(key=lambda c: (c.get("updatedAt") or 0), reverse=True)
        return rows[:limit]

    def all(self, limit: int = 500) -> list:
        """全部记忆（视图对象，按更新时间倒序）—— 面板逐条渲染用。

        带 category：面板的「类别」筛选用的是老的内容类型
        （偏好 / 事实 / 事件 / 共同经历…），那不在 /list 的固定字段里。
        """
        return [MemoryCard(c)
                for c in self.list_cards(limit=limit, with_category=True)]

    def recent(self, n: int = 10) -> list:
        """最近写/改过的 n 条（主动发言拿它当「最近的事」）。"""
        return [MemoryCard(c) for c in self.list_cards(limit=max(1, int(n or 10)))]

    def recent_shared(self, n: int = 3) -> list:
        """最近的**共同经历**（老 category=shared 那批，主动发言的规则 8 用）。"""
        want = max(1, int(n or 3))
        out = []
        for card in self.list_cards(limit=500, with_category=True):
            if str(card.get("category") or "") == "shared":
                out.append(MemoryCard(card))
                if len(out) >= want:
                    break
        return out

    @property
    def available(self) -> bool:
        """上一次调用成功了吗（没调用过 → 保守当 False）。"""
        return bool(self.last_ok)

    # -- 注入易变通道的记忆块 ---------------------------------------------
    def system_block(self, query: str = "", max_rows: int = DEFAULT_MAX_ROWS) -> str:
        """渲染「【你记得的事】」块（注入本轮易变上下文，**不**进 system prompt）。

        形状与 app/brain/memory.py 的 system_block 一致：
            标题 + 免责文案
            （★ 是重要性 0-1）
            - [kind★0.8] (今天) 主人不吃辣

        query 空 / sidecar 挂了 / 一条没召回 → 返回 ""（这轮就是没有可用记忆）。
        sidecar 给了 score <= 0 的行（精排判定「不相关」）也**不注入** ——
        老 memory.py 也是按相关度裁剪的，别把无关的旧事硬塞给模型。
        """
        rows = self.recall(query, top_k=max(1, int(max_rows or DEFAULT_MAX_ROWS)))
        if not rows:
            return ""
        lines = []
        for row in rows[: max(1, int(max_rows or DEFAULT_MAX_ROWS))]:
            if not isinstance(row, dict):
                continue
            text = str(row.get("text") or "").strip()
            if not text:
                continue
            try:
                if float(row.get("score") or 0.0) <= 0.0:
                    continue        # 精排说「不相关」：不占本轮 prompt
            except (TypeError, ValueError):
                pass
            kind = str(row.get("kind") or DEFAULT_KIND)
            try:
                importance = float(row.get("importance", 0.5))
            except Exception:  # noqa: BLE001
                importance = 0.5
            lines.append("- [%s★%.1f] (%s) %s" % (
                kind, importance, _format_time(row.get("updatedAt") or 0), text))
        if not lines:
            return ""
        parts = [MEMORY_BLOCK_HEADER, MEMORY_BLOCK_LEGEND] + lines
        return "\n".join(parts)

    def close(self) -> None:  # pragma: no cover - 与 sidecar 的生命周期无关
        """客户端没有连接池，留着这个方法是给上层统一收尾用的。"""


__all__ = [
    "MemoryClient", "MemoryCard", "card_id", "category_to_kind", "default_port",
    "CATEGORY_TO_KIND", "KIND_TO_CATEGORY", "default_base_url", "DEFAULT_PORT",
    "DEFAULT_BASE_URL", "DEFAULT_TIMEOUT", "DEFAULT_WRITE_TIMEOUT",
    "DEFAULT_MAX_ROWS", "MEMORY_BLOCK_HEADER", "MEMORY_BLOCK_LEGEND",
    "SIDECAR_KINDS", "DEFAULT_KIND",
]
