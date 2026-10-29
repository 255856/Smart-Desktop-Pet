# -*- coding: utf-8 -*-
"""记忆「睡觉」：整理记忆 + 定时启动。

睡觉做三件事（在哪算、动哪张卡都在 sidecar 那边）：
* **归组** —— 意思相近的几张卡合成一条，摘要那句话由模型写。
* **模糊** —— 不重要、又久没碰的：重要性打折，标成模糊。
* **遗忘** —— 不重要、而且久到没救的：直接忘掉。

面板是独立进程，跟 sidecar 只能隔着 HTTP 说话。所以这一层只干两件事：
把 sidecar 算好的计划翻译成「模型该写哪几句话」，再把模型写的话交回去执行。
"""
import asyncio
import datetime
import json
import logging
import re
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

#: 定时睡觉的宽限：过了点两小时内都算数（笔记本合过盖、面板晚起）
SLEEP_GRACE_SEC = 2 * 3600
#: 调度线程多久看一眼表
SLEEP_TICK_SEC = 30
#: 一次最多让模型总结几组
SLEEP_MAX_CLUSTERS = 12
#: 状态文件名（放在面板的 data/ 下）
STATE_FILE = "memory_sleep.json"

SUMMARY_SYSTEM = (
    "你在帮桌宠整理记忆。把一组零散的记忆合并成一句中文摘要，"
    "只保留事实本身，不要评价、不要加前缀、不要超过 40 个字。"
)


def _norm_hhmm(value):
    """'3:00' / '0300' / '03:00' → '03:00'；不合法就返回空串。"""
    raw = str(value or "").strip()
    if not raw:
        return ""
    m = re.match(r"^(\d{1,2})\s*[:：]?\s*(\d{2})$", raw)
    if not m:
        return ""
    hh, mm = int(m.group(1)), int(m.group(2))
    if hh > 23 or mm > 59:
        return ""
    return "%02d:%02d" % (hh, mm)


def _mechanical_summary(cluster):
    """模型叫不动时的兜底：把这一组的原文拼成一句。"""
    texts = [str(t).strip() for t in (cluster.get("texts") or []) if str(t).strip()]
    if not texts:
        return ""
    if len(texts) > 1:
        return "%s 等 %d 条相关记忆" % (texts[0][:40], len(texts))
    return texts[0][:40]


