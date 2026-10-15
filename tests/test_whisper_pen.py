"""气声 front end (WhisperDSP v2) on a model of the Baseus pen: its firmware gates the
audio itself, so every pause is digital zeros; sound comes through with hard (10 ms) or
short (30 ms) edges; a whisper sits at -60...-70 dBFS, a few dB above the pen's own
noise; about 0.3 s after the start key the microphone latching on pops (~-21 dBFS,
80-300 ms). Deterministic synthetic signals, no audio device."""
import array
from functools import lru_cache
import math
import random
import time
import unittest
from unittest.mock import patch
import warnings

with warnings.catch_warnings():
    warnings.simplefilter('ignore', DeprecationWarning)
    import audioop

from bridge.controller import WHISPER_SPEECH, Controller
from bridge.speech_meter import SpeechMeter
from bridge.voice_dsp import (COMFORT_DB, FLOOR_DEFAULT_DB, FRAME, HOLD_MAX, LOOK, MAX_GAIN_DB, POP_WAIT, SOFT_DB,
                              TARGET_DB, WhisperDSP)

try:
    from .test_bridge import FakeBackend, FakeCapture, wait_for
    from .test_whisper import Deaf
except ImportError:
    from test_bridge import FakeBackend, FakeCapture, wait_for
    from test_whisper import Deaf

RATE = 16000
PEN_NOISE = -76.0   # the pen's own noise while its gate is open
LEAD = .4           # leading digital zeros: the microphone is still latched off


def dbfs(level):
    return 32768 * 10 ** (level / 20)


def _pcm(samples):
    return array.array('h', [max(-32768, min(32767, int(round(v)))) for v in samples]).tobytes()


def coloured(rnd, n, rms, lo, hi):
    """Gaussian noise band-limited by one-pole filters (high-pass `lo`, low-pass `hi` Hz),
    scaled to `rms` 16-bit steps."""
    a_hp, a_lp = math.exp(-2 * math.pi * lo / RATE), math.exp(-2 * math.pi * hi / RATE)
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


def rumble(rnd, n, rms, hi=200.0):
    """Handling / room rumble: noise through three one-pole low-passes at `hi` Hz."""
    a = math.exp(-2 * math.pi * hi / RATE)
    s1 = s2 = s3 = 0.0
    out = []
    for _ in range(n):
        s1 += (1 - a) * (rnd.gauss(0, 1) - s1)
        s2 += (1 - a) * (s1 - s2)
        s3 += (1 - a) * (s2 - s3)
        out.append(s3)
    mean = sum(out) / n
    scale = rms / math.sqrt(sum((v - mean) ** 2 for v in out) / n)
    return [(v - mean) * scale for v in out]


def gate_shape(n, edge):
    """The pen's gate over n samples: 'hard' opens and shuts at once, 'soft' ramps over
    30 ms at both ends."""
    if edge == 'hard':
        return [1.0] * n
    ramp = int(.03 * RATE)
    return [min(1.0, i / ramp, (n - 1 - i) / ramp) for i in range(n)]


@lru_cache(maxsize=None)
def pen(seed, level=-66.0, syllables=8, syllable=.22, gap=.16, edge='hard', lead=LEAD, pop=None,
        initial=None, tail=.3, drawn=None):
    """A pen session: `lead` s of digital zeros, an optional latch-on pop (`pop` s long, loud
    at once, an 80 ms plateau, then -24 dB by its end; 0.3 s of zeros after it), then
    syllables of whisper-band noise at `level` dBFS over the pen's own noise, digital zeros
    between them (gap + 0...6 ms, so the cuts fall anywhere in a 10 ms frame).
    initial: (seconds, dB below) - a fricative-like lead-in (2-6 kHz noise) before each
    vowel, as in s/sh/x/q/c/h/f. drawn: seconds of one steady whisper before the syllables.
    Returns (pcm, ((start, vowel, end) per syllable)) in samples."""
    rnd = random.Random(seed)
    out = [0.0] * int(lead * RATE)
    if pop:
        n = int(pop * RATE)
        plateau = min(n, int(.08 * RATE))
        thump = coloured(rnd, n, dbfs(-21), 60, 3000)
        for i in range(n):
            thump[i] *= 10 ** (-24 * max(0, i - plateau) / max(1, n - plateau) / 20)
        out += thump + [0.0] * int(.3 * RATE)

    def burst(parts):
        body = [v for part in parts for v in part]
        hiss = coloured(rnd, len(body), dbfs(PEN_NOISE), 100, 7000)
        return [(v + h) * g for v, h, g in zip(body, hiss, gate_shape(len(body), edge))]

    if drawn:
        out += burst([coloured(rnd, int(drawn * RATE), dbfs(level), 300, 6000)])
        out += [0.0] * int(gap * RATE)
    spans = []
    for _ in range(syllables):
        start = len(out)
        parts = []
        if initial:
            parts.append(coloured(rnd, int(initial[0] * RATE), dbfs(level - initial[1]), 2000, 6000))
        vowel = start + sum(len(part) for part in parts)
        parts.append(coloured(rnd, int(syllable * RATE), dbfs(level), 300, 6000))
        out += burst(parts)
        spans.append((start, vowel, len(out)))
        out += [0.0] * int((gap + rnd.random() * .006) * RATE)
    out += [0.0] * int(tail * RATE)
    return _pcm(out), tuple(spans)


