"""pytest 配置：项目根目录 + 本地依赖包进 sys.path；Windows 平台守卫；Qt 单例 fixture。"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
# 将项目根目录添加到 sys.path，使 `from app.xxx import ...` 可用
sys.path.insert(0, str(ROOT))
# 本地依赖包（pydantic_settings / edge-tts / faster-whisper 等装在这里）
#
# ⚠️ 放在 sys.path **末尾**（兜底），不是最前面（影子）：
#    .local-packages 里有一批**版本比 site-packages 新**的包（attrs 26.1 等），
#    如果它排在前面，就会跟 site-packages 里同名的老版本**混搭**：
#    例如某个 pytest 插件先从 site-packages 导入了 attr(25.3)，
#    之后 import attrs(26.1) 就会炸
#        ImportError: cannot import name 'ClassProps' from 'attr._make'
#    而 chromadb 的依赖链正好 import attrs → 向量索引（Chroma）打不开。
#    放末尾之后：site-packages 里的正式版本优先，.local-packages 只补缺的。
_local = ROOT / ".local-packages"
if _local.is_dir():
    sys.path.append(str(_local))

# ⚠️ attr / attrs 的版本必须**自洽**（否则整套测试里向量索引都打不开）：
#    十几个测试模块在 import 时会 sys.path.insert(0, ".local-packages") ——
#    那之后 "import attrs" 解析到 .local-packages 的 **26.1**，
#    而 "attr._make" 可能早就从 site-packages 导入了 **25.3**，
#    于是 attrs 26.1 里那句 "from attr._make import ClassProps" 直接炸：
#        ImportError: cannot import name 'ClassProps' from 'attr._make'
#    依赖链（chromadb → jsonschema → attrs）一断，Chroma 就打不开，
#    所有要真索引的测试全挂（跟用例本身没关系，是**导入顺序**的事）。
#    办法：在这里（conftest 导入时，.local-packages 还在 sys.path 末尾）
#    **先把两个都按 site-packages 的版本导进来** —— 同一个来源 → 版本自洽；
#    之后各测试模块再怎么调 sys.path，也改不动已经进 sys.modules 的模块。
for _mod in ("attr", "attrs"):
    try:
        __import__(_mod)
    except Exception:  # noqa: BLE001 - 没装就算了，不影响别的测试
        pass

# ⚠️ Windows DLL 顺序（向量索引要用 chromadb 的原生扩展）：
#    PyQt5 自带一份**旧版** MSVC 运行时（PyQt5/Qt5/bin/msvcp140.dll 等），
#    并把那个目录加进 DLL 搜索路径 —— 之后加载的原生扩展会捡到那份旧运行时，
#    chromadb 的 Rust 绑定就是这么炸的（access violation，整进程崩）。
#    趁 Qt 还没加载（下面的 ASR fixture 会 import PyQt5），先把**系统**那份
#    MSVC 运行时预加载进来，全进程就都用系统那份 → 不再抢。
from app.brain.vector_index import preload_system_msvc_runtime  # noqa: E402

preload_system_msvc_runtime()

# ---------------------------------------------------------------------------
# token 台账路径覆盖（Stage 0.2）
#
# 生产代码路径（llm_client / langchain_agent）会把每次请求的 usage 写进
# data/token-usage.jsonl —— 那是 Stage 0.5 的**基线测量文件**，测试流量
# 写进去会把缓存命中率算废。所以整个测试会话把台账指到临时文件。
# ---------------------------------------------------------------------------
_TOKEN_LEDGER_ENV = "DESKTOP_PET_TOKEN_LEDGER"
_TOKEN_LEDGER = Path(tempfile.gettempdir()) / (
    f"desktop-pet-token-usage-test-{os.getpid()}.jsonl")
os.environ.setdefault(_TOKEN_LEDGER_ENV, str(_TOKEN_LEDGER))
try:
    _TOKEN_LEDGER.unlink()        # 每次测试会话从空台账开始
except OSError:
    pass

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


@pytest.fixture(scope="session", autouse=True)
def _mock_asr_model_load():
    """把 ASR **模型加载** mock 掉（整个测试会话），但**不关掉**预热代码路径。

    ChatWindow 构造完会延迟 1s 预热 faster-whisper。真正的模型加载
    （ctranslate2 / OpenMP 初始化）在 Windows 上会直接崩进程
    （Windows fatal exception: access violation，pytest exit 139），
    所以测试里必须挡住**模型加载**这一步。

    P0-10 修完之后，预热跑在后台线程、GUI 线程不再碰原生库；这里仍然把
    _get_model 换成秒回的假货，保证测试永远不加载真模型 —— 但预热路径本身
    （排程 / 后台线程 / 完成信号 / 幂等 / 析构保护）保持**真实**，
    由 tests/test_asr_warmup_thread.py 专门覆盖。
    """
    try:
        from app.voice import asr as asr_mod
    except Exception:  # noqa: BLE001
        yield
        return

    sentinel = object()

    def _fake_get_model(self, *args, **kwargs):  # noqa: ANN001, ARG001
        return sentinel

    original = asr_mod.SpeechRecognizer._get_model
    asr_mod.SpeechRecognizer._get_model = _fake_get_model
    yield
    asr_mod.SpeechRecognizer._get_model = original


@pytest.fixture(scope="session", autouse=True)
def _isolate_repo_data_dir(tmp_path_factory):
    """测试会话期间，**仓库 data/ 一个字节都不许被写**。

    应用里有两处「相对路径」默认值（相对进程 CWD = 仓库根）：
      · app/engine/chat_store.py —— ChatStore 默认 data/chat_history.db：
        建一个 ChatWindow 就会把**测试消息追加进用户的真实聊天历史**；
      · app/brain/_companion_log.py —— _default_log_path() → data/companion_log.txt：
        写一条决策 trace 就往用户的真实日志里追加一行。

    数据红线：测试不许碰用户数据（跟记忆库那条闸同源，
    见 tests/test_memory_lifecycle_supersede.py::test_a6_*）。
    """
    tmp = tmp_path_factory.mktemp("repo_data_isolation")

    # 1) 决策 trace 日志 → 临时文件
    from app.brain import _companion_log as cl_mod
    _orig_log_path = cl_mod._default_log_path
    log_path = tmp / "companion_log.txt"
    cl_mod._default_log_path = lambda: log_path

    # 2) 聊天历史 → 临时 db（ChatWindow 建的 ChatStore 没显式传路径）
    from app.engine import chat_store as cs_mod
    _orig_init = cs_mod.ChatStore.__init__
    chat_db = tmp / "chat_history.db"
    legacy_json = tmp / "chat_history.json"

    def _patched_init(self, data_file=None, *args, **kwargs):
        if data_file is None:
            data_file = chat_db
        kwargs.setdefault("legacy_json", legacy_json)
        _orig_init(self, data_file, *args, **kwargs)

    cs_mod.ChatStore.__init__ = _patched_init
    try:
        yield
    finally:
        cs_mod.ChatStore.__init__ = _orig_init
        cl_mod._default_log_path = _orig_log_path


@pytest.fixture(scope="session", autouse=True)
def _cleanup_test_token_ledger():
    """测试会话结束后删掉临时台账文件（仓库 data/ 一个字节都不碰）。"""
    yield
    try:
        _TOKEN_LEDGER.unlink()
    except OSError:
        pass


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
