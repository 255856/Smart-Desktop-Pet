"""TTS 缓存格式嗅探 + 引擎 cache_ext 单测。"""
import tempfile
from pathlib import Path

from app.voice.gptsovits_tts import GPTSoVITSTTS
from app.voice.voice import TTS, _cache_format_matches


def test_cache_ext_defaults():
    """基类默认 mp3，GPT-SoVITS 子类必须为 wav（api_v2 返回 wav）。"""
    assert TTS.cache_ext == ".mp3"
    assert GPTSoVITSTTS.cache_ext == ".wav"


def test_cache_format_matches_wav():
    """RIFF/WAVE 头 → 期望 .wav 时 True，期望 .mp3 时 False。"""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False, mode="wb") as fp:
        fp.write(b"RIFF$\x00\x00\x00WAVEfmt ")
        path = Path(fp.name)
    try:
        assert _cache_format_matches(path, ".wav") is True
        assert _cache_format_matches(path, ".mp3") is False
    finally:
        path.unlink()


def test_cache_format_matches_mp3_id3():
    """ID3 头 → 期望 .mp3 时 True。"""
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False, mode="wb") as fp:
        fp.write(b"ID3\x04\x00\x00\x00\x00\x00\x00abc")
        path = Path(fp.name)
    try:
        assert _cache_format_matches(path, ".mp3") is True
        assert _cache_format_matches(path, ".wav") is False
    finally:
        path.unlink()


def test_cache_format_matches_mp3_sync():
    """MPEG sync 字节 (0xFFFB) → 期望 .mp3 时 True。"""
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False, mode="wb") as fp:
        fp.write(b"\xff\xfb\x90\x00" + b"\x00" * 100)
        path = Path(fp.name)
    try:
        assert _cache_format_matches(path, ".mp3") is True
    finally:
        path.unlink()


def test_cache_format_matches_empty():
    """空文件 / 损坏 → 任意期望都返回 False。"""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False, mode="wb") as fp:
        path = Path(fp.name)
    try:
        assert _cache_format_matches(path, ".wav") is False
        assert _cache_format_matches(path, ".mp3") is False
    finally:
        path.unlink()


def test_tts_speak_queue_serialization():
    """speak() 入队 + 唯一 worker 顺序消费（不会创建多个 thread 抢 mixer）。"""
    import threading
    from app.voice.voice import TTS

    class FakeTTS(TTS):
        """替换 _speak_blocking 直接记录调用顺序，避免真实 pygame 播放。"""
        def __init__(self):
            super().__init__(voice="fake")
            self.played: list[str] = []
            self._lock = threading.Lock()

        def _speak_blocking(self, text, profile=None):  # type: ignore[override]
            with self._lock:
                self.played.append(text)

    tts = FakeTTS()
    # 入队 5 句
    for s in ["第一句", "第二句", "第三句", "第四句", "第五句"]:
        tts.speak(s)
    # 等 worker 处理
    tts._speak_queue.join()
    assert tts.played == ["第一句", "第二句", "第三句", "第四句", "第五句"]
    # worker 只启动一次
    assert tts._speak_worker_started is True


def test_chat_window_tts_drain_no_duplicate_queue():
    """_tts_drain_sentences 多次调用同一段不应重复入队。

    流式场景：_on_chunk 每收到一个 token 就调一次 _tts_drain_sentences(accumulated)，
    其中 accumulated 是「从开头到当前」的整段。如果每次从头扫描，已送过的整句
    会再次被 finditer 截出 + 入队。修复后用 _last_accumulated 增量切分。

    不真正构造 ChatWindow（依赖太多），改为直接调 _tts_drain_sentences 并传入
    同一 self / tts 的 mock，确保相同句子只入队一次。
    """
    from app.engine.tools._search import _format_results_md
    # 直接用 ChatWindow 类但只测它的 _tts_drain_sentences 方法（mock char_cfg + tts）
    import sys
    sys.path.insert(0, '.local-packages')
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)

    from app.core.config import LLMConfig, CharacterConfig
    from app.ui.chat_window import ChatWindow
    from app.voice.voice import TTS

    # 收集 speak + prepare 调用
    class CollectTTS(TTS):
        def __init__(self):
            super().__init__(voice="fake")
            self.spoken: list[str] = []
            self.prepareds: list[str] = []

        def speak(self, text):  # type: ignore[override]
            self.spoken.append(text)

        def prepare(self, text, timeout_s=30.0):  # type: ignore[override]
            self.prepareds.append(text)
            return True

    char_cfg = CharacterConfig(name="t", persona="p", tts_enabled=True)
    llm_cfg = LLMConfig(api_key="sk", model="m")
    cw = ChatWindow(llm_cfg, char_cfg, "assets/sprites", tts=CollectTTS())
    # 模拟流式：多次 chunk 累积同一段
    cw._tts_drain_sentences("主人你好呀。")
    cw._tts_drain_sentences("主人你好呀。今天天气不错。")
    cw._tts_drain_sentences("主人你好呀。今天天气不错！要不要出门。")
    cw._tts_drain_sentences("主人你好呀。今天天气不错！要不要出门。")
    cw._flush_tts_tail_to_prepare()
    # 等所有 prepare worker 完成（mock prepare 立即返回 True）
    import time
    deadline = time.monotonic() + 5
    while cw._sentence_workers and time.monotonic() < deadline:
        time.sleep(0.05)

    # 验证：每句只启动一次 prepare（不重复）
    prepared = cw.tts.prepareds
    # 关键是：同一句不应被启动 prepare 多次
    assert prepared.count("主人你好呀。") == 1, f"重复 prepare: {prepared}"
    # 第三次 chunk 时 "今天天气不错。" 已被 sanitize 替换为 "今天天气不错！"（累积文本变化）
    # —— 这里只验证「重复 prepare」被修：同一句话不应该出现 2 次以上
    assert len(prepared) <= 4, f"总 prepare 过多: {prepared}"


