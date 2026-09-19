"""Live2D 模型映射配置（profile）—— 每个模型一份 YAML，渲染器按它动态生成能力。

背景：不同 Live2D 模型自带的能力差异很大（表情分类、发型开关、配件、手势、
睡眠参数、水印形式、动作文件……），早期这些知识全部硬编码在 live2d_renderer.py
里（只适配了冰糖）。本模块把"模型专属知识"抽成 YAML 模板：

    <模型目录>/*.model.yaml

渲染器启动时加载 profile，右键菜单 / 设置页 / 触发规则映射（聊天情绪→表情、
工具动作→手势、睡觉、随机表情）全部由它驱动。换模型 = 换一份 YAML。

YAML 里没写、而模型目录里能扫到的信息（如 ``Expressions/类别 名称.exp3.json``
的文件名分类）会自动补全；完全没有 YAML 的模型也能以"文件名启发式"跑起来。

表达式来源两种模式：
    * ``auto``  ：扫描模型目录（Expressions/*.exp3.json 与根目录 *.exp3.json），
                  文件名 ``类别 名称.exp3.json`` 的"类别"前缀决定分组；
    * ``model3``：按 model3.json 的 FileReferences.Expressions 注册表解析
                  （冰糖这种根目录平铺、无类别前缀的老模型）。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

log = logging.getLogger(__name__)

# exp3 文件名分类格式："类别 名称.exp3.json"（超频猫猫官方热键包的命名规则）
_PREFIX_SPLIT = re.compile(r"^(\S+)\s+(.+)$")

# 默认通用情绪别名（模型 YAML 没写 triggers.emotions 时兜底；
# 顺序即匹配优先级：具体的名字在前，见 live2d_renderer 的解析逻辑）
DEFAULT_EMOTION_ALIASES: dict[str, str] = {}


@dataclass
class MenuItem:
    """一个可切换的外观条目（一个 exp3 表情）。"""
    name: str                      # 唯一名（用于 ExpressionManager / 菜单 id）
    label: str                     # 中文显示名
    category: str                  # 所属分类 id
    params: dict[str, float] = field(default_factory=dict)  # exp3 解析出的参数
    file: str = ""                 # 相对 model_dir 的 exp3 路径（注入 model3.json 用）


@dataclass
class Category:
    """菜单/设置里的一个分组（特殊/发型/配件/手势/表情…）。"""
    id: str
    label: str
    mode: str = "toggle"           # toggle=叠加开关 | exclusive=互斥（整组覆写）
    emotion: bool = False          # 该组是否是"情绪表情"（进入随机池 / 触发映射目标）
    hairstyle: bool = False        # 该组是否是"发型"（兼容旧 supports_hairstyles 接口）
    items: list[MenuItem] = field(default_factory=list)
    source: str = ""               # exp3 文件名的类别前缀（auto 模式匹配用）

    def params(self) -> list[str]:
        """该组全部条目会用到的参数（互斥组切换前整组复位）。"""
        out: list[str] = []
        for it in self.items:
            for k in it.params:
                if k not in out:
                    out.append(k)
        return out

    def item(self, name: str) -> Optional[MenuItem]:
        for it in self.items:
            if it.name == name:
                return it
        return None


@dataclass
class ActionSpec:
    """一次性动作（play_animation 的目标）。"""
    kind: str = "expr"             # expr=自定义表情 | pose=参数插值 | item=引用分类条目
    item: str = ""                 # kind=item：引用的分类条目名
    params: list[dict] = field(default_factory=list)        # kind=expr
    param: str = ""                # kind=pose
    value: float = 0.0             # kind=pose
    duration_ms: int = 800         # 播放时长（之后恢复当前情绪/待机）


@dataclass
class Live2DModelProfile:
    """一个 Live2D 模型的完整"专属能力描述"。"""
    model_dir: Path
    name: str = ""
    categories: list[Category] = field(default_factory=list)
    emotion_labels: dict[str, str] = field(default_factory=dict)  # 表情名→中文
    # 通用情绪名（happy/sad/angry…）→ 模型表情名
    emotion_aliases: dict[str, str] = field(default_factory=dict)
    actions: dict[str, ActionSpec] = field(default_factory=dict)
    # AI 思考中：expr=持续表情名（None=不换表情）；params=内联参数（冰糖）；items=叠加的 toggle 条目
    thinking_expr: Optional[str] = None
    thinking_params: list[dict] = field(default_factory=list)
    thinking_items: list[str] = field(default_factory=list)
    # 触摸反应：一次性表情 / 内联参数
    touch_expr: Optional[str] = None
    touch_params: list[dict] = field(default_factory=list)
    touch_hold_ms: int = 700
    # 睡觉
    sleep_params: dict[str, float] = field(default_factory=dict)   # 入睡覆写
    wake_params: dict[str, float] = field(default_factory=dict)    # 醒来复位
    sleep_expr_params: list[dict] = field(default_factory=list)    # 内联睡眠表情（冰糖）
    sleep_motion_group: str = ""                                   # 睡眠动作组（Zzz）
    # 待机动作（注入 model3.json 后由 MotionManager 自动循环）
    idle_motion_group: str = ""
    # YAML motions 段原文（idle_file/sleep_file 相对路径，注入 model3.json 用）
    motions_cfg: dict = field(default_factory=dict)
    # 水印处理：param=保证参数为安全值；drawable=桥端按 drawable 名/指纹拦截；
    # part=把水印部件透明度置 0（模型 cdi3 Parts 里有明确水印部件时首选）
    watermark_mode: str = "none"          # none | param | drawable | part
    watermark_param: str = ""
    watermark_safe_value: float = 0.0
    watermark_part_ids: list[str] = field(default_factory=list)
    watermark_exclude_params: set[str] = field(default_factory=set)
    watermark_exclude_names: set[str] = field(default_factory=set)
    # 需要隐藏的装饰部件（Part id，如超频猫猫 Part10=gz235.png 粉色翅膀背景框）
    hide_parts: list[str] = field(default_factory=list)
    # 挂机随机表情
    random_enabled: bool = False
    random_min_s: int = 25
    random_max_s: int = 70
    random_pool: list[str] = field(default_factory=list)   # 表情名（默认=全部情绪组条目）
    # 表情包（模型目录旁的贴纸 PNG，随机弹在桌宠右上角）
    stickers_dir: str = ""
    stickers_min_s: int = 30
    stickers_max_s: int = 90
    stickers_duration_s: int = 4
    stickers_size: int = 180
    # 头身姿态角度参数（走路/拖拽偏转，回待机回正）
    pose_angle_params: list[str] = field(default_factory=lambda: [
        "ParamAngleX", "ParamAngleY", "ParamAngleZ", "ParamBodyAngleY"])

    # ---------- 查询辅助 ----------
    def category(self, cid: str) -> Optional[Category]:
        for c in self.categories:
            if c.id == cid:
                return c
        return None

    def emotion_category(self) -> Optional[Category]:
        for c in self.categories:
            if c.emotion:
                return c
        return None

    def hairstyle_category(self) -> Optional[Category]:
        for c in self.categories:
            if c.hairstyle:
                return c
        return None

    def find_item(self, name: str) -> Optional[MenuItem]:
        for c in self.categories:
            it = c.item(name)
            if it is not None:
                return it
        return None

    def all_item_params(self) -> list[str]:
        """所有分类条目涉及的参数（reset_all 用）。"""
        out: list[str] = []
        for c in self.categories:
            for p in c.params():
                if p not in out:
                    out.append(p)
        return out

    # 注入 model3.json 用的表情注册表（Name/File）
    def expression_defs(self) -> list[dict[str, str]]:
        defs: list[dict[str, str]] = []
        seen: set[str] = set()
        for c in self.categories:
            for it in c.items:
                if it.name in seen or not it.file:
                    continue
                seen.add(it.name)
                defs.append({"Name": it.name, "File": it.file})
        return defs

    def emotion_options(self) -> list[tuple[str, str]]:
        """情绪选项（自然 + 情绪组条目）。"""
        cat = self.emotion_category()
        out: list[tuple[str, str]] = [("natural", "自然表情")]
        if cat:
            for it in cat.items:
                out.append((it.name, self.emotion_labels.get(it.name, it.label)))
        return out

    def random_pool_items(self) -> list[str]:
        if self.random_pool:
            return [n for n in self.random_pool if self.find_item(n) is not None]
        cat = self.emotion_category()
        return [it.name for it in cat.items] if cat else []


# ---------- 解析 ----------

def _load_yaml(model_dir: Path) -> dict:
    """查找并加载模型映射配置。

    优先级：
        1. 模型目录内的 *.model.yaml（模型作者/用户放的自定义配置）；
        2. 仓库 ``assets/live2d_profiles/<模型目录名>.model.yaml``（随项目分发的
           官方模板 —— 模型目录本身不入库时配置仍有份）。
    """
    # 1) 模型目录内任意 *.model.yaml
    for p in sorted(model_dir.glob("*.model.yaml")):
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict):
                log.info("Live2D profile: 使用 %s", p)
                return data
        except Exception as e:  # noqa: BLE001
            log.warning("Live2D profile 解析失败 %s: %s", p, e)
    # 2) 仓库内置模板（按模型目录名匹配）
    repo_dir = Path(__file__).resolve().parent.parent.parent / "assets" / "live2d_profiles"
    p = repo_dir / f"{model_dir.name}.model.yaml"
    if p.is_file():
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict):
                log.info("Live2D profile: 使用仓库模板 %s", p.name)
                return data
        except Exception as e:  # noqa: BLE001
            log.warning("Live2D profile 解析失败 %s: %s", p, e)
    return {}


def _read_exp3(path: Path) -> dict[str, float]:
    try:
        exp = json.loads(path.read_text(encoding="utf-8"))
        return {
            str(p["Id"]): float(p.get("Value", 0))
            for p in exp.get("Parameters", []) if p.get("Id")
        }
    except Exception as e:  # noqa: BLE001
        log.warning("Live2D profile: 读取表情失败 %s: %s", path.name, e)
        return {}


def _scan_expression_files(model_dir: Path, settings_name: str
                           ) -> list[tuple[str, str, dict[str, float], str]]:
    """扫描模型目录的 exp3 文件 → [(类别前缀或"", 名称, 参数, 相对路径)]。

    同时扫根目录与 Expressions/ 子目录（两个官方布局都兼容）；
    model3.json 已注册的（model3 模式用的）也会被扫到，由调用方按模式取舍。
    """
    out: list[tuple[str, str, dict[str, float], str]] = []
    seen_files: set[str] = set()
    candidates: list[Path] = []
    root = sorted(model_dir.glob("*.exp3.json"))
    sub = sorted(model_dir.glob("Expressions/*.exp3.json"))
    # Expressions/ 优先（超频猫猫在子目录，冰糖在根目录，二者不冲突）
    candidates.extend(sub)
    candidates.extend(root)
    for f in candidates:
        rel = f.relative_to(model_dir).as_posix()
        if rel in seen_files:
            continue
        seen_files.add(rel)
        # 双后缀：Path.stem 只去掉 ".json"，需手动去掉 ".exp3.json"
        name_full = f.name
        stem = (name_full[:-len(".exp3.json")] if name_full.endswith(".exp3.json")
                else f.stem)
        m = _PREFIX_SPLIT.match(stem)
        prefix, name = (m.group(1), m.group(2)) if m else ("", stem)
        params = _read_exp3(f)
        if params:
            out.append((prefix, name, params, rel))
    return out


def _is_watermark_item(profile: "Live2DModelProfile", name: str,
                       params: dict[str, float]) -> bool:
    """水印条目判定：显式名单命中，或参数层模式下水印开关参数被该条目独占。"""
    if name in profile.watermark_exclude_names:
        return True
    if (profile.watermark_mode == "param" and profile.watermark_param
            and params and set(params) <= {profile.watermark_param}):
        return True
    return False


def _parse_categories(cfg: dict, scanned: list[tuple[str, str, dict[str, float], str]],
                      profile: "Live2DModelProfile") -> None:
    """构建分类与条目。

    * auto 模式：scanned 里按"类别前缀"归组，组由 categories.source 声明
      （未声明的组自动追加，mode=toggle）；
    * model3 模式（冰糖）：条目名直接来自 YAML categories.items，参数从
      model3.json 注册的 exp3 文件读取（watermark 项被剔除）。
    """
    cat_cfgs = cfg.get("categories") or []
    mode = (cfg.get("expressions_from") or "auto").lower()
    exclude_params = profile.watermark_exclude_params

    # ---- 建分类骨架 ----
    cats: dict[str, Category] = {}
    order: list[str] = []
    for cc in cat_cfgs:
        cid = str(cc.get("id") or cc.get("label"))
        cat = Category(
            id=cid,
            label=str(cc.get("label") or cid),
            mode=str(cc.get("mode") or "toggle"),
            emotion=bool(cc.get("emotion")),
            hairstyle=bool(cc.get("hairstyle")),
            source=str(cc.get("source") or ""),
        )
        cats[cid] = cat
        order.append(cid)

    # ---- 填条目 ----
    if mode == "model3":
        # model3.json 注册表（冰糖）：Name -> File
        registered: list[tuple[str, str]] = []
        try:
            settings = json.loads(
                (profile.model_dir / _settings_file(profile)).read_text(encoding="utf-8"))
            for d in settings.get("FileReferences", {}).get("Expressions", []):
                name = d.get("Name") or d.get("name")
                fname = d.get("File") or d.get("file")
                if name and fname:
                    registered.append((str(name), str(fname)))
        except Exception as e:  # noqa: BLE001
            log.warning("Live2D profile: 读 model3.json 表情注册失败: %s", e)
        # YAML 里声明的条目名顺序为准；未声明的按注册表顺序归入 label 同名组或第一个组
        declared: dict[str, str] = {}
        for cc in cat_cfgs:
            for n in (cc.get("items") or []):
                declared[str(n)] = str(cc.get("id") or cc.get("label"))
        for name, fname in registered:
            params = {k: v for k, v in _read_exp3(profile.model_dir / fname).items()
                      if k not in exclude_params}
            if not params or _is_watermark_item(profile, name, params):
                continue  # 纯水印表情 / 水印开关条目
            cid = declared.get(name)
            if cid is None:
                # 未声明：归入与条目名相关的组（冰糖：HAIR*/hair* → 发型），否则第一个组
                if re.match(r"^(HAIR|hair|Rhair|danhair)", name):
                    cid = next((c.id for c in cats.values() if c.hairstyle), None)
                else:
                    cid = next((c.id for c in cats.values() if c.emotion),
                               order[0] if order else None)
                if cid is None:
                    continue
            cat = cats.get(cid)
            if cat is None:
                continue
            cat.items.append(MenuItem(
                name=name, label=profile.emotion_labels.get(name, name),
                category=cid, params=params, file=fname))
    else:
        # auto 模式：文件名前缀归组
        for prefix, name, params, rel in scanned:
            params = {k: v for k, v in params.items() if k not in exclude_params}
            if not params or _is_watermark_item(profile, name, params):
                continue
            cid = next((c.id for c in cats.values() if c.source == prefix), None)
            if cid is None:
                # 未声明的前缀：自动建组
                cid = prefix or "misc"
                label = prefix or "其他"
                cats[cid] = Category(id=cid, label=label, mode="toggle", source=prefix)
                order.append(cid)
            cats[cid].items.append(MenuItem(
                name=name, label=name, category=cid, params=params, file=rel))

    profile.categories = [cats[cid] for cid in order if cid in cats]


def _settings_file(profile: "Live2DModelProfile") -> str:
    model3 = sorted(profile.model_dir.glob("*.model3.json"))
    if not model3:
        raise RuntimeError(f"model_dir 没有 *.model3.json: {profile.model_dir}")
    return model3[0].name


def _parse_action(d: Any) -> ActionSpec:
    """动作条目：字符串（引用条目名）或 dict（use/params/param 内联）。"""
    if isinstance(d, str):
        return ActionSpec(kind="item", item=d, duration_ms=1600)
    d = d or {}
    if d.get("use"):
        return ActionSpec(kind="item", item=str(d["use"]),
                          duration_ms=int(d.get("hold_ms") or d.get("duration_ms") or 1600))
    if d.get("param") is not None:
        return ActionSpec(kind="pose", param=str(d["param"]),
                          value=float(d.get("value", 0.0)),
                          duration_ms=int(d.get("duration_ms") or 600))
    return ActionSpec(kind="expr", params=_expr_params_list(d.get("params")),
                      duration_ms=int(d.get("duration_ms") or 800))


def _expr_params_list(raw: Any) -> list[dict]:
    """[{Id, Value, Blend}] 内联参数清洗。"""
    out: list[dict] = []
    for p in raw or []:
        if isinstance(p, dict) and p.get("Id"):
            out.append({"Id": str(p["Id"]), "Value": float(p.get("Value", 0)),
                        "Blend": str(p.get("Blend") or "Add")})
    return out


def load_model_profile(model_dir: str | Path,
                       random_cfg: Optional[dict] = None) -> Live2DModelProfile:
    """加载模型 profile：YAML（若有）+ 文件系统扫描合并。"""
    model_dir = Path(model_dir)
    cfg = _load_yaml(model_dir)
    profile = Live2DModelProfile(model_dir=model_dir)

    wm = cfg.get("watermark") or {}
    profile.watermark_mode = str(wm.get("mode") or "none")
    profile.watermark_param = str(wm.get("param") or "")
    profile.watermark_safe_value = float(wm.get("safe_value", 0))
    profile.watermark_exclude_params = set(wm.get("exclude_params") or [])
    profile.watermark_exclude_names = set(wm.get("exclude_names") or [])
    profile.watermark_part_ids = [str(x) for x in (wm.get("part_ids") or [])]
    profile.hide_parts = [str(x) for x in (cfg.get("hide_parts") or [])]

    profile.name = str(cfg.get("name") or model_dir.name)
    profile.emotion_labels = {str(k): str(v) for k, v in (cfg.get("emotion_labels") or {}).items()}

    # 动作 / 思考 / 触摸 / 睡觉
    trig = cfg.get("triggers") or {}
    profile.emotion_aliases = {str(k).lower(): str(v)
                               for k, v in (trig.get("emotions") or {}).items()}
    profile.actions = {str(k): _parse_action(v) for k, v in (trig.get("actions") or {}).items()}
    th = trig.get("thinking") or {}
    profile.thinking_expr = th.get("expr")
    profile.thinking_params = _expr_params_list(th.get("params"))
    profile.thinking_items = [str(x) for x in (th.get("items") or [])]
    tc = trig.get("touch") or {}
    profile.touch_expr = tc.get("expr")
    profile.touch_params = _expr_params_list(tc.get("params"))
    profile.touch_hold_ms = int(tc.get("hold_ms") or 700)

    sl = cfg.get("sleep") or {}
    profile.sleep_params = {str(k): float(v) for k, v in (sl.get("params") or {}).items()}
    profile.wake_params = {str(k): float(v) for k, v in (sl.get("off_params") or {}).items()}
    profile.sleep_expr_params = _expr_params_list(sl.get("expr_params"))
    mo = cfg.get("motions") or {}
    profile.motions_cfg = mo
    profile.sleep_motion_group = str(mo.get("sleep_group") or "")
    profile.idle_motion_group = str(mo.get("idle_group") or "")

    pp = cfg.get("pose_angle_params")
    if pp:
        profile.pose_angle_params = [str(x) for x in pp]

    # 随机表情：YAML 提供池子与默认节奏；config.yaml 的 random_cfg 只做开关/节奏覆盖
    rnd = cfg.get("random") or {}
    profile.random_enabled = bool(rnd.get("enabled", False))
    profile.random_min_s = int(rnd.get("min_s") or 25)
    profile.random_max_s = int(rnd.get("max_s") or 70)
    profile.random_pool = [str(x) for x in (rnd.get("pool") or [])]
    if random_cfg:
        profile.random_enabled = bool(random_cfg.get("enabled", profile.random_enabled))
        profile.random_min_s = int(random_cfg.get("min_s") or profile.random_min_s)
        profile.random_max_s = int(random_cfg.get("max_s") or profile.random_max_s)

    # 表情包（贴纸）：相对 model_dir 解析
    st = cfg.get("stickers") or {}
    profile.stickers_dir = str(st.get("dir") or "")
    profile.stickers_min_s = int(st.get("min_s") or 30)
    profile.stickers_max_s = int(st.get("max_s") or 90)
    profile.stickers_duration_s = int(st.get("duration_s") or 4)
    profile.stickers_size = int(st.get("size") or 180)

    # 分类与条目（依赖 watermark 排除项，最后解析）
    scanned = _scan_expression_files(model_dir, "")
    _parse_categories(cfg, scanned, profile)

    # 无 YAML / YAML 未启用随机时：有情绪组就默认给一个温和的随机节奏
    if not cfg.get("random") and profile.emotion_category():
        profile.random_pool = profile.random_pool_items()
        # enabled 仍以 config 覆盖为准（默认 False，避免老模型行为变化）

    if not profile.categories:
        log.warning("Live2D profile: %s 未解析到任何表情分类", profile.name)
    else:
        log.info("Live2D profile: %s 分类=%s", profile.name,
                 {c.label: len(c.items) for c in profile.categories})
    return profile


__all__ = [
    "Live2DModelProfile", "Category", "MenuItem", "ActionSpec",
    "load_model_profile",
]
