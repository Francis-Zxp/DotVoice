"""Run a WhisperDSP file over the synthetic pen bench and score it with a local
SenseVoice proxy recogniser.

  run_bench.py --dsp <file.py|raw|gain:DB> --label NAME [--mode auto|on]
               [--subset whisper|normal|all] [--post post.py] [--levels w52,w60]

The DSP is loaded as a module of the project's bridge package and driven the way
bridge/controller.py drives it: begin(mode), process() per 50 ms (1600-byte)
chunk, flush(), stats(), reset(). The sent audio goes through the project's
SpeechMeter (WebRTC VAD) so controller._silent() can be applied with the
controller's own constants. --post loads a module with post(pcm: bytes) -> bytes
applied to the DSP output before recognition (timed separately).
Nothing in the project is written (sys.dont_write_bytecode)."""
import argparse
import ast
import ctypes
import hashlib
import importlib.util
import json
import os
import sys
import time
import wave

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.abspath(os.path.join(HERE, '..', '..'))
sys.path.insert(0, PROJECT)
from bridge.speech_meter import SpeechMeter  # noqa: E402

MODEL_DIR = os.path.join(os.environ['APPDATA'], 'OpenLess', 'models', 'sherpa-onnx', 'sense-voice-small-zh')
CHUNK = 1600
RESULTS = os.path.join(HERE, 'results')
CACHE = os.path.join(RESULTS, 'asr_cache.json')


def controller_constants():
    tree = ast.parse(open(os.path.join(PROJECT, 'bridge', 'controller.py'), encoding='utf-8').read())
    out = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in ('SILENT_MIN_AUDIO', 'SILENT_SPEECH', 'WHISPER_SPEECH'):
                out[name] = ast.literal_eval(node.value)
    return out


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    module.__package__ = name.rpartition('.')[0]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# -- CPU: thread cycle counter (process_time on Windows moves in 15.6 ms steps) --
_k32 = ctypes.windll.kernel32
_k32.GetCurrentThread.restype = ctypes.c_void_p


def cycles():
    c = ctypes.c_ulonglong()
    _k32.QueryThreadCycleTime(ctypes.c_void_p(_k32.GetCurrentThread()), ctypes.byref(c))
    return c.value


def calibrate():
    best = None
    for _ in range(3):
        t0, c0 = time.perf_counter(), cycles()
        x = 0
        while time.perf_counter() - t0 < .2:
            x += 1
        hz = (cycles() - c0) / (time.perf_counter() - t0)
        best = hz if best is None else max(best, hz)
    return best


# -- CER --
DIGITS = dict(zip('0123456789', '零一二三四五六七八九'))


def normalise(text):
    out = []
    for ch in text:
        ch = DIGITS.get(ch, ch)
        if ch.isalnum():
            out.append(ch.lower())
    return ''.join(out)


def edit_distance(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        prev = cur
    return prev[-1]


def cer(ref, hyp):
    r, h = normalise(ref), normalise(hyp)
    return edit_distance(r, h) / max(1, len(r))


class ASR:
    def __init__(self):
        import sherpa_onnx
        self.rec = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=os.path.join(MODEL_DIR, 'model.int8.onnx'), tokens=os.path.join(MODEL_DIR, 'tokens.txt'),
            language='zh', use_itn=False, num_threads=2)
        self.cache = json.load(open(CACHE, encoding='utf-8')) if os.path.exists(CACHE) else {}
        self.dirty = False

    def key(self, pcm):
        return 'sv:' + hashlib.sha256(pcm).hexdigest()

    def recognise(self, pcms):
        import numpy as np
        todo = [p for p in dict.fromkeys(pcms) if self.key(p) not in self.cache]
        for start in range(0, len(todo), 16):
            batch = todo[start:start + 16]
            streams = []
            for pcm in batch:
                s = self.rec.create_stream()
                s.accept_waveform(16000, np.frombuffer(pcm, '<i2').astype(np.float32) / 32768.0)
                streams.append(s)
            self.rec.decode_streams(streams)
            for pcm, s in zip(batch, streams):
                self.cache[self.key(pcm)] = s.result.text
            self.dirty = True
        return [self.cache[self.key(p)] for p in pcms]

    def save(self):
        if self.dirty:
            tmp = CACHE + '.tmp'
            json.dump(self.cache, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False)
            os.replace(tmp, CACHE)


def read_pcm(path):
    with wave.open(os.path.join(HERE, path), 'rb') as r:
        return r.readframes(r.getnframes())


def pure_gain(pcm, db):
    import numpy as np
    x = np.frombuffer(pcm, '<i2').astype(np.float64) * 10 ** (db / 20)
    return np.clip(np.round(x), -32768, 32767).astype('<i2').tobytes()


