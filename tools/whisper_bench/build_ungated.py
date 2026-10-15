"""Extra set for microphones without a gate (PV60, laptop/USB mics): noise over the
whole file, no digital zeros. Floors: u74 (white -74 dBFS), u60 (white -60 dBFS),
rumble (white -74 + low-frequency rumble -45 dBFS below ~120 Hz). -> manifest_ungated.json"""
import hashlib
import json
import os
import sys

import numpy as np
import scipy.signal as ss

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import build_bench as bb  # noqa: E402

FLOORS = ('u74', 'u60', 'rumble')


def noise(kind, n, seed):
    rng = np.random.default_rng(seed)
    if kind == 'u60':
        return rng.standard_normal(n) * 10 ** (-60 / 20)
    w = rng.standard_normal(n) * 10 ** (-74 / 20)
    if kind == 'rumble':
        b, a = ss.butter(2, 120 / 8000)
        r = ss.lfilter(b, a, rng.standard_normal(n))
        w = w + r / np.sqrt(np.mean(r ** 2)) * 10 ** (-45 / 20)
    return w


def main():
    corpus = json.load(open(os.path.join(HERE, 'corpus.json'), encoding='utf-8'))
    manifest = []
    for idx, text in enumerate(corpus, 1):
        src = bb.trim(bb.read_wav(os.path.join(HERE, 'tts', f's{idx:02d}.wav')))
        whisper = bb.whisperize(src, 1000 + idx)
        lead, tail = int(bb.LEAD_S * 16000), int(bb.TAIL_S * 16000)
        for name, (kind, level) in bb.LEVELS.items():
            base = src if kind == 'normal' else whisper
            s = base * 10 ** ((level - bb.active_db(base)) / 20)
            full = np.concatenate([np.zeros(lead), s, np.zeros(tail)])
            full = np.concatenate([full, np.zeros(-len(full) % bb.CHUNK)])
            for floor in FLOORS:
                y = full + noise(floor, len(full), 6000 + idx)
                x16 = np.clip(np.round(y * 32768), -32768, 32767)
                cond = f'{name}_{floor}'
                path = os.path.join('wavs_ungated', cond, f's{idx:02d}.wav')
                bb.write_wav(os.path.join(HERE, path), x16)
                manifest.append({'cond': cond, 'level': name, 'kind': kind, 'gate': floor, 'pop': 'nopop',
                                 'id': f's{idx:02d}', 'text': text, 'path': path,
                                 'sha256': hashlib.sha256(x16.astype('<i2').tobytes()).hexdigest()})
    json.dump(manifest, open(os.path.join(HERE, 'manifest_ungated.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=0)
    print(len(manifest), 'files')


if __name__ == '__main__':
    main()
