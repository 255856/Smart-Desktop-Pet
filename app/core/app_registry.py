"""应用程序路径映射注册表。

用途：让 ``open_app("QQ")`` 能直接找到 QQ 的 exe 路径，
不需要用户每次给完整路径。

两层映射：
    1. 内置映射（本文件）—— 覆盖 Windows 内置 + 常见软件 + 常见游戏
    2. 用户自定义（data/app_registry.json）—— 用户可自由增删改，
       同名时覆盖内置映射。

JSON 文件格式（data/app_registry.json）：
    {
        "qq": "C:\\\\%PROGRAMFILES%\\\\Tencent\\\\QQ\\\\Bin\\\\QQ.exe",
        "my custom app": "C:\\\\Path\\\\To\\\\App.exe",
        "another app": [
            "C:\\\\Path1\\\\App.exe",
            "C:\\\\Path2\\\\App.exe"
        ]
    }

    - 值为字符串：单一路径
    - 值为列表：按顺序尝试，找到第一个存在的即用
"""  # noqa: W605
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ------------------------------------------------------------------
#  内置映射 —— 常见应用的可执行路径列表
#  按顺序尝试，找到第一个存在的 exe 即返回
# ------------------------------------------------------------------
_BUILTIN_MAP: dict[str, list[str]] = {
    # ===== Windows 内置 =====
    "notepad": ["notepad.exe"],
    "记事本": ["notepad.exe"],
    "calc": ["calc.exe"],
    "计算器": ["calc.exe"],
    "mspaint": ["mspaint.exe"],
    "画板": ["mspaint.exe"],
    "paint": ["mspaint.exe"],
    "explorer": ["explorer.exe"],
    "文件资源管理器": ["explorer.exe"],
    "cmd": ["cmd.exe"],
    "命令提示符": ["cmd.exe"],
    "powershell": ["powershell.exe"],
    "win+r": ["cmd.exe"],
    "control": ["control.exe"],
    "控制面板": ["control.exe"],
    "taskmgr": ["taskmgr.exe"],
    "任务管理器": ["taskmgr.exe"],
    "msedge": ["msedge.exe"],
    "edge": ["msedge.exe"],
    "chrome": ["chrome.exe"],
    "google chrome": ["chrome.exe"],
    "firefox": ["firefox.exe"],
    "word": ["winword.exe"],
    "excel": ["excel.exe"],
    "powerpnt": ["powerpnt.exe"],
    "outlook": ["outlook.exe"],
    "onenote": ["onenote.exe"],
    "regedit": ["regedit.exe"],
    "gpedit.msc": ["gpedit.msc"],
    "diskmgmt.msc": ["diskmgmt.msc"],
    "devmgmt.msc": ["devmgmt.msc"],
    "services.msc": ["services.msc"],
    "msconfig": ["msconfig.exe"],

    # ===== 腾讯系 =====
    "qq": [
        r"%PROGRAMFILES%\Tencent\QQ\Bin\QQ.exe",
        r"%PROGRAMFILES% (x86)\Tencent\QQ\Bin\QQ.exe",
        r"%PROGRAMFILES%\Tencent\QQ\QQ.exe",
        r"%PROGRAMFILES% (x86)\Tencent\QQ\QQ.exe",
    ],
    "微信": [
        r"%PROGRAMFILES%\Tencent\WeChat\WeChat.exe",
        r"%PROGRAMFILES% (x86)\Tencent\WeChat\WeChat.exe",
        r"%PROGRAMFILES%\Tencent\Weixin\WeChat.exe",
        r"%PROGRAMFILES% (x86)\Tencent\Weixin\WeChat.exe",
    ],
    "wechat": [
        r"%PROGRAMFILES%\Tencent\WeChat\WeChat.exe",
        r"%PROGRAMFILES% (x86)\Tencent\WeChat\WeChat.exe",
        r"%PROGRAMFILES%\Tencent\Weixin\WeChat.exe",
        r"%PROGRAMFILES% (x86)\Tencent\Weixin\WeChat.exe",
    ],
    "wx": [
        r"%PROGRAMFILES%\Tencent\WeChat\WeChat.exe",
        r"%PROGRAMFILES% (x86)\Tencent\WeChat\WeChat.exe",
    ],
    "钉钉": [
        r"%PROGRAMFILES%\Alibaba\cainiao\DingTalk\DingTalk.exe",
        r"%PROGRAMFILES% (x86)\Alibaba\cainiao\DingTalk\DingTalk.exe",
    ],
    "dingtalk": [
        r"%PROGRAMFILES%\Alibaba\cainiao\DingTalk\DingTalk.exe",
        r"%PROGRAMFILES% (x86)\Alibaba\cainiao\DingTalk\DingTalk.exe",
    ],
    "腾讯会议": [
        r"%PROGRAMFILES%\Tencent\WeMeet\WeMeet.exe",
        r"%PROGRAMFILES% (x86)\Tencent\WeMeet\WeMeet.exe",
    ],
    "tencent meeting": [
        r"%PROGRAMFILES%\Tencent\WeMeet\WeMeet.exe",
        r"%PROGRAMFILES% (x86)\Tencent\WeMeet\WeMeet.exe",
    ],

    # ===== 办公软件 =====
    "wps": [
        r"%PROGRAMFILES%\Kingsoft\WPS Office\ksolaunch.exe",
        r"%PROGRAMFILES% (x86)\Kingsoft\WPS Office\ksolaunch.exe",
        r"%PROGRAMFILES%\Kingsoft\WPS Office\office6\wps.exe",
        r"%PROGRAMFILES% (x86)\Kingsoft\WPS Office\office6\wps.exe",
    ],
    "飞书": [
        r"%PROGRAMFILES%\Feishu\Feishu.exe",
        r"%PROGRAMFILES% (x86)\Feishu\Feishu.exe",
    ],
    "feishu": [
        r"%PROGRAMFILES%\Feishu\Feishu.exe",
        r"%PROGRAMFILES% (x86)\Feishu\Feishu.exe",
    ],

    # ===== 浏览器 =====
    "360se": [
        r"%PROGRAMFILES%\360\360se\360se.exe",
        r"%PROGRAMFILES% (x86)\360\360se\360se.exe",
        r"%PROGRAMFILES%\360\360se6\360se.exe",
        r"%PROGRAMFILES% (x86)\360\360se6\360se.exe",
    ],
    "360安全浏览器": [
        r"%PROGRAMFILES%\360\360se\360se.exe",
        r"%PROGRAMFILES% (x86)\360\360se\360se.exe",
    ],
    "搜狗浏览器": [
        r"%PROGRAMFILES%\Sogou\SogouExplorer\browser.exe",
        r"%PROGRAMFILES% (x86)\Sogou\SogouExplorer\browser.exe",
    ],
    "sogou": [
        r"%PROGRAMFILES%\Sogou\SogouExplorer\browser.exe",
        r"%PROGRAMFILES% (x86)\Sogou\SogouExplorer\browser.exe",
    ],

    # ===== 音乐 =====
    "网易云音乐": [
        r"%PROGRAMFILES%\Netease\CloudMusic\CloudMusic.exe",
        r"%PROGRAMFILES% (x86)\Netease\CloudMusic\CloudMusic.exe",
    ],
    "netease cloud music": [
        r"%PROGRAMFILES%\Netease\CloudMusic\CloudMusic.exe",
        r"%PROGRAMFILES% (x86)\Netease\CloudMusic\CloudMusic.exe",
    ],
    "neteasecloudmusic": [
        r"%PROGRAMFILES%\Netease\CloudMusic\CloudMusic.exe",
        r"%PROGRAMFILES% (x86)\Netease\CloudMusic\CloudMusic.exe",
    ],
    "酷狗音乐": [
        r"%PROGRAMFILES%\KuGou\Kgm.exe",
        r"%PROGRAMFILES% (x86)\KuGou\Kgm.exe",
    ],
    "kugou": [
        r"%PROGRAMFILES%\KuGou\Kgm.exe",
        r"%PROGRAMFILES% (x86)\KuGou\Kgm.exe",
    ],

    # ===== 视频 / 直播 =====
    "抖音": [
        r"%PROGRAMFILES%\Bytedance\Douyin\Douyin.exe",
        r"%PROGRAMFILES% (x86)\Byted 0586\Douyin\Douyin.exe",
    ],
    "douyin": [
        r"%PROGRAMFILES%\Bytedance\Douyin\Douyin.exe",
        r"%PROGRAMFILES% (x86)\Byted 0586\Douyin\Douyin.exe",
    ],
    "哔哩哔哩": [
        r"%PROGRAMFILES%\BiliBili\BiliBili.exe",
        r"%PROGRAMFILES% (x86)\BiliBili\BiliBili.exe",
    ],
    "bilibili": [
        r"%PROGRAMFILES%\BiliBili\BiliBili.exe",
        r"%PROGRAMFILES% (x86)\BiliBili\BiliBili.exe",
    ],

    # ===== 开发工具 =====
    # 注意：VS Code 默认装到 %LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe
    # （即 C:\Users\<user>\AppData\Local\Programs\Microsoft VS Code\Code.exe）
    # 之前用相对路径 "AppData\Local\..." 在 _expand_path 里找不到，因为基路径只有
    # C:\、%PROGRAMFILES%、%LOCALAPPDATA%，拼出来就成了 %LOCALAPPDATA%\AppData\...
    # 用 %LOCALAPPDATA%\Programs\... 一步到位。
    "code": [
        r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe",
        r"%PROGRAMFILES%\Microsoft VS Code\Code.exe",
    ],
    "vscode": [
        r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe",
        r"%PROGRAMFILES%\Microsoft VS Code\Code.exe",
    ],
    "notepad++": [
        r"%PROGRAMFILES%\Notepad++\notepad++.exe",
        r"%PROGRAMFILES% (x86)\Notepad++\notepad++.exe",
    ],

    # ===== 媒体播放器 =====
    "vlc": [
        r"%PROGRAMFILES%\VideoLAN\VLC\vlc.exe",
        r"%PROGRAMFILES% (x86)\VideoLAN\VLC\vlc.exe",
    ],
    "potplayer": [
        r"%PROGRAMFILES%\DAUM\PotPlayer\PotPlayerMini64.exe",
        r"%PROGRAMFILES% (x86)\DAUM\PotPlayer\PotPlayerMini64.exe",
        r"%PROGRAMFILES%\DAUM\PotPlayer\PotPlayerMini.exe",
        r"%PROGRAMFILES% (x86)\DAUM\PotPlayer\PotPlayerMini.exe",
    ],

    # ===== 截图 / 录屏 =====
    "snipaste": [
        r"%PROGRAMFILES%\Snipaste\Snipaste.exe",
        r"%PROGRAMFILES% (x86)\Snipaste\Snipaste.exe",
        r"%PROGRAMFILES%\Snipaste\Snipaste-x64.exe",
    ],
    "obs": [
        r"%PROGRAMFILES%\obs-studio\bin\64bit\obs64.exe",
        r"%PROGRAMFILES% (x86)\obs-studio\bin\64bit\obs64.exe",
        r"%PROGRAMFILES%\obs-studio\bin\64bit\obs64b.exe",
    ],
    "bandicam": [
        r"%PROGRAMFILES%\Bandicam\Bandicam.exe",
        r"%PROGRAMFILES% (x86)\Bandicam\Bandicam.exe",
    ],
    "everything": [
        r"%PROGRAMFILES%\Everything\Everything.exe",
        r"%PROGRAMFILES% (x86)\Everything\Everything.exe",
    ],

    # ===== 压缩软件 =====
    "winrar": [
        r"%PROGRAMFILES%\WinRAR\Rar.exe",
        r"%PROGRAMFILES% (x86)\WinRAR\Rar.exe",
    ],
    "winzip": [
        r"%PROGRAMFILES%\WinZip\WINZIP.EXE",
        r"%PROGRAMFILES% (x86)\WinZip\WINZIP.EXE",
    ],

    # ===== 通讯 =====
    "skype": ["skype.exe"],
    "teams": [
        r"%PROGRAMFILES%\Microsoft\Teams\current\Teams.exe",
        r"%PROGRAMFILES% (x86)\Microsoft\Teams\current\Teams.exe",
    ],
    "zoom": [
        r"%PROGRAMFILES%\Zoom\Zoom Workplace\Zoom.exe",
        r"%PROGRAMFILES% (x86)\Zoom\Zoom Workplace\Zoom.exe",
    ],

    # ===== 游戏 =====
    "星露谷物语": [
        r"%PROGRAMFILES% (x86)\Steam\steamapps\common\Stardew Valley\StardewValley.exe",
        r"%PROGRAMFILES%\Steam\steamapps\common\Stardew Valley\StardewValley.exe",
    ],
    "stardew valley": [
        r"%PROGRAMFILES% (x86)\Steam\steamapps\common\Stardew Valley\StardewValley.exe",
        r"%PROGRAMFILES%\Steam\steamapps\common\Stardew Valley\StardewValley.exe",
    ],
    "stardew": [
        r"%PROGRAMFILES% (x86)\Steam\steamapps\common\Stardew Valley\StardewValley.exe",
        r"%PROGRAMFILES%\Steam\steamapps\common\Stardew Valley\StardewValley.exe",
    ],
    "崩铁": [
        r"%PROGRAMFILES%\miHoYo\Honkai: Star Rail\launcher.exe",
        r"%PROGRAMFILES% (x86)\miHoYo\Honkai: Star Rail\launcher.exe",
    ],
    "崩坏：星穹铁道": [
        r"%PROGRAMFILES%\miHoYo\Honkai: Star Rail\launcher.exe",
        r"%PROGRAMFILES% (x86)\miHoYo\Honkai: Star Rail\launcher.exe",
    ],
    "cs2": [
        r"%PROGRAMFILES% (x86)\Steam\steamapps\common\Counter-Strike 2\cs2.exe",
        r"%PROGRAMFILES%\Steam\steamapps\common\Counter-Strike 2\cs2.exe",
    ],
    "counter-strike 2": [
        r"%PROGRAMFILES% (x86)\Steam\steamapps\common\Counter-Strike 2\cs2.exe",
        r"%PROGRAMFILES%\Steam\steamapps\common\Counter-Strike 2\cs2.exe",
    ],
    "steam": [
        r"%PROGRAMFILES% (x86)\Steam\steam.exe",
        r"%PROGRAMFILES%\Steam\steam.exe",
    ],
    "genshin": [
        r"%PROGRAMFILES%\miHoYo\Genshin Impact\GenshinImpact.exe",
        r"%PROGRAMFILES% (x86)\miHoYo\Genshin Impact\GenshinImpact.exe",
    ],
    "原神": [
        r"%PROGRAMFILES%\miHoYo\Genshin Impact\GenshinImpact.exe",
        r"%PROGRAMFILES% (x86)\miHoYo\Genshin Impact\GenshinImpact.exe",
    ],

    # ===== 网易 =====
    "网易": [
        r"%PROGRAMFILES%\NetEase\CloudMusic\CloudMusic.exe",
        r"%PROGRAMFILES% (x86)\NetEase\CloudMusic\CloudMusic.exe",
    ],
}


