# -*- coding: utf-8 -*-
"""memory_sidecar 的命令行入口：python -m memory_sidecar <cmd> [选项]。

    init                    建数据目录 + 空卡库（幂等）
    upsert --file cards.json
    search --query "..." [--top-k 8] [--mood X] [--kind Y]
    delete --ids id1,id2
    stats
    serve --port 8788       常驻 HTTP（模型只加载一次，推荐）

所有命令都把结果打成一行 JSON（UTF-8），方便桥接 / 脚本直接 parse。
"""
import argparse
import json
import sys

from .server import DEFAULT_HOST, DEFAULT_PORT, MemorySidecarServer
from .store import (
    DEFAULT_DATA_DIR,
    DEFAULT_DENSE_KEEP_RATIO,
    DEFAULT_MIN_SCORE_RATIO,
    DEFAULT_MIN_SIMILARITY,
    DEFAULT_MODEL_BUDGET,
    MemoryStore,
)


def _harden_stdio():
    """Windows 控制台默认 GBK：CJK / emoji 打不出来会直接抛 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _print(obj):
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def _build_store(args):
    return MemoryStore(
        data_dir=args.data_dir,
        use_vector=not args.no_vector,
        use_bm25=not args.no_bm25,
        use_rerank=not args.no_rerank,
        # None = 没传 → 交给环境变量 / 默认值（store 里那套优先级）
        model_budget=args.model_budget,
        min_score_ratio=args.min_score_ratio,
        min_similarity=args.min_similarity,
        dense_keep_ratio=args.dense_keep_ratio,
    )


def _read_cards(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        return data.get("cards") or []
    return data if isinstance(data, list) else []


def _cmd_init(args):
    store = _build_store(args)
    stats = store.init()
    _print({"ok": True, "dataDir": store.data_dir, "stats": stats})
    return 0


def _cmd_upsert(args):
    if not args.file:
        _print({"ok": False, "error": "upsert 需要 --file cards.json"})
        return 2
    store = _build_store(args)
    store.load()
    n = store.upsert(_read_cards(args.file))
    _print({"ok": True, "upserted": n, "stats": store.stats()})
    return 0


def _cmd_search(args):
    store = _build_store(args)
    store.load()
    res = store.search(
        args.query,
        top_k=args.top_k,
        mood=args.mood or None,
        kind=args.kind or None,
        key=args.key or None,
    )
    _print({"ok": not res.get("error"), **res})
    return 0


def _cmd_delete(args):
    store = _build_store(args)
    store.load()
    ids = [i.strip() for i in (args.ids or "").split(",") if i.strip()]
    n = store.delete(ids)
    _print({"ok": True, "deleted": n, "stats": store.stats()})
    return 0


def _cmd_stats(args):
    store = _build_store(args)
    store.load()
    _print({"ok": True, "stats": store.stats()})
    return 0


def _cmd_serve(args):
    store = _build_store(args)
    store.load()
    srv = MemorySidecarServer(
        store, host=args.host, port=args.port, warmup=not args.no_warmup
    )
    srv.serve_forever()
    return 0


def build_parser():
    ap = argparse.ArgumentParser(
        prog="python -m memory_sidecar",
        description="桌宠记忆 sidecar（JSON 记忆卡 + Chroma 向量 + BM25 关键词 + 精排）",
    )
    ap.add_argument(
        "cmd",
        choices=["init", "upsert", "search", "delete", "stats", "serve"],
        help="子命令",
    )
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="数据目录（默认 %s）" % DEFAULT_DATA_DIR)
    ap.add_argument("--file", default="", help="upsert 的卡片文件（JSON 数组或 {cards: [...]}）")
    ap.add_argument("--query", default="", help="检索语句")
    ap.add_argument("--top-k", type=int, default=8, help="返回条数（默认 8）")
    ap.add_argument("--mood", default="", help="当前情绪（命中同情绪卡有 1.15 倍加成）")
    ap.add_argument("--kind", default="", help="只看某个 kind（fact / event / topic / thought / impression）")
    ap.add_argument("--key", default="", help="只看某个 key（会话 / 人物标识）")
    ap.add_argument("--ids", default="", help="delete 的 id 列表，逗号分隔")
    ap.add_argument("--host", default=DEFAULT_HOST, help="serve 监听地址（默认 127.0.0.1）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help="serve 端口（默认 %d）" % DEFAULT_PORT)
    ap.add_argument("--no-vector", action="store_true", help="关掉向量腿（只走 BM25）")
    ap.add_argument("--no-bm25", action="store_true", help="关掉 BM25 腿（只走向量）")
    ap.add_argument("--no-rerank", action="store_true", help="关掉精排（只走 RRF 粗排）")
    ap.add_argument("--no-warmup", action="store_true", help="serve 启动后不预热模型")
    # 检索的两个旋钮（不传 = 环境变量 → 默认值；0 是「关掉」的意思，不是「没传」）
    ap.add_argument("--model-budget", type=float, default=None,
                    help="检索里「模型那一路」的时间预算（秒，默认 %.1f；0 = 不限时）"
                         % DEFAULT_MODEL_BUDGET)
    ap.add_argument("--min-score-ratio", type=float, default=None,
                    help="相关度闸门：最终分低于「顶行 × 该值」的行不返回（默认 %.2f）"
                         % DEFAULT_MIN_SCORE_RATIO)
    ap.add_argument("--min-similarity", type=float, default=None,
                    help="门禁：最像的那条卡余弦相似度低于它 → 整轮不给（默认 %.2f）"
                         % DEFAULT_MIN_SIMILARITY)
    ap.add_argument("--dense-keep-ratio", type=float, default=None,
                    help="语义保命符：cos ≥ 顶行 × 该值的行即使精排分低也保留（默认 %.2f）"
                         % DEFAULT_DENSE_KEEP_RATIO)
    return ap


def main(argv=None):
    _harden_stdio()
    args = build_parser().parse_args(argv)
    handlers = {
        "init": _cmd_init,
        "upsert": _cmd_upsert,
        "search": _cmd_search,
        "delete": _cmd_delete,
        "stats": _cmd_stats,
        "serve": _cmd_serve,
    }
    try:
        return handlers[args.cmd](args)
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        _print({"ok": False, "error": "%s: %s" % (type(e).__name__, e)})
        return 1


if __name__ == "__main__":
    sys.exit(main())
