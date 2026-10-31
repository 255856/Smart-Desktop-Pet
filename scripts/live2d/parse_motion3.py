# -*- coding: utf-8 -*-
"""按 pixi-live2d-display 的 Cubism4Motion.parse 逻辑，解析 motion3.json 的曲线。

SDK 解析规则（来自 cubism4.min.js）：
  Segments 是**扁平数字数组**：
    [首点(t0,v0), 类型1, ..., 类型2, ...]
  类型 0=线性(吃 2 个数: t1,v1) / 1=贝塞尔(吃 6 个数: c1(t,v),c2(t,v),end(t,v))
  → 线性段总长 3，贝塞尔段总长 7。
用法: python scripts/live2d/parse_motion3.py <motion3.json> [...]
"""
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

LINEAR, BEZIER, STEPPED, INV_STEPPED = 0, 1, 2, 3


def parse_curve(seg):
    """返回 (points, segments)；解析失败抛 ValueError。"""
    points = []
    segments = []
    i = 0
    n = len(seg)
    points.append((seg[0], seg[1]))
    i = 2
    while i < n:
        t = seg[i]
        if t == LINEAR or t == STEPPED or t == INV_STEPPED:
            if i + 2 >= n:
                raise ValueError(f"线性段不完整 @{i}")
            points.append((seg[i + 1], seg[i + 2]))
            i += 3
        elif t == BEZIER:
            if i + 6 >= n:
                raise ValueError(f"贝塞尔段不完整 @{i}")
            points.append((seg[i + 1], seg[i + 2]))
            points.append((seg[i + 3], seg[i + 4]))
            points.append((seg[i + 5], seg[i + 6]))
            i += 7
        else:
            raise ValueError(f"未知段类型 {t} @{i}")
        segments.append(t)
    return points, segments


def main(argv):
    for arg in argv:
        p = Path(arg)
        j = json.loads(p.read_text(encoding="utf-8"))
        print(f"=== {p.name} | dur={j['Meta']['Duration']} "
              f"fps={j['Meta']['Fps']} loop={j['Meta'].get('Loop')}")
        for c in j.get("Curves", []):
            try:
                pts, segs = parse_curve(c["Segments"])
            except ValueError as e:
                print(f"  {c.get('Id'):24s} 解析失败: {e} | segs={c['Segments']}")
                continue
            times = [t for t, _ in pts]
            mono = all(times[i] <= times[i + 1] for i in range(len(times) - 1))
            vals = [v for _, v in pts]
            print(f"  {c.get('Id'):24s} 点={len(pts):2d} 段={segs} "
                  f"时间单调={mono} 值域=[{min(vals):.3f}, {max(vals):.3f}]")
            print(f"      {pts}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