@lru_cache(maxsize=None)
def over_rumble(seed, rumble_db, whisper_db=None, seconds=4.0):
    """An ungated microphone with rumble (no digital zeros); optionally whispered syllables
    (1-6 kHz) on top. Returns (pcm, ((start, start, end) per syllable))."""
    rnd = random.Random(seed)
    total = int(seconds * RATE)
    signal = rumble(rnd, total, dbfs(rumble_db))
    spans, position, n, ramp = [], int(.5 * RATE), int(.22 * RATE), int(.03 * RATE)
    while whisper_db is not None and position + n < total - int(.3 * RATE):
        voice = coloured(rnd, n, dbfs(whisper_db), 1000, 6000)
        for i in range(n):
            signal[position + i] += voice[i] * min(1.0, i / ramp, (n - 1 - i) / ramp)
        spans.append((position, position, position + n))
        position += n + int(.15 * RATE)
    return _pcm(signal), tuple(spans)


@lru_cache(maxsize=None)
def ungated(seed, level=-60.0, floor=-74.0, syllables=6, syllable=.22, gap=.3, lead=.6, initial=None, weak=None):
    """A microphone without a gate (PV60, Tidepool, a laptop): hiss (100 Hz-7 kHz) at `floor`
    dBFS throughout, `lead` s of it alone, then whispered syllables at `level` dBFS with 5 ms
    edges. initial: (seconds, dB below) - a fricative-like lead-in (2-6 kHz) before each vowel.
    weak: every other syllable this many dB softer. Returns (pcm, ((start, vowel, end) per
    syllable)) in samples."""
    rnd = random.Random(seed)
    voice, spans, ramp = [0.0] * int(lead * RATE), [], int(.005 * RATE)
    for k in range(syllables):
        start = len(voice)
        db = level - (weak if weak and k % 2 else 0)
        parts = [coloured(rnd, int(initial[0] * RATE), dbfs(db - initial[1]), 2000, 6000)] if initial else []
        vowel = start + sum(len(part) for part in parts)
        parts.append(coloured(rnd, int(syllable * RATE), dbfs(db), 300, 6000))
        body = [v for part in parts for v in part]
        voice += [v * min(1.0, i / ramp, (len(body) - 1 - i) / ramp) for i, v in enumerate(body)]
        spans.append((start, vowel, len(voice)))
        voice += [0.0] * int(gap * RATE)
    hiss = coloured(rnd, len(voice), dbfs(floor), 100, 7000)
    return _pcm(v + h for v, h in zip(voice, hiss)), tuple(spans)


@lru_cache(maxsize=None)
def spoken(seed, level=-22.0, seconds=1.5, lead=LEAD, tail=.3):
    """Normal speech that never lets the gate shut: `seconds` of voice-band noise at `level`
    dBFS whose level rises and falls 20 dB four times a second (syllables), starting from a
    trough. Returns (pcm, onset sample)."""
    rnd = random.Random(seed)
    n = int(seconds * RATE)
    voice = coloured(rnd, n, dbfs(level), 300, 6000)
    hiss = coloured(rnd, n, dbfs(PEN_NOISE), 100, 7000)
    envelope = [10 ** (-(1 + math.cos(2 * math.pi * 4 * i / RATE)) / 2) for i in range(n)]  # 0...-20 dB
    out = [0.0] * int(lead * RATE) + [v * e + h for v, e, h in zip(voice, envelope, hiss)] + [0.0] * int(tail * RATE)
    return _pcm(out), int(lead * RATE)


