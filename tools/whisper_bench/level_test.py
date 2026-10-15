"""Is the SenseVoice proxy level-sensitive? Same whispers, pure gain 0/+20/+36 dB,
in three channel versions:
  ungated  - speech + -74 dBFS noise over the whole file (no digital zeros at all)
  gated    - the bench's gated files (digital zeros around and inside the speech)
  gated+cn - the gated files with -64 dBFS white noise added AFTER the gain everywhere
             (what v2's comfort noise does to a lifted session)
Prints mean CER per (version, level, gain) and the paired bootstrap of +20/+36 vs 0."""
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import build_bench as bb  # noqa: E402
import run_bench as rb  # noqa: E402

GAINS = (0, 20, 36)


def q16(x):
    return np.clip(np.round(x * 32768), -32768, 32767).astype('<i2').tobytes()


def main():
    corpus = json.load(open(os.path.join(HERE, 'corpus.json'), encoding='utf-8'))
    manifest = json.load(open(os.path.join(HERE, 'manifest.json'), encoding='utf-8'))
    items = []   # (version, level, gain, idx, pcm)
    cn = np.random.default_rng(5).standard_normal(16000 * 10) * 10 ** (-64 / 20)
    for idx, text in enumerate(corpus, 1):
        src = bb.trim(bb.read_wav(os.path.join(HERE, 'tts', f's{idx:02d}.wav')))
        whisper = bb.whisperize(src, 1000 + idx)
        for level_name in ('w52', 'w60', 'w66'):
            level = bb.LEVELS[level_name][1]
            s = whisper * 10 ** ((level - bb.active_db(whisper)) / 20)
            rng = np.random.default_rng(4000 + idx)
            lead, tail = int(bb.LEAD_S * 16000), int(bb.TAIL_S * 16000)
            full = np.concatenate([np.zeros(lead), s, np.zeros(tail)])
            full = full + rng.standard_normal(len(full)) * 10 ** (bb.NOISE_DB / 20)
            for g in GAINS:
                items.append(('ungated', level_name, g, idx, q16(full * 10 ** (g / 20))))
    for m in manifest:
        if m['kind'] != 'whisper' or m['pop'] != 'nopop':
            continue
        pcm = rb.read_pcm(m['path'])
        x = np.frombuffer(pcm, '<i2').astype(np.float64) / 32768
        idx = int(m['id'][1:])
        for g in GAINS:
            y = x * 10 ** (g / 20)
            items.append((f"gated_{m['gate']}", m['level'], g, idx, q16(y)))
            items.append((f"gated_{m['gate']}+cn", m['level'], g, idx, q16(y + cn[:len(y)])))
    asr = rb.ASR()
    hyps = asr.recognise([it[4] for it in items])
    asr.save()
    table = {}
    for it, hyp in zip(items, hyps):
        table[(it[0], it[1], it[2], it[3])] = rb.cer(corpus[it[3] - 1], hyp)
    rng = np.random.default_rng(7)
    versions = sorted({k[0] for k in table})
    print(f"{'version':16s} {'level':5s} " + ' '.join(f'{"+%d dB" % g:>8s}' for g in GAINS)
          + '   (+20)-(0) [95% CI]        (+36)-(0) [95% CI]')
    for v in versions:
        for lv in ('w52', 'w60', 'w66', 'all'):
            lvls = ('w52', 'w60', 'w66') if lv == 'all' else (lv,)
            cols = {g: np.array([table[(v, l, g, i)] for l in lvls for i in range(1, 31)]) for g in GAINS}
            line = f'{v:16s} {lv:5s} ' + ' '.join(f'{cols[g].mean():8.3f}' for g in GAINS)
            for g in GAINS[1:]:
                d = cols[g] - cols[0]
                idx = rng.integers(0, len(d), (10000, len(d)))
                lo, hi = np.percentile(d[idx].mean(axis=1), [2.5, 97.5])
                line += f'   {d.mean():+.4f} [{lo:+.4f},{hi:+.4f}]'
            print(line)


if __name__ == '__main__':
    main()
