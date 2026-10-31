# -*- coding: utf-8 -*-
"""记忆分区：身份 / 价值 / 程序性 + 情感权重 + 检索路由。

为什么要有这一层
----------------
sidecar（memory_sidecar/）管的是**卡片**：会过期、会衰减、能被检索到。
但桌宠「我是谁」「我坚持什么」「这件事我怎么做」这三类东西**不是卡片**：

* 它们是**设定**，不该跟主人的琐事混在一个库里被检索、被半衰期衰减；
* 它们**每轮都一样** —— 放进 system prompt 几乎零成本（本来就在前缀里），
  放进易变通道反而每轮多占 token；
* 它们**变化很慢** —— 价值一天改两次已经是极限（见下面的日限额）。

所以这三类各有一个自己的小文件（data/identity.json / values.json /
procedures.json），本模块负责：读、按 mtime 缓存、渲染成**常量块**。

⚠️ 常量块 = 前缀缓存的生命线
----------------------------
MiniMax 的 prompt cache 命中**最长公共前缀**（128-token 页粒度），
system message 在最前面 —— 它里面每轮变一次，后面全部不再匹配
（实测命中率 98.9% → 1.5%）。所以这三块**只能**随文件变，不能随每轮变：

* 内容只在**文件变了**之后才变（mtime 检查 + 进程内缓存）；
* 里面**不许**有「当前时间」这种每轮变的东西（文件里写死的日期可以）；
* 程序性记忆的「自动统计」那条路是**离线**的：统计结果落回快照文件，
  注入只读快照 —— 否则每多一条调用记录提示词就变一次，缓存直接归零。

情感权重与检索路由
------------------
* 情感（arousal 唤醒度 / valence 效价）：每张卡可以带，默认 0（中性）。
  旧卡**不需要迁移** —— 缺字段就是中性，排序完全不变。
  衰减**很慢**（半衰期 60 天 ≥ 30 天）：放久了也不该被压到召不回来。
* 路由：query 里的时间词 / 事实词 / how-to 词 → 给对应的 kind 加权。
  **只调权重，不硬过滤** —— 问事实也还能查到事件（只是靠后一点）。

红线
----
* 读不到 / 坏文件 → 当作没有（返回空块），**绝不让聊天崩**；
* 写坏了 → 原子替换（先写 .tmp 再 os.replace），不写坏原文件。
"""
from __future__ import annotations

import datetime
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# 文件名 / 目录
# --------------------------------------------------------------------------

#: 三个分区各自的文件名（都在 data/ 下）
IDENTITY_FILE = "identity.json"
VALUES_FILE = "values.json"
PROCEDURES_FILE = "procedures.json"

#: 环境变量：覆盖 data/ 目录（测试、打包版都用它指到别处）
DATA_DIR_ENV = "DESKTOP_PET_DATA_DIR"


def app_root() -> Path:
    """项目根目录（<root>/app/brain/memory_partitions.py 的上上级）。

    打包后路径不存在 → 退到 cwd。跟 prompt_layer.app_root() 一个口径。
    """
    try:
        here = Path(__file__).resolve()
        if len(here.parents) >= 3:
            return here.parents[2]
    except Exception:  # noqa: BLE001
        pass
    return Path.cwd()


def data_dir(root=None) -> Path:
    """三个分区文件所在的目录。

    优先级：显式参数 → 环境变量 DESKTOP_PET_DATA_DIR → <项目根>/data。
    """
    if root is not None:
        return Path(root)
    env = os.environ.get(DATA_DIR_ENV)
    if env:
        return Path(env)
    return app_root() / "data"


def identity_path(root=None) -> Path:
    return data_dir(root) / IDENTITY_FILE


def values_path(root=None) -> Path:
    return data_dir(root) / VALUES_FILE


def procedures_path(root=None) -> Path:
    return data_dir(root) / PROCEDURES_FILE


# --------------------------------------------------------------------------
# 读文件（mtime 缓存）
# --------------------------------------------------------------------------
# 为什么缓存：这三块每轮都要拼进 system prompt，每次读盘 + 解析 JSON 是白花的
# 延迟；为什么还要 mtime 检查：文件是主人手动改的（data/*.json），改完要立刻
# 生效 —— 只按路径缓存会把改动挡住。
#
# 缓存键 = (mtime_ns, size)：内容变了但大小没变、且落在同一个 mtime 刻度上的
# 情况在 NTFS 上几乎不可能（mtime 精度 100ns）；真要严谨可以再比一次内容哈希。

_file_cache: dict[str, tuple] = {}


