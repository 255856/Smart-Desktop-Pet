#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫描 gh-pages 分支上的私有模型目录，生成 demo 用的 custom-models.js 注册表。

用法（在 deploy-demo.yml 的 Build 步骤中）:
    python3 .github/scripts/gen_custom_models.py <models_root> <output_js>

- models_root: gh-pages checkout 里的 custom_models/ 目录
- output_js:   写入部署产物（如 _demo/custom-models.js）

每个 `models_root/<模型目录>/<名字>.model3.json` 生成一条注册项：
    { id, name(取 model3.json 文件名), badge:"Private", path:"custom_models/<rel>" }
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    models_root = Path(sys.argv[1])
    out_path = Path(sys.argv[2])

    entries = []
    if models_root.is_dir():
        for m3 in sorted(models_root.glob("*/*.model3.json")):
            rel = m3.relative_to(models_root).as_posix()
            suffix = ".model3.json"
            base = m3.name[: -len(suffix)] if m3.name.lower().endswith(suffix) else m3.stem
            name = base or m3.parent.name
            entries.append({
                "id": f"custom_{len(entries)}",
                "name": name,
                "badge": "Private",
                "path": "custom_models/" + rel,
            })

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
