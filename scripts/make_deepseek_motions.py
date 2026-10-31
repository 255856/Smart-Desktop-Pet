#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成大肥鱼的自写动作（motion3.json）。

曲线格式（pixi-live2d-display 的 Cubism4Motion.parse 规则，实测自 VTube Studio 导出的文件）：
    Segments 是**扁平数字数组**：
        [首点(t0,v0), 类型1, ...载荷..., 类型2, ...载荷...]
    类型 0=线性（载荷 2 个数：t1,v1）
    类型 1=贝塞尔（载荷 6 个数：c1(t,v), c2(t,v), end(t,v)）
    Meta.TotalPointCount = 各曲线点数之和（含贝塞尔控制点）
    Meta.TotalSegmentCount = 各曲线段数之和

用法：
    python scripts/make_deepseek_motions.py
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ANIM = ROOT / "assets" / "live2d" / "DeepSeek" / "DeepSeek" / "Animation"

LINEAR = 0


def linear(points: list[tuple[float, float]]) -> list[float]:
    """把 (t, v) 点序列编成扁平 Segments（全线性段）。"""
    out: list[float] = [round(points[0][0], 4), round(points[0][1], 4)]
    for t, v in points[1:]:
        out += [LINEAR, round(t, 4), round(v, 4)]
    return out


def ramp(t0: float, t1: float, v0: float, v1: float) -> list[tuple[float, float]]:
    return [(t0, v0), (t1, v1)]


def hold(t: float, v: float) -> list[tuple[float, float]]:
    return [(0.0, v), (t, v)]


def osc(t_end: float, t_start: float, mid: float, amp: float,
        period: float, phase: float = 0.0, steps: int = 48) -> list[tuple[float, float]]:
    """正弦振荡采样点（从 t_start 到 t_end）。"""
    pts = [(0.0, 0.0)]
    n = max(4, int((t_end - t_start) / period * steps))
    for i in range(n + 1):
        t = t_start + (t_end - t_start) * i / n
        v = mid + amp * math.sin(2 * math.pi * (t / period) + phase)
        pts.append((t, v))
    pts.append((t_end, mid + amp * math.sin(2 * math.pi * (t_end / period) + phase)))
    return pts


def build(curves: list[tuple[str, list[float]]], duration: float,
          loop: bool = True, fade_in: float = 0.08,
          fade_out: float = 0.12) -> dict:
    out_curves = []
    total_points = total_segments = 0
    for pid, segs in curves:
        out_curves.append({"Target": "Parameter", "Id": pid, "Segments": segs})
        # 复刻 SDK 的解析：首点 1 个，之后每个段加点数（线性 +1，贝塞尔 +3）
        pts = 1
        seg_count = 0
        i = 2
        while i < len(segs):
            t = segs[i]
            if t == LINEAR:
                pts += 1
                i += 3
            else:
                pts += 3
                i += 7
            seg_count += 1
        total_points += pts
        total_segments += seg_count
    return {
        "Version": 3,
        "Meta": {
            "Duration": round(duration, 4),
            "Fps": 60.0,
            "Loop": loop,
            "AreBeziersRestricted": False,
            "CurveCount": len(out_curves),
            "TotalSegmentCount": total_segments,
            "TotalPointCount": total_points,
            "FadeInTime": fade_in,
            "FadeOutTime": fade_out,
            "UserDataCount": 0,
            "TotalUserDataSize": 0,
        },
        "Curves": out_curves,
    }


def wave_motion() -> dict:
    """挥手：左臂抬起后前臂/手腕左右摆动。

    参数含义见 cdi3.json（ParamGroup26 动画参数）：
      Movement2 = 抚胸开关 / Param59 = 大臂透视 / Param60 = L小臂Z / Param61 = L手腕Z
      Param62 = L 大臂z / Param56 = 光2 / Param57 = 光4
    """
    dur = 2.0
    curves = [
        # 抬起左臂的开关（沿用模型自带「抚胸」的抬臂方式）
        ("Movement2", linear(ramp(0.0, 0.30, 0.0, 1.0))),
        ("Param56", linear(hold(dur, 1.0))),
        ("Param57", linear(hold(dur, 0.0))),
        # 大臂
        ("Param62", linear(ramp(0.0, 0.30, 0.0, 0.034))),
        ("Param59", linear(ramp(0.0, 0.35, 0.0, 0.50))),
        # 前臂：抬起来之后左右摆
        ("Param60", linear(ramp(0.05, 0.45, 0.0, 0.86)
                           + osc(dur, 0.45, 0.86, 0.16, 0.62)[1:])),
        # 手腕：跟着摆（相位错开一点，更像挥手）
        ("Param61", linear(ramp(0.05, 0.45, 0.0, 0.20)
                           + osc(dur, 0.45, 0.20, 0.26, 0.62, phase=0.9)[1:])),
    ]
    return build(curves, dur)


def wave_back_motion() -> dict:
    """挥手结束：手臂回到默认姿势。"""
    dur = 0.5
    curves = [
        ("Movement2", linear(ramp(0.0, 0.25, 1.0, 0.0))),
        ("Param56", linear(hold(dur, 1.0))),
        ("Param57", linear(hold(dur, 0.0))),
        ("Param62", linear(ramp(0.0, 0.30, 0.034, 0.0))),
        ("Param59", linear(ramp(0.0, 0.30, 0.50, 0.0))),
        ("Param60", linear(ramp(0.0, 0.30, 0.86, 0.0))),
        ("Param61", linear(ramp(0.0, 0.30, 0.20, 0.0))),
    ]
    return build(curves, dur, loop=False)