def reset_cache() -> None:
    """清掉文件缓存（改了文件但 mtime 没变时用；测试也用）。"""
    _file_cache.clear()


def _read_text_cached(path: Path) -> str:
    """读文本（mtime + size 没变就用缓存）。读不到 → 空串。"""
    key = str(path)
    try:
        st = path.stat()
    except OSError:
        _file_cache.pop(key, None)
        return ""
    stamp = (st.st_mtime_ns, st.st_size)
    hit = _file_cache.get(key)
    if hit is not None and (hit[0], hit[1]) == stamp:
        return hit[2]
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        log.debug("读 %s 失败，当作没有：%s", path, e)
        text = ""
    _file_cache[key] = (stamp[0], stamp[1], text)
    return text


def _read_json_cached(path: Path, default):
    """读 JSON（带缓存）。坏了 / 不存在 → 返回 default。"""
    text = _read_text_cached(path)
    if not text.strip():
        return default
    try:
        data = json.loads(text)
    except Exception as e:  # noqa: BLE001 - 坏文件当没有，绝不崩
        log.warning("解析 %s 失败，当作没有：%s", path, e)
        return default
    return data


def _write_json_atomic(path: Path, data) -> bool:
    """原子写 JSON（先写 .tmp 再 os.replace）。失败 → False。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = "%s.%d.tmp" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001 - 写不了就当没写，不崩
        log.warning("写 %s 失败：%s", path, e)
        return False
    reset_cache()
    return True


# --------------------------------------------------------------------------
# 一、身份记忆（data/identity.json）
# --------------------------------------------------------------------------
# 为什么单独一个文件、**不进 sidecar**
# ------------------------------------
# 身份是**设定**，不是「主人的一条记忆」：它不该被检索到、不该被半衰期衰减、
# 不该跟主人的琐事混在一张列表里。而且它是**常量** —— 放 sidecar 反而要多跑
# 一趟 HTTP 才能拿到一个每轮都不变的东西。

#: 兜底身份（文件不存在时用）。改身份请改 data/identity.json。
DEFAULT_IDENTITY = (
    {"id": "self.role", "text": "我是主人的桌面宠物，长期待在主人的桌面上"},
    {"id": "self.owner", "text": "主人是我唯一的对话对象，我称呼对方「主人」"},
    {"id": "self.scope", "text": "我的活儿是陪着主人、帮主人办点小事，不替主人做决定"},
)

IDENTITY_HEADER = "【我是谁】"


def load_identity(root=None) -> list:
    """身份事实列表（文件不存在 / 坏了 → 兜底默认）。

    文件形状：{"facts": [{"id": ..., "text": ..., "updated_at": ...}, ...]}
    （也接受直接一个数组）
    """
    data = _read_json_cached(identity_path(root), None)
    facts = []
    if isinstance(data, dict):
        facts = data.get("facts")
    elif isinstance(data, list):
        facts = data
    if not isinstance(facts, list) or not facts:
        return [dict(f) for f in DEFAULT_IDENTITY]
    out = []
    for f in facts:
        if isinstance(f, dict):
            text = str(f.get("text") or "").strip()
            if text:
                out.append({"id": str(f.get("id") or ""), "text": text,
                            "updated_at": str(f.get("updated_at") or "")})
        elif isinstance(f, str) and f.strip():
            out.append({"id": "", "text": f.strip(), "updated_at": ""})
    return out or [dict(f) for f in DEFAULT_IDENTITY]


def save_identity(facts, root=None) -> bool:
    """写身份文件（facts = [{"id", "text", "updated_at"}, ...]）。"""
    rows = []
    for f in facts or []:
        if isinstance(f, dict):
            text = str(f.get("text") or "").strip()
            if text:
                rows.append({"id": str(f.get("id") or ""), "text": text,
                             "updated_at": str(f.get("updated_at") or "")})
    return _write_json_atomic(identity_path(root), {"version": 1, "facts": rows})


def identity_block(root=None) -> str:
    """身份块（**常量**：只随 identity.json 变）。

    形状：
        【我是谁】
        - 我是主人的桌面宠物，长期待在主人的桌面上
    """
    facts = load_identity(root)
    if not facts:
        return ""
    lines = [IDENTITY_HEADER]
    for f in facts:
        stamp = f.get("updated_at")
        tail = "（%s 更新）" % stamp[:10] if stamp else ""
        lines.append("- %s%s" % (f["text"], tail))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 二、价值记忆（data/values.json）
# --------------------------------------------------------------------------
# 为什么价值要「一天最多改 2 条」
# ------------------------------
# 价值是桌宠的**性格骨架**：它要稳，也要能被慢慢修正。改得太勤 = 主人觉得
# 「这宠物今天一个样」；改得太慢 = 它学不会。所以给它一个**硬上限**：
# 同一天第 3 次编辑直接被拒，并把「为什么被拒」写在错误里 —— 让主人知道
# 明天再来，而不是让上限变成一句没人看的文档。

#: 价值的三个层级（顺序 = 从硬到软：底线 → 偏好 → 风格）
VALUE_LEVELS = ("bottom_line", "preference", "style")
VALUE_LEVEL_LABELS = {"bottom_line": "底线", "preference": "偏好", "style": "风格"}

#: 价值的条数区间（3~7 条：太少撑不起性格，太多就没重点了）
MIN_VALUES = 3
MAX_VALUES = 7

#: 每天最多改几条（硬要求）
MAX_VALUE_EDITS_PER_DAY = 2

VALUES_HEADER = "【我的价值】"

#: 兜底价值（文件不存在时用）。改价值请改 data/values.json。
DEFAULT_VALUES = (
    {"id": "v.truth", "level": "bottom_line",
     "content": "不编造事实：不确定就直说，查不到就说查不到"},
    {"id": "v.confirm", "level": "bottom_line",
     "content": "高风险操作（删文件、发消息、下单）先跟主人确认"},
    {"id": "v.chinese", "level": "preference",
     "content": "默认说简体中文，回复以短句为主"},
    {"id": "v.verify", "level": "preference",
     "content": "时效性的事（天气、新闻、榜单）先查再答"},
    {"id": "v.tone", "level": "style",
     "content": "像朋友一样说话，不用客服腔、不端架子"},
    {"id": "v.playful", "level": "style",
     "content": "气氛好时可以俏皮一点，正事时一本正经"},
)


class ValueEditRejected(Exception):
    """价值的编辑被拒（今天改的条数已经到上限）。

    消息里必须说清：**为什么**被拒、**今天改了什么**、**什么时候能再改**。
    """


def _values_doc(root=None) -> dict:
    """读 values.json（结构 {"values": [...], "edit_log": [...]}）。"""
    data = _read_json_cached(values_path(root), None)
    if not isinstance(data, dict):
        data = {}
    values = data.get("values")
    log_list = data.get("edit_log")
    return {
        "version": data.get("version") or 1,
        "values": values if isinstance(values, list) else [],
        "edit_log": log_list if isinstance(log_list, list) else [],
    }


def load_values(root=None) -> list:
    """价值列表（按层级顺序：底线 → 偏好 → 风格）。

    文件不存在 / 坏了 → 兜底默认。每条 = {id, level, content, updated_at}。
    """
    doc = _values_doc(root)
    rows = []
    for v in doc["values"]:
        if not isinstance(v, dict):
            continue
        content = str(v.get("content") or "").strip()
        if not content:
            continue
        level = str(v.get("level") or "").strip()
        if level not in VALUE_LEVELS:
            level = "preference"
        # ⚠️ 用 dict(v, ...) 而不是重建：主人手编的文件里可能有别的字段
        # （备注、来源……），改值的时候**别把人家写的东西抹掉**。
        rows.append(dict(
            v,
            id=str(v.get("id") or ""),
            level=level,
            content=content,
            updated_at=str(v.get("updated_at") or ""),
        ))
    if not rows:
        rows = [dict(v, updated_at="") for v in DEFAULT_VALUES]
    rows.sort(key=lambda r: VALUE_LEVELS.index(r["level"]))
    return rows


def _edits_today(edit_log, day: str) -> list:
    return [e for e in (edit_log or [])
            if isinstance(e, dict) and str(e.get("date") or "") == day]


def _reject_message(day: str, edits) -> str:
    done = "、".join(str(e.get("value_id") or e.get("id") or "?") for e in edits)
    return ("价值记忆每天最多改 %d 条，今天（%s）已经改了 %d 条"
            "（%s），这次改动被拒绝。价值是慢慢长出来的 —— 明天再来改。"
            % (MAX_VALUE_EDITS_PER_DAY, day, len(edits), done or "无"))


def _check_edit_budget(edit_log, now=None) -> None:
    """今天改的条数到上限 → 抛 ValueEditRejected（带明确原因）。"""
    now = now or datetime.datetime.now()
    day = now.strftime("%Y-%m-%d")
    edits = _edits_today(edit_log, day)
    if len(edits) >= MAX_VALUE_EDITS_PER_DAY:
        raise ValueEditRejected(_reject_message(day, edits))


def _stamp(now=None) -> str:
    now = now or datetime.datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S")


def update_value(value_id: str, content: str, *, root=None, now=None,
                 level: str = "") -> dict:
    """改一条价值（按 id）。成功 → 返回改完的那条。

    同一天第 3 次改动 → 抛 ValueEditRejected（消息说清为什么）。
    """
    content = (content or "").strip()
    if not content:
        raise ValueError("价值内容不能为空")
    doc = _values_doc(root)
    _check_edit_budget(doc["edit_log"], now=now)
    # ⚠️ 用**有效值**（load_values：文件空 → 兜底默认），别用文件里的原始行：
    # 新装的机器上文件还不存在，直接改会找不到那条、或者把默认的那几条弄丢。
    rows = load_values(root)
    target = None
    for v in rows:
        if str(v.get("id") or "") == str(value_id or ""):
            target = v
            break
    if target is None:
        raise KeyError("没有这条价值：%s" % value_id)
    target["content"] = content
    if level and level in VALUE_LEVELS:
        target["level"] = level
    target["updated_at"] = _stamp(now)
    day = (now or datetime.datetime.now()).strftime("%Y-%m-%d")
    doc["values"] = rows
    doc["edit_log"].append({"date": day, "value_id": str(value_id or ""),
                             "at": target["updated_at"]})
    if not _write_json_atomic(values_path(root), doc):
        raise IOError("价值文件写不进去：%s" % values_path(root))
    return {"id": str(value_id or ""), "level": target.get("level", ""),
            "content": content, "updated_at": target["updated_at"]}


def add_value(content: str, *, root=None, now=None, level: str = "preference",
              value_id: str = "") -> dict:
    """加一条价值。超过 MAX_VALUES 条 / 今天改太多 → 拒绝。

    「加一条」也算一次改动 —— 否则主人可以一口气塞 7 条新价值，
    日限额形同虚设。
    """
    content = (content or "").strip()
    if not content:
        raise ValueError("价值内容不能为空")
    if level not in VALUE_LEVELS:
        level = "preference"
    doc = _values_doc(root)
    _check_edit_budget(doc["edit_log"], now=now)
    rows = load_values(root)     # 有效值（文件空 → 兜底默认）
    if len(rows) >= MAX_VALUES:
        raise ValueError("价值最多 %d 条（现在 %d 条），要加就得先删一条旧的"
                         % (MAX_VALUES, len(rows)))
    stamp = _stamp(now)
    day = (now or datetime.datetime.now()).strftime("%Y-%m-%d")
    new_id = value_id or ("v.%d" % (len(rows) + 1))
    row = {"id": new_id, "level": level, "content": content,
           "updated_at": stamp}
    rows.append(row)
    doc["values"] = rows
    doc["edit_log"].append({"date": day, "value_id": new_id, "at": stamp})
    if not _write_json_atomic(values_path(root), doc):
        raise IOError("价值文件写不进去：%s" % values_path(root))
    return dict(row)


def save_values(rows, root=None) -> bool:
    """写价值文件（rows = [{"id", "level", "content", "updated_at"}, ...]）。

    这是「手动维护」那条路：主人 / 面板直接改这个文件即可。
    改完**不**算今天的编辑（那是 update_value / add_value 的账）——
    免得主人手改一次文件就把当天的额度用光了。
    """
    payload = []
    for v in rows or []:
        if not isinstance(v, dict):
            continue
        content = str(v.get("content") or "").strip()
        if not content:
            continue
        level = str(v.get("level") or "").strip()
        if level not in VALUE_LEVELS:
            level = "preference"
        payload.append({
            "id": str(v.get("id") or ""),
            "level": level,
            "content": content,
            "updated_at": str(v.get("updated_at") or ""),
        })
    doc = _values_doc(root)
    return _write_json_atomic(values_path(root),
                               {"version": doc.get("version") or 1,
                                "values": payload,
                                "edit_log": doc.get("edit_log") or []})


def value_edit_count(day: str, root=None) -> int:
    """某一天改了几条（给面板 / 测试看）。"""
    return len(_edits_today(_values_doc(root)["edit_log"], day))


def values_block(root=None) -> str:
    """价值块（**常量**：只随 values.json 变）。

    形状：
        【我的价值】（底线 → 偏好 → 风格）
        - 底线：不编造事实：不确定就直说，查不到就说查不到
    """
    rows = load_values(root)
    if not rows:
        return ""
    lines = ["%s（底线 → 偏好 → 风格）" % VALUES_HEADER]
    for v in rows:
        label = VALUE_LEVEL_LABELS.get(v["level"], v["level"])
        stamp = v.get("updated_at")
        tail = "（%s 更新）" % stamp[:10] if stamp else ""
        lines.append("- %s：%s%s" % (label, v["content"], tail))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 三、程序性记忆（data/procedures.json）
# --------------------------------------------------------------------------
# 「怎么做某件事」—— 工具序列 + 成功率。
#
# 两条路都要留着
# --------------
# ① 自动统计：从**工具调用记录**里数（同一串工具跑了几次、成功几次）。
#    ⚠️ 这条路是**离线的**：统计结果落回 data/procedures.json 这份**快照**，
#    注入只读快照 —— 否则每多一条调用记录，提示词就变一次，前缀缓存直接归零。
# ② 手动维护：直接编辑 data/procedures.json（主人自己写的套路）。
#
# 注入时**只放总览**（一行一条），别把每一步的细节铺开 —— 那是易变内容该干的事。

PROCEDURES_HEADER = "【我怎么做事】"

#: 兜底套路（文件不存在时用；成功率还没统计过）
DEFAULT_PROCEDURES = (
    {"id": "weather", "task": "查天气",
     "steps": ["web_search", "mcp__web-search-safe__live_query"]},
    {"id": "remember", "task": "记一件事", "steps": ["remember_fact"]},
    {"id": "recall", "task": "回想主人的事", "steps": ["recall_memory"]},
)


def load_procedures(root=None) -> list:
    """程序性记忆列表（文件不存在 / 坏了 → 兜底默认）。

    每条 = {id, task, steps, success_rate, samples, updated_at}。
    success_rate 是 None = 还没统计过。
    """
    data = _read_json_cached(procedures_path(root), None)
    rows = data.get("procedures") if isinstance(data, dict) else data
    out = []
    for p in rows or []:
        if not isinstance(p, dict):
            continue
        task = str(p.get("task") or "").strip()
        if not task:
            continue
        steps = [str(s).strip() for s in (p.get("steps") or []) if str(s).strip()]
        rate = p.get("success_rate")
        try:
            rate = None if rate is None else float(rate)
        except (TypeError, ValueError):
            rate = None
        out.append({
            "id": str(p.get("id") or ""),
            "task": task,
            "steps": steps,
            "success_rate": rate,
            "samples": int(p.get("samples") or 0),
            "updated_at": str(p.get("updated_at") or ""),
            "source": str(p.get("source") or "manual"),
        })
    return out or [dict(p, success_rate=None, samples=0, updated_at="",
                        source="manual") for p in DEFAULT_PROCEDURES]


def save_procedures(rows, root=None) -> bool:
    """写程序性记忆文件。"""
    payload = []
    for p in rows or []:
        if not isinstance(p, dict):
            continue
        task = str(p.get("task") or "").strip()
        if not task:
            continue
        payload.append({
            "id": str(p.get("id") or ""),
            "task": task,
            "steps": [str(s) for s in (p.get("steps") or [])],
            "success_rate": p.get("success_rate"),
            "samples": int(p.get("samples") or 0),
            "updated_at": str(p.get("updated_at") or ""),
            "source": str(p.get("source") or "manual"),
        })
    return _write_json_atomic(procedures_path(root),
                               {"version": 1, "procedures": payload})


def upsert_procedure(task: str, steps, *, root=None, success_rate=None,
                     samples=None, now=None, source: str = "manual") -> dict:
    """手动维护一条套路（按 task 覆盖写）。"""
    task = (task or "").strip()
    if not task:
        raise ValueError("套路名不能为空")
    step_list = [str(s).strip() for s in (steps or []) if str(s).strip()]
    rows = load_procedures(root)
    stamp = _stamp(now)
    row = None
    for r in rows:
        if r["task"] == task:
            row = r
            break
    if row is None:
        row = {"id": "p.%d" % (len(rows) + 1), "task": task, "steps": [],
               "success_rate": None, "samples": 0, "updated_at": "",
               "source": source}
        rows.append(row)
    row["steps"] = step_list
    row["success_rate"] = success_rate
    row["samples"] = int(samples or 0)
    row["updated_at"] = stamp
    row["source"] = source
    save_procedures(rows, root)
    return dict(row)


def procedures_from_calls(calls) -> list:
    """从**工具调用记录**统计套路（自动那条路）。

    calls 每条形如：
        {"task": "查天气",
         "tools": ["web_search", "mcp__web-search-safe__live_query"],
         "ok": True}

    同一个 (task, tools) 记成一条：samples = 次数，success_rate = 成功次数/总次数。
    """
    buckets = {}
    for c in calls or []:
        if not isinstance(c, dict):
            continue
        task = str(c.get("task") or "").strip()
        if not task:
            continue
        tools = tuple(str(t).strip() for t in (c.get("tools") or []) if str(t).strip())
        if not tools:
            continue
        key = (task, tools)
        ok = bool(c.get("ok"))
        hit = buckets.get(key)
        if hit is None:
            hit = {"task": task, "steps": list(tools), "samples": 0, "hits": 0}
            buckets[key] = hit
        hit["samples"] += 1
        if ok:
            hit["hits"] += 1
    out = []
    for (task, tools), hit in buckets.items():
        out.append({
            "id": "p.%s" % task,
            "task": task,
            "steps": list(tools),
            "samples": hit["samples"],
            "success_rate": round(hit["hits"] / float(hit["samples"]), 3)
            if hit["samples"] else None,
            "source": "derived",
        })
    return out


def load_call_records(path) -> list:
    """读工具调用记录（JSONL，一行一条）。

    没有这个文件 → 空列表（调用方自己决定要不要退回手动维护的套路）。
    """
    p = Path(path)
    rows = []
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except Exception:  # noqa: BLE001 - 坏行跳过
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def refresh_procedures(calls, *, root=None, now=None, keep_manual: bool = True):
    """把统计结果落回快照文件（**离线**操作，别在聊天路径里调）。

    keep_manual=True：手动维护的套路不被统计覆盖（主人写的比机器数的准）。
    """
    derived = procedures_from_calls(calls)
    rows = load_procedures(root)
    if keep_manual:
        manual = [r for r in rows if r.get("source") != "derived"]
    else:
        manual = []
    merged = {r["task"]: dict(r) for r in manual}
    stamp = _stamp(now)
    for d in derived:
        d["updated_at"] = stamp
        merged[d["task"]] = d
    out = list(merged.values())
    save_procedures(out, root)
    return out


def procedures_block(root=None) -> str:
    """程序性记忆块（**常量**：只随 procedures.json 变）。只放总览。"""
    rows = load_procedures(root)
    if not rows:
        return ""
    lines = ["%s（固定套路；成功率来自历史统计）" % PROCEDURES_HEADER]
    for p in rows:
        steps = " → ".join(p["steps"]) if p["steps"] else "（没写步骤）"
        rate = p.get("success_rate")
        if rate is None:
            tail = "（成功率还没统计）"
        else:
            tail = "（成功率 %d%%，%d 次）" % (int(round(rate * 100)), p.get("samples") or 0)
        lines.append("- %s：%s%s" % (p["task"], steps, tail))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 四、情感权重（arousal / valence）
# --------------------------------------------------------------------------
# 每张卡可以带两个数：
#   arousal  唤醒度（-1 平静/低落 … +1 激动/兴奋）
#   valence  效价（-1 负面 … +1 正面）
# 缺省 **0 = 中性** ⇒ 旧卡**不需要迁移**，排序完全不变（权重恒为 1.0）。
#
# 为什么衰减要慢
# --------------
# 情感记忆恰恰是**最该被记住**的那批（主人第一次说「我今天特别难受」）。
# 如果情感权重跟着检索的半衰期（fact 90 天 / topic 7 天）一起掉，
# 一张放了半年的情绪卡会被压到召不回来 —— 那等于「记了等于没记」。
# 所以这里给它**自己的、更慢的**半衰期（60 天 ≥ 要求的 30 天），
# 并且给了一个**下限**：老了最多被压 8%，绝不会掉到召不回来。

#: 情感权重的半衰期（天）。要求 ≥ 30 —— 情绪记忆衰减得比普通事实慢。
AFFECT_HALF_LIFE_DAYS = 60.0

#: 情感强度本身带来的「更难忘」加成（最大 +12%）
AFFECT_SALIENCE_GAIN = 0.12

#: 卡片情绪与当前心情**对齐**时的加成（最大 ±20%）
#:
#: 比「情感强度」那项大 —— 这样「我现在很难过，那条让我开心的旧事先放放」
#: 这种**反向**的情况真的会被压后一点（但有下限，见 AFFECT_MIN_WEIGHT）。
AFFECT_ALIGN_GAIN = 0.20

#: 权重的上下限：老了最多 -8%，最猛 +30%
AFFECT_MIN_WEIGHT = 0.92
AFFECT_MAX_WEIGHT = 1.30

#: 默认（中性）
DEFAULT_AROUSAL = 0.0
DEFAULT_VALENCE = 0.0


def _clamp_affect(value, default: float = 0.0) -> float:
    """把 arousal / valence 洗成 [-1, 1] 的浮点数（拿不到 → 默认）。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:      # NaN
        return default
    return max(-1.0, min(1.0, v))


