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


def pytest_configure(config):
    """注册自定义 marker，避免 pytest 输出 "PytestUnknownMarkWarning"。"""
    config.addinivalue_line(
        "markers",
        "slow: 性能 / 多线程边界测试，受 coverage/sys.settrace 计时放大，"
        "CI 默认跳、nightly / 本地手动 `--run-slow` 才跑。",
    )


def pytest_addoption(parser):
    """注册 --run-slow：默认 False，CI 快路径默认跳过 slow tests。"""
    parser.addoption(
        "--run-slow",
        action="store_true",
        default=False,
        help="也跑 @pytest.mark.slow 用例（默认跳过，受 coverage 计时放大）。",
    )


def pytest_collection_modifyitems(config, items):
    """默认 deselect 所有 slow tests，除非 --run-slow 被显式打开。

    原因：覆盖率插桩（pytest-cov 的 sys.settrace）会显著放大耗时 + 干扰
    `threading.Event.wait` 的睡眠精度，导致下面两个用例在带 --cov 的
    CI 里假阳性：
        - test_xiangqi.py::TestAI::test_ai_is_reasonably_fast
          （AI 走完 3.02s，撞破 2.0s 阈值）
        - test_audit_fixes_2026_10.py::test_cancel_countdown_actually_stops_thread
          （settrace 干扰后台线程的 Event 唤醒，本地单独跑全过）
    """
    if config.getoption("--run-slow"):
        return
    skip_slow = pytest.mark.skip(reason="slow（默认跳过，加 --run-slow 才跑）")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip_slow)


@pytest.fixture(scope="session")
def qapp():
    """提供 QApplication 单例（Qt 测试需要）。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from app.core.qt_compat import QApplication
    # 只传程序名，不把 pytest 自己的 argv 喂给 Qt：Qt 会解析命令行参数，
    # --cov-report / --tb 之类它不认识的参数在不同 PyQt5/Python 组合下
    # 行为不一致（轻则打 usage，重则直接 abort），会让用到本 fixture 的用例
    # 在 setup 阶段整片 error。应用自身 main.py 传 sys.argv 是对的，
    # 那里 argv 本来就是桌宠自己的参数。
    app = QApplication.instance() or QApplication(sys.argv[:1])
    yield app
    # 不退出 app，让 pytest 收尾


def pytest_report_header(config):
    if not IS_WINDOWS:
        return (f"[platform] {sys.platform}：已跳过 {len(_WINDOWS_ONLY_MODULES)} "
                f"个 Windows-only 测试模块")