def test_prepare_worker_signal_arity_in_source():
    """AST 解析 chat_window.__init__.py, 确认 _PrepareWorker.done 信号 arity 是 2 (int + str)。

    不构造 ChatWindow(不触发 ASR / TTS 实际链路),纯静态检查,
    避免受 faster-whisper / Qt worker thread 时序影响。
    """
    import ast
    from pathlib import Path

    src = Path("app/ui/chat_window/__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_launch_sentence_prepare":
            target = node
            break
    assert target is not None, "找不到 _launch_sentence_prepare 定义"

    prepare_worker = None
    for sub in ast.iter_child_nodes(target):
        if isinstance(sub, ast.ClassDef) and sub.name == "_PrepareWorker":
            prepare_worker = sub
    assert prepare_worker is not None, "找不到嵌套 _PrepareWorker 类"

    done_signal = None
    for stmt in prepare_worker.body:
        if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and stmt.targets[0].id == "done"
                and isinstance(stmt.value, ast.Call)):
            done_signal = stmt.value
            break
    assert done_signal is not None, "_PrepareWorker 必须有 done = Signal(...)"

    args = done_signal.args
    assert len(args) == 2, (
        f"_PrepareWorker.done 信号 arity 必须 == 2 (seq + sentence);"
        f"实际 Signal 实参数={len(args)} ({ast.unparse(done_signal)})"
    )
    # 实参类型应该是 int + str
    assert isinstance(args[0], ast.Name) and args[0].id == "int"
    assert isinstance(args[1], ast.Name) and args[1].id == "str"


def test_prepare_worker_emit_passes_correct_arity():
    """_PrepareWorker.run() 必须 emit 同样 2 个参数(seq_id, txt), 跟 Signal(int, str) 严格匹配。"""
    import ast
    from pathlib import Path

    src = Path("app/ui/chat_window/__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_launch_sentence_prepare":
            target = node
            break
    prepare_worker = None
    for sub in ast.iter_child_nodes(target):
        if isinstance(sub, ast.ClassDef) and sub.name == "_PrepareWorker":
            prepare_worker = sub
    worker_run = None
    for sub in prepare_worker.body:
        if isinstance(sub, ast.FunctionDef) and sub.name == "run":
            worker_run = sub
    assert worker_run is not None

    emit_calls = []
    for sub in ast.walk(worker_run):
        if (isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "emit"):
            # 不管接收者是 `self` / `self.done` / `_inner`
            emit_calls.append(sub)
    assert emit_calls, "_PrepareWorker.run 应该至少调一次 *.emit(...)"
    emit = emit_calls[-1]
    assert len(emit.args) == 2, (
        f"emit() 实参必须是 2 (seq_id, txt);实际 {len(emit.args)}"
    )


def test_prepare_worker_signal_signature_matches_slot():
    """已迁到 test_prepare_worker_signal_arity_in_source (AST 静态守卫)。

    不再构造 QApplication / QThread(测试环境跑会触发 faster_whisper 加载链
    导致 Windows access violation crash)。
    """


def test_chat_window_drain_speak_queue_preserves_order():
    """并发 prepare 完成时, _drain_speak_queue 必须按 seq 升序 speak, 不能颠倒。"""
    import sys, threading
    sys.path.insert(0, '.local-packages')
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(sys.argv)

    from app.core.config import LLMConfig, CharacterConfig
    from app.ui.chat_window import ChatWindow
    from app.voice.voice import TTS

    class OrderTTS(TTS):
        def __init__(self):
            super().__init__(voice="fake")
            self.spoken: list[str] = []

        def speak(self, text):  # type: ignore[override]
            self.spoken.append(text)

        def prepare(self, text, timeout_s=30.0):  # type: ignore[override]
            return True

    char_cfg = CharacterConfig(name="t", persona="p", tts_enabled=True)
    llm_cfg = LLMConfig(api_key="sk", model="m")
    cw = ChatWindow(llm_cfg, char_cfg, "assets/sprites", tts=OrderTTS())

    # 直接灌入 _pending_speak,故意把"完成顺序"打乱成 2/0/1
    cw._pending_speak = {2: "C", 0: "A", 1: "B"}

    # 但 _tts_seq_drain 起始是 0,期望按 0 → A, 1 → B, 2 → C 顺序 speak
    cw._tts_seq_drain = 0
    cw._drain_speak_queue()

    assert cw.tts.spoken == ["A", "B", "C"], (
        f"drain 必须按 seq 升序;实际 {cw.tts.spoken}"
    )
    assert cw._tts_seq_drain == 3