def affect_decay(age_days: float, half_life_days: float = AFFECT_HALF_LIFE_DAYS) -> float:
    """情感权重的衰减（半衰期默认 60 天 ⇒ 30 天时还剩 71%）。"""
    try:
        age = max(0.0, float(age_days))
    except (TypeError, ValueError):
        age = 0.0
    hl = max(1.0, float(half_life_days))
    return 0.5 ** (age / hl)


def affective_weight(arousal=DEFAULT_AROUSAL, valence=DEFAULT_VALENCE, *,
                     query_arousal: float = 0.0, query_valence: float = 0.0,
                     age_days: float = 0.0) -> float:
    """一张卡的情感偏置权重（1.0 = 不偏不倚）。

    - 中性卡（arousal=valence=0）⇒ **恒等于 1.0**（旧卡不受影响）；
    - 情绪强度高 ⇒ 略微「更难忘」（最多 +12%）；
    - 与当前心情**同向** ⇒ 再加一点（最多 +20%）；**反向** ⇒ 略微靠后；
    - 随时间衰减（半衰期 60 天），但**有下限**（≥ 0.92）：老了也不会被
      压到召不回来 —— 这正是「情感记忆要慢衰减」的意义。
    """
    a = _clamp_affect(arousal)
    v = _clamp_affect(valence)
    qa = _clamp_affect(query_arousal)
    qv = _clamp_affect(query_valence)
    intensity = (abs(a) + abs(v)) / 2.0
    alignment = (a * qa + v * qv) / 2.0
    raw = 1.0 + AFFECT_SALIENCE_GAIN * intensity + AFFECT_ALIGN_GAIN * alignment
    w = 1.0 + (raw - 1.0) * affect_decay(age_days)
    return max(AFFECT_MIN_WEIGHT, min(AFFECT_MAX_WEIGHT, w))


