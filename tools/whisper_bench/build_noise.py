"""Noise-only sessions (nobody speaks): false 'speech' here means a silent recording
escapes the cancel. 10 seeds x conditions, 3 s each. -> manifest_noise.json
  pen_zeros   digital zeros only (pen latched on, nobody talks)
  pen_pop     zeros + latch-on pop at 0.3 s
  pen_gated   -74 dBFS noise through the gate at -70 (a few frames leak)
  pen_bumps   zeros + 3 handling bumps (40 ms, -40 dBFS, low thump) through the gate
  u74/u60/rumble  ungated floors as in build_ungated.py
  u60_step    -60 dBFS noise that steps up 8 dB at 1.5 s
  u60_clicks  -60 dBFS noise + 6 clicks (5 ms, -25 dBFS)"""
import hashlib
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import build_bench as bb  # noqa: E402
import build_ungated as bu  # noqa: E402

N = 48000


def make(kind, seed):
    rng = np.random.default_rng(seed)
    x = np.zeros(N)
    if kind == 'pen_pop':
        p = bb.pop_signal(seed)
        x[4800:4800 + len(p)] += p
    elif kind == 'pen_gated':
        y = rng.standard_normal(N - 11200) * 10 ** (-74 / 20)
        g, _, _ = bb.gate(y, -55.0, 'hard')
        x[11200:] = g
    elif kind == 'pen_bumps':
        for at in (12000, 24000, 36000):
            t = np.arange(640) / 16000
            b = np.sin(2 * np.pi * 90 * t) * np.exp(-t / 0.012) + rng.standard_normal(640) * 0.2 * np.exp(-t / 0.005)
            b *= 10 ** (-40 / 20) / np.sqrt(np.mean(b ** 2))
            x[at:at + 640] += b
        x, _, _ = bb.gate(x, -55.0, 'hard')
    elif kind in ('u74', 'u60', 'rumble'):
        x = bu.noise(kind, N, seed)
    elif kind == 'u60_step':
        x = rng.standard_normal(N) * 10 ** (-60 / 20)
        x[24000:] *= 10 ** (8 / 20)
    elif kind == 'u60_clicks':
        x = rng.standard_normal(N) * 10 ** (-60 / 20)
        for at in rng.integers(4000, N - 200, 6):
            x[at:at + 80] += rng.standard_normal(80) * 10 ** (-25 / 20)
    return x


def main():
    kinds = ('pen_zeros', 'pen_pop', 'pen_gated', 'pen_bumps', 'u74', 'u60', 'rumble', 'u60_step', 'u60_clicks')
    manifest = []
    for kind in kinds:
        for s in range(10):
            x16 = np.clip(np.round(make(kind, 7000 + s) * 32768), -32768, 32767)
            path = os.path.join('wavs_noise', kind, f'n{s:02d}.wav')
            bb.write_wav(os.path.join(HERE, path), x16)
            manifest.append({'cond': kind, 'level': 'noise', 'kind': 'noise', 'gate': kind, 'pop': 'nopop',
                             'id': f'n{s:02d}', 'text': '', 'path': path,
                             'sha256': hashlib.sha256(x16.astype('<i2').tobytes()).hexdigest()})
    json.dump(manifest, open(os.path.join(HERE, 'manifest_noise.json'), 'w', encoding='utf-8'), indent=0)
    print(len(manifest), 'files')


if __name__ == '__main__':
    main()
