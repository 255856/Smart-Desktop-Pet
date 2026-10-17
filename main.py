"""桌宠主入口：启动 App。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# 加载 .local-packages
local_packages = ROOT / ".local-packages"
if local_packages.exists():
    sys.path.insert(0, str(local_packages))

from app.main import main

if __name__ == "__main__":
    sys.exit(main())