#: 心情字符串 → 情感画像（唤醒度 / 效价）。认不出来 → 中性 (0, 0)。
MOOD_AFFECT_PROFILES = {
    "happy": (0.5, 0.8),
    "joy": (0.6, 0.85),
    "excited": (0.85, 0.7),
    "calm": (0.05, 0.35),
    "sleepy": (0.0, 0.15),
    "sad": (0.3, -0.7),
    "angry": (0.9, -0.6),
    "fear": (0.8, -0.55),
    "doubt": (0.45, -0.2),
    "pride": (0.55, 0.7),
    "shy": (0.35, 0.1),
    "surprise": (0.8, 0.15),
}


def affect_profile_from_mood(mood: str) -> tuple:
    """心情 → 情感画像 (arousal, valence)。不认识的心情 → 中性 (0, 0)。"""
    key = str(mood or "").strip().lower()
    if not key:
        return (0.0, 0.0)
    return MOOD_AFFECT_PROFILES.get(key, (0.0, 0.0))


# --------------------------------------------------------------------------
# 五、检索路由
# --------------------------------------------------------------------------
# query 长什么样 → 该多看哪一类记忆。**只调权重，不硬过滤**：
# 问事实也照样能查到事件（只是排在后面一点）—— 硬过滤会让「换个说法问」
# 直接查不到，那比答错更糟。
#
# 权重怎么用
# ----------
# 命中的那一类 ×ROUTE_BOOST（1.15），其余 ×1.0。排序时乘上去。
# 权重只影响**顺序**，不改变「哪些结果被返回」—— 这样 sidecar 那套按类别
# 标定过的闸门（memory_sidecar/price_table.py）不会被我们绕开。

