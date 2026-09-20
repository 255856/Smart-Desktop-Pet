"""把 assets/sprites/<中文名>/*.png 里的「电脑桌面壁纸图片生成 (N).png」
按数字括号排序、重命名为 VPet 同款 ``{block}_{idx}_{ms}.png``。

PR 删除透明化功能：你已经有自己的方式生成透明背景 PNG，本工具不再做白底
转透明。需要时单独写脚本处理。

子状态 A / B / C 切分（按帧数自动均分）：
    帧数 ≤ 6：全 A
    帧数 7-12：A 前半 / B 后半
    帧数 > 12：A / B / C 三等分

帧间隔默认 50 ms（20 fps）—— 配合"每动作 ~81 张图"的密集动画，20 fps 才丝滑。
可通过 ``--retune-ms`` 或 ``--retune-fps`` 改写所有帧文件名的 ms 段。

typo 修正：把 ``模身体害羞`` 重命名为 ``摸身体害羞``。

用法：
    python tools/rename_to_vpet.py                       # 重命名 + typo（默认 50 ms）
    python tools/rename_to_vpet.py --retune-fps 20      # 一刀切到 50 ms
    python tools/rename_to_vpet.py --retune-ms 80        # 一刀切到 80 ms（12.5 fps）
    python tools/rename_to_vpet.py --dry                 # 只看，不真改
"""
from __future__ import annotations

import argparse
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

# 默认 50 ms = 20 fps。子状态细分；按目录查表，未列入走 DEFAULT_MS。
DEFAULT_MS_BY_CATEGORY: dict[str, int] = {
    # 走路 / 待机 / 待机动作 → 20 fps
    "默认": 50, "待机": 50,
    "待机动作1_歪头": 50, "待机动作2_打哈欠": 50, "待机动作3_摇尾巴": 50,
    "向左走": 50, "向右走": 50,
    "眨眼": 60,
    # 表情类：略慢（每帧停留久一点）
    "害怕": 80, "害羞": 80, "开心": 80, "得意": 80,
    "悲伤": 80, "惊讶": 80, "生气": 80, "疑惑": 80,
    # 触摸 / 一次性：稍快
    "摸头害羞": 50, "摸身体害羞": 50, "触摸尾巴": 50,
    "伸懒腰": 50, "起跳": 50, "转圈圈": 50,
}
DEFAULT_MS = 50   # 20 fps

# typo 修正表（一次性，跑过之后目录名就对了）
RENAME_DIRS: dict[str, str] = {
    "模身体害羞": "摸身体害羞",
}

NUM_RE = re.compile(r"(\d+)")


def _sort_key(path: Path) -> tuple[int, str]:
    """按文件名字里「最后一个数字括号」排序，没有 = 0。

    例：
        电脑桌面壁纸图片生成.png         -> (0, '...')
        电脑桌面壁纸图片生成 (1).png      -> (1, '...')
        电脑桌面壁纸图片生成 (10).png     -> (10, '...')
    """
    nums = NUM_RE.findall(path.stem)
    n = int(nums[-1]) if nums else 0
    return (n, path.name)


def _pick_block(idx: int, total: int) -> str:
    """根据帧位置返回 A/B/C。"""
    if total <= 6:
        return "A"
    if total <= 12:
        return "A" if idx < total // 2 else "B"
    third = total // 3
    if idx < third:
        return "A"
    if idx < 2 * third:
        return "B"
    return "C"


def rename_one_dir(directory: Path) -> list[tuple[Path, Path]]:
    """对一个目录做重命名。返回 [(old, new), ...]（new 不存在才执行）。"""
    files = [p for p in directory.glob("*.png") if p.is_file()]
    if not files:
        return []
    files.sort(key=_sort_key)
    total = len(files)
    ms = DEFAULT_MS_BY_CATEGORY.get(directory.name, DEFAULT_MS)

    renames: list[tuple[Path, Path]] = []
    for idx, f in enumerate(files):
        block = _pick_block(idx, total)
        new_name = f"{block}_{idx:03d}_{ms}.png"
        new_path = directory / new_name
        if new_path.exists() and new_path != f:
            log.warning("目标已存在，跳过：%s -> %s", f.name, new_path.name)
            continue
        renames.append((f, new_path))

    # 两阶段 rename：先全部 rename 到临时名再 final
    tmp_suffix = ".tmp_rename"
    for old, new in renames:
        tmp = old.with_suffix(old.suffix + tmp_suffix)
        old.rename(tmp)
    for old, new in renames:
        tmp = old.with_suffix(old.suffix + tmp_suffix)
        tmp.rename(new)
    return renames


