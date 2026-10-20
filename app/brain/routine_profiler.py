"""桌宠规律学习：从 ActivityWatch 历史窗口事件里聚合成「时段 → 活动」直方图。

主人开机后第一次主动关心触发时, 后台 worker 线程拉过去 14 天的
aw-watcher-window 事件, 按 (weekday, hour) 分桶 + 按 app 计数, 写到
data/routines.json。后续主动搭话根据当前时间查桶, 生成一句自然语言 hint
（比如「这个时间你通常在写代码 / 看文档」）注入 prompt。

不做（v57 MVP 范围外）:
- 不预测 / 不告警 — 只看主人过去做了什么, 不强加「应该做什么」
- 不做内容聚类 (app1/app2 区分粒度足够, title 不参与)
- 不做时间序列预测 / 工作效率曲线
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


_DEFAULT_DAYS = 14
_DEFAULT_MAX_EVENTS = 5000
_DEFAULT_APP_TOP_N = 3   # 单个时段保留前 3 个 app
_STALE_SECONDS = 6 * 3600   # 6 小时认为需要刷新


# ---- ActivityWatch 集成 ----
def _http_get_json(url: str, timeout: float = 8.0):
    """拉 AW 一个 JSON 端点, 失败返 None。跟 activitywatch.py 隔离避免循环依赖。"""
    try:
        import urllib.error
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "desktop-pet-rp/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="replace")
            import json as _j
            return _j.loads(raw) if raw else None
    except Exception:  # noqa: BLE001
        return None


def _fetch_aw_window_events(
    server_url: str,
    hostname: str,
    *,
    days: int = _DEFAULT_DAYS,
    max_events: int = _DEFAULT_MAX_EVENTS,
) -> list[dict]:
    """拉过去 N 天 aw-watcher-window 事件, 返原始事件列表。"""
    base = (server_url or os.environ.get("AW_SERVER_URL")
            or "http://127.0.0.1:5600").rstrip("/")
    # 找 bucket
    buckets = _http_get_json(f"{base}/api/0/buckets/")
    if isinstance(buckets, dict):
        buckets = [{"id": bid, **(m if isinstance(m, dict) else {})}
                   for bid, m in buckets.items()]
    if not isinstance(buckets, list):
        return []
    host = (hostname or os.environ.get("AW_HOSTNAME") or "").lower() or None
    bid = None
    for prefix in ("aw-watcher-window",):
        for b in buckets:
            bidn = b.get("id", "")
            if host and (bidn.startswith(f"{prefix}-{host}")
                         or bidn.startswith(f"{prefix}_{host}")):
                bid = bidn
                break
        if bid:
            break
        # fallback: 任意 prefix bucket
        for b in buckets:
            bidn = b.get("id", "")
            if bidn.startswith(prefix):
                bid = bidn
                break
        if bid:
            break
    if not bid:
        return []
    # 拉事件
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=max(1, int(days)))
    url = (
        f"{base}/api/0/buckets/{bid}/events"
        f"?start={start.isoformat().replace('+00:00', 'Z')}"
        f"&end={end.isoformat().replace('+00:00', 'Z')}"
        f"&limit={max(1, int(max_events))}"
    )
    data = _http_get_json(url, timeout=10.0)
    return data if isinstance(data, list) else []


def _normalize_app_name(app: str) -> str:
    """app 名称归一化: 取可执行文件名 (去路径 + 去扩展名 + lowercase)。"""
    if not app:
        return "未知"
    base = os.path.basename(app)
    if base.lower().endswith(".exe"):
        base = base[:-4]
    return base or app


class RoutineProfiler:
    """聚合 AW 历史 → 「时段 × app」直方图 → 给 proactive 用自然语言 hint。"""

    def __init__(self, data_dir: Path | str):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "routines.json"
        self._hist: dict[str, dict[str, int]] = {}    # {weekday: {app: count}}
        self._fetched_ts: float = 0.0
        self._lock = threading.Lock()
        self._load_from_disk()

    # ---- 持久化 ----

    def _load_from_disk(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            log.warning("RoutineProfiler: 读 %s 失败: %s", self.path, e)
            return
        hist = raw.get("histogram") or {}
        if not isinstance(hist, dict):
            return
        with self._lock:
            self._hist = {str(k): dict(v) for k, v in hist.items()
                          if isinstance(v, dict)}
            self._fetched_ts = float(raw.get("fetched_ts") or 0.0)

    def _save_to_disk(self) -> None:
        payload = {
            "histogram": self._hist,
            "fetched_ts": self._fetched_ts,
        }
        try:
            self.path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:  # noqa: BLE001
            log.warning("RoutineProfiler: 写 %s 失败: %s", self.path, e)

    # ---- 聚合 ----

    @staticmethod
    def _event_to_local_weekday_hour(ev: dict) -> Optional[tuple[int, int, str]]:
        """aw 事件 → (weekday 0-6, hour 0-23, app_name) 或 None。"""
        ts = ev.get("timestamp")
        if not ts:
            return None
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            local = dt.astimezone()
            return (local.weekday(), local.hour,
                    _normalize_app_name((ev.get("data") or {}).get("app", "")))
        except Exception:  # noqa: BLE001
            return None

    def refresh_from_aw(
        self,
        *,
        server_url: Optional[str] = None,
        hostname: Optional[str] = None,
        days: int = _DEFAULT_DAYS,
        max_events: int = _DEFAULT_MAX_EVENTS,
    ) -> bool:
        """从 AW 拉过去 N 天事件聚合成直方图, 写盘。返回是否成功。"""
        url = server_url or os.environ.get("AW_SERVER_URL") or "http://127.0.0.1:5600"
        events = _fetch_aw_window_events(
            url, hostname or "", days=days, max_events=max_events,
        )
        if not events:
            log.info("RoutineProfiler: AW 没数据, 不更新")
            return False
        counter: dict[int, Counter] = {}
        for ev in events:
            parsed = self._event_to_local_weekday_hour(ev)
            if not parsed:
                continue
            wd, _hr, app = parsed
            counter.setdefault(wd, Counter())[app] += 1
        with self._lock:
            self._hist = {
                str(wd): dict(c.most_common(_DEFAULT_APP_TOP_N * 5))
                for wd, c in counter.items()
            }
            self._fetched_ts = time.time()
        self._save_to_disk()
        log.info("RoutineProfiler: 聚合 %d 天窗口数据, %d 个时段",
                 days, len(self._hist))
        return True

    def is_stale(self, max_age_s: float = _STALE_SECONDS) -> bool:
        """距离上次聚合超过 max_age_s 秒 → 需要刷新。"""
        return (time.time() - self._fetched_ts) > max_age_s

    # ---- 主动关心接口 ----

    def hint_for_now(
        self,
        *,
        now: Optional[datetime] = None,
        top_n: int = _DEFAULT_APP_TOP_N,
    ) -> str:
        """当前时间最常用的几个 app, 拼成一句自然语言。

        没数据 / 不在 dict 里 → 返空串。
        """
        if now is None:
            now = datetime.now()
        wd = str(now.weekday())
        with self._lock:
            bucket = self._hist.get(wd)
        if not bucket:
            return ""
        # 按 count 倒序, 取 top_n
        items = sorted(bucket.items(), key=lambda kv: kv[1], reverse=True)[:max(1, int(top_n))]
        apps = [a for a, _ in items]
        if not apps:
            return ""
        # 自然语言
        wd_name = "一二三四五六日"[now.weekday()]
        if len(apps) == 1:
            return f"历史规律: 星期{wd_name} 这时通常在 {apps[0]}"
        if len(apps) == 2:
            return f"历史规律: 星期{wd_name} 这时通常在 {apps[0]} / {apps[1]}"
        return f"历史规律: 星期{wd_name} 这时通常在 {apps[0]} / {apps[1]} / {apps[2]}"


__all__ = ["RoutineProfiler"]