#: 命中路由时的加权（+15%）
ROUTE_BOOST = 1.15

#: 时间词 → 偏「事件」
TIME_WORDS = ("昨天", "上次", "那天", "之前", "前天", "上回", "上一次",
              "前几天", "yesterday", "last time", "earlier")

#: 事实类词 → 偏「事实」
FACT_WORDS = ("是什么", "什么", "多少", "谁", "哪", "叫什么", "几岁", "哪里")

#: how-to 词 → 偏「程序性」
HOWTO_WORDS = ("怎么做", "怎么", "如何", "步骤", "how to", "how do", "教程")

#: 路由的目标 kind
ROUTE_KINDS = {"time": "event", "fact": "fact", "howto": "procedural"}


def route_intent(query: str) -> str:
    """判断 query 属于哪一路路由（time / fact / howto；都不像 → ""）。

    ⚠️ 顺序有讲究：**how-to 优先**（「怎么做」里也有「怎么」），
    其次时间词，最后事实词（「哪」太泛，会命中「哪天」这种时间问法）。
    """
    q = str(query or "").strip().lower()
    if not q:
        return ""
    if any(w in q for w in HOWTO_WORDS):
        return "howto"
    if any(w in q for w in TIME_WORDS):
        return "time"
    if any(w in q for w in FACT_WORDS):
        return "fact"
    return ""


