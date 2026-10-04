"""pytest 配置：项目根目录 + 本地依赖包进 sys.path，并提供 Qt 单例 fixture。

另含平台守卫：本项目是 Windows 桌面应用（Live2D/Qt 窗口、锁屏、Wi-Fi/蓝牙开关、
剪贴板、ASR 唤醒词等都依赖 Windows 专有 API）。在没有这些 API 的平台上，
这些测试模块导入即失败，会让整个收集阶段崩掉（pytest exit code 2），
把真正的问题淹没。CI 主跑 Windows（见 .github/workflows/test.yml），
这里只是让非 Windows 平台也能跑完其余部分。
"""
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

IS_WINDOWS = sys.platform == "win32"

# 直接依赖 Windows 专有 API / Windows 侧资产的测试模块
_WINDOWS_ONLY_MODULES = {
    "test_asr.py",              # 语音识别依赖 sounddevice / winrt
    "test_wake_word.py",        # 唤醒词依赖 sounddevice
    "test_dangerous_tools.py",  # set_wifi / 锁屏 / 音量（PowerShell + pycaw）
    "test_new_tools.py",        # 剪贴板 / 亮度 / 电量（pywin32 + WMI）
    "test_confirm_dialog.py",   # 危险工具确认弹窗覆盖锁屏 / Wi-Fi
}

# 非 Windows 平台上直接不收集这些模块（collect_ignore 是 pytest 认识的变量名）
collect_ignore = [] if IS_WINDOWS else sorted(_WINDOWS_ONLY_MODULES)


@pytest.fixture(scope="session")
def qapp():
    """提供 QApplication 单例（Qt 测试需要）。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from app.core.qt_compat import QApplication
    app = QApplication.instance() or QApplication(sys.argv)
    yield app
    # 不退出 app，让 pytest 收尾


def pytest_report_header(config):
    if not IS_WINDOWS:
        return (f"[platform] {sys.platform}：已跳过 {len(_WINDOWS_ONLY_MODULES)} "
                f"个 Windows-only 测试模块")
