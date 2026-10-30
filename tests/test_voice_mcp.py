"""语音 MCP server + 「默认不朗读」开关的回归测试。

分三层：
  1. 纯逻辑（**不碰本机模型**）：工具契约、措辞规则、失败语义、开关。
  2. 起真子进程（只要 python）：MCP 握手 + tools/list + 一次 voice_status（本地工具名 mcp_voice_status）。
  3. 真模型（本机有才跑，跑不到就 skip）：mcp_voice_sing 出 wav、mcp_voice_speak 出音频。
     —— 测试套件**不依赖模型**：别的机器上第 3 层直接跳过（A8）。

另外钉死两条红线：
  · **不下载**：本机缺的音色一律报「音色不在本地」，绝不联网拉权重；
  · **不装**：失败就返回「失败：…」，绝不说「已发送」。
"""
from __future__ import annotations

import os
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.mcp import voice_server as vs  # noqa: E402


# ---------------------------------------------------------------------------
# 本机模型盘点（跑不到就 skip 第 3 层）
# ---------------------------------------------------------------------------

def _local_cfg() -> dict:
    try:
        return vs._cfg()
    except Exception:  # noqa: BLE001
        return {}


def _rvc_weight() -> Path:
    return vs._rvc_weight_path(_local_cfg())


def _rvc_python() -> Path:
    return vs._abs((_local_cfg().get("rvc") or {}).get("python") or "")


def _rvc_script() -> Path:
    return vs._abs((_local_cfg().get("rvc") or {}).get("sing_script") or "")


def _tts_url() -> str:
    return str(_local_cfg().get("tts_url") or "http://127.0.0.1:9880")


def _tts_alive() -> bool:
    try:
        ok, _detail = vs._probe_tts(_tts_url(), timeout=2.0)
    except Exception:  # noqa: BLE001
        return False
    return ok


HAS_RVC = (_rvc_python().is_file() and _rvc_script().is_file()
           and _rvc_weight().is_file())
HAS_TTS = _tts_alive()

needs_rvc = pytest.mark.skipif(
    not HAS_RVC, reason="本机没有 RVC 资产（解释器/脚本/音色权重），跳过")
needs_tts = pytest.mark.skipif(
    not HAS_TTS, reason="本机没跑 GPT-SoVITS api_v2，跳过")


def _text(result: dict) -> str:
    """MCP result → 文本。"""
    return "\n".join(str(c.get("text", "")) for c in result.get("content", []))


# ---------------------------------------------------------------------------
# 1. 工具契约 + 措辞规则 + 失败语义
# ---------------------------------------------------------------------------

class TestVoiceToolContract:
    """三个工具的形状、描述里的措辞规则、失败语义。"""

    def test_three_tools_declared(self):
        names = [t["name"] for t in vs.TOOLS]
        assert names == ["speak", "sing", "status"]

    def test_speak_schema_requires_text(self):
        t = [x for x in vs.TOOLS if x["name"] == "speak"][0]
        assert t["inputSchema"]["required"] == ["text"]
        assert "emotion" in t["inputSchema"]["properties"]

    def test_sing_schema_requires_lyrics(self):
        t = [x for x in vs.TOOLS if x["name"] == "sing"][0]
        assert t["inputSchema"]["required"] == ["lyrics"]
        props = t["inputSchema"]["properties"]
        assert {"speed", "seed", "base"} <= set(props)

    def test_sing_description_forbids_bracket_labels(self):
        """语音条前面不许加方括号标签（主人嫌那玩意儿出戏）。"""
        t = [x for x in vs.TOOLS if x["name"] == "sing"][0]
        assert "方括号标签" in t["description"]
        assert "普通文字消息" in t["description"]

    def test_sing_description_forbids_claiming_original_song(self):
        """唱的是现编的调，不许自称原创歌曲。"""
        t = [x for x in vs.TOOLS if x["name"] == "sing"][0]
        assert "现编的调" in t["description"]
        assert "原创" in t["description"]

    def test_sing_description_forbids_faking_success(self):
        t = [x for x in vs.TOOLS if x["name"] == "sing"][0]
        assert "不要假装" in t["description"]

    def test_descriptions_warn_about_frequency(self):
        """语音是打断，不是每轮都要用。"""
        for name in ("speak", "sing"):
            t = [x for x in vs.TOOLS if x["name"] == name][0]
            assert "每轮" in t["description"]