def route_weights(query: str) -> dict:
    """query → {kind: 权重}（没命中路由 → 全是 1.0）。

    命中的那一类给 ROUTE_BOOST，其余保持 1.0（**不硬过滤**）。
    """
    weights = {kind: 1.0 for kind in ROUTE_KINDS.values()}
    intent = route_intent(query)
    if intent:
        weights[ROUTE_KINDS[intent]] = ROUTE_BOOST
    return weights


def route_weight(query: str, kind: str) -> float:
    """单个 kind 的路由权重（查不到就是 1.0）。"""
    return route_weights(query).get(str(kind or ""), 1.0)


# --------------------------------------------------------------------------
# 六、把路由 + 情感合到检索结果上
# --------------------------------------------------------------------------

def rerank_rows(rows, query: str, *, mood: str = "", now_ms: int = 0,
                route: bool = True, affect: bool = True) -> list:
    """给检索结果重新排序：**权重**调整，**不删行**。

    每行的最终分 = 原分 × 路由权重(kind) × 情感权重(arousal/valence)。

    - 路由权重：query 命中时间/事实/how-to 词 → 对应 kind ×ROUTE_BOOST；
    - 情感权重：卡片带 arousal/valence 才生效；中性卡（默认）恒为 1.0；
    - 排序按新分从高到低；行本身**不增不减**（sidecar 的闸门说了算）。

    副作用：每行的 "score" 会被换成新分，"baseScore" 保留原分。
    """
    items = [r for r in (rows or []) if isinstance(r, dict)]
    if not items:
        return []
    weights = route_weights(query) if route else {}
    qa, qv = affect_profile_from_mood(mood)
    now = int(now_ms or 0)
    out = []
    for row in items:
        try:
            base = float(row.get("score") or 0.0)
        except (TypeError, ValueError):
            base = 0.0
        w = weights.get(str(row.get("kind") or ""), 1.0)
        if affect:
            age_days = 0.0
            if now:
                try:
                    updated = float(row.get("updatedAt") or 0)
                except (TypeError, ValueError):
                    updated = 0.0
                age_days = max(0.0, (now - updated) / 86400000.0)
            w *= affective_weight(row.get("arousal"), row.get("valence"),
                                  query_arousal=qa, query_valence=qv,
                                  age_days=age_days)
        new = dict(row)
        new["baseScore"] = base
        new["score"] = round(base * w, 6)
        out.append(new)
    out.sort(key=lambda r: -float(r.get("score") or 0.0))
    return out


