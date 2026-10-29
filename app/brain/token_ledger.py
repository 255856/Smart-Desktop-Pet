"""Token 台账（append-only JSONL）：把每次 LLM 请求的 token 用量记下来。

为什么要有这个台账
------------------
状态栏只显示「这一轮用了多少 token」，进程一关就没了；更要命的是 provider
明明报了缓存命中 / 未命中 / 写入量，代码却只读 prompt / completion / total
三个字段，把缓存信息全丢了 —— 于是「缓存经济性」完全不可见（qq-bridge 能做到
93% 缓存命中率，就是因为它把三个桶分开记账）。

设计约束（Stage 0.2 评审结论）
------------------------------
1. KEY = (session_id, request_index, backend, retry)
   LangGraph 那条路径没有天然的 (turn, step, retry) 坐标，所以坐标由调用方给：
   request_index 是调用方给的单调递增序号，backend 区分 lightweight / standard，
   retry 区分同一次请求的第几次重试。
2. 只存 token 桶，**绝不存金额** —— 价格表会随时间变，台账不该被重写。
3. 幂等折叠：同一个 key 重复到达 → 后到的覆盖先到的（**不累加**），
   跟 qq-bridge 的「later seq overwrites」规则一致。
4. 文件有上限：行数超过 max_lines 就压实 —— 把老的样本行折进「聚合行」，
   只保留最近的样本行；聚合行保证总量不丢。
5. 所有方法吞异常降级 —— 台账是观测设施，绝不能把异常抛进聊天链路。

路径
----
默认 <项目根>/data/token-usage.jsonl；环境变量 DESKTOP_PET_TOKEN_LEDGER 可以覆盖
（测试期间指向临时文件，免得测试流量写进基线测量文件）。

行格式（每行一个 JSON 对象）
---------------------------
{"v":1,"kind":"request","session_id":"char:鲸鱼娘:main","request_index":7,
 "backend":"lightweight","retry":0,"ts":1750000000.123,"model":"deepseek-chat",
 "prompt_tokens":120,"completion_tokens":7,"total_tokens":127,
 "cache_hit_tokens":100,"cache_miss_tokens":20,"cache_write_tokens":0}

压实后被丢掉的样本会折进一条 "kind":"aggregate" 的行（多一个 "n" 字段表示
它代表了几条请求），聚合行和保留下来的样本行互不重叠，汇总时不会重复计数。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from itertools import count
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger(__name__)

# 行版本号（以后加字段时靠它区分老行）
ROW_VERSION = 1

# 只允许写这些 token 桶 —— 金额（price / cost / money / usd）永远不进台账
BUCKET_FIELDS: tuple[str, ...] = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cache_hit_tokens",
    "cache_miss_tokens",
    "cache_write_tokens",
)
# 一行里允许出现的元字段（白名单：调用方塞别的字段进来会被丢掉）
_META_FIELDS: tuple[str, ...] = (
    "v", "kind", "session_id", "request_index", "backend", "retry", "ts", "model",
)
_ROW_FIELDS: tuple[str, ...] = _META_FIELDS + BUCKET_FIELDS

DEFAULT_SESSION = "desktop"
DEFAULT_MAX_LINES = 20000
DEFAULT_BUCKET_MS = 60_000

#: 台账路径的环境变量覆盖（测试用：把流量写到临时文件，别污染基线文件）
LEDGER_PATH_ENV = "DESKTOP_PET_TOKEN_LEDGER"

# 进程内单调递增的请求序号（两个后端共用一条序列 → key 不会互相撞）
_INDEX = count(1)

_SESSION_LOCK = threading.Lock()
_SESSION_ID = DEFAULT_SESSION

_LEDGER_LOCK = threading.Lock()
_SINGLETON: Optional["TokenLedger"] = None


def _safe_int(value: Any) -> int:
    """尽最大努力转 int；转不了就 0（绝不抛）。"""
    try:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            return int(float(value.strip()))
        return 0
    except Exception:  # noqa: BLE001
        return 0


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:  # noqa: BLE001
        return 0.0


def default_ledger_path() -> Path:
    """默认台账路径：<项目根>/data/token-usage.jsonl

    环境变量 DESKTOP_PET_TOKEN_LEDGER 可以覆盖它 —— 测试期间指向临时文件，
    免得测试流量写进仓库里的基线测量文件（Stage 0.5 要用它量缓存命中率）。
    """
    override = os.environ.get(LEDGER_PATH_ENV)
    if override:
        try:
            return Path(override)
        except Exception:  # noqa: BLE001
            pass
    try:
        return Path(__file__).resolve().parents[2] / "data" / "token-usage.jsonl"
    except Exception:  # noqa: BLE001
        return Path("data") / "token-usage.jsonl"


def next_request_index() -> int:
    """进程内单调递增的请求序号。

    LangGraph 那条路径没有天然的 turn/step 坐标，所以请求序号由这里统一发：
    同一个 key 再来一次时靠它折叠（覆盖），不会重复计数。
    """
    try:
        return next(_INDEX)
    except Exception:  # noqa: BLE001
        return int(time.time() * 1000) % 10_000_000


def set_session(session_id: str) -> None:
    """设置「当前会话 id」（没有 session_id 的调用方用它兜底）。"""
    global _SESSION_ID
    try:
        with _SESSION_LOCK:
            _SESSION_ID = str(session_id or DEFAULT_SESSION)
    except Exception:  # noqa: BLE001
        pass


def current_session() -> str:
    """当前会话 id（默认 "desktop"）。"""
    try:
        with _SESSION_LOCK:
            return _SESSION_ID
    except Exception:  # noqa: BLE001
        return DEFAULT_SESSION


def _row_key(row: dict) -> tuple:
    """台账主键：(session_id, request_index, backend, retry)。"""
    return (
        str(row.get("session_id") or ""),
        _safe_int(row.get("request_index")),
        str(row.get("backend") or ""),
        _safe_int(row.get("retry")),
    )


def _fold(rows: Iterable[dict]) -> list[dict]:
    """按主键折叠：后到的覆盖先到的（**不累加**），顺序按最后一次出现排。"""
    out: dict[tuple, dict] = {}
    for row in rows:
        try:
            key = _row_key(row)
        except Exception:  # noqa: BLE001
            continue
        out.pop(key, None)
        out[key] = row
    return list(out.values())


def _empty_buckets() -> dict:
    data = {f: 0 for f in BUCKET_FIELDS}
    data["cache_hit_rate"] = 0.0
    return data


def _empty_summary() -> dict:
    data = {
        "sessions": 0,
        "requests": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cache_hit_tokens": 0,
        "cache_miss_tokens": 0,
        "cache_write_tokens": 0,
        "cache_hit_rate": 0.0,
        "by_backend": {},
    }
    return data


def _accumulate(bucket: dict, row: dict) -> None:
    for f in BUCKET_FIELDS:
        bucket[f] = _safe_int(bucket.get(f)) + _safe_int(row.get(f))


def _hit_rate(hit: int, miss: int) -> float:
    """缓存命中率 = 命中 / (命中 + 未命中)。"""
    denom = _safe_int(hit) + _safe_int(miss)
    if denom <= 0:
        return 0.0
    return round(_safe_int(hit) / denom, 4)


class TokenLedger:
    """Append-only JSONL token 台账。

    - 只存 token 桶，不存金额（价格表会变，台账不该被重写）。
    - 同一个 (session_id, request_index, backend, retry) 重复到达 → 覆盖。
    - 行数超过 max_lines 自动压实：老样本折进聚合行，保留最近的样本行。
    - 所有方法都吞异常降级，绝不把异常抛进聊天链路。
    """

    def __init__(self, path: str | Path = None, max_lines: int = DEFAULT_MAX_LINES):
        self.path = Path(path) if path else default_ledger_path()
        try:
            self.max_lines = max(16, int(max_lines or DEFAULT_MAX_LINES))
        except Exception:  # noqa: BLE001
            self.max_lines = DEFAULT_MAX_LINES
        self._lock = threading.RLock()
        self._lines = 0
        self._counted = False

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------
    def record(self, session_id: str, request_index: int, backend: str,
               retry: int, usage: dict, model: str = "", ts: float = 0) -> bool:
        """记一条用量。返回是否真的写进去了（失败一律 False，不抛）。"""
        try:
            if not isinstance(usage, dict):
                return False
            row = self._build_row(session_id, request_index, backend, retry,
                                  usage, model, ts)
            if row is None:
                return False
            self._append(row)
            return True
        except Exception:  # noqa: BLE001
            log.debug("token ledger: record 失败（忽略）", exc_info=True)
            return False

    def _build_row(self, session_id, request_index, backend, retry,
                   usage: dict, model: str, ts: float) -> Optional[dict]:
        try:
            sid = str(session_id or current_session() or DEFAULT_SESSION)
            try:
                ridx = int(request_index)
            except Exception:  # noqa: BLE001
                ridx = next_request_index()
            try:
                rt = int(retry)
            except Exception:  # noqa: BLE001
                rt = 0
            ts_value = _safe_float(ts) or time.time()
            row = {
                "v": ROW_VERSION,
                "kind": "request",
                "session_id": sid,
                "request_index": ridx,
                "backend": str(backend or ""),
                "retry": rt,
                "ts": round(ts_value, 3),
                "model": str(model or ""),
            }
            for f in BUCKET_FIELDS:
                row[f] = _safe_int(usage.get(f))
            # 白名单：调用方往 usage 里塞金额字段也不会进文件
            return {k: row[k] for k in _ROW_FIELDS}
        except Exception:  # noqa: BLE001
            return None

    def _append(self, row: dict) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 同一个 key 又来了 -> 覆盖最后一行（**不追加第二行**）：
            # 台账是 append-only 的，但重复的 key 不该让文件长出重复行。
            if self._tail_key() == _row_key(row):
                self._replace_tail(row)
                return
            line = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            if not self._counted:
                self._lines = self._count_lines()
                self._counted = True
            self._lines += 1
            if self._lines >= self.max_lines:
                self._compact_locked()

    def _tail_key(self):
        """读文件最后一行的主键（读不到就 None）。"""
        try:
            if not self.path.is_file():
                return None
            size = self.path.stat().st_size
            if size <= 0:
                return None
            with self.path.open("rb") as f:
                f.seek(max(0, size - 4096))
                chunk = f.read()
            lines = [line for line in
                     chunk.decode("utf-8", errors="ignore").splitlines()
                     if line.strip()]
            if not lines:
                return None
            obj = json.loads(lines[-1])
            return _row_key(obj) if isinstance(obj, dict) else None
        except Exception:  # noqa: BLE001
            return None

    def _replace_tail(self, row: dict) -> None:
        """把最后一行换成新行（同 key 覆盖）。"""
        rows = self._read_rows()
        if rows:
            rows.pop()
        rows.append(row)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(self._sanitize_row(r), ensure_ascii=False,
                                   separators=(",", ":")) + "\n")
        os.replace(str(tmp), str(self.path))
        self._lines = len(rows)
        self._counted = True

    def _count_lines(self) -> int:
        try:
            if not self.path.is_file():
                return 0
            n = 0
            with self.path.open("r", encoding="utf-8") as f:
                for _ in f:
                    n += 1
            return n
        except Exception:  # noqa: BLE001
            return 0

    # ------------------------------------------------------------------
    # 压实
    # ------------------------------------------------------------------
    def compact(self) -> int:
        """压实文件，返回压实后的行数（失败返回当前行数，不抛）。"""
        try:
            with self._lock:
                return self._compact_locked()
        except Exception:  # noqa: BLE001
            log.debug("token ledger: compact 失败（忽略）", exc_info=True)
            return self._count_lines()

    def _compact_locked(self) -> int:
        rows = self._read_rows()
        aggregates = [r for r in rows if str(r.get("kind")) == "aggregate"]
        requests = [r for r in rows if str(r.get("kind")) != "aggregate"]
        folded = _fold(requests)
        keep = max(1, self.max_lines // 2)
        if len(folded) > keep:
            dropped = folded[: len(folded) - keep]
            retained = folded[len(folded) - keep:]
        else:
            dropped, retained = [], folded
        # 注意：_aggregate_rows 返回的是「已有聚合行 + 新折进去的样本」的合并结果，
        # 不能再把 aggregates 单独加一遍（那会把聚合行算两遍）。
        out = self._aggregate_rows(dropped, aggregates)
        out.extend(retained)
        out = [self._sanitize_row(r) for r in out]
        tmp = self.path.with_name(self.path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for r in out:
                f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
        os.replace(str(tmp), str(self.path))
        self._lines = len(out)
        self._counted = True
        return self._lines

    def _aggregate_rows(self, dropped: list[dict],
                        existing: list[dict]) -> list[dict]:
        """把被丢掉的样本折成聚合行。

        按 (session_id, backend) 分组 —— 这样压实之后 summary 的「分后端拆分」
        仍然完整（只按 session 聚合的话，被丢掉样本的 backend 信息就丢了）。
        """
        by_key: dict[tuple[str, str], dict] = {}
        for r in existing:
            key = (str(r.get("session_id") or ""), str(r.get("backend") or "*"))
            by_key[key] = dict(r)
        for r in dropped:
            key = (str(r.get("session_id") or ""), str(r.get("backend") or "*"))
            agg = by_key.get(key)
            if agg is None:
                agg = {
                    "v": ROW_VERSION,
                    "kind": "aggregate",
                    "session_id": key[0],
                    "request_index": 0,
                    "backend": key[1],
                    "retry": 0,
                    "ts": round(_safe_float(r.get("ts")), 3),
                    "model": "",
                    "n": 0,
                }
                for f in BUCKET_FIELDS:
                    agg[f] = 0
                by_key[key] = agg
            for f in BUCKET_FIELDS:
                agg[f] = _safe_int(agg.get(f)) + _safe_int(r.get(f))
            agg["n"] = _safe_int(agg.get("n")) + 1
        return list(by_key.values())

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------
    def _read_rows(self) -> list[dict]:
        rows: list[dict] = []
        try:
            if not self.path.is_file():
                return []
            with self.path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:  # noqa: BLE001
                        continue
                    if isinstance(obj, dict):
                        rows.append(obj)
        except Exception:  # noqa: BLE001
            log.debug("token ledger: 读取失败（忽略）", exc_info=True)
        return rows

    @staticmethod
    def _sanitize_row(row: dict) -> dict:
        out = {k: row.get(k) for k in _ROW_FIELDS if k in row}
        out.setdefault("v", ROW_VERSION)
        out.setdefault("kind", "request")
        out["session_id"] = str(row.get("session_id") or "")
        out["request_index"] = _safe_int(row.get("request_index"))
        out["backend"] = str(row.get("backend") or "")
        out["retry"] = _safe_int(row.get("retry"))
        out["ts"] = round(_safe_float(row.get("ts")), 3)
        out["model"] = str(row.get("model") or "")
        for f in BUCKET_FIELDS:
            out[f] = _safe_int(row.get(f))
        if str(row.get("kind")) == "aggregate":
            out["n"] = _safe_int(row.get("n"))
        return out

    def rows(self, session_id: Optional[str] = None) -> list[dict]:
        """折叠后的请求行（不含聚合行）。"""
        try:
            requests = [r for r in self._read_rows()
                        if str(r.get("kind")) != "aggregate"]
            folded = _fold(requests)
            if session_id is not None:
                sid = str(session_id)
                folded = [r for r in folded
                          if str(r.get("session_id") or "") == sid]
            return [self._sanitize_row(r) for r in folded]
        except Exception:  # noqa: BLE001
            return []

    def summary(self, session_id: Optional[str] = None) -> dict:
        """总量汇总：token 桶 + 缓存命中率 + 分后端拆分。"""
        out = _empty_summary()
        try:
            rows = self._read_rows()
            aggregates = [r for r in rows if str(r.get("kind")) == "aggregate"]
            requests = _fold([r for r in rows
                              if str(r.get("kind")) != "aggregate"])
            if session_id is not None:
                sid = str(session_id)
                aggregates = [r for r in aggregates
                              if str(r.get("session_id") or "") == sid]
                requests = [r for r in requests
                            if str(r.get("session_id") or "") == sid]
            for r in aggregates + requests:
                _accumulate(out, r)
                backend = str(r.get("backend") or "")
                if backend and backend != "*":
                    bucket = out["by_backend"].setdefault(backend,
                                                          _empty_buckets())
                    _accumulate(bucket, r)
            out["requests"] = len(requests) + sum(
                _safe_int(r.get("n")) for r in aggregates)
            out["sessions"] = len({str(r.get("session_id") or "") for r in rows})
            out["cache_hit_rate"] = _hit_rate(out["cache_hit_tokens"],
                                              out["cache_miss_tokens"])
            for bucket in out["by_backend"].values():
                bucket["cache_hit_rate"] = _hit_rate(
                    bucket.get("cache_hit_tokens", 0),
                    bucket.get("cache_miss_tokens", 0))
            return out
        except Exception:  # noqa: BLE001
            return _empty_summary()

    def turns(self, session_id: str) -> list[dict]:
        """某个 session 的逐请求明细（按时间排序）。"""
        try:
            rows = self.rows(session_id)
            rows.sort(key=lambda r: (_safe_float(r.get("ts")),
                                      _safe_int(r.get("request_index"))))
            return rows
        except Exception:  # noqa: BLE001
            return []

    def series(self, bucket_ms: int = DEFAULT_BUCKET_MS) -> list[dict]:
        """按时间桶聚合（bucket_ms 毫秒一桶），返回按时间升序的桶列表。"""
        try:
            ms = max(1, int(bucket_ms or DEFAULT_BUCKET_MS))
        except Exception:  # noqa: BLE001
            ms = DEFAULT_BUCKET_MS
        try:
            buckets: dict[int, dict] = {}
            for r in self.rows():
                ts = _safe_float(r.get("ts"))
                start = int(ts // ms) * ms
                slot = buckets.setdefault(start, {
                    "ts": start,
                    "requests": 0,
                    **{f: 0 for f in BUCKET_FIELDS},
                })
                for f in BUCKET_FIELDS:
                    slot[f] = _safe_int(slot.get(f)) + _safe_int(r.get(f))
                slot["requests"] = _safe_int(slot.get("requests")) + 1
            out = [buckets[k] for k in sorted(buckets)]
            for slot in out:
                slot["cache_hit_rate"] = _hit_rate(slot.get("cache_hit_tokens", 0),
                                                  slot.get("cache_miss_tokens", 0))
            return out
        except Exception:  # noqa: BLE001
            return []

    def line_count(self) -> int:
        """当前文件行数（读不出来就返回内存里的计数）。"""
        try:
            n = self._count_lines()
            self._lines = n
            self._counted = True
            return n
        except Exception:  # noqa: BLE001
            return self._lines


def get_ledger(path: Optional[str | Path] = None) -> TokenLedger:
    """进程内单例（两个后端写同一个文件）。

    传 path 拿一个**独立**实例（测试用），不传就拿单例。
    """
    global _SINGLETON
    try:
        if path is not None:
            target = Path(path)
            with _LEDGER_LOCK:
                if _SINGLETON is not None and _SINGLETON.path == target:
                    return _SINGLETON
            return TokenLedger(target)
        with _LEDGER_LOCK:
            if _SINGLETON is None:
                _SINGLETON = TokenLedger(default_ledger_path())
            return _SINGLETON
    except Exception:  # noqa: BLE001
        return TokenLedger(path or default_ledger_path())


def record_usage(usage: dict, backend: str = "", model: str = "",
                 session_id: str = "", request_index: Optional[int] = None,
                 retry: int = 0) -> bool:
    """把一次请求的 usage 写进台账（失败不抛 —— 台账是观测设施）。"""
    try:
        if request_index is None:
            request_index = next_request_index()
        return get_ledger().record(
            session_id=session_id or current_session(),
            request_index=request_index,
            backend=backend,
            retry=retry,
            usage=usage,
            model=model,
        )
    except Exception:  # noqa: BLE001
        log.debug("token ledger: record_usage 失败（忽略）", exc_info=True)
        return False


__all__ = [
    "TokenLedger",
    "BUCKET_FIELDS",
    "DEFAULT_MAX_LINES",
    "LEDGER_PATH_ENV",
    "default_ledger_path",
    "get_ledger",
    "next_request_index",
    "record_usage",
    "current_session",
    "set_session",
]