def run(dsp, pcm, chunk=800):
    """Feed like the capture thread does (chunk in samples); returns (output, samples heard
    when boosting engaged)."""
    out, engaged_at = [], None
    for k in range(0, len(pcm), chunk * 2):
        out += dsp.process(pcm[k:k + chunk * 2])
        if engaged_at is None and dsp.engaged:
            engaged_at = (k + chunk * 2) // 2
    out += dsp.flush()
    return b''.join(out), engaged_at


def session(mode='auto'):
    dsp = WhisperDSP()
    dsp.begin(mode)
    return dsp


def rms_db(pcm, start, end):
    return 20 * math.log10(max(audioop.rms(pcm[start * 2:end * 2], 2), 1e-6) / 32768)


def gain_db(before, after, start, end):
    return rms_db(after, start, end) - rms_db(before, start, end)


def middle(start, end):
    return start + (end - start) // 5, end - (end - start) // 5


def lift(level):
    """What v2 should give speech at `level` dBFS: up to the target, at most MAX_GAIN_DB."""
    return min(MAX_GAIN_DB, TARGET_DB - level)


class GatedWhisperTests(unittest.TestCase):
    """0.11.3 never engaged on these (speech_seconds 0, cancelled as silence): the floor
    rose to the whisper and a syllable cut off by the gate was no evidence."""

    def test_gated_whispers_engage_count_as_speech_and_never_move_the_floor(self):
        for level in (-60.0, -66.0):
            for edge in ('hard', 'soft'):
                with self.subTest(level=level, edge=edge):
                    pcm, spans = pen(1, level, edge=edge)
                    dsp = session('auto')
                    out, engaged_at = run(dsp, pcm)
                    self.assertEqual(len(out), len(pcm))
                    self.assertIsNotNone(engaged_at, 'a gated whisper engages boosting')
                    self.assertLessEqual(engaged_at - spans[0][0], HOLD_MAX, 'decided within a second of speech')
                    voiced = sum(end - start for start, _, end in spans) / RATE
                    self.assertGreater(dsp.speech_seconds, max(WHISPER_SPEECH, .7 * voiced),
                                       'syllables cut off by the gate are speech: never cancelled as silence')
                    self.assertLessEqual(dsp.floor_db, FLOOR_DEFAULT_DB + .5, 'the floor never rose to the whisper')
                    self.assertLess(dsp.stats()['decision_db'], SOFT_DB - 15)
                    for start, vowel, end in spans:  # the first syllable too: held and released lifted
                        self.assertGreater(gain_db(pcm, out, *middle(vowel, end)), lift(level) - 4)
                        self.assertLess(gain_db(pcm, out, *middle(vowel, end)), MAX_GAIN_DB + .5)

    def test_weak_initials_get_the_gain_of_the_following_vowel(self):
        # A fricative / aspirated initial 12 dB below its vowel: the old gate gave it ~0 dB
        # and the vowel +35...42. v2 holds the gain and looks 100 ms ahead - across chunk
        # edges too (chunks of 10, 45, 50 and 100 ms).
        for edge in ('hard', 'soft'):
            pcm, spans = pen(2, -64.0, initial=(.07, 12), edge=edge)
            for mode in ('auto', 'on'):
                for chunk in (800, 1600, 160, 720):
                    with self.subTest(edge=edge, mode=mode, chunk=chunk):
                        out, _ = run(session(mode), pcm, chunk)
                        self.assertEqual(len(out), len(pcm))
                        for start, vowel, end in spans:
                            onset = gain_db(pcm, out, start, vowel)
                            nucleus = gain_db(pcm, out, *middle(vowel, end))
                            self.assertGreater(nucleus, lift(-64.0) - 4)
                            self.assertLess(abs(onset - nucleus), 3, f'initial {onset:.1f} dB, vowel {nucleus:.1f} dB')

    def test_the_gain_is_held_through_digital_zeros(self):
        pcm, spans = pen(3, -64.0, syllables=6, gap=.8)
        out, _ = run(session('auto'), pcm)
        gains = [gain_db(pcm, out, *middle(vowel, end)) for _, vowel, end in spans]
        self.assertLess(max(gains) - min(gains), 2, 'every word gets the same lift: no pumping')
        # Mid-session, two seconds of digital zeros leave the gain exactly where it was.
        dsp = session('auto')
        cut = spans[3][2] * 2
        for k in range(0, cut, 1600):
            dsp.process(pcm[k:min(cut, k + 1600)])
        self.assertTrue(dsp.engaged)
        before = dsp.gain_db
        out = b''.join(b''.join(dsp.process(bytes(1600))) for _ in range(40))
        self.assertEqual(dsp.gain_db, before, 'zeros never move the gain')
        speech = audioop.max(out[:LOOK * FRAME * 2], 2)
        self.assertLess(audioop.max(out[(LOOK + 1) * FRAME * 2:], 2), .02 * speech, 'the pause stays silent')

    def test_gate_cuts_do_not_click(self):
        # After a hard cut the high-pass filters' tail decays smoothly; forcing it to zero
        # (v2 draft) clicked at every cut and cost the proxy recogniser ~2.7 points of CER.
        pcm, spans = pen(1, -64.0)
        out, _ = run(session('auto'), pcm)
        samples = array.array('h', out)

        def steepest(start, end):
            return max(abs(samples[k] - samples[k - 1]) for k in range(start, end))
        for _, vowel, end in spans:
            inside = steepest(vowel + 800, end - 800)
            self.assertLessEqual(steepest(end - 160, end + 480), inside, 'no step at the cut beyond the speech')
            self.assertLess(max(abs(v) for v in samples[end + 160:end + 800]), .05 * inside)

    def test_a_drawn_out_whisper_that_ends_in_zeros_keeps_the_floor(self):
        # 1.6 s of one level whisper: the 1 s steady rule used to make it the floor, and on
        # a gated pen nothing ever brings such a floor down again - every later word was lost.
        pcm, spans = pen(9, -64.0, drawn=1.6)
        dsp = session('auto')
        out, engaged_at = run(dsp, pcm)
        self.assertIsNotNone(engaged_at)
        self.assertLessEqual(dsp.floor_db, FLOOR_DEFAULT_DB + .5)
        self.assertGreater(dsp.speech_seconds, 1.6, 'the drawn-out whisper counts as speech')
        for _, vowel, end in spans:
            self.assertGreater(gain_db(pcm, out, *middle(vowel, end)), lift(-64.0) - 4)

    def test_a_session_that_starts_inside_a_gated_syllable_still_engages(self):
        pcm, spans = pen(11, -66.0, lead=0.0)
        dsp = session('auto')
        out, engaged_at = run(dsp, pcm)
        self.assertIsNotNone(engaged_at)
        self.assertLessEqual(dsp.floor_db, FLOOR_DEFAULT_DB + .5, 'the first frames never seed a gated floor')
        for _, vowel, end in spans[1:]:
            self.assertGreater(gain_db(pcm, out, *middle(vowel, end)), lift(-66.0) - 4)


