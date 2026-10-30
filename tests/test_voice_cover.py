r"""「翻唱某首歌」的回归测试（mcp_voice_sing 的 song 模式）。

分三层：
  1. 纯逻辑（**不碰网络、不碰模型**）：工具契约、措辞规则、找不到歌时的诚实语义、
     失败时是哪一步失败、绝不下载模型。
  2. 打桩的整条链路（不碰网络、不碰模型）：把「找歌 / 下载 / 音转音 / 混伴奏」
     全换成假的，看结果文本说的是**翻唱**而不是原唱。
  3. 真跑（本机有资产、且显式开环境变量才跑）：真的下载 + 分离 + 音转音，
     产出 wav；顺便证明**音色权重和分离模型没被动过**（没下载）。

钉死的红线：
  · **不下载**：本机缺的模型一律报「不在本地」，绝不联网拉权重；
  · **不装**：找不到歌就说找不到，绝不拿自己编的词冒充那首歌；
  · **诚实**：哪一步失败说哪一步，产物在哪说清楚。

参考实现：E:\study\qq-bridge-main\qq-bridge-main\src\mcp-local-media.js
（local_fetch_audio + local_voice_convert）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.mcp import voice_server as vs  # noqa: E402


# ---------------------------------------------------------------------------
# 本机资产盘点（跑不到就 skip 第 3 层）
# ---------------------------------------------------------------------------

def _real_cfg() -> dict:
    """本机真实配置（不走 vs._cfg —— 测试里那条会被 monkeypatch 掉）。

    三条路依次兜底：voice 设置读取器 → voice_server 的读法 → 直接读默认 yaml。
    读不到就返回 {}（用例会明确失败，不会「假装通过」）。
    """
    try:
        from app.voice.voice import load_voice_settings
        cfg = load_voice_settings() or {}
        if cfg:
            return cfg
    except Exception:  # noqa: BLE001
        pass
    try:
        cfg = vs._cfg() or {}
        if cfg:
            return cfg
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.core import companion_config as cc
        raw = cc._read_yaml(cc._default_yaml_path()) or {}
        voice = raw.get("voice") or {}
        if isinstance(voice, dict):
            return voice
    except Exception:  # noqa: BLE001
        pass
    return {}


#: 模块加载时抓一份真实配置（后面所有测试都基于它改，避免递归调 _cfg）。
_BASE_CFG = json.loads(json.dumps(_real_cfg()))


def _local_cfg() -> dict:
    return json.loads(json.dumps(_BASE_CFG))


def _rvc_cfg() -> dict:
    return (vs._rvc_cfg(_local_cfg()) or {})


def _sep_model():
    path, _root = vs._separate_model(_local_cfg())
    return path


HAS_COVER_ASSETS = (
    vs._abs(_rvc_cfg().get("python") or "").is_file()
    and vs._abs(_rvc_cfg().get("root") or "").is_dir()
    and vs._rvc_weight_path(_local_cfg()).is_file()
    and vs._fetch_script_path(_local_cfg()).is_file()
    and _sep_model() is not None
)

#: 真跑要联网 + 跑模型 —— 默认**不跑**，显式开环境变量才跑（A6：套件不依赖网络）。
E2E = os.environ.get("DESKTOP_PET_COVER_E2E") == "1"
needs_cover = pytest.mark.skipif(
    not (HAS_COVER_ASSETS and E2E),
    reason="真翻唱要本机 RVC 资产 + 联网；设 DESKTOP_PET_COVER_E2E=1 才跑")


def _text(result: dict) -> str:
    """MCP result → 文本。"""
    return "\n".join(str(c.get("text", "")) for c in result.get("content", []))


def _cfg_with_out(tmp_path, **over) -> dict:
    """本机配置 + 把产物目录指到 tmp_path（测试不污染 data/）。"""
    cfg = _local_cfg()
    cfg["output_dir"] = str(tmp_path)
    cfg["rvc"] = dict(cfg.get("rvc") or {})
    # 语音库也跟着搬进 tmp_path —— 不然测试会往真的 data/voice_out 里写
    cfg["rvc"]["voice_lib"] = str(tmp_path / "voice_lib")
    cfg.update(over)
    return cfg


# ---------------------------------------------------------------------------
# 1. 工具契约 + 措辞规则
# ---------------------------------------------------------------------------

class TestCoverToolContract:
    """mcp_voice_sing 的翻唱参数和诚实规则要写清楚。"""

    def test_sing_schema_has_cover_params(self):
        t = [x for x in vs.TOOLS if x["name"] == "sing"][0]
        props = t["inputSchema"]["properties"]
        assert {"song", "artist", "url", "start", "end", "mix", "pitch"} <= set(props)
        assert "lyrics" in props, "按旋律唱的老用法不能丢"

    def test_lyrics_is_not_the_only_required_field(self):
        """song 和 lyrics 二选一 —— 不能逼模型一定要编词。"""
        t = [x for x in vs.TOOLS if x["name"] == "sing"][0]
        required = t["inputSchema"].get("required") or []
        assert "lyrics" not in required

    def test_description_says_when_to_use_cover(self):
        """主人说「唱《XXX》」时要走这条。"""
        t = [x for x in vs.TOOLS if x["name"] == "sing"][0]
        assert "唱《" in t["description"]
        assert "song" in t["description"]

    def test_description_states_honesty_rules(self):
        t = [x for x in vs.TOOLS if x["name"] == "sing"][0]
        desc = t["description"]
        assert "找不到" in desc, "必须写明找不到歌怎么办"
        assert "绝不许" in desc, "必须写明不许编词冒充那首歌"
        assert "翻唱" in desc and "不是原唱" in desc, "必须写明这是翻唱不是原唱"
        assert "哪一步" in desc, "必须写明失败要说哪一步"
        assert "不要假装" in desc, "必须写明不许假装唱过了"

    def test_description_keeps_lyrics_mode_rules(self):
        """按旋律唱的老规矩还在（现编的调 / 不自称原创）。"""
        t = [x for x in vs.TOOLS if x["name"] == "sing"][0]
        assert "现编的调" in t["description"]
        assert "原创" in t["description"]
        assert "方括号标签" in t["description"]
        assert "普通文字消息" in t["description"]


class TestCoverHonestFailure:
    """找不到歌 / 缺资产 —— 都要如实说，绝不装成功。"""

    def test_no_song_and_no_lyrics_fails(self):
        res = vs.handle_sing({})
        text = _text(res)
        assert text.startswith("失败：")
        assert "song" in text and "lyrics" in text

    def test_song_not_found_is_honest(self, tmp_path, monkeypatch):
        """找不到那首歌 → 说找不到，**不产出任何音频**。"""
        monkeypatch.setattr(vs, "_cfg", lambda: _cfg_with_out(tmp_path))
        monkeypatch.setattr(vs, "_find_audio_url",
                            lambda song, artist="": (None, "", "搜索后端都不可用"))
        res = vs.handle_cover({"song": "一首根本不存在的歌"})
        text = _text(res)
        assert res.get("isError") is True
        assert text.startswith("失败：")
        assert "没找到" in text
        assert "不会" in text and "编的词" in text
        # 绝不能出现「唱完了」这种话
        for bad in ("翻唱完了", "唱完了", "已经放给主人听了"):
            assert bad not in text
        assert not list(tmp_path.glob("*.wav")), "找不到歌却产出了音频"

    def test_missing_weight_says_not_local(self, tmp_path, monkeypatch):
        """本机没有音色权重 → 如实说，**不下载**。"""
        cfg = _cfg_with_out(tmp_path)
        cfg["rvc"] = dict(cfg.get("rvc") or {})
        cfg["rvc"]["model"] = "not_local.pth"
        monkeypatch.setattr(vs, "_cfg", lambda: cfg)
        res = vs.handle_cover({"song": "任意一首歌"})
        text = _text(res)
        assert text.startswith("失败：")
        assert "音色不在本地" in text
        assert "下载" in text

    def test_missing_fetch_script_says_not_local(self, tmp_path, monkeypatch):
        cfg = _cfg_with_out(tmp_path)
        cfg["rvc"] = dict(cfg.get("rvc") or {})
        cfg["rvc"]["fetch_script"] = str(tmp_path / "没有这个脚本.py")
        monkeypatch.setattr(vs, "_cfg", lambda: cfg)
        res = vs.handle_cover({"song": "任意一首歌"})
        text = _text(res)
        assert text.startswith("失败：")
        assert "下载脚本不在本机" in text
        assert "下载" in text

    def test_separation_model_missing_never_asks_for_separation(self, tmp_path, monkeypatch):
        """本机没有分离模型 → **不许**把 --separate 交给 pymss（它会自动下载）。"""
        cfg = _cfg_with_out(tmp_path)
        monkeypatch.setattr(vs, "_cfg", lambda: cfg)
        monkeypatch.setattr(vs, "_separate_model", lambda c: (None, None))
        seen = {}

        class _FakeProc:
            returncode = 0
            stdout = ""
            stderr = ""

        def _fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            return _FakeProc()

        monkeypatch.setattr(vs.subprocess, "run", _fake_run)
        vs._run_fetch(cfg, "http://example.com/x.mp3", tmp_path,
                     sep_model=None, sep_model_dir=None)
        assert "--separate" not in seen["cmd"], "没有本地分离模型却让人去分离（会下载模型）"
        assert "--model" not in seen["cmd"]


# ---------------------------------------------------------------------------
# 2. 打桩的整条链路（不碰网络、不碰模型）
# ---------------------------------------------------------------------------

class TestCoverPipeline:
    """整条链路的结果文本要说清楚：这是翻唱，伴奏混了没有，文件在哪。"""

    def _stub_cover(self, tmp_path, monkeypatch, *, instrumental=True):
        cfg = _cfg_with_out(tmp_path)
        monkeypatch.setattr(vs, "_cfg", lambda: cfg)
        monkeypatch.setattr(vs, "_find_audio_url",
                            lambda song, artist="": ("http://example.com/song.mp3",
                                                     "测试音源", ""))
        lib = tmp_path / "voice_lib"
        lib.mkdir(parents=True, exist_ok=True)
        incoming = lib / "incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        (incoming / "song.wav").write_bytes(b"RIFF" + b"\0" * 400)

        def _fake_fetch(cfg_, url, lib_dir, start=0.0, end=0.0,
                        sep_model=None, sep_model_dir=None):
            payload = {"ok": True, "name": "incoming/song.wav", "bytes": 404}
            if instrumental:
                (incoming / "song.instrumental.wav").write_bytes(b"RIFF" + b"\0" * 400)
                payload["instrumental"] = "incoming/song.instrumental.wav"
            return {"ok": True, "payload": payload, "stage": "", "error": ""}

        def _fake_convert(cfg_, src, out, pitch=0):
            import wave
            out.parent.mkdir(parents=True, exist_ok=True)
            with wave.open(str(out), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(48000)
                w.writeframes(b"\0" * 48000 * 3)
            return True, ""

        def _fake_mix(cfg_, vocal, instrumental_path, out, volume=0.75):
            import shutil
            shutil.copyfile(str(vocal), str(out))
            return True, ""

        monkeypatch.setattr(vs, "_run_fetch", _fake_fetch)
        monkeypatch.setattr(vs, "_run_convert", _fake_convert)
        monkeypatch.setattr(vs, "_mix_with_instrumental", _fake_mix)
        monkeypatch.setattr(vs, "_play", lambda p: True)
        return cfg

    def test_cover_result_says_cover_not_original(self, tmp_path, monkeypatch):
        self._stub_cover(tmp_path, monkeypatch)
        res = vs.handle_cover({"song": "月亮代表我的心", "artist": "邓丽君"})
        text = _text(res)
        assert res.get("isError") is not True, text
        assert "翻唱完了" in text
        assert "不是原唱" in text
        assert "原曲伴奏" in text, "混了伴奏要说清楚"
        assert "音频在" in text
        assert "秒" in text and "KB" in text

    def test_cover_result_reports_mix_off_as_cappella(self, tmp_path, monkeypatch):
        self._stub_cover(tmp_path, monkeypatch)
        res = vs.handle_cover({"song": "月亮代表我的心", "mix": "off"})
        text = _text(res)
        assert res.get("isError") is not True, text
        assert "清唱版" in text
        assert "伴奏被静音" in text

    def test_cover_writes_voice_meta_for_ui(self, tmp_path, monkeypatch):
        """产物要写旁路文件（界面才知道路径 / 时长）。"""
        self._stub_cover(tmp_path, monkeypatch)
        vs.handle_cover({"song": "月亮代表我的心"})
        metas = list(tmp_path.glob("*.voice.json"))
        assert metas, "没有写旁路文件"
        data = json.loads(metas[0].read_text(encoding="utf-8"))
        assert data["kind"] == "sing", "界面按工具名认 kind，别自创"
        assert data["duration"] > 0
        assert Path(data["path"]).is_file()

    def test_fetch_failure_names_the_step(self, tmp_path, monkeypatch):
        """下载 / 分离失败 → 说**是哪一步**失败。"""
        cfg = _cfg_with_out(tmp_path)
        monkeypatch.setattr(vs, "_cfg", lambda: cfg)
        monkeypatch.setattr(vs, "_find_audio_url",
                            lambda song, artist="": ("http://example.com/song.mp3", "测试音源", ""))
        monkeypatch.setattr(vs, "_run_fetch",
                            lambda *a, **k: {"ok": False, "payload": {},
                                             "stage": "人声分离", "error": "分离模型坏了"})
        res = vs.handle_cover({"song": "任意一首歌"})
        text = _text(res)
        assert text.startswith("失败：")
        assert "人声分离失败" in text
        assert "分离模型坏了" in text

    def test_mix_on_without_instrumental_fails(self, tmp_path, monkeypatch):
        self._stub_cover(tmp_path, monkeypatch, instrumental=False)
        res = vs.handle_cover({"song": "任意一首歌", "mix": "on"})
        text = _text(res)
        assert text.startswith("失败：")
        assert "伴奏" in text


class TestSearchHelpers:
    """找歌的两条路：项目自己的搜索后端 → 免 key 的公开音源。"""

    def test_project_backend_first(self, monkeypatch):
        monkeypatch.setattr(
            vs, "_project_web_search",
            lambda q, limit=8: ([{"title": "月亮代表我的心.mp3",
                                  "url": "http://example.com/song.mp3",
                                  "content": ""}], "Tavily"))
        monkeypatch.setattr(vs, "_probe_audio",
                            lambda url, timeout=20.0: {"type": "audio/mpeg", "size": "1"})
        url, source, _notes = vs._find_audio_url("月亮代表我的心", "邓丽君")
        assert url and url.endswith(".mp3")
        assert "搜索后端" in source

    def test_fallback_to_public_source(self, monkeypatch):
        monkeypatch.setattr(vs, "_project_web_search",
                            lambda q, limit=8: ([], "Tavily 和 DuckDuckGo 都不可用"))
        monkeypatch.setattr(vs, "_netease_pick",
                            lambda song, artist="": {"id": 1, "name": "月亮代表我的心",
                                                    "artists": "邓丽君"})
        monkeypatch.setattr(vs, "_netease_direct_url",
                            lambda sid: "http://cdn.example.com/x.mp3")
        url, source, _notes = vs._find_audio_url("月亮代表我的心", "邓丽君")
        assert url == "http://cdn.example.com/x.mp3"
        assert "月亮代表我的心" in source

    def test_nothing_found_returns_none(self, monkeypatch):
        monkeypatch.setattr(vs, "_project_web_search",
                            lambda q, limit=8: ([], "都不可用"))
        monkeypatch.setattr(vs, "_netease_pick", lambda song, artist="": None)
        url, source, notes = vs._find_audio_url("一首根本不存在的歌")
        assert url is None
        assert source == ""
        assert notes, "没找到也要说清为什么"

    def test_netease_skips_vip_songs(self, monkeypatch):
        """VIP 歌（fee=1）拿不到直链 —— 别挑它。"""
        monkeypatch.setattr(vs, "_netease_candidates",
                            lambda song, artist="", limit=8: [
                                {"id": 1, "name": "月亮代表我的心",
                                 "artists": [{"name": "邓丽君"}], "fee": 1},
                            ])
        assert vs._netease_pick("月亮代表我的心") is None


# ---------------------------------------------------------------------------
# 3. 真跑（本机有资产 + 显式开环境变量才跑）
# ---------------------------------------------------------------------------

@needs_cover
class TestRealCover:
    """A1 / A2 / A3：真下载、真分离、真音转音；权重和分离模型不能被动过。"""

    def test_real_cover_produces_file(self, tmp_path, monkeypatch):
        cfg = _cfg_with_out(tmp_path)
        monkeypatch.setattr(vs, "_cfg", lambda: cfg)
        monkeypatch.setattr(vs, "_play", lambda p: True)
        weight = vs._rvc_weight_path(_local_cfg())
        sep = _sep_model()
        before = (weight.stat().st_mtime_ns, weight.stat().st_size,
                  sep.stat().st_mtime_ns, sep.stat().st_size)

        res = vs.handle_cover({"song": "月亮代表我的心", "artist": "邓丽君",
                               "start": 0, "end": 60})
        text = _text(res)
        assert res.get("isError") is not True, text
        assert "翻唱完了" in text
        wavs = sorted(tmp_path.glob("cover_*.wav"))
        assert wavs, "没有产出 wav：" + text
        size, dur = vs._wav_info(wavs[0])
        assert size > 1000
        assert dur > 1.0

        after = (weight.stat().st_mtime_ns, weight.stat().st_size,
                 sep.stat().st_mtime_ns, sep.stat().st_size)
        assert before == after, "本机模型被改了（下载/替换过）：%s → %s" % (before, after)

    def test_real_not_found_song_is_honest(self, tmp_path, monkeypatch):
        cfg = _cfg_with_out(tmp_path)
        monkeypatch.setattr(vs, "_cfg", lambda: cfg)
        res = vs.handle_cover({"song": "zzz这是一首根本不存在的歌zzz"})
        text = _text(res)
        assert text.startswith("失败："), text
        assert "没找到" in text
        assert not list(tmp_path.glob("*.wav")), "找不到歌却产出了音频"
