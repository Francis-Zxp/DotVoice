"""气声 (whisper) front end: deterministic synthetic signals, no audio device."""
import array
from functools import lru_cache
import math
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch
import warnings

with warnings.catch_warnings():
    warnings.simplefilter('ignore', DeprecationWarning)
    import audioop

from bridge.controller import WHISPER_SPEECH, Controller
from bridge.prefs import Preferences
from bridge.speech_meter import SpeechMeter
from bridge.voice_dsp import CEIL, HOLD_MAX, MAX_GAIN_DB, TARGET_DB, WhisperDSP

try:
    from .test_bridge import FakeBackend, FakeCapture, wait_for
    from .core_fakes import make_core
except ImportError:
    from test_bridge import FakeBackend, FakeCapture, wait_for
    from core_fakes import make_core

RATE = 16000
FLOOR = 16       # steady noise, about -66 dBFS
WHISPER = 200    # syllable bursts around -46 dBFS
NORMAL = 3000    # normal speaking level, around -22 dBFS


def _pcm(samples):
    return array.array('h', [max(-32768, min(32767, int(round(v)))) for v in samples]).tobytes()


@lru_cache(maxsize=None)
def speech_like(seed, bursts, sigma, lead=.3, burst=.2, gap=.12, tail=.3, floor=FLOOR):
    """Syllable-like noise bursts (sin² envelope, slightly bright like a whisper)
    over a steady noise floor. Returns (pcm, ((start, end) sample span per burst))."""
    rnd = random.Random(seed)
    out = [rnd.gauss(0, floor) for _ in range(int(lead * RATE))]
    spans = []
    for _ in range(bursts):
        n = int(burst * RATE)
        w = [rnd.gauss(0, sigma) for _ in range(n + 1)]
        spans.append((len(out), len(out) + n))
        out += [rnd.gauss(0, floor) + math.sin(math.pi * i / n) ** 2 * 1.2 * (w[i + 1] - .3 * w[i])
                for i in range(n)]
        out += [rnd.gauss(0, floor) for _ in range(int(gap * RATE))]
    out += [rnd.gauss(0, floor) for _ in range(int(tail * RATE))]
    return _pcm(out), tuple(spans)


def noise(seed, seconds, sigma=FLOOR):
    rnd = random.Random(seed)
    return _pcm(rnd.gauss(0, sigma) for _ in range(int(seconds * RATE)))


def run(dsp, pcm, chunk=800):
    """Feed like the capture thread does; returns (output, samples heard when boosting engaged)."""
    out, engaged_at = [], None
    for k in range(0, len(pcm), chunk):
        out += dsp.process(pcm[k:k + chunk])
        if engaged_at is None and dsp.engaged:
            engaged_at = (k + chunk) // 2
    out += dsp.flush()
    return b''.join(out), engaged_at


def gain_db(before, after, start, end):
    a, b = before[start * 2:end * 2], after[start * 2:end * 2]
    return 20 * math.log10(max(audioop.rms(b, 2), 1e-6) / max(audioop.rms(a, 2), 1e-6))


def middle(span):
    start, end = span
    return start + (end - start) // 5, end - (end - start) // 5


def session(mode):
    dsp = WhisperDSP()
    dsp.begin(mode)
    return dsp


