# -*- coding: utf-8 -*-
"""进程树 CPU / 内存采样工具（测量脚本共用）。

口径（全项目统一）：
* **进程树**：以被测进程为根，含全部子孙（Chromium 的 GPU / 渲染进程、
  sidecar 这类子进程都在里面）。
* **CPU%**：两次采样之间 (user+system 的增量) / (时间增量)，即「占满一个核心
  的百分比」。100% = 一个核心跑满；整机 CPU 占比 = 这个数 / 核心数。
* **内存**：rss = 工作集（Windows 任务管理器看到的那个数），
  priv = 私有提交（独占的那部分，共享页不算）。
* **时间轴**：默认 250ms 一格，记录每格的 CPU% / 内存 / 进程数。

注意：共享内存会被重复计数 —— Chromium 的 DLL 与纹理在多个进程里各映射一份，
所以「树内 RSS 求和」会高估；报告里同时给 private 口径。
"""
from __future__ import annotations

import os
import time

__all__ = ["tree", "sample", "delta", "Timeline", "MAIN_PID"]

MAIN_PID = os.getpid()


def tree(proc):
    """pid -> psutil.Process（含自身与全部子孙）。"""
    out = {proc.pid: proc}
    try:
        for kid in proc.children(recursive=True):
            out[kid.pid] = kid
    except Exception:  # noqa: BLE001
        pass
    return out


def sample(proc):
    """采样进程树：pid -> {name, rss, priv, cpu}。"""
    rows = {}
    for pid, p in tree(proc).items():
        try:
            with p.oneshot():
                mi = p.memory_info()
                ct = p.cpu_times()
                priv = 0
                try:
                    priv = int(getattr(p.memory_full_info(), "private", 0) or 0)
                except Exception:  # noqa: BLE001
                    priv = 0
            rows[pid] = {
                "name": p.name(),
                "rss": int(mi.rss),
                "priv": priv,
                "cpu": float(ct.user + ct.system),
            }
        except Exception:  # noqa: BLE001
            continue
    return rows


def delta(before, after, wall_s):
    """两次采样之间的 CPU% / 内存汇总。"""
    total_cpu = 0.0
    main_cpu = 0.0
    rss = 0
    priv = 0
    per = []
    for pid, row in after.items():
        old = before.get(pid)
        rss += row["rss"]
        priv += row.get("priv", 0)
        d = 0.0
        if old is not None:
            d = max(0.0, row["cpu"] - old["cpu"])
        total_cpu += d
        if pid == MAIN_PID:
            main_cpu = d
        per.append({
            "pid": pid,
            "name": row["name"],
            "rss_mb": round(row["rss"] / 1048576, 1),
            "private_mb": round(row.get("priv", 0) / 1048576, 1),
            "cpu_pct": round(d / wall_s * 100, 2) if wall_s > 0 else 0.0,
        })
    return {
        "process_count": len(after),
        "rss_mb": round(rss / 1048576, 1),
        "private_mb": round(priv / 1048576, 1),
        "cpu_pct_total": round(total_cpu / wall_s * 100, 2) if wall_s > 0 else 0.0,
        "cpu_pct_main": round(main_cpu / wall_s * 100, 2) if wall_s > 0 else 0.0,
        "cpu_seconds": round(total_cpu, 2),
        "per_process": sorted(per, key=lambda x: -x["rss_mb"]),
    }


class Timeline:
    """启动 CPU 时间轴：按固定间隔采样，记录每格 CPU% / 内存 / 进程数。"""

    def __init__(self, proc, interval: float = 0.25):
        self.proc = proc
        self.interval = interval
        self.rows: list[dict] = []
        self.marks: list[dict] = []
        self._prev = None
        self._t0 = None
        self._last_at = None

    def start(self):
        self._t0 = time.perf_counter()
        self._prev = sample(self.proc)
        self._last_at = self._t0
        return self._t0

    def tick(self):
        """采一格。返回该格的汇总 dict。"""
        now = time.perf_counter()
        cur = sample(self.proc)
        wall = max(1e-6, now - (self._last_at or now))
        self._last_at = now
        d = delta(self._prev, cur, wall)
        row = {
            "t": round(now - self._t0, 2),
            "cpu_pct": d["cpu_pct_total"],
            "cpu_pct_main": d["cpu_pct_main"],
            "rss_mb": d["rss_mb"],
            "private_mb": d["private_mb"],
            "process_count": d["process_count"],
        }
        self.rows.append(row)
        self._prev = cur
        return d

    def mark(self, label: str):
        """在时间轴上打一个标记（谁在第几秒就绪）。"""
        t = round(time.perf_counter() - (self._t0 or time.perf_counter()), 2)
        self.marks.append({"t": t, "label": label})
        return t

    def summary(self) -> dict:
        if not self.rows:
            return {}
        peak = max(self.rows, key=lambda r: r["cpu_pct"])
        total_cpu_s = sum(r["cpu_pct"] / 100.0 * self.interval for r in self.rows)
        return {
            "window_s": self.rows[-1]["t"],
            "peak_cpu_pct": peak["cpu_pct"],
            "peak_at_s": peak["t"],
            "mean_cpu_pct": round(
                sum(r["cpu_pct"] for r in self.rows) / len(self.rows), 2),
            "cpu_seconds_total": round(total_cpu_s, 2),
            "peak_rss_mb": max(r["rss_mb"] for r in self.rows),
            "end_rss_mb": self.rows[-1]["rss_mb"],
            "end_private_mb": self.rows[-1]["private_mb"],
            "max_process_count": max(r["process_count"] for r in self.rows),
            "marks": self.marks,
        }
