# -*- coding: utf-8 -*-
"""让「大飞鱼」按旋律唱出歌词（而不是平读）。

原理
----
RVC 的生成器是 **F0 驱动** 的：内容来自 HuBERT 特征，音高来自 F0 曲线。
常规用法里 F0 是从输入音频里提取的，所以 RVC 只能「翻唱」——源音频是念白，出来就是念白。

这里把这条链路拆开用：
  1. GPT-SoVITS 念一遍歌词        -> 提供内容（字）和她的音色
  2. 依据歌词生成一条旋律曲线     -> 每个字一个音，音高在字内保持不变（这正是「唱」和「念」的区别）
  3. 把旋律曲线灌进 RVC 替换原音高 -> 她的嗓子唱出这条旋律

实现上只改一件事：替换 `infer.vc.pipeline.Pipeline.get_f0`，
让它返回我们算好的旋律，而不是从源音频提取的音高。
"""
import argparse
import json
import os
import re
import sys
import urllib.request

import numpy as np

# RVC **代码**装在哪（RVC WebUI 本体 + 它的 venv）—— 只借它的 infer 模块。
RVC_ROOT = os.environ.get('RVC_ROOT') or r'E:\AI\RVC\Retrieval-based-Voice-Conversion-WebUI-main'

# **模型/权重在桌宠项目里**（models/rvc/）：weights / indices / rmvpe / hubert / pretrained
# —— 不再依赖 RVC 安装目录里的 assets（换机器、换安装位置都不影响）。
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJ = os.path.dirname(os.path.dirname(_HERE))
ASSETS_ROOT = os.environ.get('RVC_ASSETS_ROOT') or os.path.join(_PROJ, 'models', 'rvc')
GSV_URL = 'http://127.0.0.1:9880'
REF_AUDIO = r'E:\study\desktop-pet\voice\大飞鱼\dfy_ref.wav'
REF_TEXT = '什么嘛，这么简单的事情，你居然专门跑来问我。'

# 五声音阶（宫商角徵羽），避免生成旋律里出现听起来很怪的小二度
PENTA = [0, 2, 4, 7, 9]
# 音域锚点：E4 329.63Hz —— 大飞鱼演唱基频中位数是 336Hz，落在她的舒适区
BASE_HZ = 329.63
# 乐句走向模板（音级）
SHAPES = [
    [0, 1, 2, 3, 2, 1, 0, -1],
    [0, 2, 1, 3, 2, 4, 3, 2, 0],
    [4, 3, 2, 1, 0, 1, 2, 0],
    [0, 1, 3, 4, 5, 4, 3, 1, 0],
    [2, 3, 1, 2, 0, 1, 2, 3, 1, 0],
]
F0_MIN, F0_MAX = 190.0, 530.0


def deg_to_semi(d):
    """音级 -> 半音数（支持负音级）"""
    oct_, idx = divmod(int(d), len(PENTA))
    return PENTA[idx] + 12 * oct_


def deg_to_hz(d, base=BASE_HZ):
    hz = base * (2 ** (deg_to_semi(d) / 12.0))
    return float(min(max(hz, F0_MIN), F0_MAX))


def split_syllables(text):
    """把歌词切成音节单元：汉字一字一音节，拉丁词按词，标点单独成组用于断句。"""
    out = []
    for chunk in re.findall(r'[一-鿿]|[A-Za-z]+(?:[\'\-][A-Za-z]+)*|\d+|[^\s]', text):
        if chunk in '，。！？；：、,.!?;:…~～—-《》“”"\'()（）':
            out.append(('punct', chunk))
        elif chunk.strip():
            out.append(('syl', chunk))
    return out


def make_melody(text, seed=0):
    """生成 [音级, ...] 序列（按音节顺序），标点用于断句。"""
    rng = np.random.RandomState(seed)
    items = split_syllables(text)
    phrases = []
    cur = []
    for kind, tok in items:
        if kind == 'punct':
            if cur:
                phrases.append(cur)
                cur = []
            continue
        cur.append(tok)
    if cur:
        phrases.append(cur)

    degrees = []
    base_deg = 0
    for pi, ph in enumerate(phrases):
        n = len(ph)
        shape = SHAPES[pi % len(SHAPES)]
        # 把模板重采样到 n 个音节
        if n == 1:
            idx = np.array([float(shape[0])])
        else:
            xs = np.linspace(0, len(shape) - 1, n)
            idx = np.interp(xs, np.arange(len(shape)), np.array(shape, dtype=float))
        # 以句为单位居中：否则多句连唱会一路往下漂（末尾掉到很低的音，发闷）
        idx = idx - idx.mean()
        # 限制单句内部的起伏幅度（±3 个音级 ≈ ±4 个半音）
        idx = np.clip(idx, -3.0, 3.0)
        # 接到上一句的结束音级上，保持乐句连贯
        shift = base_deg - idx[0]
        cur_deg = [int(round(v + shift)) for v in idx]
        degrees.extend(cur_deg)
        # 下一句的起点夹在 [-2, 2]，避免整体跑调
        base_deg = int(np.clip(cur_deg[-1], -2, 2))
    if not degrees:
        degrees = [0]
    return degrees