class TestHonestFailure:
    """失败必须说失败：不许「已发送」。"""

    def test_unknown_tool_fails(self):
        res = vs.handle_call("nope", {})
        assert _text(res).startswith("失败：")
        assert res.get("isError") is True

    def test_empty_lyrics_fails(self):
        res = vs.handle_sing({"lyrics": "   "})
        assert _text(res).startswith("失败：")

    def test_empty_text_fails(self):
        res = vs.handle_speak({"text": ""})
        assert _text(res).startswith("失败：")

    def test_missing_voice_is_reported_not_downloaded(self, tmp_path, monkeypatch):
        """本机没有音色 → 说「音色不在本地」，**不下载**。"""
        monkeypatch.setattr(vs, "_cfg", lambda: {
            "output_dir": str(tmp_path),
            "voice_dir": str(tmp_path / "没有这个音色"),
            "tts_url": "http://127.0.0.1:1",
            "rvc": {
                "python": str(tmp_path / "no_python.exe"),
                "root": str(tmp_path / "no_rvc"),
                "sing_script": str(tmp_path / "no_sing.py"),
                "model": "not_local.pth",
            },
        })
        res = vs.handle_sing({"lyrics": "测试"})
        text = _text(res)
        assert text.startswith("失败：")
        assert "不在本机" in text
        assert "不会" in text and ("下载" in text or "安装" in text)
        # 绝不能出现「成功」类措辞
        for bad in ("唱完", "已经放给主人听了", "已发送"):
            assert bad not in text

    def test_missing_voice_for_speak(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vs, "_cfg", lambda: {
            "output_dir": str(tmp_path),
            "voice_dir": str(tmp_path / "没有这个音色"),
            "tts_url": "http://127.0.0.1:1",
        })
        res = vs.handle_speak({"text": "你好"})
        text = _text(res)
        assert text.startswith("失败：")
        assert "音色不在本地" in text
        assert "已发送" not in text

    def test_tts_down_says_so(self, tmp_path, monkeypatch):
        """TTS 服务不在 → 如实说没响应，不假装念了。"""
        voice_dir = tmp_path / "v"
        voice_dir.mkdir()
        (voice_dir / "x_ref.wav").write_bytes(b"RIFF" + b"\0" * 200)
        (voice_dir / "x_ref.lab").write_text("参考文本", encoding="utf-8")
        monkeypatch.setattr(vs, "_cfg", lambda: {
            "output_dir": str(tmp_path),
            "voice_dir": str(voice_dir),
            "tts_url": "http://127.0.0.1:9",   # 不会有人监听
        })
        res = vs.handle_speak({"text": "你好"})
        text = _text(res)
        assert text.startswith("失败：")
        assert "合成失败" in text
        assert "已发送" not in text and "念完" not in text


class TestStatusHonesty:
    """mcp_voice_status 必须说清「本机有什么 / 没有什么」。"""

    def test_status_lists_services_and_local_voices(self):
        res = vs.handle_status()
        text = _text(res)
        assert "本机语音服务状态" in text
        assert "TTS 合成服务" in text
        assert "RVC" in text
        assert "不会下载" in text

    def test_status_says_voice_not_local_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vs, "_cfg", lambda: {
            "output_dir": str(tmp_path),
            "voice_dir": str(tmp_path / "空目录"),
            "tts_url": "http://127.0.0.1:9",
            "rvc": {
                "python": str(tmp_path / "x.exe"),
                "root": str(tmp_path / "no_rvc"),
                "sing_script": str(tmp_path / "x.py"),
                "model": "not_local.pth",
            },
        })
        text = _text(vs.handle_status())
        assert "音色不在本地" in text
        assert "不会自动下载" in text


