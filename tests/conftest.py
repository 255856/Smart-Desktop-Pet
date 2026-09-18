"""pytest 配置：确保项目根目录在 sys.path 中。"""
import sys
from pathlib import Path

# 将项目根目录添加到 sys.path，使 `from app.xxx import ...` 可用
sys.path.insert(0, str(Path(__file__).parent.parent))
