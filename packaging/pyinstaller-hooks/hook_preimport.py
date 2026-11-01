"""PyInstaller runtime hook — runs BEFORE the app's main code.

Purpose: pre-load ctranslate2 DLLs before PyQt5 DLLs to avoid the
Windows DLL conflict (segfault when both are loaded).

This hook is referenced in desktop-pet.spec via `runtime_hooks`.
"""
import ctypes
import logging
import sys
from pathlib import Path

log = logging.getLogger("desktop-pet.preimport")


def _try_load_dll(lib_path: str) -> None:
    """Best-effort DLL load. Silently ignores failures."""
    try:
        ctypes.CDLL(lib_path, ctypes.RTLD_GLOBAL)
        log.info("  preloaded: %s", Path(lib_path).name)
    except OSError:
        pass


def _find_dlls(patterns: list[str], search_paths: list[Path]) -> list[str]:
    found = []
    for sp in search_paths:
        if not sp.is_dir():
            continue
        for pat in patterns:
            found.extend(str(p) for p in sorted(sp.rglob(pat)))
    return found


# Search in _MEIPASS (PyInstaller temp dir) and sys.path
meipass = getattr(sys, "_MEIPASS", None)
search_paths = [Path(p) for p in sys.path]
if meipass:
    search_paths.insert(0, Path(meipass))

# Pre-load ctranslate2 DLLs (required by faster-whisper)
# Must happen BEFORE any PyQt5 import in main.py
ctdlls = _find_dlls(["ctranslate2*.dll", "libctranslate2*.dll"], search_paths)
for dl in ctdlls:
    _try_load_dll(dl)

# Also pre-load any ONNX runtime DLLs (ctranslate2 may depend on them)
onnx_dlls = _find_dlls(["onnxruntime*.dll"], search_paths)
for dl in onnx_dlls:
    _try_load_dll(dl)

# Pre-load mkl / intel mkl DLLs if present (ctranslate2 on Windows uses them)
mkl_dlls = _find_dlls(["mkl_*.dll", "mkl_core.dll", "mkl_sequential.dll"], search_paths)
for dl in mkl_dlls:
    _try_load_dll(dl)

log.info("Preimport hook done — ctranslate2 DLLs loaded before PyQt5")

# 设置 Qt 平台插件路径（PyInstaller onedir 模式必需）
# 在 onedir 模式下，PyQt5/plugins/ 位于 exe 同目录下
import os as _os
_exe_dir = Path(sys.executable).parent
_qt_plugins = _exe_dir / "PyQt5" / "plugins"
if _qt_plugins.is_dir():
    _os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(_qt_plugins)
    log.info("Qt plugins path set: %s", _qt_plugins)
else:
    # onefile 模式下，_MEIPASS/PyQt5/plugins/
    if meipass:
        _qt_plugins2 = Path(meipass) / "PyQt5" / "plugins"
        if _qt_plugins2.is_dir():
            _os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(_qt_plugins2)
            log.info("Qt plugins path set (onefile): %s", _qt_plugins2)
