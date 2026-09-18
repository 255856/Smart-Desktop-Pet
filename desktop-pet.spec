# -*- mode: python ; coding: utf-8 -*-
"""Auto-generated spec for desktop-pet."""
import os
import sys
from pathlib import Path

SPEC_DIR = getattr(sys, "_PYINSTALLER_SPEC_DIR", None)
if SPEC_DIR is None:
    SPEC_DIR = Path(__file__).resolve().parent if "__file__" in dir() else Path(sys.argv[0]).resolve().parent
ROOT = Path(SPEC_DIR)

data_files = []
assets_root = ROOT / "assets"
for dirpath, dirnames, filenames in os.walk(assets_root):
    dirnames[:] = [d for d in dirnames if d not in (".", "..", "tts_cache")
                   and not d.startswith(".")]
    for fn in filenames:
        src = os.path.join(dirpath, fn)
        rel = os.path.relpath(src, ROOT)
        data_files.append((src, os.path.dirname(rel)))

cfg = ROOT / "config.example.yaml"
if cfg.is_file():
    data_files.append((str(cfg), "."))

plugins = []
try:
    from PyQt5.QtCore import QLibraryInfo
    pdir = QLibraryInfo.location(QLibraryInfo.LibraryLocation.PluginsPath)
    for ptype in ["platforms", "styles", "iconengines", "imageformats", "platformthemes"]:
        pp = Path(pdir) / ptype
        if pp.is_dir():
            for fn in pp.iterdir():
                if fn.suffix.lower() in (".dll", ".so", ".dylib"):
                    plugins.append((str(fn), f"PyQt5/plugins/{ptype}"))
except Exception:
    pass

hidden = [
    "PyQt5", "PyQt5.QtCore", "PyQt5.QtGui", "PyQt5.QtWidgets",
    "huggingface_hub", "transformers", "sounddevice", "soundfile",
    "edge_tts", "numpy", "scipy", "ctranslate2",
    "PIL", "PIL.Image", "urllib3", "onnxruntime",
    "certifi", "yaml", "httpx", "httpcore", "h11",
    "pygame", "pygame.mixer",
    # 添加 app 包的所有模块，确保子模块也能被打包
    "app.qt_compat", "app.config", "app.characters", "app.pet_window",
    "app.tray", "app.reminder", "app.save", "app.state", "app.motion",
    "app.llm_client", "app.voice", "app.settings_window", "app.sprite_atlas",
    "app.animations", "app.asr", "app.chat_window", "app.character",
    "app.works", "app.pyinstaller-hooks.hook_preimport",
]

a = Analysis(
    [str(ROOT / "app" / "main.py")],
    pathex=[str(ROOT)],
    binaries=plugins,
    datas=data_files,
    hiddenimports=hidden,
    hookspath=[str(ROOT / "pyinstaller-hooks")],
    runtime_hooks=[str(ROOT / "pyinstaller-hooks" / "hook_preimport.py")],
    excludes=["pytest", "tkinter", "idlelib"],
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts,
    name="desktop-pet",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
)

coll = COLLECT(
    exe, a.binaries, a.zipfiles, a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="desktop-pet",
)
