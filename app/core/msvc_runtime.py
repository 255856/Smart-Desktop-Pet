# -*- coding: utf-8 -*-
"""Windows DLL 陷阱：PyQt5 与原生扩展（chromadb / torch）会抢 MSVC 运行时。

PyQt5 自带一份**旧版** MSVC 运行时（PyQt5/Qt5/bin/msvcp140.dll 等），
并把那个目录加进 DLL 搜索路径 —— 于是**后**加载的原生扩展会捡到那份旧运行时，
ABI 不匹配时整进程直接崩（Windows fatal exception: access violation）。

办法：在 PyQt5 **之前**把系统目录里那份 MSVC 运行时预加载进进程，
全进程就都用系统那份（Qt 也会复用它）→ 不再抢 → 不会崩。

（这段原来住在已经删掉的向量索引模块里，那模块下线后搬到这里 ——
 conftest 仍然要在 import PyQt5 之前调它，记忆 sidecar 那边的 chroma 也吃这条保护。）
"""
from __future__ import annotations

import ctypes
import os

#: 需要预加载的那几个运行时 DLL
_MSVC_RUNTIME_DLLS = ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll")


def preload_system_msvc_runtime() -> bool:
    """把**系统目录**里的 MSVC 运行时提前加载（Windows only）。

    必须在 **import PyQt5 之前**调用才有效（晚了 Qt 已经占住旧版本）。
    返回是否成功（非 Windows / 某个 DLL 缺失都不抛异常）。
    """
    if os.name != "nt":
        return True
    root = os.environ.get("SystemRoot") or "C:/Windows"
    ok = True
    for dll in _MSVC_RUNTIME_DLLS:
        try:
            ctypes.WinDLL(os.path.join(root, "System32", dll))
        except Exception:  # noqa: BLE001
            ok = False
    return ok


def _loaded_module_path(name: str) -> str:
    """某个**已加载** DLL 的完整路径（拿不到就返回空串）。"""
    if os.name != "nt":
        return ""
    try:
        from ctypes import wintypes
        handle = wintypes.HMODULE()
        get = ctypes.windll.kernel32.GetModuleHandleExW
        get.argtypes = [wintypes.DWORD, wintypes.LPCWSTR,
                        ctypes.POINTER(wintypes.HMODULE)]
        get.restype = wintypes.BOOL
        # 0x2 = GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT
        if not get(0x2, name, ctypes.byref(handle)):
            return ""
        buf = ctypes.create_unicode_buffer(1024)
        length = wintypes.DWORD(1024)
        ctypes.windll.kernel32.GetModuleFileNameW(
            handle, buf, ctypes.byref(length))
        return buf.value
    except Exception:  # noqa: BLE001
        return ""


def system_msvc_runtime_loaded() -> bool:
    """系统目录里的 MSVC 运行时是不是已经被加载进本进程了？"""
    path = _loaded_module_path("msvcp140.dll")
    if not path:
        return False
    return "system32" in os.path.normcase(path)


__all__ = [
    "preload_system_msvc_runtime", "system_msvc_runtime_loaded",
    "_loaded_module_path", "_MSVC_RUNTIME_DLLS",
]