def retune_ms(base: Path, new_ms: int) -> int:
    """把所有 PNG 文件名里「_<ms>.png」末段改成 new_ms。

    例：A_000_125.png → A_000_85.png (new_ms=85)
    """
    n = 0
    for f in base.rglob("*.png"):
        stem = f.stem
        # 末段必须是数字（ms）
        parts = stem.split("_")
        if len(parts) < 3:
            continue
        if not parts[-1].isdigit():
            continue
        new_stem = "_".join(parts[:-1] + [str(new_ms)])
        new_path = f.with_name(new_stem + f.suffix)
        if new_path != f:
            f.rename(new_path)
            n += 1
    return n


def fix_typo(base: Path) -> int:
    """把目录名 typo 修正。"""
    fixed = 0
    for old, new in RENAME_DIRS.items():
        op = base / old
        np = base / new
        if op.is_dir() and not np.exists():
            op.rename(np)
            log.info("typo 修正：%s -> %s", old, new)
            fixed += 1
        elif op.is_dir() and np.exists():
            log.warning("typo 修正失败，目标已存在：%s -> %s", old, new)
    return fixed


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="重命名 sprite 帧到 VPet 同款命名")
    p.add_argument(
        "--base",
        default=r"E:\study\desktop-pet\assets\sprites",
        help="sprite 根目录",
    )
    p.add_argument("--dry", action="store_true", help="只看，不真改名")
    p.add_argument("--retune-ms", type=int, default=None,
                   help="把帧 PNG 文件名里的 <ms> 段一刀切到这个值（与 --retune-fps 互斥）")
    p.add_argument("--retune-fps", type=float, default=None,
                   help="按 fps 自动算 ms 一刀切（如 20 → 50 ms）。与 --retune-ms 互斥")
    return p.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = parse_args()

    base = Path(args.base)
    if not base.is_dir():
        log.error("目录不存在：%s", base)
        return

    # retune 模式：仅改 ms 段，其它啥也不做
    if args.retune_ms is not None or args.retune_fps is not None:
        if args.retune_ms is not None and args.retune_fps is not None:
            log.error("--retune-ms 与 --retune-fps 互斥，只能选一个")
            return
        if args.retune_fps is not None:
            new_ms = max(1, int(round(1000.0 / args.retune_fps)))
            print(f"按 {args.retune_fps} fps → {new_ms} ms")
        else:
            new_ms = args.retune_ms
        n = retune_ms(base, new_ms)
        print(f"✅ retune 完成：{n} 张 PNG 的 ms 段已改为 {new_ms}（约 {1000.0/new_ms:.1f} fps）")
        return

    # 重命名 + typo 流程
    if not args.dry:
        fix_typo(base)

    subdirs = sorted(d for d in base.iterdir() if d.is_dir())
    total_dirs = 0
    total_renamed = 0
    print("=" * 80)
    print(f"{'目录':<24}{'帧数':>5}    默认 ms")
    print("-" * 80)
    for sub in subdirs:
        pngs = list(sub.glob("*.png"))
        if not pngs:
            continue
        ms = DEFAULT_MS_BY_CATEGORY.get(sub.name, DEFAULT_MS)
        print(f"{sub.name:<24}{len(pngs):>5}    {ms} ms")
        total_dirs += 1
        if not args.dry:
            rename_one_dir(sub)
            total_renamed += 1
    print("=" * 80)
    if args.dry:
        print(f"[dry-run] 将处理 {total_dirs} 个目录")
    else:
        print(f"✅ 重命名完成：{total_renamed} 个目录")


if __name__ == "__main__":
    main()
