#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""打包脚本：生成独立的桌面宠物。

使用方式：
    pip install pyinstaller
    python build.py              # 正常打包
    python build.py --clean      # 清理后重新打包

产物：
    dist/desktop-pet/
    ├── desktop-pet.exe     ← 双击运行
    └── _internal/           ← Python 环境 + 所有依赖 + assets

注意：
    - ASR 语音模型不在打包文件里，首次语音识别时自动下载
    - 如网络受限，可在 config.yaml 设置 asr.enabled=false
    - 打包后运行出错时，请查看 dist/desktop-pet/crash.log
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _clean():
    # 清理 build 目录（总是可以删除）
    p = ROOT / "build"
    if p.exists():
        shutil.rmtree(p)
        print(f"  [OK] 已清理 {p}")

    # 清理 dist 目录（如果文件被占用则跳过）
    p = ROOT / "dist"
    if p.exists():
        try:
            shutil.rmtree(p)
            print(f"  [OK] 已清理 {p}")
        except PermissionError:
            print(f"  [WARN] dist 目录被占用，跳过清理，PyInstaller 将覆盖更新")
            # 移除被占用的 exe 文件并重建目录
            old_exe = ROOT / "dist" / "desktop-pet.exe"
            if old_exe.exists():
                old_exe.unlink()
                print(f"  [OK] 已删除旧版单文件 exe: {old_exe}")
            # 移除空目录
            try:
                p.rmdir()
                print(f"  [OK] 已清理空目录: {p}")
            except:
                pass


def _write_spec_file() -> str:
    """动态生成 spec 文件"""
    spec_path = ROOT / "desktop-pet.spec"
    spec_content = '''# -*- mode: python ; coding: utf-8 -*-
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
'''
    spec_path.write_text(spec_content, encoding="utf-8")
    return str(spec_path)


def build(clean: bool = False) -> None:
    _clean()
    spec_path = _write_spec_file()
    print(f"  [OK] 已生成 spec 文件")

    pyi = [sys.executable, "-m", "PyInstaller",
           "--clean", "--noconfirm",
           "--distpath", str(ROOT / "dist"),
           "--workpath", str(ROOT / "build"),
           spec_path]

    print(f"  [INFO] 开始打包（约 5-10 分钟，请勿中断）...\n")

    start = time.time()
    proc = subprocess.Popen(
        pyi, cwd=str(ROOT),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )

    # 写入日志文件
    log_dir = ROOT / "build" / "desktop-pet"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "desktop-pet.log"

    last_progress = 0
    while True:
        line = proc.stdout.readline()
        if not line:
            break
        print(f"    {line.rstrip()}")
        with open(log_path, "a", encoding="utf-8", errors="replace") as f:
            f.write(line)
        # 每 30 秒打印一次进度提示
        elapsed = time.time() - start
        if elapsed - last_progress >= 30:
            print(f"\n  [INFO] 打包进行中... (已运行 {elapsed:.0f}s)\n", flush=True)
            last_progress = elapsed

    proc.wait()
    elapsed = time.time() - start

    print(f"\n  [INFO] 打包完成，耗时 {elapsed:.0f}s")

    if proc.returncode != 0:
        print(f"  [ERR] 打包失败 (exit={proc.returncode})")
        print(f"        日志文件: {log_path}")
        return

    # 验证产物结构（PyInstaller onedir 模式：exe 和 _internal/ 在同一目录）
    out = ROOT / "dist" / "desktop-pet"
    internal = out / "_internal"
    if out.is_dir():
        total = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
        count = len([p for p in out.rglob("*") if p.is_file()])
        # 关键文件检查：onedir 模式下资源在 _internal/ 下
        checks = [
            (out / "desktop-pet.exe", "exe 文件"),
            (internal, "Python 环境 (_internal/)"),
            (internal / "config.example.yaml", "示例配置 (_internal/)"),
            (internal / "assets" / "sprites", "精灵资源 (_internal/assets/sprites/)"),
        ]
        print(f"  [OK] 打包成功: {out}")
        print(f"       大小: {total / 1024 / 1024:.1f} MB  ({count} 个文件)")

        # 验证所有关键文件
        all_ok = True
        for path, desc in checks:
            if path.exists():
                print(f"       ✓ {desc}: {path}")
            else:
                print(f"       ✗ {desc}: {path}")
                all_ok = False

        if all_ok:
            print(f"  [OK] 所有关键文件验证通过")
            print(f"       分发：将 dist/desktop-pet/ 整个文件夹复制给别人即可")
            print(f"       运行：双击 desktop-pet.exe")
        else:
            print(f"  [WARN] 缺少关键文件 - 打包可能失败")
    else:
        print(f"  [ERR] 产物目录不存在: {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="打包桌面宠物")
    ap.add_argument("--clean", action="store_true")
    args = ap.parse_args()

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("缺少 PyInstaller: pip install pyinstaller")
        sys.exit(1)

    build(clean=args.clean)
