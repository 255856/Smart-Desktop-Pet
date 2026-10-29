# -*- coding: utf-8 -*-
"""测试用的**假记忆 sidecar**：不需要真进程、网络或模型。

实现的端点和 payload 形状与 memory_sidecar/server.py **一致**（/health、/stats、
/search、/upsert、/delete、/supersede、/rebuild），检索用「CJK 二元组 + 英文整词」
的重合度打分 —— 够验证工具链 / 记忆块的形状，但**不做真语义**。
真正的语义召回（「我不能吃什么」→「主人不吃辣」）由 opt-in 的
tests/test_memory_sidecar_e2e.py 打真 sidecar 验证。
"""
from __future__ import annotations

import json
import re

_CJK = re.compile(r"[\u4e00-\u9fff]")
_WORD = re.compile(r"[A-Za-z0-9_]+")


def _tokens(text: str) -> set:
    """粗糙的检索词：CJK 单字 + 二元组 + 英文整词（小写）。"""
    s = str(text or "").lower()
    out = {w for w in _WORD.findall(s)}
    chars = _CJK.findall(s)
    out.update(chars)
    out.update(chars[i] + chars[i + 1] for i in range(len(chars) - 1))
    return out


class FakeSidecar:
    """一个进程内的假 sidecar（把它塞进 MemoryClient.http 就能用）。"""

    def __init__(self, cards=None) -> None:
        self.cards = {}
        for card in (cards or []):
            self.cards[card["id"]] = dict(card)
        self.calls = []          # [(method, path, payload), ...]
        self.fail = None         # None / "refused" / "timeout" / ("500", 500, b"boom")

    # -- 断言工具用的辅助 --------------------------------------------------
    def paths(self):
        return [c[1] for c in self.calls]

    def count(self, path):
        return sum(1 for c in self.calls if c[1] == path)

    # -- 端点实现 ----------------------------------------------------------
    def handle(self, method, url, payload, timeout):
        """MemoryClient 注入点：签名与 _urllib_http 相同。"""
        raw = "/" + url.split("://", 1)[-1].split("/", 1)[-1]
        path, _, query = raw.partition("?")
        payload = dict(payload or {})
        for pair in query.split("&"):
            k, _, v = pair.partition("=")
            if k:
                payload.setdefault(k, v)

        class _Timeout(Exception):
            pass

        if self.fail == "refused":
            raise ConnectionRefusedError("fake sidecar: 连接被拒绝")
        if self.fail == "timeout":
            raise _Timeout("fake sidecar: 超时")
        if isinstance(self.fail, tuple):
            self.calls.append((method, path, payload))
            return self.fail[1], self.fail[2]

        self.calls.append((method, path, payload))
        payload = payload or {}
        if path == "/health":
            return self._json({
                "ok": True,
                "cards": len(self.cards),
                "cardsTotal": len(self.cards),
                "models": {"embed": "ready", "rerank": "ready"},
                "bm25": "ready",
                "degraded": False,
                "degradation": [],
                "dataDir": "<fake>",
                "port": 8788,
            })
        if path == "/stats":
            by_kind = {}
            for card in self.cards.values():
                by_kind[card["kind"]] = by_kind.get(card["kind"], 0) + 1
            return self._json({"ok": True, "stats": {
                "cards": len(self.cards), "active": len(self.cards),
                "superseded": 0, "byKind": by_kind, "degraded": False,
            }})
        if path == "/list":
            rows = [dict(c, status="active") for c in self.cards.values()]
            rows.sort(key=lambda c: (c.get("updatedAt") or 0), reverse=True)
            limit = int((payload or {}).get("limit") or 50)
            return self._json({"ok": True, "cards": rows[: max(1, limit)],
                               "total": len(rows)})
        if path == "/upsert":
            n = 0
            for card in payload.get("cards") or []:
                self.cards[card["id"]] = dict(card, status="active")
                n += 1
            return self._json({"ok": True, "upserted": n})
        if path == "/delete":
            n = 0
            for cid in payload.get("ids") or []:
                if self.cards.pop(str(cid), None) is not None:
                    n += 1
            return self._json({"ok": True, "deleted": n})
        if path == "/supersede":
            old_id = str(payload.get("oldId") or "")
            card = dict(payload.get("card") or {})
            if old_id in self.cards:
                self.cards[old_id]["status"] = "superseded"
                self.cards[old_id]["supersededBy"] = card.get("id")
            self.cards[card["id"]] = dict(card, status="active")
            return self._json({"ok": True, "upserted": 1})
        if path == "/rebuild":
            return self._json({"ok": True, "rebuilt": {
                "vector": len(self.cards), "bm25": len(self.cards), "errors": []}})
        if path == "/search":
            return self._json({"ok": True, "results": self._search(payload),
                               "path": "fake", "degraded": False})
        return self._json({"ok": False, "error": "not found: %s" % path}, status=404)

    def _search(self, payload):
        query = str(payload.get("query") or "")
        top_k = int(payload.get("top_k") or 8)
        kind = payload.get("kind")
        key = payload.get("key")
        mood = payload.get("mood")
        if not query.strip():
            return []
        q = _tokens(query)
        scored = []
        for card in self.cards.values():
            if card.get("status", "active") != "active":
                continue
            if kind and card.get("kind") != kind:
                continue
            if key and card.get("key") != key:
                continue
            overlap = len(q & _tokens(card.get("text")))
            if overlap <= 0:
                continue
            # 与 sidecar 的公式同形：相对分 × 情绪加成 × 重要度
            rel = overlap / max(1, len(q))
            m = 1.15 if (mood and card.get("mood") == mood) else 1.0
            imp = 0.5 + 0.5 * float(card.get("importance", 0.5))
            scored.append((rel * m * imp, card))
        scored.sort(key=lambda x: -x[0])
        out = []
        for score, card in scored[: max(1, top_k)]:
            row = dict(card)
            row.pop("status", None)
            row["score"] = round(score, 4)
            row["updatedAt"] = card.get("updatedAt") or 1
            out.append(row)
        return out

    @staticmethod
    def _json(obj, status=200):
        return status, json.dumps(obj, ensure_ascii=False).encode("utf-8")


def install(client, fake=None):
    """把假 sidecar 装到一个 MemoryClient 上（不走网络），返回那个 fake。"""
    fake = fake or FakeSidecar()
    client._http = fake.handle
    client._failed_at = 0.0
    client.last_error = ""
    client.last_ok = None
    return fake


__all__ = ["FakeSidecar", "install"]
