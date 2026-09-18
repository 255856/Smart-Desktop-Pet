"""桌宠主入口 —— 导入 app.main 的完整版 App。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# 设置 PYTHONPATH，让 Python 找到 .local-packages 里的包
local_packages = ROOT / ".local-packages"
if local_packages.exists():
    sys.path.insert(0, str(local_packages))

from app.main import main

if __name__ == "__main__":
    sys.exit(main())