def beckon_motion() -> dict:
    """招手：左手抬起后手腕小幅快摆（比挥手幅度小、频率快）。

    基于模型自带的「抚胸」抬手姿势（Movement2=1 + Param59/60/61）。
    """
    dur = 1.8
    curves = [
        ("Movement2", linear(ramp(0.0, 0.28, 0.0, 1.0))),
        ("Param56", linear(hold(dur, 1.0))),
        ("Param57", linear(hold(dur, 0.0))),
        ("Param62", linear(ramp(0.0, 0.28, 0.0, 0.034))),
        ("Param59", linear(ramp(0.0, 0.32, 0.0, 0.50))),
        # 前臂小幅前后摆
        ("Param60", linear(ramp(0.05, 0.40, 0.0, 0.82)
                           + osc(dur, 0.40, 0.82, 0.07, 0.34)[1:])),
        # 手腕快速摆（招手）
        ("Param61", linear(ramp(0.05, 0.40, 0.0, 0.20)
                           + osc(dur, 0.40, 0.20, 0.14, 0.30, phase=0.6)[1:])),
    ]
    return build(curves, dur)


def point_beckon_motion() -> dict:
    """指过来：右手竖起食指后小臂轻晃（招呼/指人）。

    基于模型自带的「竖食指」姿势（Movement3=1 + Param40/39/41/42/43）。
    """
    dur = 1.8
    curves = [
        ("Movement1", linear(hold(dur, 0.0))),
        ("Movement3", linear(ramp(0.0, 0.30, 0.0, 1.0))),
        ("Param56", linear(hold(dur, 1.0))),
        ("Param57", linear(hold(dur, 0.0))),
        ("Param40", linear(ramp(0.15, 0.55, 0.0, 1.0))),
        ("Param36", linear(hold(dur, 0.0))),
        ("Param39", linear(ramp(0.15, 0.60, 0.0, 0.322))),
        ("Param41", linear(ramp(0.15, 0.60, 0.0, 0.708)
                           + osc(dur, 0.60, 0.708, 0.10, 0.55)[1:])),
        ("Param42", linear(ramp(0.15, 0.60, 0.0, -0.058)
                           + osc(dur, 0.60, -0.058, 0.08, 0.55, phase=1.0)[1:])),
        ("Param43", linear(ramp(0.15, 0.60, 0.0, -0.164))),
    ]
    return build(curves, dur)


def beckon_back_motion() -> dict:
    """招手 / 指过来 结束：手放回默认姿势。"""
    dur = 0.5
    curves = [
        ("Movement2", linear(ramp(0.0, 0.25, 1.0, 0.0))),
        ("Movement3", linear(ramp(0.0, 0.25, 1.0, 0.0))),
        ("Movement1", linear(hold(dur, 0.0))),
        ("Param56", linear(hold(dur, 1.0))),
        ("Param57", linear(hold(dur, 0.0))),
        ("Param40", linear(ramp(0.0, 0.25, 1.0, 0.0))),
        ("Param62", linear(ramp(0.0, 0.30, 0.034, 0.0))),
        ("Param59", linear(ramp(0.0, 0.30, 0.50, 0.0))),
        ("Param60", linear(ramp(0.0, 0.30, 0.86, 0.0))),
        ("Param61", linear(ramp(0.0, 0.30, 0.20, 0.0))),
        ("Param36", linear(hold(dur, 0.0))),
        ("Param39", linear(ramp(0.0, 0.30, 0.322, 0.0))),
        ("Param41", linear(ramp(0.0, 0.30, 0.708, 0.0))),
        ("Param42", linear(ramp(0.0, 0.30, -0.058, 0.0))),
        ("Param43", linear(ramp(0.0, 0.30, -0.164, 0.0))),
    ]
    return build(curves, dur, loop=False)


def nod_motion() -> dict:
    """点头：模型头部俯仰输入 AngleY 上下点头（物理链会把它铺到头和身体）。"""
    dur = 1.2
    pts = [(0.0, 0.0)]
    n = 30
    for i in range(n + 1):
        t = dur * i / n
        pts.append((t, 14.0 * math.sin(2 * math.pi * t / 0.6)))
    return build([("AngleY", linear(pts))], dur)


def shake_motion() -> dict:
    """摇头：模型头部左右摇 AngleX。"""
    dur = 1.2
    pts = [(0.0, 0.0)]
    n = 30
    for i in range(n + 1):
        t = dur * i / n
        pts.append((t, 16.0 * math.sin(2 * math.pi * t / 0.45)))
    return build([("AngleX", linear(pts))], dur)


def main() -> int:
    ANIM.mkdir(parents=True, exist_ok=True)
    files = {
        "挥手.motion3.json": wave_motion(),
        "挥手切回.motion3.json": wave_back_motion(),
        "招手.motion3.json": beckon_motion(),
        "指过来.motion3.json": point_beckon_motion(),
        "手部动作切回.motion3.json": beckon_back_motion(),
    }
    for name, data in files.items():
        p = ANIM / name
        p.write_text(json.dumps(data, ensure_ascii=False, indent="\t"),
                     encoding="utf-8")
        print(f"写入 {p}  curves={data['Meta']['CurveCount']} "
              f"points={data['Meta']['TotalPointCount']} "
              f"segments={data['Meta']['TotalSegmentCount']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
