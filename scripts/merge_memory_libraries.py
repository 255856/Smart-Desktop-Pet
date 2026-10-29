# -*- coding: utf-8 -*-
"""Stage 2.6 · legacy / live 双库合并（一次性迁移脚本）—— CLI 薄壳。

**判定与落库只有一份实现**：§app/brain/memory_merge.py§。
面板（§app/web/dashboard.py§ 的 §/api/memory/merge-preview§ 与
§POST /api/maintenance/merge-memory§）调的是同一个模块，所以 CLI 报告里
看到的四类判定（duplicate / conflict / invalid / unique），面板预览里
看到的**逐行一致**。

这个脚本只干三件事：解析命令行 → 调共享实现 → 打印中文报告 / JSON。

背景
----
桌宠现在有两个记忆库并存：

* legacy 库 data/memory.db          —— 老路径留下的，运行时**不再**打开
* live 库    data/memory_<角色名>.db —— 运行中真正打开的那份

两边的 id 是**各自独立**生成的（m<uuid 前 10 位>），跨库没有共同 id 可去重，
所以只能按**语义**比对。共享模块复用记忆库自己的
MemoryStore.detect_conflicts() 与 MemoryStore._similarity()
（**不另造相似度指标**），把 legacy 每一行分成四类：

============ ======================================================= ==========
类别          含义                                                  处理
============ ======================================================= ==========
duplicate     语义重复（同一个意思）                                保留 live 那条
conflict      同主题但**取值不同**                                  两条都保留 + 打
                                                                       conflict 标记
invalid       占位符 / 没填完的模板                                  丢弃（报告里
                                                                       明确说明）
unique        live 库里没有                                          并进 live
============ ======================================================= ==========

红线（PLAN 附录 A · 前提 7）
---------------------------
PLAN 2.6 原文写的是「删除 legacy 库」，但项目红线是
「既有用户数据不动：不删除、不覆盖」，Stage 5 面板的合并端点也是**归档**。
因此本脚本**归档而不删除**：legacy 库会被**移动**到
<data-dir>/backups/memory_legacy-<时间戳>.db（字节不变，仍可打开）；
动手之前还会先把两个库各备份一份并校验可还原。

用法
----
    python scripts/merge_memory_libraries.py            # 试运行（默认，什么都不改）
    python scripts/merge_memory_libraries.py --apply    # 真合并
    python scripts/merge_memory_libraries.py --live <db> --legacy <db> --apply

退出码
------
0  成功（试运行或合并都算）
1  失败（live 库不存在 / 备份失败 / 写库失败 / 归档失败）—— 不会留下改了一半的库
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from app.brain import memory_merge as mm
except Exception as exc:  # pragma: no cover - 环境坏了才走
    print(f"导入 app.brain.memory_merge 失败：{exc}", file=sys.stderr)
    raise SystemExit(2)

# --------------------------------------------------------------------------
# 共享实现（唯一一份）—— 这里只是把它暴露成脚本自己的名字，
# 免得 §import merge_memory_libraries as M§ 的调用方找不到原来的入口。
# --------------------------------------------------------------------------

MemoryStore = mm.MemoryStore
MemoryItem = mm.MemoryItem
Row = mm.Row
Group = mm.Group

DEFAULT_SIMILARITY_THRESHOLD = mm.DEFAULT_SIMILARITY_THRESHOLD
GROUP_ALIASES = mm.GROUP_ALIASES

#: 记忆库自己的 category → kind 映射
_CATEGORY_TO_KIND = mm._CATEGORY_TO_KIND
#: 记忆库自己的 schema
_MEMORY_SCHEMA = mm._MEMORY_SCHEMA

read_rows = mm.read_rows
row_from_dict = mm.row_from_dict
rows_to_dicts = mm.rows_to_dicts
build_union_store = mm.build_union_store
is_placeholder = mm.is_placeholder
values_agree = mm.values_agree
classify = mm.classify
classify_rows = mm.classify_rows
classify_paths = mm.classify_paths
classify_items = mm.classify_items
group_to_dict = mm.group_to_dict
known_group = mm.known_group
normalize_label = mm.normalize_label
mark_conflict = mm.mark_conflict
insert_row = mm.insert_row
backup_databases = mm.backup_databases
backup_databases_zip = mm.backup_databases_zip
archive_legacy = mm.archive_legacy
apply_merge = mm.apply_merge
resolve_paths = mm.resolve_paths
character_name = mm.character_name
render_report = mm.render_report

#: 旧名字（保持脚本原来的内部调用点可用）
_sha256 = mm.sha256
_now = mm.now
_stamp = mm.stamp


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=(
            "Stage 2.6 · legacy / live 双库合并（默认试运行，什么都不改）。\n"
            "  · 默认 --dry-run：只打印「会做什么」，一个字节都不改；\n"
            "  · --apply：先备份两个库（并校验可还原），再把 legacy 独有的有效行\n"
            "    并进 live 库、给取值冲突的两条打 conflict 标记，最后把 legacy 库\n"
            "    **移动**到 <data-dir>/backups/memory_legacy-<时间戳>.db ——\n"
            "    这是**归档不是删除**（PLAN 2.6 原文写「删除」，但项目红线是\n"
            "    「既有用户数据不动：不删除、不覆盖」，Stage 5 面板也是归档）。"),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", default=None,
                    help="live 记忆库路径（默认自动找 data/memory_<角色名>.db）")
    ap.add_argument("--legacy", default=None,
                    help="legacy 记忆库路径（默认 data/memory.db）")
    ap.add_argument("--data-dir", default="data",
                    help="data 目录（默认 data；备份/归档默认放在它下面）")
    ap.add_argument("--archive-dir", default=None,
                    help="legacy 归档目录（默认 <data-dir>/backups）")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="dry_run", action="store_true",
                       help="只试运行（**默认行为**：只打印会做什么，一个字节都不改）")
    mode.add_argument("--apply", dest="apply", action="store_true",
                      help="真正执行合并（先备份，再写 live，再归档 legacy）")
    ap.add_argument("--similarity-threshold", type=float,
                    default=DEFAULT_SIMILARITY_THRESHOLD,
                    help="主题匹配阈值（默认 %(default)s）")
    ap.add_argument("--json", action="store_true",
                    help="输出机器可读的 JSON（人看的报告就不打了）")
    return ap.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    root = ROOT
    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = root / data_dir
    live_path, legacy_path = resolve_paths(root, data_dir, args.live, args.legacy)

    if not live_path.is_file():
        print(f"live 库不存在：{live_path}", file=sys.stderr)
        print("（没有 live 库就没有可合并的目标，先让桌宠写过一次记忆）",
              file=sys.stderr)
        return 1
    try:
        same = live_path.resolve() == legacy_path.resolve()
    except OSError:
        same = str(live_path).lower() == str(legacy_path).lower()
    if same:
        print("live 库和 legacy 库是同一个文件，没有独立的 live 库可合并。",
              file=sys.stderr)
        return 1
    if not legacy_path.is_file():
        msg = (f"legacy 库不存在：{legacy_path}"
               "（可能已经归档过了 —— 没什么可做的，幂等退出）")
        if args.json:
            print(json.dumps({"ok": True, "mode": "apply" if args.apply
                              else "dry-run", "noop": True, "message": msg},
                             ensure_ascii=False, indent=2))
        else:
            print(msg)
        return 0

    live_rows = read_rows(live_path)
    legacy_rows = read_rows(legacy_path)

    groups, stats = classify_rows(live_rows, legacy_rows,
                                  float(args.similarity_threshold))

    result = None
    if args.apply:
        archive_dir = Path(args.archive_dir) if args.archive_dir else (
            data_dir / "backups")
        if not archive_dir.is_absolute():
            archive_dir = root / archive_dir
        backup_dir = archive_dir / _stamp()
        result = apply_merge(live_path, legacy_path, groups, archive_dir,
                             backup_dir)
        if not result.get("ok"):
            print(result.get("error") or "合并失败", file=sys.stderr)
            return 1

    if args.json:
        print(json.dumps({
            "ok": True,
            "mode": "apply" if args.apply else "dry-run",
            "live": str(live_path),
            "legacy": str(legacy_path),
            "live_count": len(live_rows),
            "legacy_count": len(legacy_rows),
            "threshold": float(args.similarity_threshold),
            "groups": [{
                "label": g.label, "keyword": g.keyword,
                "relation": g.relation, "action": g.action,
                "similarity": round(g.similarity, 3),
                "legacy_id": g.legacy.id if g.legacy else None,
                "legacy_content": g.legacy.content if g.legacy else None,
                "live_id": g.live.id if g.live else None,
                "live_content": g.live.content if g.live else None,
                "note": g.note,
            } for g in groups],
            "result": result,
        }, ensure_ascii=False, indent=2))
        return 0

    print(render_report(groups, stats, live_path, legacy_path, len(live_rows),
                        len(legacy_rows), float(args.similarity_threshold),
                        "apply" if args.apply else "dry-run", result))
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
