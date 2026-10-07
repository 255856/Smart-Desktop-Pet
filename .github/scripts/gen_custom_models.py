#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫描 gh-pages 分支上的私有模型目录，生成 demo 用的 custom-models.js 注册表。

用法（在 deploy-demo.yml 的 Build 步骤中）:
    python3 .github/scripts/gen_custom_models.py <models_root> <output_js>

- models_root: gh-pages checkout 里的 custom_models/ 目录
- output_js:   写入部署产物（如 _demo/custom-models.js）

每个 `models_root/<模型目录>/<名字>.model3.json` 生成一条注册项：
    { id, name(取 model3.json 文件名), badge:"Private", path:"custom_models/<rel>",
      motions:    [{group, file}],   # 递归扫描 *.motion3.json（VTS 导出常不在 model3.json 登记）
      expressions:[{name, file}] }   # 递归扫描 *.exp3.json
页面加载时若 model3.json 未登记这些文件，会按注册表注入（见 live2d-demo.js loadModel）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

M3_SUFFIX = ".model3.json"
MOTION_SUFFIX = ".motion3.json"
EXP_SUFFIX = ".exp3.json"


def _motion_group(motion_file: Path, model_dir: Path) -> str:
    """动作组命名：文件名含 idle → Idle；根目录散文件 → Motion；子目录名美化后作组名。"""
    if "idle" in motion_file.stem.lower():
        return "Idle"
    if motion_file.parent == model_dir:
        return "Motion"
    d = motion_file.parent.name.lower()
    if d in ("motions", "motion"):
        return "Motion"
    return motion_file.parent.name


def scan_model_dir(model_dir: Path, models_root: Path) -> dict:
    entry: dict = {}
    m3 = next(iter(sorted(model_dir.glob(f"*{M3_SUFFIX}"))), None)
    if m3 is None:
        return entry
    entry["name"] = m3.name[: -len(M3_SUFFIX)] or model_dir.name
    entry["path"] = "custom_models/" + m3.relative_to(models_root).as_posix()
    motions = []
    seen_motions = set()
    for mf in sorted(model_dir.rglob(f"*{MOTION_SUFFIX}")):
        group = _motion_group(mf, model_dir)
        key = (group.lower(), mf.stem.lower())
        if key in seen_motions:
            continue   # 根目录散文件与 motions/ 子目录里的同名动作视为重复
        seen_motions.add(key)
        # 文件路径相对模型目录（注入 model3.json 后由渲染器按模型 URL 解析）
        motions.append({
            "group": group,
            "file": mf.relative_to(model_dir).as_posix(),
        })
    if motions:
        entry["motions"] = motions
    expressions = []
    for ef in sorted(model_dir.rglob(f"*{EXP_SUFFIX}")):
        expressions.append({
            "name": ef.name[: -len(EXP_SUFFIX)],
            "file": ef.relative_to(model_dir).as_posix(),
        })
    if expressions:
        entry["expressions"] = expressions
    return entry


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    models_root = Path(sys.argv[1])
    out_path = Path(sys.argv[2])

    entries = []
    if models_root.is_dir():
        for model_dir in sorted(p for p in models_root.iterdir() if p.is_dir()):
            entry = scan_model_dir(model_dir, models_root)
            if entry.get("path"):
                entry = {"id": f"custom_{len(entries)}", "badge": "Private", **entry}
                entries.append(entry)
            else:
                print(f"[gen_custom_models] 跳过（无 model3.json）: {model_dir.name}", file=sys.stderr)

    lines = [
        '"use strict";',
        "// 由 .github/workflows/deploy-demo.yml 自动生成（勿手改）",
        "window.CUSTOM_MODELS = " + json.dumps(entries, ensure_ascii=False, indent=2) + ";",
        "",
    ]
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[gen_custom_models] {len(entries)} custom model(s) -> {out_path}")
    print(out_path.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