class FilterTests(unittest.TestCase):
    def test_highpass_removes_dc_and_hum_and_keeps_the_speech_band(self):
        def filtered(signal, mode='auto'):
            dsp = session(mode)
            band = dsp._band if mode == 'on' else (lambda x: x)  # engaged output path
            x = b''.join(band(dsp._highpass(signal[k:k + 1600])) for k in range(0, len(signal), 1600))
            return audioop.lin2lin(audioop.mul(x, 4, 4.0), 4, 2)[RATE:]  # second half: settled

        def tone(freq, dc=0):
            return _pcm(dc + 8000 * math.sin(2 * math.pi * freq * i / RATE) for i in range(RATE))
        reference = 8000 / math.sqrt(2)
        # Not (yet) engaged: the gentle analysis filter only.
        voice = filtered(tone(1000, dc=3000))
        self.assertLess(abs(audioop.avg(voice, 2)), 3, 'DC offset removed')
        self.assertAlmostEqual(audioop.rms(voice, 2) / reference, 1, delta=.06)  # within 0.5 dB
        self.assertGreater(audioop.rms(filtered(tone(200)), 2) / reference, .85)  # gentle
        self.assertLess(audioop.rms(filtered(tone(50)), 2) / reference, .5, 'mains hum at least 6 dB down')
        # Engaged whisper: rumble below ~200 Hz goes, the whisper band stays within 1 dB.
        voice = filtered(tone(1000, dc=3000), 'on')
        self.assertLess(abs(audioop.avg(voice, 2)), 3)
        self.assertAlmostEqual(audioop.rms(voice, 2) / reference, 1, delta=.12)
        self.assertGreater(audioop.rms(filtered(tone(400), 'on'), 2) / reference, .7)
        self.assertLess(audioop.rms(filtered(tone(60), 'on'), 2) / reference, .2, 'rumble ~14 dB down')


