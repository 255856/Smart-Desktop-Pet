"""pytest 配置：确保项目根目录 + 本地依赖包在 sys.path 中。"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# 将项目根目录添加到 sys.path，使 `from app.xxx import ...` 可用
sys.path.insert(0, str(ROOT))
# 本地依赖包（pydantic_settings / edge-tts / faster-whisper 等装在这里）
_local = ROOT / ".local-packages"
if _local.is_dir():
    sys.path.insert(0, str(_local))


@pytest.fixture(scope="session")
def qapp():
    """提供 QApplication 单例（Qt 测试需要）。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from app.core.qt_compat import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    yield app
    # 不退出 app，让 pytest 收尾