def synth_lyrics(text, ref_audio=REF_AUDIO, ref_text=REF_TEXT, speed=1.0):
    """调 GPT-SoVITS 念一遍歌词，返回 wav 字节。"""
    payload = {
        'text': text,
        'text_lang': 'zh',
        'ref_audio_path': ref_audio,
        'prompt_text': ref_text,
        'prompt_lang': 'zh',
        'media_type': 'wav',
        'streaming_mode': False,
        'text_split_method': 'cut0',
        'speed_factor': speed,
    }
    req = urllib.request.Request(
        GSV_URL + '/tts',
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(req, timeout=600) as r:
        return r.read()


def voiced_runs(f0, min_len=3):
    runs = []
    start = None
    for i, v in enumerate(f0):
        if v > 0 and start is None:
            start = i
        elif v == 0 and start is not None:
            if i - start >= min_len:
                runs.append((start, i))
            start = None
    if start is not None and len(f0) - start >= min_len:
        runs.append((start, len(f0)))
    return runs


def build_melody_f0(f0_orig, degrees, n_syll, vibrato=0.3, glide_frames=4, base=BASE_HZ):
    """把旋律铺到与源 F0 等长的数组上。

    - 只有源音频「有声音」（f0>0）的帧才给音高，清音/静音保持 0；
    - 每个音节（用有声段近似）内部音高保持不变 —— 这是「唱」和「念」的关键差别；
    - 音与音之间做几帧滑音，避免跳变产生咔哒声；
    - 叠一点颤音让音色更接近真唱。
    """
    total = len(f0_orig)
    out = np.zeros(total, dtype=np.float64)
    runs = voiced_runs(f0_orig)
    if not runs:
        return out
    n_runs = len(runs)
    hz_of_run = []
    for j in range(n_runs):
        si = int(j * n_syll / n_runs) if n_syll > 0 else 0
        si = min(si, max(0, len(degrees) - 1))
        hz_of_run.append(deg_to_hz(degrees[si], base))
    for j, (s, e) in enumerate(runs):
        out[s:e] = hz_of_run[j]
    # 音间滑音
    for j in range(1, n_runs):
        s, _ = runs[j]
        prev_hz = hz_of_run[j - 1]
        g = min(glide_frames, max(0, s - runs[j - 1][0]))
        if g > 0:
            for k in range(g):
                idx = s - g + k
                if 0 <= idx < total:
                    w = (k + 1) / (g + 1)
                    out[idx] = prev_hz * (1 - w) + hz_of_run[j] * w
    # 颤音
    if vibrato > 0:
        t = np.arange(total) / 100.0
        vib = 2 ** (vibrato / 12.0 * np.sin(2 * np.pi * 5.5 * t))
        out = out * vib
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--text', required=True, help='歌词')
    ap.add_argument('--out', required=True, help='输出 wav 路径')
    ap.add_argument('--model', default='da_fei_yu.pth')
    ap.add_argument('--ref-audio', default=REF_AUDIO)
    ap.add_argument('--ref-text', default=REF_TEXT)
    ap.add_argument('--speed', type=float, default=0.92, help='念白语速（放慢更像唱）')
    ap.add_argument('--vibrato', type=float, default=0.3, help='颤音幅度（半音）')
    ap.add_argument('--seed', type=int, default=0)
    ap.add_argument('--base', type=float, default=BASE_HZ, help='旋律基准音高 Hz')
    ap.add_argument('--tts-wav', default='', help='已有念白 wav（给了就跳过 TTS）')
    args = ap.parse_args()

    os.chdir(RVC_ROOT)
    sys.path.insert(0, RVC_ROOT)
    # 资产**优先用项目里的** models/rvc/；没有才回退到 RVC 安装目录。
    def _assets(*parts):
        p = os.path.join(ASSETS_ROOT, *parts)
        return p if os.path.exists(p) else os.path.join(RVC_ROOT, 'assets', *parts)

    os.environ.setdefault('rmvpe_root', _assets('rmvpe'))
    os.environ.setdefault('weight_root', _assets('weights'))
    os.environ.setdefault('index_root', os.path.join(RVC_ROOT, 'logs'))
    os.environ.setdefault('outside_index_root', _assets('indices'))
    os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')


    import soundfile as sf
    import torch
    from configs.config import Config
    from infer.audio import load_audio
    from infer.vc.modules import VC
    from infer.vc.pipeline import Pipeline

    # 1) 念白
    if args.tts_wav:
        wav_path = args.tts_wav
    else:
        wav_bytes = synth_lyrics(args.text, args.ref_audio, args.ref_text, args.speed)
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        tmp = os.path.join(RVC_ROOT, 'logs', 'sing_src.wav')
        with open(tmp, 'wb') as f:
            f.write(wav_bytes)
        wav_path = tmp
    print('念白:', wav_path)

    # 2) 旋律
    degrees = make_melody(args.text, args.seed)
    n_syll = len(degrees)
    print(f'音节 {n_syll} 个，音级序列: {degrees}')

    # 3) 算 p_len（与 RVC 内部一致：16k 音频两端各 pad x_pad 秒）
    # configs.config.Config 会自己 parse_args()，会把本脚本的参数当成未知参数报错，
    # 所以先把 sys.argv 藏起来（同 infer/cli.py 的 create_config 做法）。
    _argv = sys.argv[:]
    sys.argv = [sys.argv[0]]
    try:
        config = Config()
    finally:
        sys.argv = _argv
    t_pad = 16000 * config.x_pad
    audio16 = load_audio(wav_path, 16000)
    p_len = (len(audio16) + 2 * t_pad) // 160

    # 4) 替换 get_f0：返回我们的旋律
    state = {'melody': None, 'mask': None}

    def sing_get_f0(self, x, p_len, f0_up_key, f0_method):
        if not hasattr(self, 'model_rmvpe'):
            from infer.rmvpe import RMVPE
            self.model_rmvpe = RMVPE(
                os.path.join(os.environ['rmvpe_root'], 'rmvpe.pt'),
                is_half=self.is_half, device=self.device,
            )
        f0 = self.model_rmvpe.infer_from_audio(x, thred=0.03)
        if len(f0) < p_len:
            f0 = np.pad(f0, (0, p_len - len(f0)))
        f0 = f0[:p_len]
        mel = state['melody']
        if mel is None or len(mel) < p_len:
            mel = np.pad(mel, (0, max(0, p_len - len(mel))))
        out = np.where(f0 > 0, mel[:p_len], 0.0)
        # 与原实现一致：把 0 用插值填平
        uv = out == 0
        if uv.any() and (~uv).any():
            out[uv] = np.interp(np.where(uv)[0], np.where(~uv)[0], out[~uv])
        out *= pow(2, f0_up_key / 12)
        f0bak = out.copy()
        f0_mel_min = 1127 * np.log(1 + 50 / 700)
        f0_mel_max = 1127 * np.log(1 + 1100 / 700)
        f0_mel = 1127 * np.log(1 + out / 700)
        f0_mel[f0_mel > 0] = (f0_mel[f0_mel > 0] - f0_mel_min) * 254 / (f0_mel_max - f0_mel_min) + 1
        f0_mel[f0_mel <= 1] = 1
        f0_mel[f0_mel > 255] = 255
        return np.rint(f0_mel).astype(np.int32), f0bak

    Pipeline.get_f0 = sing_get_f0

    # 5) 先算一次原始 F0 拿到「有声/无声」掩码，再铺旋律
    rmvpe_mod = __import__('infer.rmvpe', fromlist=['RMVPE'])
    rm = rmvpe_mod.RMVPE(os.path.join(os.environ['rmvpe_root'], 'rmvpe.pt'),
                         is_half=config.is_half, device=config.device)
    padded = np.pad(audio16, (t_pad, t_pad), mode='reflect')
    f0_orig = rm.infer_from_audio(padded, thred=0.03)
    if len(f0_orig) < p_len:
        f0_orig = np.pad(f0_orig, (0, p_len - len(f0_orig)))
    f0_orig = f0_orig[:p_len]
    state['melody'] = build_melody_f0(f0_orig, degrees, n_syll, vibrato=args.vibrato, base=args.base)
    voiced = float((f0_orig > 0).mean())
    print(f'有声帧占比 {voiced*100:.1f}%  旋律长度 {len(state["melody"])}  (p_len={p_len})')

    # 6) 跑 RVC（protect=0 完全采用旋律音高；index_rate=0 不用检索索引，避免索引把音高拉回源）
    vc = VC(config)
    model_path = args.model if os.path.isabs(args.model) else os.path.join(
        os.environ['weight_root'], args.model)
    vc.get_vc(os.path.basename(model_path))
    info, out_audio = vc.vc_single(
        0, wav_path, 0, 'rmvpe', '', 0.0, 0, 1.0, 0.0
    )
    print(info)
    if out_audio is None or out_audio[1] is None:
        print('RVC 推理失败')
        return 1
    sr, audio = out_audio
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    sf.write(args.out, audio, sr)
    dur = len(audio) / float(sr)
    a16 = np.asarray(audio, dtype=np.float64)
    if a16.size and np.abs(a16).max() > 2: a16 = a16 / 32768.0
    rms = float(np.sqrt((a16 ** 2).mean())) if a16.size else 0.0
    print(f'已输出 {args.out}  {dur:.2f}s  {sr}Hz  rms={rms:.4f}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
