#!/usr/bin/env python3
"""重建 / 修复记忆库的向量索引（Chroma + bge-small-zh-v1.5）。

为什么需要这个脚本：
    向量索引是**派生索引** —— 真相永远是 SQLite 的 `memories` 表。
    索引可以随时整个删掉重建（脚本 / 面板都有重建入口）。
    索引**只由显式重建创建**：store 自己不会建索引目录，
    所以「跑一遍测试」不会在 data/ 里留下任何东西。

用法
----
    python scripts/rebuild_vector_index.py                 # 重建当前角色的 live 库
    python scripts/rebuild_vector_index.py --all           # 重建 data/ 下所有记忆库
    python scripts/rebuild_vector_index.py --db data/memory_小汐.db
    python scripts/rebuild_vector_index.py --status         # 只看状态，不重建

行为
----
    - **幂等**：跑两次结果一样（索引是 SQLite 的函数）；
    - **不碰 data/memory*.db**：只读库、只写 data/vectors/<角色>/；
    - 模型没下载 / 没网 → **明确报错并退出码 1**（不假装成功）；
    - 第一次跑会下载模型（bge-small-zh-v1.5 ≈ 95 MB、bge-reranker-base ≈ 1.1 GB），
      下载在**后台线程**里进行时不会阻塞聊天路径（见 app/brain/vector_index.py）。
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.brain.memory import MemoryStore  # noqa: E402
from app.brain.memory_merge import character_name, resolve_paths  # noqa: E402
from app.brain.vector_index import index_dir_for  # noqa: E402


def _human_bytes(n: int) -> str:
    step = 1024.0
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < step:
            return "%.1f %s" % (value, unit)
        value /= step
    return "%.1f TB" % value


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for p in path.rglob("*"):
            if p.is_file():
                total += p.stat().st_size
    except OSError:
        pass
    return total


def rebuild_one(db_path: Path, character: str = "", quiet: bool = False) -> dict:
    """重建一个记忆库的索引。返回一份结果字典（给面板/测试用）。"""
    db_path = Path(db_path)
    started = time.time()
    if not db_path.is_file():
        return {"ok": False, "db": str(db_path), "error": "记忆库不存在"}
    store = MemoryStore(db_path, character_name=character)
    idx_dir = store.vector_index_dir
    before = _dir_size(idx_dir) if idx_dir and idx_dir.is_dir() else 0
    if not quiet:
        print("[重建] %s" % db_path)
        print("       索引目录：%s" % (idx_dir or "?"))
    report = store.rebuild_vector_index(blocking=True)
    after = _dir_size(idx_dir) if idx_dir and idx_dir.is_dir() else 0
    out = {
        "ok": bool(report.get("ok")),
        "db": str(db_path),
        "index_dir": str(idx_dir) if idx_dir else None,
        "count": report.get("count") or 0,
        "error": report.get("error"),
        "bytes_before": before,
        "bytes_after": after,
        "seconds": round(time.time() - started, 2),
    }
    if not quiet:
        if out["ok"]:
            print("       ✅ 索引 %d 行，%s，用时 %.1fs"
                  % (out["count"], _human_bytes(after), out["seconds"]))
        else:
            print("       ❌ 失败：%s" % (out["error"] or "未知原因"))
    return out


def status_one(db_path: Path, character: str = "") -> dict:
    """只看状态（不重建、不写任何东西）。"""
    db_path = Path(db_path)
    store = MemoryStore(db_path, character_name=character)
    st = store.vector_index_status()
    st["db"] = str(db_path)
    # ⚠️ 别再用 store.count()（内存里的行数）覆盖 n_source：
    #    真相是**库里的行数**（裸 SQL 写过的库，内存可能是旧的），
    #    而且覆盖完 n_source 就跟 n_indexed / state 对不上了。
    return st


def _all_dbs(data_dir: Path) -> list[Path]:
    out = []
    for p in sorted(data_dir.glob("memory*.db")):
        out.append(p)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="重建记忆库的向量索引（Chroma + bge）")
    ap.add_argument("--root", default=str(ROOT), help="项目根目录")
    ap.add_argument("--data-dir", default="data", help="数据目录（相对 root）")
    ap.add_argument("--db", default="", help="指定记忆库（默认当前角色的 live 库）")
    ap.add_argument("--character", default="", help="角色名（决定索引目录）")
    ap.add_argument("--all", action="store_true", help="重建 data/ 下所有记忆库")
    ap.add_argument("--status", action="store_true", help="只看状态，不重建")
    ap.add_argument("--quiet", action="store_true", help="不打印逐条进度")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    root = Path(args.root)
    data_dir = Path(args.data_dir)
    if not data_dir.is_absolute():
        data_dir = root / data_dir

    if args.all:
        dbs = _all_dbs(data_dir)
        if not dbs:
            print("data/ 下没有记忆库（%s）" % data_dir)
            return 1
    else:
        if args.db:
            db = Path(args.db)
            if not db.is_absolute():
                db = root / db
            dbs = [db]
        else:
            live, _legacy = resolve_paths(root, data_dir, None, None)
            dbs = [live]
        character = args.character or (character_name(root) if not args.db else "")

    results = []
    for db in dbs:
        if args.status:
            st = status_one(db, args.character)
            print("%-46s 状态=%-10s 已索引=%s 源=%s 漂移=%s %s"
                  % (db.name, st.get("state"), st.get("n_indexed"),
                     st.get("n_source"), st.get("drift"),
                     ("错误：" + str(st.get("error"))) if st.get("error") else ""))
            results.append({"ok": True, "status": st})
        else:
            results.append(rebuild_one(db, args.character, quiet=args.quiet))

    failed = [r for r in results if not r.get("ok")]
    if not args.quiet:
        print("-" * 60)
        print("汇总：%d 个记忆库，成功 %d，失败 %d"
              % (len(results), len(results) - len(failed), len(failed)))
        for r in failed:
            print("  ❌ %s：%s" % (r.get("db"), r.get("error") or "未知原因"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
