"""pytest 配置：项目根目录 + 本地依赖包进 sys.path；Windows 平台守卫；Qt 单例 fixture。"""
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
    """默认 deselect 所有 slow tests，除非 --run-slow 被显式打开。"""
    if config.getoption("--run-slow"):
        return
    skip_slow = pytest.mark.skip(reason="slow（默认跳过，加 --run-slow 才跑）")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip_slow)


@pytest.fixture(autouse=True)
def _isolate_tool_choice_denylist(tmp_path):
    """把 tool_choice 黑名单（落盘路径 + 内存态）整体隔离到临时目录，逐用例重置。"""
    from app.brain import llm_client as lc

    lc._TOOL_CHOICE_CACHE = tmp_path / "denylist.json"
    lc._TOOL_CHOICE_IGNORED.clear()
    lc._TOOL_CHOICE_IGNORED.update(lc._TOOL_CHOICE_SEED)
    yield
    lc._TOOL_CHOICE_IGNORED.clear()
    lc._TOOL_CHOICE_IGNORED.update(lc._TOOL_CHOICE_SEED)


@pytest.fixture(scope="session")
def qapp():
    """提供 QApplication 单例（Qt 测试需要）。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from app.core.qt_compat import QApplication
    # 只传程序名，不把 pytest 自己的 argv 喂给 Qt，避免 --cov-report 等参数被 Qt 误解析
    app = QApplication.instance() or QApplication(sys.argv[:1])
    yield app
    # 不退出 app，让 pytest 收尾


def pytest_report_header(config):
    if not IS_WINDOWS:
        return (f"[platform] {sys.platform}：已跳过 {len(_WINDOWS_ONLY_MODULES)} "
                f"个 Windows-only 测试模块")