class LatchOnPopTests(unittest.TestCase):
    def test_the_pop_never_decides_normal_and_is_muted_in_a_whisper_session(self):
        for length in (.08, .15, .25):
            for mode in ('auto', 'on'):
                with self.subTest(pop=length, mode=mode):
                    pcm, spans = pen(4, -64.0, pop=length)
                    dsp = session(mode)
                    out, engaged_at = run(dsp, pcm)
                    self.assertEqual(len(out), len(pcm))
                    self.assertIsNotNone(engaged_at, 'the pop did not settle the session as normal speech')
                    stats = dsp.stats()
                    self.assertLess(stats['decision_db'], SOFT_DB - 15, 'decided on the whisper, not the pop')
                    self.assertAlmostEqual(stats['pop_seconds'], length, delta=.025)
                    self.assertTrue(stats['pop_muted'])
                    start, end = int(LEAD * RATE), int((LEAD + length) * RATE)
                    self.assertEqual(out[(start - FRAME) * 2:end * 2], bytes((end - start + FRAME) * 2),
                                     'the pop is muted')
                    self.assertLess(audioop.max(out[end * 2:(spans[0][0] - FRAME) * 2], 2), 50,
                                    'and nothing of it comes back after (its filter tail fades in from zero)')
                    self.assertGreater(gain_db(pcm, out, *middle(*spans[0][1:])), lift(-64.0) - 4,
                                       'the first syllable is lifted like the rest')
                    voiced = sum(end - begin for begin, _, end in spans) / RATE
                    self.assertLess(dsp.speech_seconds, voiced + .05, 'the pop is never speech')

    def test_a_pop_alone_is_no_speech_and_goes_out_silent(self):
        pcm, _ = pen(6, pop=.25, syllables=0)
        dsp = session('auto')
        out, engaged_at = run(dsp, pcm)
        self.assertIsNone(engaged_at)
        self.assertEqual(dsp.speech_seconds, 0, 'the silent-recording cancel still applies')
        self.assertEqual(out, bytes(len(pcm)), 'nothing but digital zeros reaches the engine')
        self.assertEqual(SpeechMeter().feed(out)['speech_seconds'], 0, 'nor anything the VAD could call speech')

    def test_a_first_soft_word_right_after_the_latch_is_never_muted(self):
        # Soft but louder than -45 dBFS, at once, cut off by the gate within 300 ms: it looks
        # like a pop, but it is no louder than the rest of the session - so it is speech.
        pcm, spans = pen(14, -44.0)
        dsp = session('auto')
        out, engaged_at = run(dsp, pcm)
        self.assertIsNotNone(engaged_at)
        self.assertFalse(dsp.stats()['pop_muted'])
        for _, vowel, end in spans:
            self.assertGreater(gain_db(pcm, out, *middle(vowel, end)), lift(-44.0) - 4)

    def test_normal_speech_after_the_pop_is_untouched(self):
        # Decided normal: nothing is muted - a short first word right after the latch can
        # look just like a pop, and normal speech is sent byte for byte.
        for pcm, _ in (pen(7, -22.0, pop=.2), pen(8, -22.0)):
            dsp = session('auto')
            out, engaged_at = run(dsp, pcm)
            self.assertIsNone(engaged_at)
            self.assertEqual(out, pcm)
            self.assertFalse(dsp.stats()['pop_muted'])

    def test_a_short_word_alone_is_sent_as_it_came_and_counts_as_speech(self):
        # Review 2026-10-14: one short word (好) with no pop before it - loud at once, cut off
        # by the gate within 300 ms - looked like the pop. With nothing else heard within a
        # second it went out as digital zeros and, never counted as speech, the recording
        # was cancelled as silent (0.11.3 sent it). Alone, only a transient that dies away is
        # muted now (test_a_pop_alone_is_no_speech_and_goes_out_silent); a word is not.
        for kw in (dict(syllables=1), dict(syllables=1, syllable=.25, tail=1.0)):
            with self.subTest(**kw):
                pcm, _ = pen(8, -22.0, **kw)
                dsp = session('auto')
                out, engaged_at = run(dsp, pcm)
                self.assertIsNone(engaged_at)
                self.assertEqual(out, pcm)
                self.assertFalse(dsp.stats()['pop_muted'])
                self.assertGreaterEqual(dsp.speech_seconds, WHISPER_SPEECH, 'never cancelled as a silent recording')

    def test_a_short_first_word_and_a_pause_wait_for_what_follows(self):
        # Review 2026-10-14: the word was muted a second after it whatever came next. Now a
        # pop with nothing after it waits (up to POP_WAIT) - here normal speech says it was a word.
        for gap in (.5, .8, 1.6):
            with self.subTest(gap=gap):
                first, _ = pen(8, -22.0, syllables=1, tail=gap)
                rest, _ = pen(16, -22.0, syllables=6, syllable=.3, lead=0)
                pcm = first + rest
                dsp = session('auto')
                out, engaged_at = run(dsp, pcm)
                self.assertIsNone(engaged_at)
                self.assertEqual(out, pcm, 'the first word too, byte for byte')
                self.assertFalse(dsp.stats()['pop_muted'])

    def test_a_pop_waits_for_speech_at_most_pop_wait(self):
        # Pop, 3.5 s of digital zeros, then a whisper: the pop is judged by itself after
        # POP_WAIT (a transient: muted), never holding more back, and the whisper still engages.
        pop, _ = pen(6, pop=.25, syllables=0, tail=3.5)
        whisper, spans = pen(1, -66.0, lead=0)
        pcm, shift = pop + whisper, len(pop) // 2
        dsp = session('auto')
        fed = sent = 0
        out = []
        for k in range(0, len(pcm), 1600):
            piece = pcm[k:k + 1600]
            fed += len(piece) // 2
            out += dsp.process(piece)
            sent = sum(len(chunk) for chunk in out) // 2
            self.assertLessEqual(fed - sent, POP_WAIT + LOOK * FRAME + 800)
        out = b''.join(out + dsp.flush())
        self.assertEqual(len(out), len(pcm))
        self.assertTrue(dsp.stats()['pop_muted'])
        self.assertTrue(dsp.engaged)
        start, end = int(LEAD * RATE), int((LEAD + .25) * RATE)
        self.assertEqual(out[start * 2:end * 2], bytes((end - start) * 2), 'the pop is muted')
        for _, vowel, end in spans[1:]:
            self.assertGreater(gain_db(pcm, out, *middle(vowel + shift, end + shift)), lift(-66.0) - 4)

    def test_a_pop_running_straight_into_a_whisper_never_decides_normal(self):
        # No digital zeros between the pop and the first syllable: the candidate outlasts
        # 300 ms, and its first 300 ms (the pop) still stay out of the decision - it never
        # rose into its peak, so it is not taken for a voice (test_a_first_word_that_rises_...).
        pcm, _ = pen(4, -64.0, pop=.15)
        cut, gap = int((LEAD + .15) * RATE), int(.3 * RATE)
        pcm = pcm[:cut * 2] + pcm[(cut + gap) * 2:]
        dsp = session('auto')
        out, engaged_at = run(dsp, pcm)
        self.assertEqual(len(out), len(pcm))
        self.assertIsNotNone(engaged_at)
        self.assertLess(dsp.stats()['decision_db'], SOFT_DB - 15)