# ---------------------------------------------------------------------------
# 2. 真子进程：MCP 握手 + tools/list（只要 python，不碰模型）
# ---------------------------------------------------------------------------

class TestVoiceMcpServerProcess:
    """起真的 voice_server 子进程，走完整 JSON-RPC。"""

    def test_tools_list_over_stdio(self):
        import asyncio
        from app.mcp.protocol import MCPClientRegistry, MCPServerConfig

        async def drive():
            reg = MCPClientRegistry()
            reg.register(MCPServerConfig(
                name="voice",
                command=sys.executable,
                args=["-m", "app.mcp.voice_server"],
                cwd=str(ROOT),
                description="voice server（测试）",
                call_timeout=240.0,
            ))
            started = await reg.start_all()
            try:
                names = [t["name"] for t in started.get("voice", [])]
                status = await reg.call("voice", "status", {})
                return names, status
            finally:
                await reg.stop_all()

        names, status = asyncio.run(drive())
        assert set(names) == {"speak", "sing", "status"}
        assert "本机语音服务状态" in status

    def test_registry_exposes_per_server_timeout(self):
        """跑推理的 server 要能单独调长超时（唱歌 ~30s，默认 30s 会先超时）。"""
        from app.mcp.protocol import MCPClientRegistry, MCPServerConfig
        reg = MCPClientRegistry()
        reg.register(MCPServerConfig(name="voice", command="python",
                                     call_timeout=240.0))
        assert reg.call_timeout_of("voice") == 240.0
        reg.register(MCPServerConfig(name="fs", command="python"))
        assert reg.call_timeout_of("fs") == 30.0


class TestBrainRegistration:
    """桌宠侧：voice server 要被注册、白名单要放行、手册要提一嘴。"""

    def test_voice_mcp_enabled_by_default(self):
        from app.brain.brain_controller import BrainController
        assert BrainController._voice_mcp_enabled() is True

    def test_allowlist_has_voice_prefix(self):
        """mcp_voice_ 前缀在执行期白名单里（否则工具会被闸拦住）。"""
        from app.core import companion_config as cc
        raw = cc._read_yaml(cc._default_yaml_path()) or {}
        prefixes = raw["tool_allowlist"]["prefixes"]
        assert "mcp_voice_" in prefixes

    def test_handbook_mentions_voice_when_registered(self):
        from app.brain.prompt_layer import build_tool_handbook
        from app.engine.tools import ToolRegistry
        reg = ToolRegistry()
        reg.register(vs and __import__("app.engine.tools", fromlist=["Tool"]).Tool(
            name="mcp_voice_sing",
            description="唱",
            parameters={"type": "object", "properties": {}},
            fn=lambda: "",
        ))
        handbook = build_tool_handbook(registry=reg, level=10)
        assert "【语音】" in handbook
        assert "mcp_voice_sing" in handbook
        assert "方括号标签" in handbook
        assert "原创歌曲" in handbook

    def test_handbook_has_no_voice_section_without_tools(self):
        from app.brain.prompt_layer import build_tool_handbook
        from app.engine.tools import ToolRegistry
        handbook = build_tool_handbook(registry=ToolRegistry(), level=10)
        assert "【语音】" not in handbook


# ---------------------------------------------------------------------------
# 3. 自动朗读开关（默认关；能打开）
# ---------------------------------------------------------------------------

