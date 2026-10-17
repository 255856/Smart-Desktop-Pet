#!/usr/bin/env python
"""run.py: 开发态启动器。"""
from __future__ import annotations

import sys
from pathlib import Path

# 把项目根加进 sys.path，让 `python -m scripts.run` 也可以
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if __name__ == "__main__":
    from app.main import main
    raise SystemExit(main())
