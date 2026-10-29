#!/usr/bin/env python3
r"""预取记忆检索要用的两个模型（bge-small-zh-v1.5 + bge-reranker-base）。

为什么需要这个脚本
------------------
    桌宠的语义记忆检索要两个模型：
        * BAAI/bge-small-zh-v1.5   —— 向量模型（≈ 92 MB）
        * BAAI/bge-reranker-base  —— 交叉编码器重排（≈ 1.06 GB）
    **运行时默认不自己下载**：聊天路径上悄悄下 1.2 GB 会跟聊天抢磁盘 / CPU /
    带宽，主人的体感是「桌宠变笨了」，而不是「模型在下载」。
    所以下载改成一个**显式动作**：跑一次这个脚本。

用法
----
    python scripts/prefetch_models.py                 # 缺什么下什么 + 校验
    python scripts/prefetch_models.py --check         # 只看在不在（不下载、不加载）
    python scripts/prefetch_models.py --force         # 忽略缓存，重新下载
    python scripts/prefetch_models.py --skip-verify   # 只下载，不加载校验
    python scripts/prefetch_models.py --cache-dir D:\hf   # 换缓存目录（= HF_HOME）

行为
----
    - **幂等**：模型已经在缓存里 → 跳过（不重复下载、不写任何东西）；
    - **只下运行要用的文件**：不重复下 pytorch_model.bin（跟 safetensors
      等价，光它就多 1.06 GB）；
    - **不写仓库**：只写 HuggingFace 缓存目录（认 HF_HOME / HF_HUB_CACHE）；
    - **校验**：下载完真的加载一遍（证明下好了、还能用）；
    - 没网 / 下载失败 → **明确报错并退出码 1**（不假装成功）。

退出码
------
    0 = 两个模型都在缓存里（且校验通过）；1 = 有缺失或失败。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# ⚠️ 必须早于任何 transformers / sentence_transformers 的 import
#    （本机装了 TF 2.19 + Keras 3，transformers 4.x 见到 Keras 3 直接 raise）
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_FLAX", "0")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.brain.vector_index import (  # noqa: E402
    EMBED_MODEL_ID,
    MODEL_FILES,
    RERANK_MODEL_ID,
    hf_cache_dir,
    model_cache_bytes,
    model_files_present,
)

#: 要预取的两个模型（顺序 = 打印顺序）
MODELS = (
    ("向量模型", EMBED_MODEL_ID, "embed"),
    ("重排模型", RERANK_MODEL_ID, "rerank"),
)


def _human_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024.0:
            return "%.1f %s" % (value, unit)
        value /= 1024.0
    return "%.1f TB" % value


def plan(models=MODELS, force: bool = False) -> list[dict]:
    """每个模型要干什么（**只查磁盘**，不下载、不加载）。

    返回 [{"role","id","kind","present","bytes","action"}]，
    action ∈ {"skip", "download"}。
    """
    out = []
    for role, model_id, kind in models:
        present = model_files_present(model_id)
        out.append({
            "role": role, "id": model_id, "kind": kind,
            "present": present,
            "bytes": model_cache_bytes(model_id) if present else 0,
            "action": "download" if (force or not present) else "skip",
        })
    return out


def download_one(model_id: str, force: bool = False) -> dict:
    """下载一个模型。返回 {"ok", "seconds", "bytes", "error"}。"""
    from huggingface_hub import snapshot_download  # 延迟 import：HF_HOME 要先定好
    patterns = list(MODEL_FILES.get(model_id, ()))
    if not patterns:
        return {"ok": False, "seconds": 0.0, "bytes": 0,
                "error": "不知道这个模型要哪些文件"}
    started = time.time()
    try:
        snapshot_download(model_id, allow_patterns=patterns,
                          force_download=bool(force))
    except Exception as e:  # noqa: BLE001 - 没网 / 磁盘满 / 权限
        return {"ok": False, "seconds": round(time.time() - started, 2),
                "bytes": 0, "error": "%s: %s" % (type(e).__name__, e)}
    return {"ok": True, "seconds": round(time.time() - started, 2),
            "bytes": model_cache_bytes(model_id), "error": None}


def verify_one(model_id: str, kind: str) -> dict:
    """真的加载一遍（证明下好了、还能用）。**只读缓存，不写任何东西**。"""
    started = time.time()
    try:
        if kind == "embed":
            from sentence_transformers import SentenceTransformer
            model = SentenceTransformer(model_id, local_files_only=True)
            # sentence-transformers 5.x 改名了（旧的会告警）
            getter = getattr(model, "get_embedding_dimension", None) or                 getattr(model, "get_sentence_embedding_dimension", None)
            dim = getter() if callable(getter) else None
        else:
            from sentence_transformers import CrossEncoder
            model = CrossEncoder(model_id, local_files_only=True)
            dim = None
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "seconds": round(time.time() - started, 2),
                "error": "%s: %s" % (type(e).__name__, e)}
    return {"ok": True, "seconds": round(time.time() - started, 2),
            "dim": dim, "error": None}


def check_only(models=MODELS) -> tuple[int, list[dict]]:
    """--check 模式：只报告在不在。返回 (缺失个数, 逐个结果)。"""
    rows = []
    missing = 0
    for role, model_id, _kind in models:
        present = model_files_present(model_id)
        if not present:
            missing += 1
        rows.append({"role": role, "id": model_id, "present": present,
                     "bytes": model_cache_bytes(model_id) if present else 0})
    return missing, rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="预取（下载 + 校验）记忆检索要用的两个模型")
    ap.add_argument("--check", action="store_true",
                    help="只检查模型在不在缓存里（不下载、不加载）")
    ap.add_argument("--force", action="store_true",
                    help="忽略已有缓存，强制重新下载")
    ap.add_argument("--skip-verify", action="store_true",
                    help="下载完不加载校验（更快）")
    ap.add_argument("--cache-dir", default="",
                    help="HuggingFace 缓存根目录（等价于设 HF_HOME；"
                         "真正的缓存目录是它下面的 hub/）")
    args = ap.parse_args(argv)

    if args.cache_dir:
        # ⚠️ 必须在 import huggingface_hub **之前**设好（那边是导入时算的常量）
        os.environ["HF_HOME"] = str(Path(args.cache_dir).expanduser())
        os.environ.pop("HF_HUB_CACHE", None)

    cache = hf_cache_dir()
    print("[预取] HuggingFace 缓存目录：%s" % cache)

    if args.check:
        missing, rows = check_only()
        print("")
        for row in rows:
            mark = "在" if row["present"] else "不在"
            size = ("（%s）" % _human_bytes(row["bytes"])) if row["present"] else ""
            print("  [%s] %s %s：%s%s"
                  % ("✓" if row["present"] else "✗", row["role"],
                     row["id"], mark, size))
        print("-" * 60)
        if missing:
            print("结论：缺 %d 个模型 → 跑 python scripts/prefetch_models.py 下载"
                  % missing)
            return 1
        print("结论：两个模型都在缓存里（不用下载）")
        return 0

    # —— 计划（先看清楚要干什么，再动手）——
    rows = plan(force=args.force)
    print("")
    total = len(rows)
    results = []
    failed = 0
    for n, row in enumerate(rows, 1):
        print("[%d/%d] %s %s" % (n, total, row["role"], row["id"]))
        if row["action"] == "skip":
            print("      已在缓存（%s）→ 跳过下载" % _human_bytes(row["bytes"]))
        else:
            print("      不在缓存 → 开始下载（预计几百 MB ~ 1 GB，看网速）")
            got = download_one(row["id"], force=args.force)
            if not got["ok"]:
                print("      下载失败：%s" % got["error"])
                failed += 1
                results.append({**row, "downloaded": False,
                                "download_seconds": got["seconds"],
                                "verified": False, "error": got["error"]})
                continue
            print("      下载完成：%s，用时 %.1fs"
                  % (_human_bytes(got["bytes"]), got["seconds"]))
            row["bytes"] = got["bytes"]
        if not args.skip_verify:
            chk = verify_one(row["id"], row["kind"])
            if chk["ok"]:
                extra = ("（向量维度 %d）" % chk["dim"]) if chk.get("dim") else ""
                print("      校验通过：加载成功，用时 %.1fs%s"
                      % (chk["seconds"], extra))
            else:
                print("      校验失败：%s" % chk["error"])
                failed += 1
                results.append({**row, "downloaded": row["action"] == "download",
                                "verified": False, "error": chk["error"]})
                continue
        results.append({**row, "downloaded": row["action"] == "download",
                        "verified": True, "error": None})

    print("-" * 60)
    ok = [r for r in results if r.get("verified") and not r.get("error")]
    if not ok:
        print("汇总：没有模型可用（语义检索仍然是关着的）")
        print("      没网？先让机器能上 huggingface.co，再跑一次这个脚本。")
        return 1
    total_bytes = sum(int(r.get("bytes") or 0) for r in ok)
    print("汇总：%d/%d 个模型就绪（合计 %s）"
          % (len(ok), total, _human_bytes(total_bytes)))
    if failed:
        print("      有 %d 个模型没成功 → 语义检索会退回「关键词 + TF-IDF」" % failed)
    print("下一步：打开面板（http://127.0.0.1:8766）→「健康与维护」"
          "→「重建向量索引」，语义检索就上线了。")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
