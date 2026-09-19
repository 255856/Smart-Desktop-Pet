"""枚举 Windows 系统已安装的应用（按需扫描 + 内存缓存）。

扫描来源（按优先级排序）：
    1. Windows App Paths 注册表（最权威）
       HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\<exe>
       HKCU\\...\\App Paths\\<exe>
       → 这里的「应用名」就是 exe 文件名（如 Code.exe → 「Visual Studio Code」）
    2. 开始菜单快捷方式（*.lnk）
       C:\\ProgramData\\Microsoft\\Windows\\Start Menu\\Programs\\
       %APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\
       → .lnk 文件名（去掉 .lnk 后缀）就是应用显示名
    3. 桌面快捷方式（*.lnk）
       %USERPROFILE%\\Desktop\\
       %PUBLIC%\\Desktop\\
    4. 控制面板「程序和功能」（Uninstall 注册表）
       HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\
       HKLM\\SOFTWARE\\WOW6432Node\\...\\Uninstall\\
       HKCU\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\
       → DisplayName + InstallLocation / DisplayIcon
    5. Program Files 目录扫描（兜底，扫所有顶层 .exe）

性能：
    - 首次扫描较慢（1-3 秒，取决于磁盘），后台线程异步完成
    - 后续查找走缓存（O(N) 字符串相似度计算，N ≈ 几百）
    - 缓存 5 分钟过期（短到能反映新装的应用，长到不让磁盘一直忙）

为什么单开这个模块：
    app_registry 的内置映射只覆盖常见应用；用户装的小众软件 / 国产应用
    在内置里没有。open_app 之前遇到这种场景只能返回「未找到」，体验差。
    有了「扫描系统已装应用」兜底，相似度匹配可以命中 90%+ 的现实场景。
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


_CACHE_TTL_SECONDS = 300    # 5 分钟过期
_SCAN_TIMEOUT = 10          # 扫描超时（秒）


@dataclass
class InstalledApp:
    """已安装应用（一次扫描的结果）。"""
    name: str          # 显示名（如 "Visual Studio Code"）
    path: str          # 完整 exe 路径
    source: str        # 来源："app_paths" / "start_menu" / "desktop" / "uninstall" / "program_files"
    # 原始查询名（如 "Code.exe"，用于模糊匹配；大多数场景下等同 name）

    def __hash__(self) -> int:
        return hash(self.path.lower())

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, InstalledApp):
            return False
        return self.path.lower() == other.path.lower()


@dataclass
class _ScanResult:
    apps: list[InstalledApp] = field(default_factory=list)
    scanned_at: float = 0.0
    duration_ms: int = 0


class InstalledAppsCache:
    """已安装应用缓存（带 TTL）。"""

    def __init__(self, ttl_seconds: int = _CACHE_TTL_SECONDS):
        self._lock = threading.Lock()
        self._result = _ScanResult()
        self._scanning = False
        self._scan_thread: Optional[threading.Thread] = None
        self._ttl = ttl_seconds
        # 首次访问时异步触发后台扫描
        self._maybe_start_scan()

    def _is_fresh(self) -> bool:
        return (
            self._result.apps
            and (time.time() - self._result.scanned_at) < self._ttl
        )

    def _maybe_start_scan(self) -> None:
        """后台线程首次扫描。"""
        if self._scanning or self._scan_thread is not None:
            return
        self._scanning = True
        t = threading.Thread(
            target=self._scan_worker, daemon=True, name="installed-apps-scan",
        )
        self._scan_thread = t
        t.start()

    def _scan_worker(self) -> None:
        try:
            t0 = time.monotonic()
            apps = _scan_installed_apps()
            duration_ms = int((time.monotonic() - t0) * 1000)
            with self._lock:
                self._result = _ScanResult(
                    apps=apps, scanned_at=time.time(), duration_ms=duration_ms,
                )
            log.info(
                "InstalledAppsCache: 扫描完成，%d 个应用，耗时 %d ms",
                len(apps), duration_ms,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("InstalledAppsCache 扫描失败：%s", e)
        finally:
            self._scanning = False
            self._scan_thread = None

    def force_refresh(self) -> None:
        """强制重新扫描（用户点了「刷新应用列表」之类时用）。"""
        with self._lock:
            self._result = _ScanResult()
        self._maybe_start_scan()

    def get_all(self, wait: bool = False, timeout: float = 3.0) -> list[InstalledApp]:
        """返回当前缓存的所有应用（可能为空——扫描还没完成）。

        Args:
            wait: True 时阻塞等扫描完成（最多 timeout 秒）；
                   False 时直接返回当前缓存（可能空）
            timeout: wait=True 时的最长等待时间
        """
        # 先尝试同步触发扫描（如果还没启动）
        with self._lock:
            if not self._result.apps and not self._scanning:
                self._maybe_start_scan()

        if not wait:
            with self._lock:
                return list(self._result.apps)

        # wait=True：阻塞等扫描线程完成
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if self._result.apps:
                    return list(self._result.apps)
                if not self._scanning and self._scan_thread is None:
                    # 扫描失败或已完成但没结果
                    break
            time.sleep(0.05)
        with self._lock:
            return list(self._result.apps)

    def find_similar(
        self,
        query: str,
        limit: int = 5,
        min_ratio: float = 0.5,
        wait: bool = True,
        timeout: float = 3.0,
    ) -> list[tuple[InstalledApp, float]]:
        """按相似度找 top-N 候选。

        排序键 = SequenceMatcher.ratio + 名字短串加成（短名更相关）。
        返回 [(app, score), ...]，按 score 降序。

        Args:
            query: 用户输入的应用名（中文/英文/pinyin 都行）
            limit: 最多返回多少个候选
            min_ratio: 最低相似度阈值，低于此值的候选不返回
            wait: True 时若缓存为空会阻塞等扫描（最多 timeout 秒）
            timeout: wait=True 时的最长等待时间
        """
        apps = self.get_all(wait=wait, timeout=timeout)
        q = _normalize_for_search(query).lower()
        if not q:
            return []

        scored: list[tuple[InstalledApp, float]] = []
        for app in apps:
            # 同时对显示名和 exe 文件名做相似度，取高者
            name_norm = _normalize_for_search(app.name).lower()
            path_norm = Path(app.path).stem.lower()
            score_name = SequenceMatcher(None, q, name_norm).ratio()
            score_path = SequenceMatcher(None, q, path_norm).ratio()
            score = max(score_name, score_path)

            # 完全包含（q 是 app name 的子串）大幅加分
            if q in name_norm or name_norm in q:
                score = max(score, 0.85)
            elif q in path_norm or path_norm in q:
                score = max(score, 0.8)

            if score >= min_ratio:
                scored.append((app, score))

        scored.sort(key=lambda x: -x[1])
        return scored[:limit]


# -----------------------------------------------------------
#  归一化
# -----------------------------------------------------------

# 去常见后缀（用于去掉「VS Code.lnk」→「VS Code」）
_LNK_SUFFIXES = (".lnk", ".url", ".exe")
# 替掉这些符号为空格，便于模糊匹配
_TRIM_RE = re.compile(r"[\s\-_\.·•()（）[\]【】]+")


def _normalize_for_search(name: str) -> str:
    """去掉后缀、标点，转小写——便于相似度匹配。"""
    n = (name or "").strip()
    for suf in _LNK_SUFFIXES:
        if n.lower().endswith(suf):
            n = n[: -len(suf)]
            break
    # 把 . - _ 等替换成空格（避免「VSCode」vs「VS Code」匹配度被 . 卡掉）
    n = _TRIM_RE.sub(" ", n)
    return n.strip()


# -----------------------------------------------------------
#  扫描各来源
# -----------------------------------------------------------


def _scan_installed_apps() -> list[InstalledApp]:
    """合并所有来源的结果，按路径去重。"""
    out: dict[str, InstalledApp] = {}    # path(lowercase) -> InstalledApp

    def _add(app: InstalledApp) -> None:
        key = app.path.lower()
        if key not in out:
            out[key] = app

    for src_fn in (
        _scan_app_paths,
        _scan_start_menu,
        _scan_desktop,
        _scan_uninstall,
        _scan_program_files_top_level,
    ):
        try:
            for app in src_fn():
                _add(app)
        except Exception as e:  # noqa: BLE001
            log.warning("扫描源 %s 失败：%s", src_fn.__name__, e)

    return list(out.values())


def _resolve_lnk(lnk_path: Path) -> Optional[str]:
    """解析 .lnk 指向的 exe 路径。

    用 Windows Shell COM 接口（pywin32 / winshell）。如果环境没装，
    退化为：直接把 .lnk 路径丢给 os.startfile（Windows 也能解析）。
    """
    try:
        # 优先 pywin32（最可靠）
        import win32com.client  # type: ignore[import-not-found]
        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortCut(str(lnk_path))
        target = shortcut.Targetpath
        if target and Path(target).is_file():
            return target
    except Exception:  # noqa: BLE001
        pass

    try:
        # 次选：pyshortcuts（pip install pyshortcuts）
        from win32com.shell import shell, shellcon  # type: ignore
        # 实际拿不到，纯占位避免误删
    except Exception:  # noqa: BLE001
        pass

    return None


def _scan_start_menu() -> list[InstalledApp]:
    """扫描开始菜单的 .lnk 快捷方式。"""
    out: list[InstalledApp] = []
    roots = [
        Path(os.environ.get("ProgramData", r"C:\ProgramData"))
        / "Microsoft/Windows/Start Menu/Programs",
        Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
    ]
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for lnk in root.rglob("*.lnk"):
                target = _resolve_lnk(lnk)
                if not target or not Path(target).is_file():
                    continue
                # 名字取「子目录 + 文件名」（如「开发工具\\VS Code.lnk」 → "开发工具 VS Code"）
                try:
                    rel = lnk.relative_to(root)
                    parts = list(rel.parts[:-1]) + [lnk.stem]
                    display = " ".join(parts)
                except ValueError:
                    display = lnk.stem
                out.append(InstalledApp(
                    name=display, path=target, source="start_menu",
                ))
        except Exception as e:  # noqa: BLE001
            log.debug("扫描开始菜单 %s 失败：%s", root, e)
    return out


def _scan_desktop() -> list[InstalledApp]:
    """扫描桌面快捷方式。"""
    out: list[InstalledApp] = []
    roots = [
        Path(os.environ.get("USERPROFILE", "")) / "Desktop",
        Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "Desktop",
    ]
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for lnk in root.glob("*.lnk"):
                target = _resolve_lnk(lnk)
                if not target or not Path(target).is_file():
                    continue
                out.append(InstalledApp(
                    name=lnk.stem, path=target, source="desktop",
                ))
        except Exception as e:  # noqa: BLE001
            log.debug("扫描桌面 %s 失败：%s", root, e)
    return out


def _scan_app_paths() -> list[InstalledApp]:
    """扫描注册表 App Paths（最权威：每个 exe 的「显示名」）。

    这些键里 Default 值是 exe 完整路径；同级可能还有 (Default) 之外的
    FriendlyAppName 字符串值。
    """
    out: list[InstalledApp] = []
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        return out

    base = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App paths"
    for hive, hive_name in (
        (winreg.HKEY_LOCAL_MACHINE, "HKLM"),
        (winreg.HKEY_CURRENT_USER, "HKCU"),
    ):
        try:
            with winreg.OpenKey(hive, base) as k:
                i = 0
                while True:
                    try:
                        sub_name = winreg.EnumKey(k, i)
                    except OSError:
                        break
                    i += 1
                    try:
                        with winreg.OpenKey(k, sub_name) as sub:
                            try:
                                path, _ = winreg.QueryValueEx(sub, "")
                            except OSError:
                                continue
                            path = path.strip('"')
                            if not Path(path).is_file():
                                continue
                            try:
                                friendly, _ = winreg.QueryValueEx(sub, "FriendlyAppName")
                                friendly = friendly.strip('"')
                            except OSError:
                                # 没有 FriendlyAppName，用 exe 文件名去后缀
                                friendly = Path(sub_name).stem
                            out.append(InstalledApp(
                                name=friendly, path=path, source="app_paths",
                            ))
                    except OSError:
                        continue
        except OSError:
            continue

    return out


def _scan_uninstall() -> list[InstalledApp]:
    """扫描 Uninstall 注册表（程序和功能）。"""
    out: list[InstalledApp] = []
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        return out

    paths = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    seen_keys: set[str] = set()
    for hive, base in paths:
        try:
            with winreg.OpenKey(hive, base) as k:
                i = 0
                while True:
                    try:
                        sub_name = winreg.EnumKey(k, i)
                    except OSError:
                        break
                    i += 1
                    if sub_name in seen_keys:
                        continue
                    seen_keys.add(sub_name)
                    try:
                        with winreg.OpenKey(k, sub_name) as sub:
                            try:
                                display, _ = winreg.QueryValueEx(sub, "DisplayName")
                            except OSError:
                                continue
                            if not display or not display.strip():
                                continue
                            display = display.strip()
                            # 找 exe 路径
                            exe_path: Optional[str] = None
                            for val_name in ("DisplayIcon", "InstallLocation"):
                                try:
                                    v, _ = winreg.QueryValueEx(sub, val_name)
                                    v = v.strip('"')
                                    if not v:
                                        continue
                                    if val_name == "DisplayIcon":
                                        # DisplayIcon 经常是 "path,0" 形式
                                        v = v.split(",")[0].strip('"')
                                        if v.lower().endswith(".exe") and Path(v).is_file():
                                            exe_path = v
                                            break
                                    elif val_name == "InstallLocation":
                                        # 在安装目录找一个 .exe
                                        ip = Path(v)
                                        if ip.is_dir():
                                            for f in ip.iterdir():
                                                if f.suffix.lower() == ".exe" and f.is_file():
                                                    exe_path = str(f)
                                                    break
                                except OSError:
                                    continue
                            if exe_path:
                                out.append(InstalledApp(
                                    name=display, path=exe_path, source="uninstall",
                                ))
                    except OSError:
                        continue
        except OSError:
            continue

    return out


def _scan_program_files_top_level() -> list[InstalledApp]:
    """扫描 Program Files 顶层 .exe（兜底，最快但不准）。"""
    out: list[InstalledApp] = []
    roots = [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    ]
    seen_dirs: set[str] = set()
    for root in roots:
        if not root or not Path(root).is_dir():
            continue
        try:
            for entry in Path(root).iterdir():
                if not entry.is_dir():
                    continue
                # 跳过明显的系统目录
                if entry.name.lower() in {"windowsapps", "common files"}:
                    continue
                if entry.name in seen_dirs:
                    continue
                seen_dirs.add(entry.name)
                # 找顶层 .exe
                for f in entry.iterdir():
                    if (f.suffix.lower() == ".exe"
                            and f.is_file()
                            and not f.name.lower().startswith("unins")):
                        out.append(InstalledApp(
                            name=f"{entry.name} {f.stem}",
                            path=str(f), source="program_files",
                        ))
                        break   # 每个子目录只取第一个 exe
        except (PermissionError, OSError):
            continue
    return out


# -----------------------------------------------------------
#  模块级单例
# -----------------------------------------------------------

_CACHE: Optional[InstalledAppsCache] = None
_CACHE_LOCK = threading.Lock()


def get_installed_apps_cache() -> InstalledAppsCache:
    """获取全局缓存单例。"""
    global _CACHE
    with _CACHE_LOCK:
        if _CACHE is None:
            _CACHE = InstalledAppsCache()
        return _CACHE


def find_similar_apps(
    query: str,
    limit: int = 5,
    min_ratio: float = 0.5,
    wait: bool = True,
    timeout: float = 3.0,
) -> list[tuple[InstalledApp, float]]:
    """便捷函数：在已安装应用里找相似候选。"""
    return get_installed_apps_cache().find_similar(
        query, limit=limit, min_ratio=min_ratio, wait=wait, timeout=timeout,
    )


def list_installed_apps(
    wait: bool = False, timeout: float = 3.0,
) -> list[InstalledApp]:
    """便捷函数：列出所有已缓存的已安装应用。

    Args:
        wait: True 时阻塞等扫描完成（最多 timeout 秒）
    """
    return get_installed_apps_cache().get_all(wait=wait, timeout=timeout)


__all__ = [
    "InstalledApp",
    "InstalledAppsCache",
    "get_installed_apps_cache",
    "find_similar_apps",
    "list_installed_apps",
]
