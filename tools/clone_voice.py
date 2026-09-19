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
重启桌宠即用克隆音色说话。样本建议：10~30 秒、干净人声、无背景音乐。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


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
                    help="目标音色样本音频（mp3/wav/m4a，建议共 10~60 秒）")
    default_key, default_base = _load_defaults()
    ap.add_argument("--api-key", default=default_key)
    ap.add_argument("--base-url", default=default_base)
    ap.add_argument("--group-id", default="")
    args = ap.parse_args()

    if not args.api_key:
        print("❌ 缺少 api-key（也未能在 config.yaml 的 llm.api_key 找到）")
        return 1

    import httpx

    base = args.base_url.rstrip("/")
    gid = f"?GroupId={args.group_id}" if args.group_id else ""
    headers = {"Authorization": f"Bearer {args.api_key}"}

    # 1) 上传样本（purpose=voice_clone）
    file_ids: list[int] = []
    with httpx.Client(timeout=60) as client:
        for sample in args.samples:
            p = Path(sample)
            if not p.is_file():
                print(f"❌ 样本不存在: {p}")
                return 1
            print(f"↑ 上传 {p.name} ...")
            resp = client.post(
                f"{base}/files{gid}",
                headers=headers,
                data={"purpose": "voice_clone"},
                files={"file": (p.name, p.read_bytes(),
                                "application/octet-stream")},
            )
            data = resp.json()
            base_resp = data.get("base_resp") or {}
            if base_resp.get("status_code", 0) != 0:
                print(f"❌ 上传失败: {base_resp}")
                return 1
            fid = (data.get("file") or {}).get("file_id")
            print(f"  file_id = {fid}")
            file_ids.append(int(fid))

        # 2) 克隆（样本 file_id 逗号连接挂到同一 voice_id）
        voice_id_str = ",".join(str(f) for f in file_ids)
        print(f"→ 克隆为 voice_id = {args.voice_id} ...")
        resp = client.post(
            f"{base}/voice_clone{gid}",
            headers=headers,
            json={"file_id": voice_id_str, "voice_id": args.voice_id},
        )
        data = resp.json()
        base_resp = data.get("base_resp") or {}
        if base_resp.get("status_code", 0) != 0:
            print(f"❌ 克隆失败: {base_resp}")
            return 1

    print("\n✅ 克隆完成！把下面两行填进 config.yaml 的 character: 段：")
    print("  tts_engine: minimax")
    print(f"  minimax_voice_id: {args.voice_id}")
    print("重启桌宠即可用新音色说话。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