class TestAutoReadSwitch:
    """A4：默认不朗读；开关能把它打开。"""

    def test_default_table_has_auto_read_off(self):
        from app.core import companion_config as cc
        raw = cc._read_yaml(cc._default_yaml_path()) or {}
        assert raw["voice"]["auto_read"] is False

    def test_helper_reads_switch(self, monkeypatch):
        from app.ui.chat_window import ChatWindow
        monkeypatch.setattr(
            "app.voice.voice.load_voice_settings",
            lambda **kw: {"auto_read": True})
        assert ChatWindow._voice_auto_read_enabled(None) is True
        monkeypatch.setattr(
            "app.voice.voice.load_voice_settings",
            lambda **kw: {"auto_read": False})
        assert ChatWindow._voice_auto_read_enabled(None) is False

    def test_helper_defaults_to_off_on_error(self, monkeypatch):
        from app.ui.chat_window import ChatWindow

        def _boom(**kw):
            raise RuntimeError("读不到配置")
        monkeypatch.setattr("app.voice.voice.load_voice_settings", _boom)
        assert ChatWindow._voice_auto_read_enabled(None) is False

    @staticmethod
    def _fake_window(auto_read: bool):
        from app.ui.chat_window import ChatWindow
        w = types.SimpleNamespace()
        w._TTS_SENT_END = ChatWindow._TTS_SENT_END
        w.char_cfg = types.SimpleNamespace(tts_enabled=True)
        w.tts = object()
        w._auto_read = auto_read
        w.prepared = []
        w._tts_sent_tail = 0
        w._tts_tail = ""
        w._voice_auto_read_enabled = lambda: w._auto_read
        w._launch_sentence_prepare = lambda s: w.prepared.append(s)
        return w

    def test_no_prepare_when_auto_read_off(self):
        """默认（关）→ 一句都不 prepare、不朗读。"""
        from app.ui.chat_window import ChatWindow
        w = self._fake_window(False)
        ChatWindow._tts_drain_sentences(w, "你好。今天天气好。")
        assert w.prepared == []

    def test_prepare_when_auto_read_on(self):
        """开关打开 → 回到原来的逐句朗读。"""
        from app.ui.chat_window import ChatWindow
        w = self._fake_window(True)
        ChatWindow._tts_drain_sentences(w, "你好。今天天气好。")
        assert w.prepared == ["你好。", "今天天气好。"]

    def test_tail_not_prepared_when_off(self):
        from app.ui.chat_window import ChatWindow
        w = self._fake_window(False)
        w._tts_tail = "尾巴没标点"
        ChatWindow._flush_tts_tail_to_prepare(w)
        assert w.prepared == []

    def test_tail_prepared_when_on(self):
        from app.ui.chat_window import ChatWindow
        w = self._fake_window(True)
        w._tts_tail = "尾巴没标点"
        ChatWindow._flush_tts_tail_to_prepare(w)
        assert w.prepared == ["尾巴没标点"]


# ---------------------------------------------------------------------------
# 4. 真模型（本机有才跑）
# ---------------------------------------------------------------------------

@needs_rvc
class TestRealRvcSing:
    """A2：mcp_voice_sing 真的用本机 RVC 出一段 wav。"""

    def test_sing_produces_wav_and_does_not_download(self, tmp_path, monkeypatch):
        cfg = dict(_local_cfg())
        cfg["output_dir"] = str(tmp_path)
        monkeypatch.setattr(vs, "_cfg", lambda: cfg)
        played = []
        monkeypatch.setattr(vs, "_play", lambda p: played.append(str(p)) or True)

        weight = _rvc_weight()
        before = (weight.stat().st_mtime_ns, weight.stat().st_size)

        res = vs.handle_sing({"lyrics": "今天天气真好呀", "seed": 11})
        text = _text(res)
        assert res.get("isError") is not True, text
        assert text.startswith("唱完")

        wavs = sorted(tmp_path.glob("sing_*.wav"))
        assert wavs, "没有产出 wav：" + text
        wav = wavs[0]
        size, dur = vs._wav_info(wav)
        assert size > 1000, "wav 太小：%d 字节" % size
        assert dur > 0.3, "wav 时长异常：%.2f 秒" % dur
        assert played, "应该把音频放给主人听"

        # 红线：权重没被动过（没下载、没替换）
        after = (weight.stat().st_mtime_ns, weight.stat().st_size)
        assert before == after, "本机权重被改了：%s → %s" % (before, after)

    def test_sing_uses_configured_weight(self, tmp_path, monkeypatch):
        """脚本必须用配置里那个本机权重（而不是随便一个）。"""
        cfg = dict(_local_cfg())
        cfg["output_dir"] = str(tmp_path)
        monkeypatch.setattr(vs, "_cfg", lambda: cfg)
        seen = {}

        class _FakeProc:
            returncode = 0
            stdout = ""
            stderr = ""

        def _fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            out = Path(cmd[cmd.index("--out") + 1])
            out.write_bytes(b"RIFF" + b"\0" * 400)
            return _FakeProc()

        monkeypatch.setattr(vs.subprocess, "run", _fake_run)
        res = vs.handle_sing({"lyrics": "测试", "seed": 3})
        assert res.get("isError") is not True
        assert "--model" in seen["cmd"]
        model = seen["cmd"][seen["cmd"].index("--model") + 1]
        assert model == cfg["rvc"]["model"]
        assert model.endswith(".pth")