def run(args):
    consts = controller_constants()
    manifest = json.load(open(os.path.join(HERE, args.manifest), encoding='utf-8'))
    if args.subset != 'all':
        manifest = [m for m in manifest if m['kind'] == args.subset]
    if args.levels:
        manifest = [m for m in manifest if m['level'] in args.levels.split(',')]
    if args.conds:
        manifest = [m for m in manifest if m['cond'] in args.conds.split(',')]
    dsp = None
    if args.dsp not in ('raw',) and not args.dsp.startswith('gain:'):
        module = load_module(os.path.abspath(args.dsp), 'bridge._bench_dsp')
        dsp = module.WhisperDSP()
    post = load_module(os.path.abspath(args.post), '_bench_post') if args.post else None
    hz = calibrate()
    rows, outs = [], []
    for m in manifest:
        pcm = read_pcm(m['path'])
        audio_s = len(pcm) / 32000
        meter = SpeechMeter()
        row = {k: m[k] for k in ('cond', 'level', 'kind', 'gate', 'pop', 'id')}
        if dsp is None:
            out = pcm if args.dsp == 'raw' else pure_gain(pcm, float(args.dsp[5:]))
            meter.feed(out)
            row.update(engaged=None, gain_db=None, dsp_speech_s=None, cpu_ms_per_s=0.0, active=False)
        else:
            sent = []
            dsp.begin(args.mode)
            c0 = cycles()
            for off in range(0, len(pcm), CHUNK):
                sent.extend(dsp.process(pcm[off:off + CHUNK]))
            sent.extend(dsp.flush())
            spent = (cycles() - c0) / hz
            for piece in sent:
                meter.feed(piece)
            st = dsp.stats()
            out = b''.join(sent)
            row.update(engaged=bool(dsp.engaged), gain_db=round(dsp.gain_db, 2),
                       dsp_speech_s=round(dsp.speech_seconds, 3), active=bool(dsp.active),
                       cpu_ms_per_s=round(spent * 1000 / audio_s, 3),
                       decided_at=st.get('decided_at'), decision_db=st.get('decision_db'),
                       pop_muted=st.get('pop_muted'), comfort=st.get('comfort'))
            dsp.reset()
        heard = meter.snapshot()
        silent = (heard['audio_seconds'] >= consts['SILENT_MIN_AUDIO'] and heard['speech_seconds'] < consts['SILENT_SPEECH']
                  and not (row['active'] and (row['dsp_speech_s'] or 0) >= consts['WHISPER_SPEECH']))
        row.update(vad_speech_s=heard['speech_seconds'], cancel=bool(silent), ident=out == pcm,
                   len_ok=len(out) == len(pcm))
        if post is not None:
            c0 = cycles()
            out = post.post(out)
            row['post_cpu_ms_per_s'] = round((cycles() - c0) / hz * 1000 / audio_s, 3)
        rows.append(row)
        outs.append(out)
    asr = ASR()
    hyps = asr.recognise(outs)
    asr.save()
    for row, m, hyp in zip(rows, manifest, hyps):
        row['hyp'] = hyp
        row['cer'] = round(cer(m['text'], hyp), 4)
    result = {'label': args.label, 'dsp': args.dsp, 'mode': args.mode, 'post': args.post,
              'constants': consts, 'cycles_hz': hz, 'rows': rows}
    os.makedirs(RESULTS, exist_ok=True)
    json.dump(result, open(os.path.join(RESULTS, args.label + '.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=0)
    text = report(rows)
    open(os.path.join(RESULTS, args.label + '.txt'), 'w', encoding='utf-8').write(text)
    print(text)


def summarise(rows):
    n = len(rows)
    eng = [r for r in rows if r.get('engaged')]
    mean = lambda v: sum(v) / len(v) if v else float('nan')  # noqa: E731
    return {'n': n, 'cer': mean([r['cer'] for r in rows]), 'eng': len(eng),
            'gain': mean([r['gain_db'] for r in eng]) if eng else None,
            'sp': mean([r['dsp_speech_s'] for r in rows if r['dsp_speech_s'] is not None]) if rows[0]['dsp_speech_s'] is not None else None,
            'sp0': sum(1 for r in rows if r['dsp_speech_s'] == 0),
            'cancel': sum(1 for r in rows if r['cancel']), 'ident': sum(1 for r in rows if r['ident']),
            'cpu': mean([r['cpu_ms_per_s'] for r in rows]),
            'post': mean([r['post_cpu_ms_per_s'] for r in rows]) if 'post_cpu_ms_per_s' in rows[0] else None,
            'vad': mean([r['vad_speech_s'] for r in rows])}


def fmt(name, s):
    gain = '-' if s['gain'] is None else f"{s['gain']:.1f}"
    sp = '-' if s['sp'] is None else f"{s['sp']:.2f}"
    post = '' if s['post'] is None else f" {s['post']:7.2f}"
    return (f"{name:16s} {s['n']:4d} {s['cer']:.3f} {s['eng']:4d} {gain:>5s} {sp:>5s} {s['sp0']:4d} "
            f"{s['cancel']:4d} {s['ident']:4d} {s['vad']:5.2f} {s['cpu']:6.2f}{post}")


def report(rows):
    head = 'condition           n   CER   eng  gain  sp_s  sp0 canc idnt vad_s cpu_ms/s' + (' post_ms/s' if 'post_cpu_ms_per_s' in rows[0] else '')
    lines = [head]
    groups = {}
    for r in rows:
        groups.setdefault(r['cond'], []).append(r)
    for cond in sorted(groups):
        lines.append(fmt(cond, summarise(groups[cond])))
    lines.append('')
    levels = {}
    for r in rows:
        levels.setdefault(r['level'], []).append(r)
        levels.setdefault(r['kind'], []).append(r)
    for name in ('n22', 'n30', 'w52', 'w60', 'w66', 'normal', 'whisper'):
        if name in levels:
            lines.append(fmt(name, summarise(levels[name])))
    return '\n'.join(lines)


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--dsp', required=True)
    p.add_argument('--label', required=True)
    p.add_argument('--mode', default='auto')
    p.add_argument('--subset', default='all', choices=('all', 'whisper', 'normal'))
    p.add_argument('--levels', default='')
    p.add_argument('--conds', default='')
    p.add_argument('--post', default='')
    p.add_argument('--manifest', default='manifest.json')
    run(p.parse_args())