class NormalSpeechOnThePenTests(unittest.TestCase):
    def test_normal_speech_through_the_gate_is_sent_byte_for_byte_and_undelayed(self):
        for edge in ('hard', 'soft'):
            with self.subTest(edge=edge):
                pcm, _ = pen(12, -22.0, edge=edge, syllables=10)
                dsp = session('auto')
                pieces = []
                for k in range(0, len(pcm), 1600):
                    chunk = pcm[k:k + 1600]
                    decided = dsp.decided
                    returned = dsp.process(chunk)
                    if decided:
                        self.assertEqual(returned, [chunk], 'once decided, each chunk goes out at once, as it came')
                    pieces += returned
                pieces += dsp.flush()
                self.assertFalse(dsp.engaged)
                self.assertEqual(b''.join(pieces), pcm)
                self.assertGreater(dsp.speech_seconds, 1.0)

    def test_quiet_normal_speech_with_soft_first_words_is_untouched(self):
        # Bench regression (TTS at -30 dBFS, first words -37...-44): its first 0.3 s of
        # speech read -37 dBFS and 0.11.3 / v2 at SOFT_DB -36 engaged on it. Two syllables
        # at -38 dBFS after the latch-on pop, then the rest at -26.
        soft, _ = pen(15, -38.0, syllables=2, pop=.12, tail=0)
        rest, _ = pen(16, -26.0, syllables=6, lead=0)
        pcm = soft + rest
        dsp = session('auto')
        out, engaged_at = run(dsp, pcm)
        self.assertIsNone(engaged_at)
        self.assertEqual(out, pcm)
        self.assertGreater(dsp.stats()['decision_db'], SOFT_DB)

    def test_a_first_word_that_rises_into_its_vowel_decides_at_once(self):
        # Review 2026-10-14: with no latch-on pop the first sound was still taken for one -
        # until the gate shut, or for its first 300 ms - and kept out of the decision, so
        # normal speech was held ~0.25 s longer (the first live text came that much later;
        # bench median 0.38-0.42 s after the onset, 0.11.3 0.14-0.17 s). A sound that rises
        # into its peak is no pop: it decides at once.
        for seed in (21, 22, 23):
            with self.subTest(seed=seed):
                pcm, onset = spoken(seed)
                dsp = session('auto')
                out, engaged_at = run(dsp, pcm)
                self.assertIsNone(engaged_at)
                self.assertEqual(out, pcm)
                self.assertLessEqual(dsp.decided_at - onset, .25 * RATE, 'decided on the first syllable')
                self.assertEqual(dsp.stats()['pop_seconds'], 0)

    def test_short_bumps_ending_in_zeros_are_not_speech(self):
        pcm, _ = pen(10, -50.0, syllable=.05, gap=.3, syllables=10)
        dsp = session('auto')
        out, engaged_at = run(dsp, pcm)
        self.assertIsNone(engaged_at)
        self.assertEqual(dsp.speech_seconds, 0, 'shorter than a syllable: no evidence')
        self.assertEqual(out, pcm)