@needs_tts
class TestRealGptSovitsSpeak:
    """A3：mcp_voice_speak 真的通过本机 GPT-SoVITS 出音频。"""

    def test_speak_produces_audio(self, tmp_path, monkeypatch):
        cfg = dict(_local_cfg())
        cfg["output_dir"] = str(tmp_path)
        monkeypatch.setattr(vs, "_cfg", lambda: cfg)
        played = []
        monkeypatch.setattr(vs, "_play", lambda p: played.append(str(p)) or True)

        res = vs.handle_speak({"text": "主人，今天也要好好吃饭哦。"})
        text = _text(res)
        assert res.get("isError") is not True, text
        assert text.startswith("念完")

        wavs = sorted(tmp_path.glob("speak_*.wav"))
        assert wavs, "没有产出 wav：" + text
        size, dur = vs._wav_info(wavs[0])
        assert size > 1000
        assert dur > 0.3
        assert played


class TestPlaybackHook:
    """播放入口：文件不存在就老实说不行。"""

    def test_play_missing_file_returns_false(self, tmp_path):
        from app.voice.voice import play_audio_file
        assert play_audio_file(tmp_path / "没有这个.wav") is False

    def test_play_real_wav_starts_audio(self, tmp_path, monkeypatch):
        """起一个后台播放器进程就算成功（不校验耳朵）。"""
        import wave
        from app.voice import voice as vmod
        wav = tmp_path / "t.wav"
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(32000)
            w.writeframes(b"\0" * 3200)
        started = []

        class _FakeProc:
            def wait(self, timeout=None):
                return 0
            def kill(self):
                pass

        def _fake_popen(cmd, **kwargs):
            started.append(cmd)
            return _FakeProc()

        monkeypatch.setattr(vmod.subprocess, "Popen", _fake_popen)
        monkeypatch.setattr(vmod.shutil, "which", lambda n: "C:/fake/ffplay.exe")
        assert vmod.play_audio_file(wav) is True
        assert started and "ffplay" in started[0][0]


class TestVoiceSettingsLoader:
    """voice 段读取：默认表 + 用户覆盖表 + mtime 缓存。"""

    def test_load_voice_settings_has_section(self):
        from app.voice.voice import load_voice_settings
        cfg = load_voice_settings()
        assert "auto_read" in cfg
        assert "rvc" in cfg

    def test_load_voice_settings_caches_by_mtime(self, monkeypatch):
        from app.voice import voice as vmod
        calls = []
        real = vmod._voice_settings_merged

        def _spy():
            calls.append(1)
            return real()
        monkeypatch.setattr(vmod, "_voice_settings_merged", _spy)
        vmod.load_voice_settings(force=True)
        n = len(calls)
        vmod.load_voice_settings()
        vmod.load_voice_settings()
        assert len(calls) == n, "mtime 没变时不该重复读 yaml"

    def test_settings_paths_point_to_local_files(self):
        """配置里写的都是**本机已有**的东西（不是要下载的地址）。"""
        cfg = _local_cfg()
        rvc = cfg.get("rvc") or {}
        for key in ("python", "sing_script"):
            p = vs._abs(rvc.get(key) or "")
            assert p.is_file(), "%s 不在本机：%s" % (key, p)
        assert not str(rvc.get("sing_script", "")).startswith("http")
        assert not str(rvc.get("python", "")).startswith("http")
