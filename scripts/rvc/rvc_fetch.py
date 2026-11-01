# -*- coding: utf-8 -*-
"""下载一段音频 -> 可选裁剪片段 -> 可选分离人声 -> 放进桥接语音库。

给「AI 自己上网找歌并翻唱」用：AI 用 web_search 找到音频直链后调本脚本，
拿到一个**语音库内相对名**，再交给 local_voice_convert 做 RVC 音转音。

之所以一定要分离人声：RVC 会把整条音频的音高一起改，
带伴奏直接进去 = 伴奏也跟着变调，听起来是糊的；分离出干声才像翻唱。
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

RVC_ROOT = r'E:\AI\RVC\Retrieval-based-Voice-Conversion-WebUI-main'
FFMPEG = r'C:\Program Files\ffmpeg\bin\ffmpeg.exe'
MAX_BYTES = 80 * 1024 * 1024


def download(url, dest):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read(MAX_BYTES + 1)
    if not data or len(data) > MAX_BYTES:
        raise RuntimeError('下载内容为空或超过 %d MB' % (MAX_BYTES // 1048576))
    with open(dest, 'wb') as f:
        f.write(data)
    return len(data)


def run(cmd, timeout=1800):
    p = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8',
                       errors='replace', timeout=timeout, cwd=RVC_ROOT)
    if p.returncode != 0:
        tail = ((p.stdout or '') + (p.stderr or ''))[-600:]
        raise RuntimeError('命令失败: %s\n%s' % (' '.join(str(c) for c in cmd[:6]), tail))
    return p.stdout


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--url', required=True)
    ap.add_argument('--lib-dir', required=True, help='语音库目录（绝对路径）')
    ap.add_argument('--start', type=float, default=0.0, help='起始秒')
    ap.add_argument('--end', type=float, default=0.0, help='结束秒（0 = 到结尾）')
    ap.add_argument('--separate', action='store_true', help='分离人声（去伴奏）')
    ap.add_argument('--model', default='bs_roformer_anvuew_sdr_12.45.ckpt')
    ap.add_argument('--name', default='', help='输出文件名（不含扩展名）')
    ap.add_argument('--device', default='cuda')
    args = ap.parse_args()

    os.chdir(RVC_ROOT)
    sys.path.insert(0, RVC_ROOT)
    work = os.path.join(RVC_ROOT, 'logs', 'fetch')
    os.makedirs(work, exist_ok=True)
    incoming = os.path.join(args.lib_dir, 'incoming')
    os.makedirs(incoming, exist_ok=True)

    stem = args.name or ('song_' + os.urandom(4).hex())
    raw = os.path.join(work, stem + '.raw')
    n = download(args.url, raw)
    print(f'已下载 {n/1048576:.1f} MB -> {raw}')

    # 统一转成 44.1k 单声道 wav（RVC 内部会再重采样，格式统一更省事）
    wav = os.path.join(work, stem + '.wav')
    cmd = [FFMPEG, '-y', '-v', 'error']
    if args.start > 0:
        cmd += ['-ss', str(args.start)]
    if args.end > 0:
        cmd += ['-t', str(args.end - args.start)]
    cmd += ['-i', raw, '-ac', '1', '-ar', '44100', wav]
    run(cmd, timeout=300)
    print('已转码 ->', wav)

    final = wav
    if args.separate:
        outdir = os.path.join(work, stem + '_sep')
        os.makedirs(outdir, exist_ok=True)
        run([sys.executable, '-m', 'tools.pymss.cli', 'infer', args.model,
             '--input', wav, '--output', outdir, '--device', args.device, '--format', 'wav'],
            timeout=3600)
        # 优先取人声轨（vocals）；没有就退回分离目录里最大的那个 wav
        cands = []
        for root, _, files in os.walk(outdir):
            for fn in files:
                if fn.lower().endswith('.wav'):
                    fp = os.path.join(root, fn)
                    cands.append((os.path.getsize(fp), fp))
        if not cands:
            raise RuntimeError('人声分离没有产出任何 wav')
        cands.sort(reverse=True)
        pick = None
        for size, fp in cands:
            if 'vocal' in os.path.basename(fp).lower():
                pick = fp
                break
        pick = pick or cands[0][1]
        print('分离产物:', [c[1] for c in cands], '-> 选用', pick)
        final = pick

    dest = os.path.join(incoming, stem + '.wav')
    if os.path.abspath(final) != os.path.abspath(dest):
        run([FFMPEG, '-y', '-v', 'error', '-i', final, '-ac', '1', '-ar', '44100', dest], timeout=300)
    rel = os.path.join('incoming', stem + '.wav').replace('\\', '/')
    out = {'ok': True, 'name': rel, 'bytes': os.path.getsize(dest)}

    # 伴奏轨也留着：翻唱时可以把「她的嗓子 + 原曲伴奏」混回去，就是完整翻唱；
    # 只用人声轨就是清唱。两个都给，让调用方（AI）自己选。
    if args.separate:
        inst = None
        for size, fp in cands:
            # pymss 输出的伴奏轨叫 <name>_instrument.wav（是 instrument 不是 instrumental，
            # 早先按 'instrumental' 匹配找不到它，伴奏就永远没被复制出去）。
            if 'instrument' in os.path.basename(fp).lower():
                inst = fp
                break
        if inst:
            inst_dest = os.path.join(incoming, stem + '.instrumental.wav')
            run([FFMPEG, '-y', '-v', 'error', '-i', inst, '-ac', '1', '-ar', '44100', inst_dest], timeout=300)
            out['instrumental'] = os.path.join('incoming', stem + '.instrumental.wav').replace('\\', '/')
            out['instrumentalBytes'] = os.path.getsize(inst_dest)
            print('伴奏轨 ->', out['instrumental'])

    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except urllib.error.HTTPError as e:
        print(json.dumps({'ok': False, 'error': f'HTTP {e.code}'}, ensure_ascii=False))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({'ok': False, 'error': str(e)[:400]}, ensure_ascii=False))
        sys.exit(1)