class ComfortNoiseTests(unittest.TestCase):
    """Lifted 30+ dB, a whisper leaves the gate's digital zeros 70+ dB below it; the bench's
    proxy recogniser (SenseVoice) lost ~6 points of CER to that until a steady floor filled
    the gaps."""

    def test_gaps_in_a_lifted_gated_whisper_carry_a_steady_floor_the_vad_ignores(self):
        for edge in ('hard', 'soft'):
            for mode in ('auto', 'on'):
                with self.subTest(edge=edge, mode=mode):
                    pcm, spans = pen(1, -66.0, edge=edge)
                    dsp = session(mode)
                    out, _ = run(dsp, pcm)
                    self.assertTrue(dsp.stats()['comfort'])
                    lead = spans[0][0] - (LOOK + 2) * FRAME
                    self.assertEqual(out[:lead * 2], bytes(lead * 2), 'nothing before the first word')
                    gaps = b''
                    for (_, _, end), (start, _, _) in zip(spans, spans[1:]):
                        gap = (end + 800, start - 800)  # away from the filters' tails and the next onset
                        self.assertAlmostEqual(rms_db(out, *gap), COMFORT_DB, delta=1.5)
                        gaps += out[gap[0] * 2:gap[1] * 2]
                    tail = spans[-1][2] + 800
                    self.assertAlmostEqual(rms_db(out, tail, len(out) // 2), COMFORT_DB, delta=1.5,
                                           msg='to the end of the recording')
                    # WebRTC VAD flags its first ~70 ms of any sound above -68 dBFS while its noise
                    # model settles, then nothing: far below the silent-recording rule's 0.25 s.
                    self.assertLess(SpeechMeter().feed(gaps)['speech_seconds'], .1, 'the real VAD ignores it')

    def test_no_comfort_noise_without_a_gate_a_lift_or_a_word(self):
        pcm, _ = over_rumble(31, -50.0, -65.0)  # ungated microphone: its own noise fills the pauses
        dsp = session('auto')
        run(dsp, pcm)
        self.assertTrue(dsp.engaged)
        self.assertFalse(dsp.stats()['comfort'])
        normal, pop = pen(12, -22.0, syllables=10)[0], pen(6, pop=.25, syllables=0)[0]
        for name, pcm, sent in (('normal speech', normal, normal), ('a pop alone', pop, bytes(len(pop)))):
            dsp = session('auto')
            out, _ = run(dsp, pcm)
            self.assertFalse(dsp.stats()['comfort'], name)
            self.assertEqual(out, sent, name)
        for mode in ('auto', 'on'):
            dsp = session(mode)
            out, _ = run(dsp, bytes(RATE * 2))
            self.assertEqual(out, bytes(RATE * 2), mode)
            self.assertFalse(dsp.stats()['comfort'])
        dsp = session('on')  # lifted from the start, but no word ever reaches the delay line
        run(dsp, pop)
        self.assertFalse(dsp.stats()['comfort'])


class BandLevelTests(unittest.TestCase):
    def test_a_whisper_under_rumble_is_heard_and_rumble_alone_is_not(self):
        # Rumble swamps the full-band level; the band level (~1.5-6 kHz) still hears the
        # whisper (0.11.3 heard nothing at -50 / -55 dBFS rumble).
        for level in (-50.0, -55.0):
            with self.subTest(rumble=level):
                pcm, spans = over_rumble(31, level, -65.0)
                dsp = session('auto')
                out, engaged_at = run(dsp, pcm)
                self.assertIsNotNone(engaged_at)
                self.assertGreater(dsp.speech_seconds, .5)
                pcm, _ = over_rumble(32, level)
                dsp = session('auto')
                out, engaged_at = run(dsp, pcm)
                self.assertIsNone(engaged_at, 'rumble alone is no whisper')
                self.assertEqual(dsp.speech_seconds, 0)
                self.assertEqual(out, pcm)


class UngatedMicrophoneTests(unittest.TestCase):
    """The same front end runs for microphones without a gate: hiss throughout, no zeros."""

    def test_a_faint_initial_before_the_first_word_is_lifted_with_its_vowel(self):
        # An initial 24 dB under a -60 dBFS whisper hardly lifts -74 dBFS hiss: nothing
        # stands out before the vowel. v2's first cut sent it unlifted and the vowel +30 dB
        # (the proxy recogniser heard 合同 as 各种); now the last 100 ms of sound wait.
        pcm, spans = ungated(41, initial=(.08, 24))
        start, vowel, end = spans[0]
        for mode in ('auto', 'on'):
            for chunk in (800, 1600, 160, 720):
                with self.subTest(mode=mode, chunk=chunk):
                    out, _ = run(session(mode), pcm, chunk)
                    self.assertEqual(len(out), len(pcm))
                    onset = gain_db(pcm, out, start + (vowel - start) // 4, vowel)
                    nucleus = gain_db(pcm, out, *middle(vowel, end))
                    self.assertGreater(nucleus, lift(-60.0) - 4)
                    self.assertLess(abs(onset - nucleus), 3, f'initial {onset:.1f} dB, vowel {nucleus:.1f} dB')

    def test_weak_syllables_that_do_not_stand_out_from_the_hiss_keep_the_held_gain(self):
        # Every other syllable 10 dB softer, only ~2 dB above -66 dBFS hiss. v2's first cut
        # expanded such frames 10 dB down as noise (bench 2026-10-14: ungated whispers at
        # 0 dB SNR lost ~6 points of CER to it). The held gain lifts them like the rest.
        pcm, spans = ungated(42, level=-58.0, floor=-66.0, gap=.4, weak=10)
        out, _ = run(session('on'), pcm)
        gains = [gain_db(pcm, out, *middle(vowel, end)) for _, vowel, end in spans]
        self.assertGreater(min(gains[::2]), lift(-58.0) - 4)
        self.assertLess(max(gains) - min(gains), 2, f'strong {gains[::2]}, weak {gains[1::2]}')


class LatencyAndCostTests(unittest.TestCase):
    def test_the_look_ahead_delays_lifted_audio_only_and_flush_sends_the_tail(self):
        pcm, _ = pen(1, -64.0)
        dsp = session('on')
        fed = sent = 0
        for k in range(0, len(pcm), 1600):
            piece = pcm[k:k + 1600]
            fed += len(piece) // 2
            sent += sum(len(chunk) for chunk in dsp.process(piece)) // 2
            if dsp.decided:
                self.assertLessEqual(fed - sent, LOOK * FRAME, 'at most the 100 ms look-ahead behind')
        sent += sum(len(chunk) for chunk in dsp.flush()) // 2
        self.assertEqual(sent, fed, 'flush sends the tail at once: nothing waits for audio to come')
        self.assertEqual(dsp.flush(), [])

    def test_cpu_stays_far_below_five_percent_of_a_core(self):
        pcm, _ = pen(13, -64.0, syllables=27, initial=(.05, 10))  # ~11 s
        seconds = len(pcm) / 2 / RATE
        for mode in ('auto', 'on'):
            dsp = session(mode)
            started = time.perf_counter()
            for k in range(0, len(pcm), 1600):
                dsp.process(pcm[k:k + 1600])
            dsp.flush()
            share = (time.perf_counter() - started) / seconds
            self.assertLess(share, .05, f'{mode}: {share * 100:.2f}% of a core')


class PenControllerTests(unittest.TestCase):
    def record(self, pcm, chunk=800, meter=lambda: SpeechMeter(detector=Deaf())):
        backend, events = FakeBackend(), []
        with patch('bridge.controller.SpeechMeter', meter):
            control = Controller(lambda *event: events.append(event), backend, FakeCapture)
            try:
                control.whisper_mode = 'auto'
                control.command('arm')
                wait_for(lambda: control.phase == 'idle')
                control.command('start')
                wait_for(lambda: control.phase == 'recording')
                for k in range(0, len(pcm), chunk * 2):
                    control._audio(pcm[k:k + chunk * 2])
                control.command('stop')
                wait_for(lambda: control.phase == 'processing' or any(k == 'empty' for k, _ in events), 6)
                wait_for(lambda: control.phase in ('processing', 'idle'), 6)
            finally:
                control.close()
        sent = b''.join(call[1] for call in backend.calls if isinstance(call, tuple))
        states = [value for kind, value in events if kind == 'state']
        return backend, sent, states, any(kind == 'empty' for kind, _ in events)

    def test_a_pen_whisper_reaches_the_engine_and_a_pop_alone_is_cancelled(self):
        pcm, _ = pen(5, -66.0, pop=.15)
        backend, sent, states, empty = self.record(pcm)
        self.assertFalse(empty, 'a whisper the VAD cannot hear is not discarded as silence')
        self.assertIn('stop', backend.calls)
        self.assertEqual(len(sent), len(pcm))
        self.assertTrue(any(s['phase'] == 'recording' and s.get('whisper') for s in states))
        pcm, _ = pen(6, pop=.25, syllables=0)
        backend, sent, states, empty = self.record(pcm, meter=SpeechMeter)  # the real VAD
        self.assertTrue(empty, 'only the latch-on pop: cancelled, never sent for recognition')
        self.assertNotIn('stop', backend.calls)

    def test_a_short_word_alone_reaches_the_engine_untouched(self):
        # Review 2026-10-14: muted as a pop and cancelled as a silent recording.
        pcm, _ = pen(8, -22.0, syllables=1, tail=1.0)
        for name, meter in (('real VAD', SpeechMeter), ('VAD hears nothing', lambda: SpeechMeter(detector=Deaf()))):
            with self.subTest(meter=name):
                backend, sent, states, empty = self.record(pcm, meter=meter)
                self.assertFalse(empty)
                self.assertIn('stop', backend.calls)
                self.assertEqual(sent, pcm)


if __name__ == '__main__':
    unittest.main()