class GainTests(unittest.TestCase):
    def test_soft_speech_is_lifted_within_the_gain_bounds(self):
        pcm, spans = speech_like(1, 5, WHISPER)
        dsp = session('on')
        out, _ = run(dsp, pcm)
        self.assertEqual(len(out), len(pcm))
        for span in spans:
            lifted = gain_db(pcm, out, *middle(span))
            self.assertGreater(lifted, 12, 'a whisper is lifted toward normal loudness')
            self.assertLess(lifted, MAX_GAIN_DB + .5)
        limit = 10 ** ((MAX_GAIN_DB + .5) / 20)
        for start in range(0, len(pcm) // 2, 160):  # no 10 ms frame anywhere beyond the gain cap
            a = audioop.rms(pcm[start * 2:start * 2 + 320], 2)
            b = audioop.rms(out[start * 2:start * 2 + 320], 2)
            self.assertLessEqual(b, a * limit + 2)
        self.assertLessEqual(dsp.gain_db, MAX_GAIN_DB)
        dsp = session('on')
        run(dsp, pcm[:middle(spans[-1])[1] * 2])
        speech = dsp.meter_gain
        self.assertGreater(speech, 4.0, 'mid-word, the level meter follows the lift')
        dsp.flush()
        dsp.process(noise(18, .6))
        # v2 holds the gain through pauses (no gate, no expander): the meter shows what is sent.
        self.assertLess(dsp.meter_gain, speech * 10 ** (2 / 20), 'held in a pause, not raised')
        self.assertGreater(dsp.meter_gain, speech / 10 ** (2 / 20), 'nor dropped')

    def test_silence_and_steady_noise_are_never_lifted(self):
        for mode in ('on', 'auto'):
            zeros = bytes(RATE * 2)
            out, _ = run(session(mode), zeros)
            self.assertEqual(out, zeros, mode)
        hiss = noise(2, 2.0)
        dsp = session('on')
        out, _ = run(dsp, hiss)
        self.assertLessEqual(audioop.rms(out, 2), audioop.rms(hiss, 2) * 10 ** (1 / 20))
        self.assertEqual(dsp.speech_seconds, 0)
        # Between whispered words the gain is held (v2: no gate swinging 0 -> +20 dB per
        # syllable, and no expander: at low SNR one took weak syllables for hiss).
        pcm, spans = speech_like(3, 4, WHISPER, gap=.6)
        out, _ = run(session('on'), pcm)
        for start, end in spans[:-1]:
            speech = gain_db(pcm, out, *middle((start, end)))
            pause = [gain_db(pcm, out, end + k, end + k + 800) for k in range(int(.3 * RATE), int(.5 * RATE), 800)]
            self.assertLess(max(pause), speech + 1.5, 'pause noise is never lifted more than speech')
            self.assertGreater(min(pause), speech - 1.5, 'nor dropped: the gain is held')
            self.assertLess(max(pause) - min(pause), 1.5, 'and steady: no pumping')

    def test_limiter_keeps_lifted_peaks_below_full_scale(self):
        whisper, _ = speech_like(4, 4, 150, tail=0)
        rnd = random.Random(5)
        clicks = [rnd.gauss(0, FLOOR) for _ in range(int(.3 * RATE))]
        for i in range(0, len(clicks), 80):  # sparse peaks: quiet frames with a high crest factor
            clicks[i] += 2600 if (i // 80) % 2 else -2600
        loud = [12000 * math.sin(2 * math.pi * 440 * i / RATE) for i in range(int(.2 * RATE))]
        pcm = whisper + _pcm(clicks) + _pcm(loud) + noise(6, .3)
        out, _ = run(session('on'), pcm)
        self.assertEqual(len(out), len(pcm))
        self.assertLessEqual(audioop.max(out, 2), CEIL + 1, 'no sample reaches full scale')
        samples, source = array.array('h', out), array.array('h', pcm)
        base = len(whisper) // 2
        region = range(base, base + len(clicks))
        self.assertGreater(max(abs(samples[i]) for i in region), 2 * 2600, 'peaks are still lifted')
        for i in range(base, base + len(clicks), 80):
            self.assertEqual(samples[i] > 0, source[i] > 0, 'saturates, never wraps around')
        start = base + len(clicks) + 320  # after the frame that straddles the onset
        self.assertLessEqual(max(abs(samples[i]) for i in range(start, base + len(clicks) + len(loud))),
                             12000 * 1.02, 'a loud frame after a whisper is not lifted')


class AutoTests(unittest.TestCase):
    def test_auto_lifts_only_speech_that_stays_soft(self):
        for name, pcm in (('normal speech', speech_like(7, 5, NORMAL)[0]), ('steady noise', noise(8, 2.0)),
                          ('silence', bytes(RATE * 2))):
            dsp = session('auto')
            out, engaged_at = run(dsp, pcm)
            self.assertIsNone(engaged_at, name)
            self.assertEqual(out, pcm, name + ' is sent byte for byte')
        pcm, spans = speech_like(9, 5, WHISPER)
        dsp = session('auto')
        out, engaged_at = run(dsp, pcm)
        self.assertEqual(len(out), len(pcm))
        self.assertIsNotNone(engaged_at, 'a whisper engages boosting')
        self.assertLessEqual(engaged_at - spans[0][0], HOLD_MAX, 'decided within a second of speech')
        self.assertGreater(gain_db(pcm, out, *middle(spans[0])), 12,
                           'the first syllable was held and released already lifted')

    def test_held_audio_comes_out_complete_and_in_order(self):
        pcm, _ = speech_like(10, 1, WHISPER, lead=.1, tail=.1)
        dsp = session('auto')
        returned = [dsp.process(pcm[k:k + 800]) for k in range(0, len(pcm), 800)]
        self.assertIn([], returned, 'the first syllable is held while deciding')
        out = b''.join(b''.join(chunks) for chunks in returned) + b''.join(dsp.flush())
        self.assertEqual(len(out), len(pcm))
        normal, _ = speech_like(11, 2, NORMAL, lead=.1, tail=.1)
        out, _ = run(session('auto'), normal, chunk=1600)
        self.assertEqual(out, normal, 'held normal speech is released untouched, in order')

    def test_a_session_that_turns_soft_later_ramps_up(self):
        loud, loud_spans = speech_like(12, 3, NORMAL, tail=0)
        soft, soft_spans = speech_like(13, 12, WHISPER, lead=0)
        pcm = loud + soft
        dsp = session('auto')
        out, engaged_at = run(dsp, pcm)
        self.assertIsNotNone(engaged_at)
        self.assertGreater(engaged_at, len(loud) // 2, 'normal speech first: not engaged then')
        self.assertEqual(out[:loud_spans[-1][1] * 2], pcm[:loud_spans[-1][1] * 2])
        offset = len(loud) // 2
        for start, end in soft_spans[-2:]:
            self.assertGreater(gain_db(pcm, out, *middle((start + offset, end + offset))), 12)

    def test_off_is_an_exact_bypass(self):
        pcm, _ = speech_like(14, 3, WHISPER)
        dsp = session('off')
        self.assertEqual(dsp.process(pcm), [pcm])
        self.assertEqual(dsp.flush(), [])
        self.assertFalse(dsp.engaged or dsp.active)
        self.assertEqual(dsp.speech_seconds, 0)
        self.assertEqual(dsp.meter_gain, 1.0)


def dbfs(level):
    return 32768 * 10 ** (level / 20)


def band_noise(rnd, n, rms):
    """Gaussian noise in the speech band (one-pole high-pass ~300 Hz, low-pass ~3.4 kHz),
    scaled to `rms` 16-bit steps."""
    a_hp, a_lp = math.exp(-2 * math.pi * 300 / RATE), math.exp(-2 * math.pi * 3400 / RATE)
    hp = low = previous = 0.0
    out = []
    for _ in range(n):
        x = rnd.gauss(0, 1)
        hp = a_hp * (hp + x - previous)
        previous = x
        low += (1 - a_lp) * (hp - low)
        out.append(low)
    scale = rms / math.sqrt(sum(v * v for v in out) / len(out))
    return [v * scale for v in out]


@lru_cache(maxsize=None)
def very_soft(seed, level=-66.0, floor=-80.0, bursts=6, burst=.22, gap=.14, lead=.4, tail=.4, click=None):
    """Syllable-like bursts of speech-band noise whose 10 ms frames sit at `level` dBFS
    (30 ms ramps, flat middle) over a speech-band floor at `floor` dBFS - very soft speech
    on a quiet microphone. `click`: a 3 ms tick that many seconds before the first burst.
    Returns (pcm, ((start, end) per burst))."""
    rnd = random.Random(seed)
    total = int((lead + bursts * (burst + gap) + tail) * RATE)
    signal = band_noise(rnd, total, dbfs(floor))
    n, ramp = int(burst * RATE), int(.03 * RATE)
    spans, position = [], int(lead * RATE)
    for _ in range(bursts):
        voice = band_noise(rnd, n, dbfs(level))
        for i in range(n):
            signal[position + i] += voice[i] * math.sin(math.pi / 2 * min(1.0, i / ramp, (n - 1 - i) / ramp)) ** 2
        spans.append((position, position + n))
        position += n + int(gap * RATE)
    if click is not None:
        at = spans[0][0] - int(click * RATE)
        for i in range(48):
            signal[at + i] += dbfs(-26) * (1 if i % 2 else -1)
    return _pcm(signal), tuple(spans)


def hiss(seed, seconds, level, wobble=0.0):
    """Speech-band noise at `level` dBFS; `wobble` dB of slow (0.7 Hz) level change."""
    samples = band_noise(random.Random(seed), int(seconds * RATE), dbfs(level))
    return _pcm(v * 10 ** (wobble * math.sin(2 * math.pi * .7 * i / RATE) / 20) for i, v in enumerate(samples))


def level_dbfs(pcm, start, end):
    return 20 * math.log10(max(audioop.rms(pcm[start * 2:end * 2], 2), 1e-6) / 32768)


@lru_cache(maxsize=None)
def noise_steps():
    """Noise only (field review 2026-10-14): steady hiss that steps up, and a microphone
    that opens (digital zeros), fades in over 50-200 ms and stays at a steady level."""
    cases = []
    for seed, (a, b) in enumerate(((-80, -70), (-80, -66))):
        rnd = random.Random(50 + seed)
        cases.append((f'hiss stepping {a} -> {b} dBFS',
                      _pcm(band_noise(rnd, RATE, dbfs(a)) + band_noise(rnd, 2 * RATE, dbfs(b)))))
    for level in (-70.0, -66.0, -62.0):
        for fade in (.05, .1, .2):
            rnd = random.Random(int(60 - level) + int(fade * 100))
            ramp = int(fade * RATE)
            signal = band_noise(rnd, 2 * RATE, dbfs(level))
            cases.append((f'{fade * 1000:.0f} ms fade-in to steady {level} dBFS',
                          bytes(int(.1 * RATE) * 2) + _pcm(v * min(1.0, i / ramp) for i, v in enumerate(signal))))
    return tuple(cases)


class VerySoftSpeechTests(unittest.TestCase):
    """Field report 2026-10-12: with the Doubao engine normal speech is perfect, very quiet
    speech extremely inaccurate. Such speech sits at -60...-70 dBFS, 10-20 dB over a quiet
    microphone's floor: it must count as speech, engage 'auto' and be lifted toward the
    target (-26 dBFS, at most +36 dB) - while hiss and silence stay untouched."""

    def test_very_soft_speech_engages_and_is_lifted_toward_the_target(self):
        for level in (-62.0, -66.0, -70.0):
            with self.subTest(level=level):
                pcm, spans = very_soft(21, level)
                dsp = session('auto')
                out, engaged_at = run(dsp, pcm)
                self.assertEqual(len(out), len(pcm))
                self.assertIsNotNone(engaged_at, 'very soft speech engages boosting')
                self.assertLessEqual(engaged_at - spans[0][0], HOLD_MAX, 'decided within a second of speech')
                self.assertGreater(dsp.speech_seconds, .6, 'it counts as speech, never as silence')
                reach = min(TARGET_DB, level + MAX_GAIN_DB)  # -70 dBFS stops 4 dB short: no +40 dB on noise
                for span in spans:  # the first syllable too: it was held and released lifted
                    lifted = gain_db(pcm, out, *middle(span))
                    self.assertGreater(lifted, 30, f'{level} dBFS lifted by only {lifted:.1f} dB')
                    self.assertLess(lifted, MAX_GAIN_DB + .5)
                    self.assertGreater(level_dbfs(out, *middle(span)), reach - 4, 'close to the target')
                    self.assertLess(level_dbfs(out, *middle(span)), TARGET_DB + 6)
                stats = dsp.stats()
                self.assertTrue(stats['engaged'] and stats['decided'])
                self.assertLess(stats['decision_db'], -55)

    def test_hiss_and_silence_are_never_lifted(self):
        steady = (('speech-band hiss at -80 dBFS', hiss(31, 2.5, -80)), ('hiss at -66 dBFS', hiss(32, 2.5, -66)),
                  ('digital silence', bytes(RATE * 2)))
        for name, pcm in steady + (('hiss whose level drifts by 1.5 dB', hiss(33, 3.0, -80, wobble=1.5)),):
            with self.subTest(name):
                dsp = session('auto')
                out, engaged_at = run(dsp, pcm)
                self.assertIsNone(engaged_at)
                self.assertEqual(out, pcm, 'sent byte for byte')
                self.assertEqual(dsp.speech_seconds, 0)
        for name, pcm in steady:  # "always": still nothing to lift
            with self.subTest('on: ' + name):
                dsp = session('on')
                out, _ = run(dsp, pcm)
                self.assertLessEqual(audioop.rms(out, 2), audioop.rms(pcm, 2) * 10 ** (1 / 20) + 1)
                self.assertEqual(dsp.speech_seconds, 0)

    def test_pauses_between_very_soft_words_keep_a_steady_gain(self):
        # v2 (Tidepool's way): the gain is held in pauses - no gate pumping the hiss from
        # 0 to +36 dB with every word, and no expander either.
        pcm, spans = very_soft(22, -66.0, gap=.6)
        out, _ = run(session('auto'), pcm)
        for start, end in spans[:-1]:
            speech = gain_db(pcm, out, *middle((start, end)))
            pause = [gain_db(pcm, out, end + k, end + k + 800) for k in range(int(.3 * RATE), int(.5 * RATE), 800)]
            self.assertLess(max(pause), speech + 1.5)
            self.assertGreater(min(pause), speech - 1.5)
            self.assertLess(max(pause) - min(pause), 1.5, 'no pumping')
            onset = gain_db(pcm, out, end + int(.6 * RATE) - 160, end + int(.6 * RATE) + 640)
            self.assertGreater(onset, speech - 2, 'open again by the next word (look-ahead)')

    def test_noise_that_steps_up_or_fades_in_is_never_speech(self):
        # Field review 2026-10-14: steady noise stepping up (-80 -> -70 dBFS), or a microphone
        # fading in over 50 ms and then steady at -70 / -66 / -62, engaged +36...42 dB and
        # counted as speech, so the recording escaped the silent cancel (hallucinated text).
        for name, pcm in noise_steps():
            with self.subTest(name):
                dsp = session('auto')
                out, engaged_at = run(dsp, pcm)
                self.assertIsNone(engaged_at, 'no lift for noise')
                self.assertEqual(out, pcm, 'sent byte for byte')
                self.assertLess(dsp.speech_seconds, WHISPER_SPEECH, 'no speech evidence')
                self.assertGreater(dsp.floor_db, -75, 'the floor follows the louder background')
        for name, pcm in noise_steps()[:2]:  # "always": the gate does not lift the new hiss for long
            with self.subTest('on: ' + name):
                dsp = session('on')
                run(dsp, pcm)
                self.assertLess(dsp.speech_seconds, WHISPER_SPEECH)

    def test_a_single_noisy_frame_does_not_open_the_gate(self):
        rnd = random.Random(41)
        quiet = band_noise(rnd, RATE, dbfs(-80))
        blip = band_noise(rnd, 160, dbfs(-50))  # one 10 ms frame, aligned to the analysis frames
        tail = band_noise(rnd, 640, dbfs(-80))
        dsp = session('auto')
        dsp.process(_pcm(quiet))
        frames = dsp._analyse(dsp._highpass(_pcm(blip + tail)))
        self.assertGreater(frames[0].db, -55, 'the blip is well above the floor')
        self.assertTrue(frames[0].above)
        self.assertFalse(any(frame.active for frame in frames), 'one frame is no onset: nothing is active')
        self.assertEqual(dsp.active_frames, 0)
        self.assertFalse(dsp.holding)
        frames = dsp._analyse(dsp._highpass(_pcm(band_noise(rnd, 480, dbfs(-50)) + tail)))
        self.assertEqual([frame.active for frame in frames[:3]], [False, True, True], 'three frames are')
        self.assertTrue(dsp.holding)

    def test_soft_speech_without_pauses_does_not_pin_the_floor(self):
        # Syllables at 4 Hz between -70 and -62 dBFS that never drop back to the -80 dBFS
        # background: their frames used to become the "floor" (-69.6), so later syllables
        # hardly counted. Frames of modulated runs never feed the floor.
        rnd = random.Random(27)
        lead, seconds = int(.3 * RATE), 5.0
        signal = band_noise(rnd, lead + int(seconds * RATE), dbfs(-80))
        voice = band_noise(rnd, int(seconds * RATE), 1.0)
        for i, v in enumerate(voice):
            signal[lead + i] += v * dbfs(-70 + 8 * math.sin(math.pi * 4 * i / RATE) ** 2)
        pcm = _pcm(signal)
        dsp = session('auto')
        out, engaged_at = run(dsp, pcm)
        self.assertIsNotNone(engaged_at)
        self.assertLess(dsp.floor_db, -76, 'the floor stays at the background')
        self.assertGreater(dsp.speech_seconds, .8 * seconds, 'every syllable counts')
        self.assertGreater(gain_db(pcm, out, int(4.5 * RATE), int(5.2 * RATE)), 30, 'and is lifted')

    def test_a_click_alone_does_not_settle_the_decision_before_very_soft_speech(self):
        # The pen bumped 1.5 s before the first very soft syllable: the click is no speech,
        # so the session does not settle on "normal, untouched" and lift the speech late.
        pcm, spans = very_soft(23, -66.0, lead=2.0, click=1.5)
        dsp = session('auto')
        out, engaged_at = run(dsp, pcm)
        self.assertIsNotNone(engaged_at)
        self.assertGreater(gain_db(pcm, out, *middle(spans[0])), 30, 'the first syllable is lifted too')


class Deaf:
    """WebRTC VAD on a whisper: no voicing, so (almost) nothing is speech."""
    def is_speech(self, frame, rate):
        return False


class ControllerTests(unittest.TestCase):
    def record(self, mode, pcm, chunk=800):
        backend, events = FakeBackend(), []
        with patch('bridge.controller.SpeechMeter', lambda: SpeechMeter(detector=Deaf())):
            control = Controller(lambda *event: events.append(event), backend, FakeCapture)
            try:
                control.whisper_mode = mode
                control.command('arm')
                wait_for(lambda: control.phase == 'idle')
                control.command('start')
                wait_for(lambda: control.phase == 'recording')
                for k in range(0, len(pcm), chunk):
                    control._audio(pcm[k:k + chunk])
                control.command('stop')
                wait_for(lambda: control.phase == 'processing' or any(k == 'empty' for k, _ in events), 6)
                wait_for(lambda: control.phase in ('processing', 'idle'), 6)
            finally:
                control.close()
        sent = b''.join(call[1] for call in backend.calls if isinstance(call, tuple))
        states = [value for kind, value in events if kind == 'state']
        empty = any(kind == 'empty' for kind, _ in events)
        return backend, sent, states, empty

    def test_whispers_are_kept_while_true_silence_is_still_cancelled(self):
        whisper, _ = speech_like(15, 4, WHISPER)
        backend, sent, states, empty = self.record('auto', whisper)
        self.assertFalse(empty, 'a whisper the VAD cannot hear is not discarded as silence')
        self.assertIn('stop', backend.calls)
        self.assertEqual(len(sent), len(whisper))
        self.assertNotEqual(sent, whisper, 'OpenLess gets the lifted audio')
        self.assertTrue(any(s['phase'] == 'recording' and s.get('whisper') for s in states))
        self.assertTrue(any(s['phase'] == 'processing' and s.get('whisper') for s in states))

        for name, pcm in (('silence', bytes(RATE * 2)), ('steady noise', noise(16, 1.0))):
            backend, _, states, empty = self.record('auto', pcm)
            self.assertTrue(empty, name + ' is still cancelled')
            self.assertNotIn('stop', backend.calls)
            self.assertFalse(any(s.get('whisper') for s in states), name)

    def test_very_soft_speech_reaches_the_engine_lifted_and_hiss_is_still_cancelled(self):
        pcm, _ = very_soft(24, -66.0)
        backend, sent, states, empty = self.record('auto', pcm)
        self.assertFalse(empty, 'very soft speech is not discarded as silence')
        self.assertIn('stop', backend.calls)
        self.assertEqual(len(sent), len(pcm))
        self.assertGreater(audioop.rms(sent, 2), audioop.rms(pcm, 2) * 10, 'the engine gets it 20+ dB louder')
        self.assertTrue(any(s['phase'] == 'recording' and s.get('whisper') for s in states))
        backend, _, states, empty = self.record('auto', hiss(25, 1.5, -80))
        self.assertTrue(empty, 'a quiet microphone with nobody speaking is still cancelled')
        self.assertNotIn('stop', backend.calls)

    def test_noise_that_steps_up_or_fades_in_is_still_cancelled_as_silent(self):
        for name, pcm in noise_steps()[:3] + noise_steps()[-3:]:
            with self.subTest(name):
                backend, sent, states, empty = self.record('auto', pcm)
                self.assertTrue(empty, 'nobody spoke: cancelled, never sent for recognition')
                self.assertNotIn('stop', backend.calls)
                self.assertFalse(any(s.get('whisper') for s in states), 'no whisper lift announced')

    def test_off_keeps_the_old_rule_and_audio(self):
        whisper, _ = speech_like(15, 4, WHISPER)
        backend, sent, states, empty = self.record('off', whisper)
        self.assertTrue(empty, 'off: the VAD-only rule, exactly as before')
        self.assertEqual(sent, whisper, 'off: audio untouched')
        self.assertFalse(any(s.get('whisper') for s in states))

    def test_normal_speech_in_auto_is_untouched_and_on_flags_the_session_from_the_start(self):
        normal, _ = speech_like(17, 3, NORMAL)
        backend, sent, states, empty = self.record('auto', normal)
        self.assertFalse(empty)
        self.assertEqual(sent, normal)
        self.assertFalse(any(s.get('whisper') for s in states))
        backend, sent, states, empty = self.record('on', normal)
        self.assertFalse(empty)
        self.assertTrue(next(s for s in states if s['phase'] == 'starting').get('whisper'))
        self.assertEqual(len(sent), len(normal))

    def test_level_meter_shows_the_lifted_level(self):
        events = []
        control = Controller(lambda *event: events.append(event), FakeBackend(), FakeCapture)
        try:
            control.command('arm')
            wait_for(lambda: control.phase == 'idle')
            control.command('start')
            wait_for(lambda: control.phase == 'recording')
            control.dsp.meter_gain = 4.0
            control.last_meter = 0
            control._audio((1000).to_bytes(2, 'little', signed=True) * 800)
            self.assertEqual([v for k, v in events if k == 'level'][-1], .5)
        finally:
            control.close()


class PrefsTests(unittest.TestCase):
    def test_voice_whisper_pref_reaches_the_next_recording(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        events = []
        core = make_core(Path(folder.name), events=events)
        self.addCleanup(core.close)
        self.assertEqual(core.snapshot()['prefs']['voice'], {'whisper': 'auto', 'fast_raw': False})
        self.assertEqual(core.controller.whisper_mode, 'auto')
        self.assertEqual(core.prefs_set({'voice': {'whisper': 'on'}})['voice']['whisper'], 'on')
        self.assertEqual(core.controller.whisper_mode, 'on')
        for bad in ({'voice': {'whisper': 'loud'}}, {'voice': {'boost': True}}, {'voice': 'on'}):
            self.assertIn('error', core.prefs_set(bad))
        self.assertEqual(core.controller.whisper_mode, 'on')
        saved = Preferences(Path(folder.name) / 'runtime' / 'prefs.json')
        self.assertEqual(saved.get('voice', 'whisper'), 'on')
        control = core.controller
        control.command('arm')
        wait_for(lambda: control.phase == 'idle')
        control.command('start')
        wait_for(lambda: control.phase == 'recording')
        self.assertTrue(next(v for k, v in events if k == 'state' and v['phase'] == 'starting').get('whisper'))
        self.assertTrue(core.snapshot()['state'].get('whisper'))



class EngageContinuityTests(unittest.TestCase):
    def test_mid_session_engage_crossfades_and_held_audio_gets_the_band(self):
        from bridge.voice_dsp import _crossfade
        raw = _pcm([1000] * 400)
        lifted = _pcm([3000] * 400)
        mixed = _crossfade(raw, lifted)
        first, last = audioop.getsample(mixed, 2, 0), audioop.getsample(mixed, 2, 330)
        self.assertLess(first, 1300)          # starts near the raw level
        self.assertEqual(last, 3000)          # ends on the lifted signal
        samples = [audioop.getsample(mixed, 2, i) for i in range(0, 320, 40)]
        self.assertEqual(samples, sorted(samples))  # a ramp, no jump
        dsp = session('on')
        tone = _pcm(int(300 * math.sin(2 * math.pi * 60 * i / RATE)) for i in range(1600))
        out = b''.join(b''.join(dsp.process(tone)) for _ in range(12)) + b''.join(dsp.flush())
        self.assertFalse(dsp.raw_sent)        # 'on': nothing left untouched, no crossfade needed
        self.assertTrue(len(out) > 0)

if __name__ == '__main__':
    unittest.main()
