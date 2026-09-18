"""pytest 配置：确保项目根目录 + 本地依赖包在 sys.path 中。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 将项目根目录添加到 sys.path，使 `from app.xxx import ...` 可用
sys.path.insert(0, str(ROOT))
# 本地依赖包（pydantic_settings / edge-tts / faster-whisper 等装在这里）
_local = ROOT / ".local-packages"
if _local.is_dir():
    sys.path.insert(0, str(_local))
