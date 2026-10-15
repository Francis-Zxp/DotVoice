"""Build the synthetic pen bench: 30 TTS sentences -> normal (-22/-30 dBFS) and
LPC-whispered (-52/-60/-66 dBFS) speech -> pen channel (leading digital zeros,
optional latch-on pop, -74 dBFS noise over the speech span, 10 ms-frame gate
at max(level-15, -70) dBFS, hard or soft edges) -> wavs/<cond>/sNN.wav.

Everything is seeded; the manifest records a sha256 of every file.
Channel constants are ASSUMED (not calibrated on real pen recordings)."""
import hashlib
import json
import os
import sys
import wave

import numpy as np
import scipy.signal as ss

HERE = os.path.dirname(os.path.abspath(__file__))
RATE = 16000
FRAME = 160
LEVELS = {'n22': ('normal', -22.0), 'n30': ('normal', -30.0),
          'w52': ('whisper', -52.0), 'w60': ('whisper', -60.0), 'w66': ('whisper', -66.0)}
GATES = ('hard', 'soft')
POPS = ('nopop', 'pop')
LEAD_S = 0.7          # digital zeros before the speech (microphone latching on)
TAIL_S = 0.25         # digital zeros after it (release)
POP_AT_S = 0.3
POP_LEN_S = 0.12
POP_DB = -21.0
NOISE_DB = -74.0
GATE_REL_DB = 15.0
GATE_ABS_DB = -70.0
SOFT_RAMP = 480       # samples (30 ms)
HARD_ATTACK = 160     # samples (10 ms)
LPC_ORDER = 18
CHUNK = 800           # 50 ms


def read_wav(path):
    with wave.open(path, 'rb') as r:
        assert r.getframerate() == RATE and r.getnchannels() == 1 and r.getsampwidth() == 2, path
        return np.frombuffer(r.readframes(r.getnframes()), '<i2').astype(np.float64) / 32768.0


def write_wav(path, x16):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(x16.astype('<i2').tobytes())


def frame_db(x):
    n = len(x) // FRAME
    f = x[:n * FRAME].reshape(n, FRAME)
    return 10 * np.log10(np.mean(f ** 2, axis=1) + 1e-20)


def active_db(x):
    """Speech-active RMS: 10 ms frames within 25 dB of the 95th percentile."""
    db = frame_db(x)
    keep = db > np.percentile(db, 95) - 25
    n = len(db)
    f = x[:n * FRAME].reshape(n, FRAME)[keep]
    return 10 * np.log10(np.mean(f ** 2) + 1e-20)


def trim(x):
    db = frame_db(x)
    on = np.nonzero(db > db.max() - 45)[0]
    a = max(0, (on[0] - 5) * FRAME)
    b = min(len(x), (on[-1] + 6) * FRAME)
    return x[a:b]


def levinson(r, order):
    a = np.zeros(order + 1)
    a[0] = 1.0
    err = r[0]
    for i in range(1, order + 1):
        acc = r[i] + np.dot(a[1:i], r[i - 1:0:-1])
        k = -acc / err
        a[1:i] = a[1:i] + k * a[i - 1:0:-1]
        a[i] = k
        err *= (1 - k * k)
        if err <= 0:
            err = 1e-12
            break
    return a, err