def _run_coroutine(coro):
    """在当前线程跑一个协程（面板的路由是同步的，没有事件循环）。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    box = {}

    def worker():
        try:
            box["value"] = asyncio.run(coro)
        except Exception as e:  # noqa: BLE001
            box["error"] = e

    th = threading.Thread(target=worker, name="memory-sleep-chat", daemon=True)
    th.start()
    th.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


class MemorySleepService:
    """睡觉服务：定时 + 计划 + 执行。

    sidecar_url  指记忆 sidecar（面板是独立进程，只能走 HTTP）。
    project_root 仓库根（读 config.yaml 拿模型配置）。
    summarizer   自定义总结函数（测试用；给 clusters，返回 {组 id: 摘要}）。
    clock        自定义时钟（测试用；返回 datetime）。
    """

    def __init__(self, data_dir, sidecar_url: str = "", project_root=None,
                 summarizer=None, clock=None):
        self._data_dir = Path(data_dir)
        self._sidecar_url = sidecar_url or ""
        self._root = Path(project_root) if project_root else None
        self._summarizer = summarizer
        self._clock = clock or datetime.datetime.now
        self._lock = threading.Lock()
        self._running = False
        self._stop = threading.Event()
        self._thread = None
        self._state = self._load()

    # -- 状态 ------------------------------------------------------------
    def _default_state(self):
        return {"enabled": False, "time": "03:00", "lastRunAt": 0, "lastReport": {}}

    def _state_path(self):
        return self._data_dir / STATE_FILE

    def _load(self):
        try:
            raw = json.loads(self._state_path().read_text(encoding="utf-8"))
        except Exception:
            return self._default_state()
        state = self._default_state()
        if isinstance(raw, dict):
            for key in list(state):
                if key in raw:
                    state[key] = raw[key]
        state["time"] = _norm_hhmm(state["time"]) or "03:00"
        return state

    def _save(self):
        path = self._state_path()
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self._state, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            log.warning("写睡觉状态失败 %s：%s", path, e)

    def state(self) -> dict:
        with self._lock:
            data = dict(self._state)
        data["nextRunAt"] = self._next_run_at()
        return data

    def set_schedule(self, enabled=None, time=None) -> dict:
        """改定时设置。enabled / time 传 None 表示不动那一项。"""
        with self._lock:
            if enabled is not None:
                self._state["enabled"] = bool(enabled)
            if time is not None:
                hhmm = _norm_hhmm(time)
                if hhmm:
                    self._state["time"] = hhmm
            self._save()
        return self.state()

    # -- 定时 ------------------------------------------------------------
    def _today_at(self, now):
        hh, mm = (self._state.get("time") or "03:00").split(":")
        return now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)

    def _next_run_at(self) -> int:
        now = self._clock()
        target = self._today_at(now)
        if target <= now:
            target = target + datetime.timedelta(days=1)
        return int(target.timestamp() * 1000)

    def _due(self) -> bool:
        """到了点没？（过了点两小时内算数，今天跑过就不再来一次）"""
        if not self._state.get("enabled"):
            return False
        now = self._clock()
        target = self._today_at(now)
        if now < target:
            return False
        if (now - target).total_seconds() > SLEEP_GRACE_SEC:
            return False
        return int(self._state.get("lastRunAt") or 0) < int(target.timestamp() * 1000)

    def start_scheduler(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._scheduler_loop,
                                        name="memory-sleep-scheduler", daemon=True)
        self._thread.start()

    def stop_scheduler(self):
        self._stop.set()

    def _scheduler_loop(self):
        while not self._stop.wait(SLEEP_TICK_SEC):
            try:
                if self._due():
                    self.run(trigger="schedule")
            except Exception as e:  # noqa: BLE001
                log.warning("定时睡觉出错：%s", e)

    # -- 计划 / 执行 -----------------------------------------------------
    def _client(self):
        from app.brain.memory_client import MemoryClient
        return MemoryClient(self._sidecar_url or "", timeout=10.0)

    def plan(self) -> dict:
        """问 sidecar 要一份睡觉计划（只算不做）。"""
        return self._client().sleep_plan(timeout=15.0)

    def run(self, dry_run: bool = False, trigger: str = "manual") -> dict:
        """睡一觉：算计划 → 让模型写摘要 → 交回 sidecar 执行。"""
        with self._lock:
            if self._running:
                return {"ok": False, "error": "上一次睡觉还没结束"}
            self._running = True
        try:
            return self._run_locked(dry_run=dry_run, trigger=trigger)
        finally:
            with self._lock:
                self._running = False

    def _run_locked(self, dry_run: bool, trigger: str) -> dict:
        client = self._client()
        plan = client.sleep_plan(timeout=15.0)
        if not plan.get("ok"):
            return {"ok": False, "error": plan.get("reason") or "记忆 sidecar 没连上"}

        clusters = plan.get("clusters") or []
        summaries = self._summarize(clusters)
        groups = []
        for cluster in clusters:
            text = str(summaries.get(cluster.get("id")) or "").strip()
            if not text:
                continue
            groups.append({"ids": list(cluster.get("ids") or []), "summary": text})

        if dry_run:
            return {
                "ok": True,
                "dryRun": True,
                "trigger": trigger,
                "merged": len(groups),
                "blurred": len(plan.get("blur") or []),
                "forgotten": len(plan.get("forget") or []),
                "summaries": [g["summary"] for g in groups],
            }

        result = client.apply_sleep(clusters=groups, blur=True, forget=True,
                                    timeout=30.0)
        if not result.get("ok"):
            return {"ok": False, "error": result.get("reason") or "执行失败"}

        report = {
            "at": int(time.time() * 1000),
            "trigger": trigger,
            "merged": int(result.get("merged") or 0),
            "blurred": int(result.get("blurred") or 0),
            "forgotten": int(result.get("forgotten") or 0),
            "summaries": [g["summary"] for g in groups],
        }
        with self._lock:
            self._state["lastRunAt"] = report["at"]
            self._state["lastReport"] = report
            self._save()
        return {"ok": True, **report}

    # -- 总结 ------------------------------------------------------------
    def _summarize(self, clusters) -> dict:
        clusters = [c for c in clusters if c.get("ids")]
        if not clusters:
            return {}
        if self._summarizer is not None:
            try:
                got = self._summarizer(clusters) or {}
            except Exception as e:  # noqa: BLE001
                log.warning("总结记忆失败：%s", e)
                got = {}
            return {c["id"]: (str(got.get(c["id"]) or "").strip()
                              or _mechanical_summary(c)) for c in clusters}
        try:
            got = self._llm_summarize(clusters)
        except Exception as e:  # noqa: BLE001
            log.warning("模型总结失败，退回机械合并：%s", e)
            got = {}
        return {c["id"]: (str(got.get(c["id"]) or "").strip()
                          or _mechanical_summary(c)) for c in clusters}

    def _llm_summarize(self, clusters) -> dict:
        """一次问模型要所有组的摘要（比一组一问快得多）。"""
        clusters = clusters[:SLEEP_MAX_CLUSTERS]
        blocks = []
        for i, cluster in enumerate(clusters, 1):
            lines = "\n".join("- " + str(t) for t in (cluster.get("texts") or []))
            blocks.append("组 %d：\n%s" % (i, lines))
        prompt = (
            "下面每组是桌宠记住的几件事。请为每组写一句中文摘要："
            "一行一句，格式为「序号. 摘要」，只写事实本身，不要解释。\n\n"
            + "\n\n".join(blocks)
        )
        text = self._chat(prompt)
        out = {}
        for line in (text or "").splitlines():
            m = re.match(r"\s*(\d+)\s*[.、)）:：]?\s*(.+)", line)
            if not m:
                continue
            idx = int(m.group(1))
            body = m.group(2).strip()
            if 1 <= idx <= len(clusters) and body:
                out[clusters[idx - 1]["id"]] = body
        return out

    def _llm_config(self):
        from app.core.config import load_config
        root = self._root or Path(__file__).resolve().parent.parent.parent
        return load_config(root / "config.yaml").llm

    def _chat(self, prompt: str) -> str:
        from app.brain.llm_client import ChatMessage, LLMClient
        cfg = self._llm_config()
        client = LLMClient(cfg, SUMMARY_SYSTEM, request_timeout_s=90)
        return _run_coroutine(
            client.chat_once([ChatMessage(role="user", content=prompt)])
        )
