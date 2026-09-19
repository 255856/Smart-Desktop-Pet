"""MiniMax 声音克隆：上传目标音色样本 → 生成自定义 voice_id（方案 A）。

用法：
    python tools/clone_voice.py --voice-id my_pet_voice --samples a.mp3 b.wav
    可选：--api-key（默认读 config.yaml 的 llm.api_key）
          --base-url（默认 https://api.minimaxi.com/v1）
          --group-id（MiniMax 控制台的 GroupId，部分账号需要）

成功后把 voice_id 填进 config.yaml：
    character:
      tts_engine: minimax
      minimax_voice_id: my_pet_voice
重启桌宠即用克隆音色说话。样本建议：每段 10 秒以上、干净人声、无背景音乐。
（也可以在 config.yaml 配 character.minimax_samples 指向样本目录，
桌宠启动时自动克隆，无需手动跑本工具。）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.voice.minimax_tts import SAMPLE_EXTS, clone_voice  # noqa: E402


def _load_defaults():
    """从 config.yaml 读 llm.api_key / llm.base_url 作为默认值。"""
    try:
        import yaml
        cfg = yaml.safe_load(
            (Path(__file__).resolve().parent.parent / "config.yaml")
            .read_text(encoding="utf-8")) or {}
        llm = cfg.get("llm") or {}
        return llm.get("api_key", ""), llm.get("base_url",
                                               "https://api.minimaxi.com/v1")
    except Exception:  # noqa: BLE001
        return "", "https://api.minimaxi.com/v1"


def main() -> int:
    ap = argparse.ArgumentParser(description="MiniMax 声音克隆")
    ap.add_argument("--voice-id", required=True,
                    help="自定义 voice_id（如 my_pet_voice），填回 config.yaml 用")
    ap.add_argument("--samples", nargs="+", required=True,
                    help="目标音色样本音频/目录（每段建议 10 秒以上）")
    default_key, default_base = _load_defaults()
    ap.add_argument("--api-key", default=default_key)
    ap.add_argument("--base-url", default=default_base)
    ap.add_argument("--group-id", default="")
    args = ap.parse_args()

    if not args.api_key:
        print("❌ 缺少 api-key（也未能在 config.yaml 的 llm.api_key 找到）")
        return 1

    paths = [Path(s) for s in args.samples
             if Path(s).suffix.lower() in SAMPLE_EXTS]
    if not paths:
        print("❌ 未找到有效音频样本（支持 wav/mp3/m4a/flac）")
        return 1

    print(f"共 {len(paths)} 段样本，开始克隆 → voice_id = {args.voice_id}")
    try:
        clone_voice(args.api_key, args.voice_id, paths,
                    base_url=args.base_url, group_id=args.group_id)
    except Exception as e:  # noqa: BLE001
        print(f"❌ 克隆失败: {e}")
        return 1

    print("\n✅ 克隆完成！把下面两行填进 config.yaml 的 character: 段：")
    print("  tts_engine: minimax")
    print(f"  minimax_voice_id: {args.voice_id}")
    print("重启桌宠即可用新音色说话。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
