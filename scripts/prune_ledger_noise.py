# -*- coding: utf-8 -*-
"""清掉台账里的**测量噪声**行，只留桌宠 app 自己的会话。

为什么需要它
------------
台账 §data/token-usage.jsonl§ 是「成本看得见」的数据源。但开发期那些**一次性探针**
（临时内联脚本直接调 LLM 客户端）既没设 §DESKTOP_PET_TOKEN_LEDGER§ 指向临时文件，
也没设 session id，于是：
- 写进了**生产台账**；
- session id 落到默认的 §desktop§（§app/brain/token_ledger.py:96§ 的 §DEFAULT_SESSION§）。

实测后果：台账 122 行里 93 行是测量噪声，算出来的「最贵的一轮」（89 次请求、138 万输入
token、约 1.5 元）根本不是主人的使用 —— 面板会拿这个数字吓人。

怎么区分
--------
桌宠 app 自己的会话 id 形如 §char:<角色名>:main§（见 token_ledger 的会话 id 约定）。
本脚本**只保留**符合该约定的行，其余（§desktop§ 默认会话、探针专用的 §char:x:probe§ 之类）
全部视为噪声移除。

安全
----
- **先备份**：原文件复制到 §data/backups/token-usage.pruned-<时间戳>.jsonl§ 再动；
- **原子替换**：写临时文件再 §os.replace§；
- **被占用就明说**：台宠 app 正在运行时文件可能被占用（Windows 上 replace 会失败）——
  这时**不动原文件**，只提示「先关掉桌宠再跑」。
- §--dry-run§：只报告要删多少行，不动文件。

用法
----
    python scripts/prune_ledger_noise.py --dry-run
    python scripts/prune_ledger_noise.py
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

LEDGER = Path("data/token-usage.jsonl")
BACKUP_DIR = Path("data/backups")
#: 桌宠 app 自己的会话 id 形如 char:小汐:main
APP_SESSION_RE = re.compile(r"^char:.+:main$")


def _rows(path: Path):
    out = []
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            out.append({"__unparsed__": line})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="清理台账里的测量噪声行")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不改文件")
    ap.add_argument("--ledger", default=str(LEDGER), help="台账路径（默认 data/token-usage.jsonl）")
    args = ap.parse_args()

    path = Path(args.ledger)
    if not path.is_file():
        print(f"台账不存在：{path}")
        return 0

    rows = _rows(path)
    keep = [r for r in rows if APP_SESSION_RE.match(str(r.get("session_id") or ""))]
    drop = len(rows) - len(keep)
    print(f"台账 {path}")
    print(f"  总行数 {len(rows)}｜保留（app 自己的会话 char:*:main）{len(keep)}｜"
          f"移除（测量噪声）{drop}")
    if not keep:
        print("  一行 app 会话都没有 —— 全是噪声？先别删，用 --dry-run 之外的方式人工确认。")
        return 1
    if args.dry_run:
        print("  --dry-run：没动文件")
        return 0
    if drop == 0:
        print("  没有要清的行")
        return 0

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = BACKUP_DIR / ("token-usage.pruned-%s.jsonl" % stamp)
    shutil.copy2(path, backup)
    print(f"  已备份：{backup}")

    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for r in keep:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    try:
        os.replace(tmp, path)
    except PermissionError:
        tmp.unlink(missing_ok=True)
        print("  **台账正被占用（多半是桌宠 app 在跑）——原文件没动。**")
        print("  关掉桌宠再跑一次即可。原文件仍在 %s" % path)
        return 2
    print(f"  已清理：{path}（现在 {len(keep)} 行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