def _normalize_key(name: str) -> str:
    """统一映射 key：转小写、去空格、去标点。"""
    return re.sub(r"[\s\-_.（）()]+", "", name.lower())


class AppRegistry:
    """应用程序路径映射注册表。

    用法::

        reg = AppRegistry()
        path = reg.resolve("QQ")       # -> "C:\Program Files\Tencent\Q\Bin\Q.exe"
        path = reg.resolve("qq")       # 同样结果（key 归一化）
        reg.add_custom("my app", "C:\\\\MyApp.exe")
        reg.save()
    """

    def __init__(self, user_file: str | Path | None = None) -> None:
        self._user_file = Path(user_file) if user_file else None
        self._custom: dict[str, list[str]] = {}  # 归一化 key -> 路径列表
        self._all: dict[str, list[str]] = {}     # 归一化 key -> 路径列表（内置 + 自定义）
        self._build_index()
        if self._user_file:
            self._load_user()

    # ------------------------------------------------------------------
    #  构建索引
    # ------------------------------------------------------------------
    def _build_index(self) -> None:
        """从内置映射构建索引。"""
        self._all.clear()
        for name, paths in _BUILTIN_MAP.items():
            key = _normalize_key(name)
            if key not in self._all:
                self._all[key] = []
            for p in paths:
                if p not in self._all[key]:
                    self._all[key].append(p)
        # 自定义覆盖内置
        for key, paths in self._custom.items():
            self._all[key] = list(paths)

    def _load_user(self) -> None:
        """从用户 JSON 文件加载自定义映射。"""
        if not self._user_file or not self._user_file.is_file():
            return
        try:
            data = json.loads(self._user_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                log.warning("app_registry JSON 格式错误：根节点不是 dict")
                return
            self._custom.clear()
            for name, val in data.items():
                key = _normalize_key(name)
                if isinstance(val, str):
                    self._custom[key] = [val]
                elif isinstance(val, list):
                    self._custom[key] = [str(v) for v in val]
                else:
                    log.warning("app_registry JSON 中 %s 的值类型无效：%s", name, type(val))
            self._build_index()  # 合并内置 + 自定义
            log.info("app_registry：从 %s 加载了 %d 个自定义映射",
                     self._user_file, len(self._custom))
        except Exception as e:  # noqa: BLE001
            log.warning("加载 app_registry 失败：%s", e)

    def save(self) -> None:
        """保存自定义映射到用户 JSON 文件。"""
        if not self._user_file:
            return
        try:
            self._user_file.parent.mkdir(parents=True, exist_ok=True)
            # 反归一化：用原始名称保存（取 key 原样）
            data: dict[str, str | list[str]] = {}
            for key, paths in self._custom.items():
                if len(paths) == 1:
                    data[key] = paths[0]
                else:
                    data[key] = paths
            self._user_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            log.info("app_registry：已保存 %d 个自定义映射到 %s",
                     len(self._custom), self._user_file)
        except Exception as e:  # noqa: BLE001
            log.warning("保存 app_registry 失败：%s", e)

    # ------------------------------------------------------------------
    #  查询 / 修改
    # ------------------------------------------------------------------
    def resolve(self, app_name: str) -> Optional[str]:
        """根据名称查找可执行文件路径。

        查找顺序：
            1. 归一化 key 精确匹配 → 按列表顺序尝试每个路径，找到存在的就返回
            2. 内置映射的模糊匹配（SequenceMatcher.ratio ≥ 0.7）
            3. **已安装应用相似度搜索**（兜底）—— 扫描系统开始菜单 / 桌面 /
               App Paths / Uninstall 注册表 / Program Files，按相似度排序
            4. 都找不到 → 返回 None
        """
        name = app_name.strip()
        if not name:
            return None

        key = _normalize_key(name)

        # 1) 内置精确匹配
        paths = self._all.get(key)
        if paths:
            for p in paths:
                full = _expand_path(p)
                if full and Path(full).is_file():
                    return full
            # 精确 key 存在但所有路径都失效 → 不再走内置模糊匹配
            # 但仍走「已安装应用扫描」，说不定用户自己装了
            return self._resolve_from_installed_apps(name)

        # 2) 内置模糊匹配
        from difflib import SequenceMatcher
        best_match: tuple[float, str] | None = None
        for k, plist in self._all.items():
            if k == key:
                continue
            ratio = SequenceMatcher(None, key, k).ratio()
            if ratio < 0.7:
                continue
            for p in plist:
                full = _expand_path(p)
                if full and Path(full).is_file():
                    if best_match is None or ratio > best_match[0]:
                        best_match = (ratio, full)
                    break
        if best_match:
            return best_match[1]

        # 3) 已安装应用扫描（兜底）
        return self._resolve_from_installed_apps(name)

    def _resolve_from_installed_apps(self, name: str) -> Optional[str]:
        """扫描系统已装应用，按相似度匹配第一个相似度 ≥ 0.85 的。

        用 0.85 阈值（比内置映射的 0.7 高）—— 扫描结果噪声大，宁缺勿滥：
        用户说「vscode」时，宁可返回 None 让上层报告「找不到 + 推荐 top 5」，
        也不要错开别的 IDE。
        """
        try:
            from app.core.installed_apps import find_similar_apps
        except ImportError:
            return None
        candidates = find_similar_apps(name, limit=3, min_ratio=0.85)
        for app, score in candidates:
            if score >= 0.85 and Path(app.path).is_file():
                return app.path
        return None

    def find_similar_in_registry(
        self, query: str, limit: int = 5,
    ) -> list[tuple[str, float]]:
        """在**内置**映射（已安装 + 内置 key）里找相似候选。

        用于上层工具列出「没找到时给你 top 5 推荐」。

        Returns: [(候选显示名, 相似度), ...]
        """
        from difflib import SequenceMatcher
        key = _normalize_key(query)
        scored: list[tuple[str, float]] = []
        for k in self._all.keys():
            if k == key:
                continue
            score = SequenceMatcher(None, key, k).ratio()
            if score >= 0.5:
                # 把归一化 key 反向转成原始显示名（取 _BUILTIN_MAP 中第一个匹配的）
                display = self._key_to_display(k)
                scored.append((display, score))
        # 去重（同 display 多个 key）
        seen: dict[str, float] = {}
        for display, score in scored:
            if display not in seen or score > seen[display]:
                seen[display] = score
        out = list(seen.items())
        out.sort(key=lambda x: -x[1])
        return out[:limit]

    def _key_to_display(self, key: str) -> str:
        """归一化 key → 用户能看懂的显示名（用内置映射里第一个匹配的）。"""
        # 内置映射里找
        for original_name, paths in _BUILTIN_MAP.items():
            if _normalize_key(original_name) == key:
                return original_name
        return key

    def add_custom(self, name: str, path_or_paths: str | list[str]) -> None:
        """添加或覆盖一个自定义映射。"""
        key = _normalize_key(name)
        if isinstance(path_or_paths, str):
            self._custom[key] = [path_or_paths]
        else:
            self._custom[key] = list(path_or_paths)
        self._build_index()

    def remove_custom(self, name: str) -> bool:
        """移除一个自定义映射。"""
        key = _normalize_key(name)
        if key in self._custom:
            del self._custom[key]
            self._build_index()
            return True
        return False

    def list_custom(self) -> dict[str, list[str]]:
        """返回所有自定义映射。"""
        return dict(self._custom)

    def list_all_keys(self) -> list[str]:
        """返回所有可用的映射 key（内置 + 自定义）。"""
        return sorted(self._all.keys())


def _expand_path(p: str) -> Optional[str]:
    """展开路径并返回存在的文件路径。

    支持：
        - 绝对路径 → 直接检查
        - 环境变量路径（%PROGRAMFILES%\\...）→ 展开后检查
        - 相对路径 → 依次在 C:\\、%PROGRAMFILES%、%PROGRAMFILES(X86)%、
          %LOCALAPPDATA%、%APPDATA% 下尝试
        - 裸文件名（notepad.exe）→ 原样返回（由调用方处理）
    """
    expanded = os.path.expandvars(p)
    expanded = os.path.expanduser(expanded)

    # 绝对路径：直接检查
    if os.path.isabs(expanded):
        if Path(expanded).is_file():
            return expanded
        return None

    # 相对路径：依次在常见基准目录下尝试
    bases = [
        "C:\\",
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
        os.environ.get("LOCALAPPDATA", ""),
        os.environ.get("APPDATA", ""),
    ]
    for base in bases:
        if not base:
            continue
        full = os.path.join(base, expanded)
        if Path(full).is_file():
            return full

    # 裸文件名（如 notepad.exe）：在 Windows 系统目录下查找
    if "\\" not in p and "/" not in p:
        for win_dir in (r"C:\Windows", r"C:\Windows\System32"):
            candidate = os.path.join(win_dir, p)
            if Path(candidate).is_file():
                return candidate
        # 如果找不到，返回原始名称（os.startfile 可能仍能找到）
        return p

    return None


def find_app_exe(app_name: str) -> Optional[str]:
    """便捷函数：查找应用可执行文件路径。

    先查注册表映射，找不到则在 Program Files 目录搜索。
    """
    # 1) 注册表映射
    if _REGISTRY is not None:
        result = _REGISTRY.resolve(app_name)
        if result:
            return result

    # 2) 在 Program Files 等目录搜索
    name = app_name.strip().lower()
    exe_name = name + ".exe"
    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
        base = Path(os.environ.get(env, "C:\\Program Files"))
        if not base.is_dir():
            continue
        try:
            for root, dirs, files in os.walk(base):
                depth = str(root).replace(str(base), "").count(os.sep)
                if depth >= 4:
                    dirs[:] = []
                    continue
                for f in files:
                    if f.lower() == exe_name:
                        return os.path.join(root, f)
        except PermissionError:
            pass

    # 3) 搜索 LocalAppData\Programs
    local_apps = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs"
    if local_apps.is_dir():
        try:
            for root, dirs, files in os.walk(local_apps):
                depth = str(root).replace(str(local_apps), "").count(os.sep)
                if depth >= 3:
                    dirs[:] = []
                    continue
                for f in files:
                    if f.lower() == exe_name:
                        return os.path.join(root, f)
        except PermissionError:
            pass

    return None


# 模块级单例（惰性初始化）
_REGISTRY: Optional[AppRegistry] = None


def get_registry() -> AppRegistry:
    """获取全局注册表单例。"""
    global _REGISTRY
    if _REGISTRY is None:
        user_file = Path(__file__).resolve().parent.parent / "data" / "app_registry.json"
        _REGISTRY = AppRegistry(user_file=user_file)
    return _REGISTRY