# --------------------------------------------------------------------------
# 七、三块常量（给 prompt_layer 用）
# --------------------------------------------------------------------------

def constant_blocks(root=None) -> list:
    """身份 / 价值 / 程序性三块（**常量**，按顺序）。

    空块直接不返回（调用方拼 prompt 时会跳过空段）。
    """
    blocks = []
    for fn in (identity_block, values_block, procedures_block):
        try:
            text = fn(root)
        except Exception as e:  # noqa: BLE001 - 坏文件不许崩聊天
            log.debug("渲染 %s 失败，跳过：%s", getattr(fn, "__name__", fn), e)
            continue
        if text and text.strip():
            blocks.append(text)
    return blocks


__all__ = [
    "AFFECT_HALF_LIFE_DAYS", "AFFECT_MAX_WEIGHT", "AFFECT_MIN_WEIGHT",
    "DATA_DIR_ENV", "DEFAULT_AROUSAL", "DEFAULT_VALENCE", "FACT_WORDS",
    "HOWTO_WORDS", "IDENTITY_FILE", "MAX_VALUES", "MAX_VALUE_EDITS_PER_DAY",
    "MIN_VALUES", "PROCEDURES_FILE", "ROUTE_BOOST", "TIME_WORDS",
    "VALUE_LEVELS", "VALUE_LEVEL_LABELS", "VALUES_FILE",
    "ValueEditRejected", "add_value", "affect_decay", "affect_profile_from_mood",
    "affective_weight", "app_root", "constant_blocks", "data_dir",
    "identity_block", "identity_path", "load_call_records", "load_identity",
    "load_procedures", "load_values", "procedures_block", "procedures_from_calls",
    "procedures_path", "refresh_procedures", "rerank_rows", "reset_cache",
    "route_intent", "route_weight", "route_weights", "save_identity",
    "save_procedures", "save_values", "update_value", "upsert_procedure",
    "value_edit_count", "values_block", "values_path",
]
