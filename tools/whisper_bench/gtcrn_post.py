"""--post stage: GTCRN streaming denoiser (sherpa-onnx OnlineSpeechDenoiser, gtcrn_simple.onnx)
after the DSP, with observation adding: y = enhanced + ALPHA * original (aligned to the
denoiser's delay), PREGAIN dB applied before the denoiser and taken off after.
Env: GTCRN_ALPHA (default 0.3), GTCRN_PREGAIN_DB (default 0), GTCRN_CHUNK (samples, 800)."""
import os

import numpy as np
import sherpa_onnx

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = os.path.join(HERE, 'models', 'gtcrn_simple.onnx')
ALPHA = float(os.environ.get('GTCRN_ALPHA', '0.3'))
PREGAIN = 10 ** (float(os.environ.get('GTCRN_PREGAIN_DB', '0')) / 20)
CHUNK = int(os.environ.get('GTCRN_CHUNK', '800'))
_cfg = sherpa_onnx.OnlineSpeechDenoiserConfig(
    model=sherpa_onnx.OfflineSpeechDenoiserModelConfig(
        gtcrn=sherpa_onnx.OfflineSpeechDenoiserGtcrnModelConfig(model=MODEL), num_threads=1))
_den = sherpa_onnx.OnlineSpeechDenoiser(_cfg)
DELAY = None


def enhance(x):
    """Streaming: CHUNK samples at a time, then flush. Returns all enhanced samples."""
    _den.reset()
    out = []
    for k in range(0, len(x), CHUNK):
        out.append(np.asarray(_den.run(x[k:k + CHUNK].astype(np.float32), 16000).samples, dtype=np.float32))
    out.append(np.asarray(_den.flush().samples, dtype=np.float32))
    return np.concatenate(out)


def measure_delay():
    rng = np.random.default_rng(3)
    t = np.arange(32000) / 16000
    x = (rng.standard_normal(32000) * 0.05 * (np.sin(2 * np.pi * 3 * t) > 0)).astype(np.float32)
    y = enhance(x)
    n = min(len(x), len(y))
    best = max(range(0, 1200), key=lambda d: float(np.dot(x[:n - d], y[d:n])))
    return best


def post(pcm):
    global DELAY
    if DELAY is None:
        DELAY = measure_delay()
    x = np.frombuffer(pcm, '<i2').astype(np.float32) / 32768.0
    y = enhance(x * PREGAIN) / PREGAIN
    y = np.concatenate([y, np.zeros(max(0, len(x) + DELAY - len(y)), np.float32)])[:len(x) + DELAY]
    aligned = np.concatenate([np.zeros(DELAY, np.float32), x])
    z = (y + ALPHA * aligned)[DELAY:DELAY + len(x)]
    return np.clip(np.round(z * 32768), -32768, 32767).astype('<i2').tobytes()
