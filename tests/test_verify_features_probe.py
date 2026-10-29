"""Stage 2.5：verify_features.py 的「冲突检测」检查必须**真调用**。

以前那一项是：
    check("  · 冲突检测（detect_conflicts）", "detect_conflicts" in mem_src)
也就是 **grep 源码文本** —— detect_conflicts 被改坏了（甚至整个删了只留个空壳）
也照样「通过」。现在改成真调用 + 真断言。

这里钉住：
- A3 detect_conflicts 坏了 → 检查必须**失败**（不是「名字还在就绿」）
- 探测必须用**临时库**：仓库的 data/ 一个字节都不能动（mtime + size + 行数）
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"


def _load_verify_module():
    """按路径加载 scripts/verify_features.py（它不是包）。"""
    path = ROOT / "scripts" / "verify_features.py"
    spec = importlib.util.spec_from_file_location("verify_features_probe", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _snapshot_data_dir() -> dict[str, tuple[int, int]]:
    """data/ 下每个文件的 (mtime_ns, size)。"""
    out: dict[str, tuple[int, int]] = {}
    if not DATA_DIR.is_dir():
        return out
    for p in sorted(DATA_DIR.rglob("*")):
        if p.is_file():
            st = p.stat()
            out[str(p.relative_to(DATA_DIR))] = (st.st_mtime_ns, st.st_size)
    return out


def _memory_db_rows() -> dict[str, int]:
    """data/ 下记忆库的**行数**（验收要求：行数也不能变）。"""
    out: dict[str, int] = {}
    for p in sorted(DATA_DIR.glob("memory*.db")):
        conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
        try:
            out[p.name] = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        except sqlite3.Error:
            out[p.name] = -1
        finally:
            conn.close()
    return out


# --------------------------------------------------------------------------
# A3：真调用
# --------------------------------------------------------------------------

def test_probe_passes_on_healthy_detect_conflicts():
    mod = _load_verify_module()
    ok, detail = mod._probe_detect_conflicts()
    assert ok, f"健康的 detect_conflicts 应该通过：{detail}"


def test_probe_fails_when_detect_conflicts_is_broken(monkeypatch):
    """detect_conflicts 坏了 → 检查必须失败。

    这条就是「假检查」的照妖镜：老实现（grep 源码文本）在这种情况下照样绿。
    """
    from app.brain.memory import MemoryStore

    mod = _load_verify_module()
    monkeypatch.setattr(MemoryStore, "detect_conflicts",
                        lambda self, *a, **k: [])
    ok, detail = mod._probe_detect_conflicts()
    assert not ok, "detect_conflicts 坏了，检查却通过了 —— 说明还是假检查"
    assert detail


def test_probe_fails_when_detect_conflicts_raises(monkeypatch):
    """detect_conflicts 抛异常 → 检查必须失败（不能把异常吞成「通过」）。"""
    from app.brain.memory import MemoryStore

    mod = _load_verify_module()

    def _boom(self, *a, **k):
        raise RuntimeError("坏了")

    monkeypatch.setattr(MemoryStore, "detect_conflicts", _boom)
    try:
        ok, _ = mod._probe_detect_conflicts()
    except RuntimeError:
        return          # 直接抛出来也算「检查没通过」
    assert not ok, "detect_conflicts 抛异常，检查却通过了"


def test_probe_uses_temp_store_not_repo_data(monkeypatch):
    """探测必须用临时库 —— 仓库 data/ 的文件不能被碰。"""
    mod = _load_verify_module()
    seen: list[str] = []

    from app.brain.memory import MemoryStore
    real_init = MemoryStore.__init__

    def _spy_init(self, data_file, *a, **kw):
        seen.append(str(data_file))
        return real_init(self, data_file, *a, **kw)

    monkeypatch.setattr(MemoryStore, "__init__", _spy_init)
    ok, _ = mod._probe_detect_conflicts()
    assert ok
    assert seen, "应该真的构造了一个 MemoryStore"
    for path in seen:
        assert "desktop-pet-verify-" in path or not Path(path).exists() or \
            Path(path).parent != DATA_DIR, f"探测碰了仓库 data/：{path}"


# --------------------------------------------------------------------------
# A4：探测过程 data/ 不变
# --------------------------------------------------------------------------

def test_probe_does_not_modify_repo_data():
    """跑一次探测，data/ 的 mtime + size + 记忆库行数一个都不能变。"""
    mod = _load_verify_module()
    before = _snapshot_data_dir()
    before_rows = _memory_db_rows()

    ok, _ = mod._probe_detect_conflicts()
    assert ok

    after = _snapshot_data_dir()
    assert after == before, "探测之后 data/ 变了：\n" + _diff(before, after)
    assert _memory_db_rows() == before_rows, "探测之后记忆库行数变了"


def _diff(a: dict, b: dict) -> str:
    out = []
    for k in sorted(set(a) | set(b)):
        if a.get(k) != b.get(k):
            out.append(f"  {k}: {a.get(k)} -> {b.get(k)}")
    return "\n".join(out)