def whisperize(x, seed):
    """LPC order 18 (25 ms Hann, 10 ms hop, 0.97 pre-emphasis, 0.994 bandwidth
    expansion), white-noise excitation with the frame's residual power, then a
    2nd-order 500 Hz high-pass. No de-emphasis (whispers are tilted up)."""
    rng = np.random.default_rng(seed)
    pre = ss.lfilter([1, -0.97], [1], x)
    N, hop = 400, FRAME
    win = np.hanning(N)
    pad = np.concatenate([np.zeros(N // 2), pre, np.zeros(N)])
    out = np.zeros(len(x))
    zi = np.zeros(LPC_ORDER)
    expand = 0.994 ** np.arange(LPC_ORDER + 1)
    wnorm = np.sum(win ** 2)
    for start in range(0, len(x), hop):
        seg = pad[start:start + N] * win
        r = np.correlate(seg, seg, 'full')[N - 1:N + LPC_ORDER]
        if r[0] < 1e-12:
            a, power = np.r_[1.0, np.zeros(LPC_ORDER)], 0.0
        else:
            r = r.copy()
            r[0] *= 1 + 1e-6
            a, err = levinson(r, LPC_ORDER)
            a = a * expand
            power = err / wnorm
        n = min(hop, len(x) - start)
        exc = rng.standard_normal(n) * np.sqrt(power)
        y, zi = ss.lfilter([1.0], a, exc, zi=zi)
        out[start:start + n] = y
    b, a = ss.butter(2, 500 / (RATE / 2), 'high')
    return ss.lfilter(b, a, out)


def pop_signal(seed):
    rng = np.random.default_rng(seed)
    n = int(POP_LEN_S * RATE)
    t = np.arange(n) / RATE
    thump = np.sin(2 * np.pi * 170 * t) * np.exp(-t / 0.03)
    hiss = rng.standard_normal(n) * np.exp(-t / 0.02) * 0.3
    p = thump + hiss
    p *= 10 ** (POP_DB / 20) / np.sqrt(np.mean(p ** 2))
    return p


def gate(y, level, kind):
    thr = max(level - GATE_REL_DB, GATE_ABS_DB)
    db = frame_db(np.concatenate([y, np.zeros(-len(y) % FRAME)]))
    open_ = db > thr
    g = np.zeros(len(open_) * FRAME)
    cur = 0.0
    ramp = np.arange(1, FRAME + 1, dtype=float)
    for k, t in enumerate(open_):
        if kind == 'hard':
            # closes at once; opens over 10 ms
            seg = np.minimum(1.0, cur + ramp / HARD_ATTACK) if t else np.zeros(FRAME)
        else:
            seg = np.minimum(1.0, cur + ramp / SOFT_RAMP) if t else np.maximum(0.0, cur - ramp / SOFT_RAMP)
        g[k * FRAME:(k + 1) * FRAME] = seg
        cur = seg[-1]
    return y * g[:len(y)], thr, open_


def build():
    corpus = json.load(open(os.path.join(HERE, 'corpus.json'), encoding='utf-8'))
    manifest = []
    zeroed = {}
    for idx, text in enumerate(corpus, 1):
        src = trim(read_wav(os.path.join(HERE, 'tts', f's{idx:02d}.wav')))
        whisper = whisperize(src, 1000 + idx)
        if idx == 1 or True:
            cw = whisper / np.sqrt(10 ** (active_db(whisper) / 10)) * 10 ** (-30 / 20)
            write_wav(os.path.join(HERE, 'clean', f's{idx:02d}_whisper.wav'),
                      np.clip(np.round(cw * 32768), -32768, 32767))
        noise = np.random.default_rng(2000 + idx).standard_normal(len(src)) * 10 ** (NOISE_DB / 20)
        pop = pop_signal(3000 + idx)
        for name, (kind, level) in LEVELS.items():
            base = src if kind == 'normal' else whisper
            s = base * 10 ** ((level - active_db(base)) / 20)
            y = s + noise
            act = frame_db(s[:len(s) // FRAME * FRAME]) > active_db(s) - 25
            for g in GATES:
                gated, thr, open_ = gate(y, level, g)
                n = min(len(act), len(open_))
                zeroed.setdefault((name, g), []).append(float(np.mean(~open_[:n][act[:n]])))
                for p in POPS:
                    lead = np.zeros(int(LEAD_S * RATE))
                    if p == 'pop':
                        at = int(POP_AT_S * RATE)
                        lead[at:at + len(pop)] += pop
                    full = np.concatenate([lead, gated, np.zeros(int(TAIL_S * RATE))])
                    full = np.concatenate([full, np.zeros(-len(full) % CHUNK)])
                    x16 = np.clip(np.round(full * 32768), -32768, 32767)
                    cond = f'{name}_{g}_{p}'
                    path = os.path.join('wavs', cond, f's{idx:02d}.wav')
                    write_wav(os.path.join(HERE, path), x16)
                    manifest.append({'cond': cond, 'level': name, 'kind': kind, 'gate': g, 'pop': p,
                                     'id': f's{idx:02d}', 'text': text, 'path': path,
                                     'gate_db': thr,
                                     'sha256': hashlib.sha256(x16.astype('<i2').tobytes()).hexdigest()})
    with open(os.path.join(HERE, 'manifest.json'), 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=0)
    digest = hashlib.sha256(json.dumps([m['sha256'] for m in manifest]).encode()).hexdigest()
    print('files', len(manifest), 'manifest digest', digest)
    print('share of speech-active frames the gate zeroes:')
    for (name, g), v in sorted(zeroed.items()):
        print(f'  {name} {g}: {np.mean(v):.3f}')


if __name__ == '__main__':
    build()
