"""气声 (whispered / very soft speech) front end for the pen microphone - WhisperDSP v2.

16 kHz mono S16LE in, the same out. Everything heavy runs inside audioop (C):
there is no per-sample Python loop, so a 50 ms chunk costs well under a
millisecond. Nothing is stored; PCM only passes through memory.

The Baseus pen gates its own audio: its firmware sends digital zeros whenever
nobody speaks - also while the microphone is latched on - and lets sound through
with hard (10 ms) or short (~30 ms) edges. Normal speech peaks around -20 dBFS,
a whisper sits at -60...-70 dBFS, only a few dB above the pen's own noise, and
about 0.3 s after the start key the microphone latching on makes a pop (~-21
dBFS, 80-300 ms). v2 is modelled on Tidepool, which handles whispers well with
no gate, noise suppression or expander at all: one smooth gain that only speech
moves and that is held, never dropped, in between. (Denoising artefacts hurt
recognition more than residual noise does.)

Per chunk:
  1. DC removal + gentle high-pass. Two cascaded "x - lowpass(x)" stages;
     the one-pole low-pass is audioop.ratecv at 1:1 with weights (a C IIR),
     run on 32-bit samples so integer rounding stays far below one 16-bit
     step. Each stage is ~64 Hz; together -3 dB at ~100 Hz, 12 dB/octave.
     Once a whisper is engaged, the output gets two more stages at ~130 Hz
     (-3 dB near 200 Hz overall): a whisper has no voicing and almost no
     energy down there, while hand-on-pen rumble and room hum do. Analysis
     also gets a band level: two more one-pole stages (~900 Hz each: -23 dB
     at 300 Hz, about -4 dB from 2 to 6 kHz, given back), where a whisper's
     energy is, so rumble that swamps the full-band level does not hide it.
  2. Analysis per 10 ms frame: both levels in dBFS and a noise floor for each
     (10th percentile of the last 4 s of background frames).
     Digital zeros are the pen's gate (or a microphone latched off), neither
     background nor evidence. A gated pen may never send background at all,
     so the floors keep a default (-80 dBFS) until real background arrives:
     frames within 300 ms of digital zeros - a syllable's edges, the pen's own
     noise around speech - never feed them. An ungated microphone (no digital
     zeros so far) seeds them from its first frames, as before; zeros within
     the first 2 s throw that seed away. While no floor is known, 300 ms of
     flat level far from zeros counts as background too.
     A frame is *above* when either level is clearly above its floor (6 dB,
     5 dB once a whisper is engaged) and an absolute minimum (-72 dBFS, -74 for
     the band level); it is *active* from the second such frame in a row, so
     one noisy frame never is.
     Speech is modulated: a run of frames above the floor counts as speech
     when it is syllable-long (>= 80 ms) and either level then falls >= 6 dB
     below the run's recent peak - inside the run or within 50 ms after it -
     or when the run ends in digital zeros (the pen's gate shutting is a
     syllable's offset). That keeps a whisper - which WebRTC VAD barely hears -
     from being cancelled as silence, while a click, a breath of noise, steady
     noise that steps up, or a microphone fading in is never evidence. A run
     that stays level for 1 s is the new background and resets the floor; on a
     gated source only after another second, and digital zeros within 3 s undo
     it (a drawn-out whisper: no background opens a gate and lets it shut).
     Frames of counted runs never feed the floor, so a whisper cannot pin the
     floor at its own level.
  3. Latch-on pop: a short transient right after the session's leading digital
     zeros (loud within 30 ms and at full level within 20 ms - a sound that
     rises into its peak is a voice - over within 300 ms) is never speech
     evidence and never decides; a longer stretch keeps its first 300 ms out
     of the decision. A short first word can look just like one, so a pop that
     ends in digital zeros stays held until speech says which it was (up to
     3 s): its frames go out muted in a soft session where it is >= 15 dB
     louder than the speech, never after a normal decision. With nothing else
     heard it is muted only when it was loud (> -35 dBFS) and died away like a
     transient (15 dB down by its end, never rising again); otherwise it goes
     out as it came and counts as speech: a one-word recording is no silence.
  4. Decision ("auto"): the first syllables are held back (at most 1 s; a held
     pop waits longer, see 3.) until their level is known; before that, while
     anything but digital zeros was heard in the last 100 ms, the last 100 ms
     stay back too, so a weak initial before the first active frame - often too faint to tell
     from the floor - is held, and lifted, with its vowel (sent unlifted, it
     ends up 30 dB under the vowel: 合同 came out as 各种 from an ungated
     microphone). A gated pen's leading zeros still go out at once.
     Consistently soft speech (below -40 dBFS: midway between quiet normal
     speech and a loud whisper) engages boosting, and the held audio is
     released already lifted, so the first words of a whisper benefit as
     much as the rest. Only syllable-shaped runs (not runs cut off by the
     gate) may decide "clearly normal" early. Normal speech is sent
     byte-for-byte untouched, and undelayed once decided. "on" always boosts;
     "off" bypasses everything.
  5. AGC (engaged): a speech-level follower (fast attack, slow release) runs on
     active frames only and sets gain = target - level, 0...+36 dB (target
     -26 dBFS: speech at -62 dBFS reaches it; more gain on a few effective
     bits adds noise, not information). Through pauses, digital zeros and
     noise the gain is held, never dropped - and no expander pushes the hiss
     down either: one (10 dB off frames below the floor, away from speech)
     took weak syllables for noise at low SNR and bought nothing measurable
     elsewhere (bench 2026-10-14). A 100 ms delay line across chunks gives
     the gain a look-ahead: each frame is lifted with the gain the AGC
     reaches 100 ms later, and a frame already louder than the target (a pop,
     a cough, normal speech after a whisper) is lifted at most to target +
     6 dB, starting before it arrives - so a weak initial (s, sh, x, q, c, h,
     f, aspiration) gets the gain of the vowel after it. The delay applies to
     lifted audio only, and flush() sends the tail at once: no added latency
     at the end of a recording.
  6. Soft limiter: per 2.5 ms block the peak after gain is bent by a soft
     knee (-6 dBFS) toward a -1 dBFS ceiling. The block's samples are known
     before scaling, so no sample can clip; the limiter never pushes below
     the original level, and audioop.mul saturates as the last guard. When a
     session switches from untouched to lifted audio mid-way, the first 20 ms
     crossfade.
  7. Comfort noise (engaged, gated source): from ~100 ms before the first word,
     steady white noise at -64 dBFS goes under the lifted output, as in
     telephony's comfort-noise generation. Lifting a whisper by 30+ dB leaves
     the gate's digital zeros 70+ dB below it - a silence no microphone makes,
     which log-energy recogniser features read as an extreme (SenseVoice lost
     ~6 points of CER on gated whispers to it; a 1-LSB floor already won back
     most of that). Too quiet for WebRTC VAD to call speech; never on an
     ungated microphone, in untouched audio, or before the first word.
"""
from collections import deque
import math
import random
import warnings

with warnings.catch_warnings():
    warnings.simplefilter('ignore', DeprecationWarning)
    import audioop

MODES = ('auto', 'on', 'off')
RATE = 16000
FRAME = 160                  # 10 ms analysis frame, samples
BLOCK = 40                   # 2.5 ms gain / limiter block, samples
SCALE = 16384                # 32-bit working domain: one 16-bit step
FULL = 32768.0

HP_WEIGHT = 39               # y = (x + 39 y[-1]) / 40: one-pole low-pass at ~64 Hz
WHISPER_HP_WEIGHT = 19       # ~130 Hz: the extra stages once a whisper is engaged
BAND_WEIGHTS = (3, 7)        # y = (3 x + 7 y[-1]) / 10: ~900 Hz, the band level's two stages
BAND_GAIN_DB = 4.0           # what those stages take from 1.5-6 kHz: a whisper reads the same in both levels

SILENT_RMS = 1.5             # 16-bit steps: digital silence / DC only
FLOOR_WINDOW = 400           # frames (4 s) of background levels
FLOOR_PERCENTILE = .1
FLOOR_MIN = 10               # background frames before a floor leaves its default
FLOOR_DEFAULT_DB = -80.0     # until real background is heard (a gated pen may never send any)
ZERO_GUARD = 30              # frames (300 ms) next to digital zeros: never background
SEED_TRUST = 200             # frames: zeros this early show a gated pen, and a floor seeded before them goes
# Very soft speech (field report 2026-10-12: Doubao perfect at normal level, extremely
# inaccurate when speaking very quietly) sits at -60...-70 dBFS, a few dB above a quiet
# microphone's floor.
SNR_DB = 6.0                 # above: this far above the noise floor...
ENGAGED_SNR_DB = 5.0         # ...or this far once a whisper is engaged (faint consonants count)
ABS_MIN_DB = -72.0           # ...and above this, whatever the floor says
BAND_ABS_MIN_DB = -74.0      # the same for the band level
SUSTAIN = 8 * FRAME          # an active run this long is speech, not a click...
MOD_DB = 6.0                 # ...once its level falls this far below the run's peak (or the gate shuts)
RUN_GAP = 5                  # frames: the fall may come this soon after the run (its offset)
FALL_FRAMES = 30             # ...from a peak in the last 300 ms: syllables, not a slow drift
GATE_FRAMES = 2              # frames above the floor in a row before a frame is active
STEADY_FRAMES = 100          # 1 s above the floor without that fall: background, not speech...
STEADY_WAIT = 100            # ...a gated source waits 1 s more before resetting the floor,
STEADY_UNDO = 300            # and digital zeros within 3 s undo it: a drawn-out whisper
BOOT_FRAMES = 30             # while no floor is known yet, 300 ms this flat is background too
BOOT_SPREAD_DB = 6.0         # (its frames all within this range: no onset, no syllable)

POP_LEAD = 5                 # frames of digital zeros the session starts with (microphone still latched off)
POP_ONSET = 100              # a latch-on pop starts within 1 s of the session start,
POP_ATTACK = 3               # is loud within its first 3 frames
POP_MIN_DB = -45.0           # (louder than any whisper)
POP_EDGE_DB = 3.0            # with its first 20 ms this close to its peak (a switch, not a voice rising into a vowel)
POP_MAX = 30                 # and ends in digital zeros within 300 ms. It is muted
POP_ABOVE_DB = 15.0          # in a soft session when this much louder than its speech,
POP_LOUD_DB = -35.0          # or - nothing else heard - when its peak is above this
POP_FADE_DB = 15.0           # and it died away like a transient: its last 30 ms this far below its peak,
POP_RISE_DB = 3.0            # never rising again by more than this (a short word through the gate does not)
POP_WAIT = 3 * RATE          # samples held at most while a pop waits for speech to judge it by

TARGET_DB = -26.0            # wanted speech level (upper envelope of 10 ms frames)
HEADROOM_DB = 6.0            # no lifted frame ends up louder than target + this
MAX_GAIN_DB = 36.0           # speech at -62 dBFS reaches the target; more only lifts the pen's noise
# Soft: the speech level (80th pct of counted frames) below SOFT_DB. Normal pen speech
# peaks near -20 dBFS and whispers sit at -52...-70; the first 0.3 s of quiet normal
# speech can read -37 (soft first words), so the line sits in the middle of the gap.
SOFT_DB = -40.0
LOUD_DB = -33.0              # median of the first syllable-shaped frames at or above this = clearly normal
CLEAR_MARGIN_DB = 6.0        # this far below SOFT_DB is clearly a whisper
EARLY_FRAMES = 8             # counted frames before a "clearly normal" decision
EARLY_SOFT_FRAMES = 15       # counted frames before a "clearly soft" decision
DECIDE_FRAMES = 30           # counted frames for a full decision
HOLD_MAX = RATE              # samples held at most while deciding (1 s)
DEFAULT_GAIN_DB = 12.0       # "on" with no speech to measure yet
LATE_WINDOW = 150            # counted frames considered for a later switch to soft
LATE_MIN = 100
LATE_MARGIN_DB = 2.0

LEVEL_ATTACK = .1            # per active frame (tau ~100 ms)
LEVEL_RELEASE = .005         # per active frame (tau ~2 s of speech)
LEVEL_STEP_DB = 10.0         # one frame pulls the level from at most this far above it (a bump is no new level)
GAIN_ATTACK = .3             # per active frame toward a lower gain
GAIN_RISE_DB = .1            # per active frame toward a higher gain (10 dB per second of speech)
ENGAGE_RISE_DB = 1.0         # per active frame while engaging mid-session
LOOK = 10                    # frames (100 ms) of look-ahead on the lifted path: a delay line across chunks

KNEE = FULL * .5             # -6 dBFS
CEIL = FULL * 10 ** (-1 / 20)  # -1 dBFS
LIMIT_RELEASE = .02          # per block (tau ~125 ms)
XFADE = 320                  # samples (20 ms) of raw -> lifted crossfade on a mid-session engage
COMFORT_DB = -64.0           # comfort noise under a lifted whisper from a gated pen (dBFS, after gain)
COMFORT_LEN = 24007          # samples in its table (~1.5 s; prime, so it never lines up with chunks)


def _db(rms):
    return 20.0 * math.log10(max(rms, 1e-3) / FULL)


def _percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * (len(ordered) - 1) + .5))]


def _nonempty(pieces):
    return [piece for piece in pieces if piece]


_comfort_table = None


def _comfort_noise():
    """White noise at COMFORT_DB in the 32-bit output domain, made once (seeded)."""
    global _comfort_table
    if _comfort_table is None:
        white = random.Random(20261014).randbytes(COMFORT_LEN * 2)  # uniform 16-bit: -4.8 dBFS RMS
        _comfort_table = audioop.mul(audioop.lin2lin(white, 2, 4), 4, 10 ** ((COMFORT_DB + 4.77) / 20))
    return _comfort_table


class _Frame:
    """One 10 ms analysis frame. window: inside a latch-on pop's first 300 ms (never
    evidence); pop: the pop itself; muted: sent as digital zeros."""
    __slots__ = ('index', 'n', 'db', 'band', 'silent', 'above', 'active', 'window', 'pop', 'muted')

    def __init__(self, index, n):
        self.index, self.n = index, n
        self.db = self.band = -120.0
        self.silent = True
        self.above = self.active = self.window = self.pop = self.muted = False


class WhisperDSP:
    """One recording at a time. begin() starts a session, process() takes each
    captured chunk and returns the chunks ready to send (none while the first
    syllables are held, several when they are released), flush() releases the
    rest at the end, reset() makes it an exact pass-through again.

    Only the controller thread calls these. meter_gain is read from the
    capture thread (a float swap) to scale the level meter."""

    def __init__(self):
        self.begin('off')

    def reset(self):
        self.begin('off')

    def begin(self, mode='auto'):
        self.mode = mode if mode in MODES else 'auto'
        self.active = self.mode != 'off'   # analysed: whisper-aware silence rule applies
        self.engaged = self.mode == 'on'   # output is filtered and lifted
        self.decided = False
        self.holding = False
        self.held = []
        self.held_samples = 0
        self.hold_start = 0                # held samples when the decision's clock started
        self.pop_waiting = False           # a held pop waits past HOLD_MAX for speech to judge it by
        self.pop_sound = None              # held samples before the first active frame after it
        self.highpass = [None] * 6
        self.raw_sent = False              # untouched audio already went out this session
        self.filtered_sent = False
        # analysis
        self.index = 0                     # frames analysed
        self.last_zero = -ZERO_GUARD - 1   # the last digitally silent frame
        self.last_sound = -(1 << 30)       # the last frame with sound (not digital zeros)
        self.floor_ring = deque(maxlen=FLOOR_WINDOW)
        self.band_ring = deque(maxlen=FLOOR_WINDOW)
        self.floor_db = self.band_floor_db = FLOOR_DEFAULT_DB
        self.floor_age = 0
        self.floor_known = False           # real background was heard (else the floors are the default)
        self.floor_seeded = False          # ...learned before any digital zeros (an ungated microphone, so far)
        self.pending = deque()             # background frames still proving they are not next to zeros
        self.steady_undo = None            # a gated source's floor before a steady reset, while zeros may undo it
        self.first = []                    # counted frame levels until the decision
        self.first_shaped = []             # ...of those, the syllable-shaped runs (the only ones that say "normal")
        self.recent = deque(maxlen=LATE_WINDOW)
        self.recent_new = 0
        self.streak = 0                    # frames above the floor in a row
        self.run_open = False
        self.run_frames = []               # the current run's frames
        self.run_recent = deque(maxlen=FALL_FRAMES)  # its last frames (the peak's window)
        self.run_span = 0                  # frames since the run began (short dips included)
        self.run_gap = 0                   # frames since the run's last frame above the floor
        self.run_tail = False              # the run continues a counted syllable (never floor)
        self.run_steady_at = None          # when it was found level (a gated source waits before resetting)
        self.sustained = 0                 # samples in counted, syllable-length runs
        self.analysed = 0                  # samples
        self.active_frames = 0
        self.decided_at = None             # samples analysed when the decision fell
        self.decision_db = None            # the speech level it was made from
        # latch-on pop
        self.pop_state = 'lead'            # lead (leading zeros), candidate, done
        self.lead_zeros = 0
        self.pop_frames = []
        self.pop_skipped = []              # (frame, shaped) of the candidate's runs counted meanwhile
        self.pop_loud = False
        self.pop_peak = None               # the confirmed pop's loudest frame (dBFS)
        self.pop_transient = False         # it died away like a transient (see POP_FADE_DB)
        self.pop_verdict = None            # muted (True) or sent (False), once rendered
        self.pop_samples = 0
        self.pop_muted = False
        # gain computer
        self.level_db = TARGET_DB
        self.agc_db = 0.0
        self.rise = GAIN_RISE_DB
        self.line = deque()                # (frame, raw, lifted-path samples) awaiting their look-ahead
        self.line_active = -(1 << 30)      # the newest active frame that entered the line
        self.gain = 1.0
        self.limit = 1.0
        self.meter_gain = 1.0
        self.comfort_at = 0                # position in the comfort-noise table

    # -- public ------------------------------------------------------------------
    @property
    def speech_seconds(self):
        return self.sustained / RATE

    @property
    def gain_db(self):
        return self.agc_db if self.engaged else 0.0

    def stats(self):
        """The session's numbers (diagnostic recordings, the offline check below)."""
        return {'version': 2, 'mode': self.mode, 'engaged': self.engaged, 'gain_db': round(self.gain_db, 1),
                'floor_db': round(self.floor_db, 1), 'band_floor_db': round(self.band_floor_db, 1),
                'level_db': round(self.level_db, 1),
                'speech_seconds': round(self.speech_seconds, 3),
                'audio_seconds': round(self.analysed / RATE, 3),
                'active_frames': self.active_frames, 'decided': self.decided,
                'decided_at': None if self.decided_at is None else round(self.decided_at / RATE, 3),
                'decision_db': None if self.decision_db is None else round(self.decision_db, 1),
                'pop_seconds': round(self.pop_samples / RATE, 3), 'pop_muted': self.pop_muted,
                'comfort': self.comfort,
                'limits': {'abs_min_db': ABS_MIN_DB, 'band_abs_min_db': BAND_ABS_MIN_DB, 'snr_db': SNR_DB,
                           'engaged_snr_db': ENGAGED_SNR_DB, 'mod_db': MOD_DB, 'soft_db': SOFT_DB,
                           'target_db': TARGET_DB, 'max_gain_db': MAX_GAIN_DB,
                           'lookahead_ms': LOOK * FRAME * 1000 // RATE, 'comfort_db': COMFORT_DB}}

    def process(self, pcm):
        if not self.active or not pcm or len(pcm) % 2:
            return [pcm] if pcm else []
        x = self._highpass(pcm)
        chunk = (pcm, x, self._analyse(x))
        if self.decided and not self.held:
            return _nonempty([self._render(chunk)])
        self.held.append(chunk)
        self.held_samples += len(pcm) // 2
        if not self.holding and not self.decided:
            # Nothing to hold yet. The last 100 ms of sound stay back: a weak initial there
            # (too faint to tell from the floor) is held - and lifted - with its vowel if one
            # follows. Digital zeros go out at once.
            return self._release(keep=LOOK * FRAME if self.index - self.last_sound < LOOK else 0)
        pending = self.pop_peak is not None and self.pop_verdict is None
        if pending and self.pop_sound is None and any(frame.active for frame in chunk[2]):
            self.pop_sound = self.held_samples - len(pcm) // 2  # the first sound after the pop
        if not self.decided and not self.first and (self.pop_waiting or
                                                    self.held_samples - self.hold_start >= HOLD_MAX):
            if pending and self.held_samples < POP_WAIT:
                # A latch-on pop - or a short first word just like one - and no speech since:
                # what follows says which (_pop_mute). It waits for that, up to POP_WAIT; the
                # decision's clock starts again with the next sound.
                self.pop_waiting = True
                self.hold_start = self.held_samples if self.pop_sound is None else self.pop_sound
                if self.held_samples - self.hold_start < HOLD_MAX:
                    return []
            # Only blips so far (a click, a breath of noise) - or a pop nothing followed,
            # judged by itself: nothing to decide on. They go out (_pop_mute) and the
            # decision stays open for real speech.
            return self._release()
        if not self.decided and self.held_samples - self.hold_start >= HOLD_MAX:
            self._decide(final=True)
        return self._release() if self.decided or not self.holding else []

    def flush(self):
        """End of the recording: decide with what was heard and release it, the
        look-ahead's tail included (nothing waits for audio that will not come)."""
        out = []
        if self.held:
            if self.holding and not self.decided:
                self._decide(final=True)
            out = self._release()
        if self.line:
            out += _nonempty([self._lifted([self._emit() for _ in range(len(self.line))])])
        return out

    # -- filters -----------------------------------------------------------------
    def _highpass(self, pcm):
        """16-bit PCM -> high-passed 32-bit samples (one 16-bit step = SCALE)."""
        x = audioop.mul(audioop.lin2lin(pcm, 2, 4), 4, .25)
        return self._stages(x, (0, 1), (1, HP_WEIGHT))

    def _band(self, x):
        """The extra ~130 Hz stages for an engaged whisper (output path only)."""
        return self._stages(x, (2, 3), (1, WHISPER_HP_WEIGHT))

    def _presence(self, x):
        """The ~900 Hz stages for the band level (analysis only)."""
        return self._stages(x, (4, 5), BAND_WEIGHTS)

    def _stages(self, x, stages, weights):
        for stage in stages:
            low, self.highpass[stage] = audioop.ratecv(x, 4, 1, 1, 1, self.highpass[stage], *weights)
            x = audioop.add(x, audioop.mul(low, 4, -1.0), 4)
        return x

    # -- analysis ----------------------------------------------------------------
    def _analyse(self, x):
        band = self._presence(x)
        frames = []
        total = len(x) // 4
        for start in range(0, total, FRAME):
            n = min(FRAME, total - start)
            self.analysed += n
            self.index += 1
            frame = _Frame(self.index, n)
            rms = audioop.rms(x[start * 4:(start + n) * 4], 4) / SCALE
            if rms <= SILENT_RMS:
                self._zeros(frame)
            else:
                frame.silent = False
                self.last_sound = frame.index
                frame.db = _db(rms)
                frame.band = _db(audioop.rms(band[start * 4:(start + n) * 4], 4) / SCALE) + BAND_GAIN_DB
                self._sound(frame)
            frames.append(frame)
        if not self.decided and self.first:
            self._decide()
        return frames

    def _zeros(self, frame):
        """Digital silence: the pen's gate is shut (or its microphone latched off)."""
        if self.floor_seeded:
            # A gated source after all: what seeded the floor were the gate's first sounds
            # (speech, its edges, the pen's own noise), not background. Back to the default -
            # unless seconds of ungated audio came first (a microphone with a dropout).
            if frame.index <= SEED_TRUST:
                self.floor_ring.clear()
                self.band_ring.clear()
                self.floor_db = self.band_floor_db = FLOOR_DEFAULT_DB
                self.floor_known = False
            self.floor_seeded = False
        self.last_zero = frame.index
        self.streak = 0
        if self.pop_state == 'lead':
            self.lead_zeros += 1
        elif self.pop_state == 'candidate':
            self._pop_end(ended=True)
        if self.run_open:
            self._close_run(zeros=True)  # the gate shut: the syllable's offset
        if self.steady_undo is not None:
            self._undo_steady()
        self.pending.clear()              # what came right before the gate shut is no background

    def _sound(self, frame):
        if self.pop_state != 'done':
            self._pop_track(frame)
        snr = ENGAGED_SNR_DB if self.engaged else SNR_DB
        frame.above = (frame.db > max(self.floor_db + snr, ABS_MIN_DB)
                       or frame.band > max(self.band_floor_db + snr, BAND_ABS_MIN_DB))
        seeding = not self.floor_known and self.last_zero < 1
        if frame.above:
            self.streak += 1
            self._run_above(frame)
        else:
            self.streak = 0
            if not seeding:
                self._background(frame)
            self._run_quiet(frame)
        if seeding:
            # No digital zeros yet: an ungated microphone (so far). Like v1, its first frames
            # seed the floor at once - louder or fluctuating background (room noise, rumble)
            # has no other way in - and the first zeros throw that seed away (_zeros).
            self.floor_seeded = True
            self._track_floor(frame)
        if frame.above and self.streak >= GATE_FRAMES:
            frame.active = True
            self.active_frames += 1
            if not self.decided:
                self.holding = True  # from the first active frame: the syllable may be soft
        if self.steady_undo is not None and frame.index - self.steady_undo['at'] > STEADY_UNDO:
            self.steady_undo = None  # the gate stayed open: that really is the background now
        while self.pending and frame.index - self.pending[0].index >= ZERO_GUARD:
            self._track_floor(self.pending.popleft())

    # A run: frames above the floor, with dips shorter than RUN_GAP that are no real fall.
    def _run_above(self, frame):
        if not self.run_open:
            self._open_run(False)
        self.run_gap = 0
        self.run_span += 1
        if self._fallen(frame):
            # Fell well below its peak and is still above the floor: the next syllable
            # follows without a pause. What came before is one (when long enough).
            self._close_run(modulated=True)
            self._open_run(True)
            self.run_span = 1
        self.run_frames.append(frame)
        self.run_recent.append(frame)
        self._level_run()

    def _run_quiet(self, frame):
        if not self.run_open:
            return
        self.run_gap += 1
        self.run_span += 1
        if self._fallen(frame):
            self._close_run(modulated=True)   # the syllable's offset
            return
        self.run_recent.append(frame)
        if self.run_gap >= RUN_GAP:
            self._close_run()   # it sank just below the threshold: no syllable shape
        else:
            self._level_run()

    def _level_run(self):
        """A run that stays level is background (see _steady). On a gated source - digital
        zeros heard this session - wait STEADY_WAIT first: when the gate shuts meanwhile, it
        was a drawn-out whisper the pen let through, and it counts as one (_close_run);
        when it shuts soon after the reset, _undo_steady takes the reset back."""
        if self.run_steady_at is None:
            if not (self.run_span >= STEADY_FRAMES or self._flat_start()):
                return
            self.run_steady_at = self.index
        if self.last_zero < 1 or self.index - self.run_steady_at >= STEADY_WAIT:
            self._steady()

    def _flat_start(self):
        """No floor known yet and the last 300 ms of this run were flat and far from digital
        zeros: background louder than the default floor (room noise, a noisy microphone),
        learned without seeding the floor from whatever the session began with."""
        if self.floor_known or self.run_span < BOOT_FRAMES or len(self.run_frames) < BOOT_FRAMES:
            return False
        frames = self.run_frames[-BOOT_FRAMES:]
        if frames[0].index - self.last_zero <= ZERO_GUARD:
            return False
        levels = [f.db for f in frames]
        return max(levels) - min(levels) <= BOOT_SPREAD_DB

    def _fallen(self, frame):
        """Either level fell MOD_DB below the run's recent peak in that level - one that
        carried the run (was above its floor), so a level sitting on its floor never does."""
        if not self.run_recent:
            return False
        peak = max(f.db for f in self.run_recent)
        if peak - frame.db >= MOD_DB and peak > max(self.floor_db + SNR_DB, ABS_MIN_DB):
            return True
        peak = max(f.band for f in self.run_recent)
        return peak - frame.band >= MOD_DB and peak > max(self.band_floor_db + SNR_DB, BAND_ABS_MIN_DB)

    def _open_run(self, tail):
        self.run_open, self.run_frames, self.run_span, self.run_gap, self.run_tail = True, [], 0, 0, tail
        self.run_steady_at = None
        self.run_recent.clear()

    def _end_run(self):
        frames, tail = self.run_frames, self.run_tail
        self.run_open, self.run_frames, self.run_span, self.run_gap, self.run_tail = False, [], 0, 0, False
        self.run_steady_at = None
        self.run_recent.clear()
        return frames, tail

    def _close_run(self, modulated=False, zeros=False):
        frames, tail = self._end_run()
        if (modulated or zeros) and sum(f.n for f in frames) >= SUSTAIN:
            # Syllable-long and shaped like one (or cut off by the pen's gate): the
            # whole run, from its first frame, is speech.
            self._count(frames, shaped=modulated)
        elif not tail and not zeros and (self.floor_known or self.last_zero >= 1):
            for frame in frames:  # a click, a short noise: background after all (unless seeding took it)
                self._background(frame)

    def _count(self, frames, shaped):
        for frame in frames:
            if frame.window or frame.pop:
                if self.pop_state == 'candidate':
                    self.pop_skipped.append((frame, shaped))  # counted after all if it is no pop
                continue  # the latch-on pop is never speech evidence
            self._count_frame(frame, shaped)

    def _count_frame(self, frame, shaped):
        self.sustained += frame.n
        if not self.decided:
            self.first.append(frame.db)
            if shaped:
                self.first_shaped.append(frame.db)
        self._late(frame.db)

    def _steady(self):
        """1 s above the floor without the fall of a syllable: the noise stepped up (or the
        microphone faded in). That level is the floor now; none of it was speech - unless a
        gated source shuts its gate soon after (_undo_steady)."""
        frames, _ = self._end_run()
        background = [f for f in frames if f.index - self.last_zero > ZERO_GUARD]
        if not background:
            return
        if self.last_zero >= 1:
            if self.steady_undo is None:
                self.steady_undo = {'floor': (list(self.floor_ring), list(self.band_ring), self.floor_db,
                                              self.band_floor_db, self.floor_known), 'frames': []}
            self.steady_undo['at'] = self.index
            self.steady_undo['frames'] += frames
        for frame in frames:
            frame.active = False  # still held or in the delay line: the gain must not follow noise
        self.floor_known = True
        self.pending.clear()
        self.floor_ring.clear()
        self.band_ring.clear()
        self.floor_ring.extend(f.db for f in background)
        self.band_ring.extend(f.band for f in background)
        self.floor_age = 0
        self.floor_db = _percentile(self.floor_ring, FLOOR_PERCENTILE)
        self.band_floor_db = _percentile(self.band_ring, FLOOR_PERCENTILE)

    def _undo_steady(self):
        """The gate shut within 3 s of a steady reset: no background keeps a gate open and
        then lets it shut - that was a drawn-out whisper. The floor goes back (on a gated
        pen nothing else would ever bring it down) and the run counts as speech."""
        undo, self.steady_undo = self.steady_undo, None
        ring, band, self.floor_db, self.band_floor_db, self.floor_known = undo['floor']
        self.floor_ring.clear()
        self.band_ring.clear()
        self.floor_ring.extend(ring)
        self.band_ring.extend(band)
        self.floor_age = 0
        self._count(undo['frames'], shaped=False)

    def _background(self, frame):
        if frame.index - self.last_zero > ZERO_GUARD:  # never right after digital zeros
            self.pending.append(frame)

    def _track_floor(self, frame):
        self.floor_ring.append(frame.db)
        self.band_ring.append(frame.band)
        self.floor_age += 1
        count = len(self.floor_ring)
        if self.floor_age >= 10 or count < 20:
            self.floor_age = 0
            self.floor_known = self.floor_known or count >= FLOOR_MIN
            self.floor_db = _percentile(self.floor_ring, FLOOR_PERCENTILE)
            self.band_floor_db = _percentile(self.band_ring, FLOOR_PERCENTILE)

    def _pop_track(self, frame):
        """The first sound after the session's leading digital zeros may be the pen's
        microphone latching on: loud at once, over within 300 ms."""
        if self.pop_state == 'lead':
            if self.lead_zeros < POP_LEAD or frame.index > POP_ONSET:
                self.pop_state = 'done'
                return
            self.pop_state = 'candidate'
            if not self.decided:
                self.holding = True  # it may have to be muted: hold it from its first frame
        if len(self.pop_frames) >= POP_MAX:
            self._pop_end(ended=False)  # too long for a pop; its first 300 ms stay out of the decision
            return
        self.pop_frames.append(frame)
        frame.window = True
        if frame.db >= POP_MIN_DB and len(self.pop_frames) <= POP_ATTACK:
            self.pop_loud = True
        if len(self.pop_frames) >= POP_ATTACK and not self.pop_loud:
            self._pop_end(ended=False)  # a soft onset: no pop
        elif len(self.pop_frames) > 2 and frame.db > max(f.db for f in self.pop_frames[:2]) + POP_EDGE_DB:
            self._pop_end(ended=False)  # it rises into its peak like a voice: no pop either

    def _pop_end(self, ended):
        frames, self.pop_frames, self.pop_state = self.pop_frames, [], 'done'
        skipped, self.pop_skipped = self.pop_skipped, []
        levels = [frame.db for frame in frames]
        peak = max(levels)
        if not self.pop_loud or peak - max(levels[:2]) > POP_EDGE_DB:
            # No pop (a soft onset, or one that rose into its peak like a voice): these
            # frames are what they are - runs already closed over them count after all.
            for frame in frames:
                frame.window = False
            for frame, shaped in skipped:
                self._count_frame(frame, shaped)
            if not self.decided and not self.first:
                self.holding = any(frame.active for frame in frames)
            return
        if not ended:
            return  # too long for a pop: its first 300 ms stay out of the decision (a pop running into a whisper)
        # Loud at once and cut off by the gate within 300 ms: the latch-on pop. Never
        # speech evidence, and it stays held with whatever follows: whether it is muted
        # depends on that (_pop_mute) - a short first word right after the latch can look alike.
        for frame in frames:
            if frame.active:
                self.active_frames -= 1
            frame.pop, frame.active = True, False
        self.pop_samples += sum(frame.n for frame in frames)
        self.pop_peak = peak
        top = levels.index(peak)
        rises = [levels[k] - min(levels[top:k]) for k in range(top + 1, len(levels))]
        self.pop_transient = peak - max(levels[-3:]) >= POP_FADE_DB and max(rises, default=0.0) <= POP_RISE_DB

    def _pop_mute(self):
        """Whether the held pop goes out muted, settled when it is rendered: in a soft session
        when it is far louder than the speech (a whisperer's first word is not), never after
        a normal decision (it may have been a short first word). With nothing else heard -
        no speech within POP_WAIT, or none before the recording ended - only when it was loud
        and died away like a transient: a short word alone is sent as it came and counts as
        speech (muted and never counted, a one-word recording was cancelled as silent)."""
        if self.pop_peak is None:
            return False
        if self.pop_verdict is None:
            if self.decision_db is not None:
                mute = self.decision_db < SOFT_DB and self.pop_peak >= self.decision_db + POP_ABOVE_DB
            else:
                mute = self.pop_transient and self.pop_peak >= POP_LOUD_DB
            self.pop_verdict = mute
            if not mute and self.pop_samples >= SUSTAIN:
                self.sustained += self.pop_samples  # sent as sound, syllable-long: most likely a first word
        return self.pop_verdict

    def _late(self, db):
        """A session that began loud (or too briefly to judge) and then stays
        soft engages later, ramping the gain up instead of jumping."""
        self.recent.append(db)
        self.recent_new += 1
        if self.engaged or not self.decided or self.recent_new < 10 or len(self.recent) < LATE_MIN:
            return
        self.recent_new = 0
        level = _percentile(self.recent, .8)
        if level < SOFT_DB - LATE_MARGIN_DB:
            self.decision_db = level
            self._engage(level, ramp=True)

    def _decide(self, final=False):
        levels = self.first
        count = len(levels)
        median = _percentile(levels, .5) if count else None
        # Speech level: the 80th percentile once there is enough to rank; before
        # that the median (+3 dB, about the same for speech), which a couple of
        # loud frames cannot move.
        level = None if not count else _percentile(levels, .8) if count >= 20 else median + 3
        # "Clearly normal" only from syllable-shaped runs: a run the pen's gate cut off
        # (a bump, a pop it let through) is evidence of sound, not of its level.
        shaped = self.first_shaped
        loud = len(shaped) >= EARLY_FRAMES and _percentile(shaped, .5) >= LOUD_DB
        clearly_soft = count >= EARLY_SOFT_FRAMES and level < SOFT_DB - CLEAR_MARGIN_DB
        if not (loud or clearly_soft or count >= DECIDE_FRAMES or final):
            return
        self.decided = True
        self.decided_at, self.decision_db = self.analysed, level
        if self.mode == 'on':
            self._engage(level)
        elif not loud and count >= EARLY_FRAMES and level < SOFT_DB:
            self._engage(level)
        # Otherwise normal speech stays untouched; too little to judge leaves
        # the decision to _late(), which can still engage later.

    def _engage(self, level, ramp=False):
        if level is None:
            level = TARGET_DB - DEFAULT_GAIN_DB
        self.engaged = True
        self.level_db = level
        if ramp:
            self.agc_db, self.rise = 0.0, ENGAGE_RISE_DB
        else:
            # The held syllables get the measured gain at once (that is why they were held).
            self.agc_db, self.rise = min(MAX_GAIN_DB, max(0.0, TARGET_DB - level)), GAIN_RISE_DB

    # -- output ------------------------------------------------------------------
    def _release(self, keep=0):
        """Render the held chunks, oldest first: all of them, or all but the newest ones
        that cover `keep` samples."""
        out = []
        while self.held and (not keep or self.held_samples - len(self.held[0][0]) // 2 >= keep):
            chunk = self.held.pop(0)
            self.held_samples -= len(chunk[0]) // 2
            out.append(self._render(chunk))
        if not keep:
            self.holding = self.pop_waiting = False
            self.hold_start = 0
        return _nonempty(out)

    def _render(self, chunk):
        raw, x, frames = chunk
        if any(frame.pop for frame in frames) and self._pop_mute():
            for frame in frames:
                frame.muted = frame.pop
            self.pop_muted = True
        if not self.engaged:
            self.meter_gain = 1.0
            self.raw_sent = True
            return _mute(raw, frames)      # byte-for-byte what the microphone sent (a muted pop aside)
        x = self._band(x)
        emitted, pos = [], 0
        for frame in frames:
            n = frame.n
            self._follow(frame)
            if frame.active:
                self.line_active = frame.index
            self.line.append((frame, bytes(n * 2) if frame.muted else raw[pos * 2:(pos + n) * 2],
                              x[pos * 4:(pos + n) * 4]))
            pos += n
            if len(self.line) > LOOK:
                emitted.append(self._emit())
        return self._lifted(emitted)

    def _follow(self, frame):
        """The AGC, at the delay line's input: only speech moves it. Through pauses,
        digital zeros and noise the gain is held."""
        if not (frame.active and self.decided):
            return
        db = min(frame.db, self.level_db + LEVEL_STEP_DB)
        coefficient = LEVEL_ATTACK if db > self.level_db else LEVEL_RELEASE
        self.level_db += (db - self.level_db) * coefficient
        want = min(MAX_GAIN_DB, max(0.0, TARGET_DB - self.level_db))
        if want < self.agc_db:
            self.agc_db += (want - self.agc_db) * GAIN_ATTACK
        elif want > self.agc_db:
            self.agc_db = min(want, self.agc_db + self.rise)
            if self.agc_db >= want:
                self.rise = GAIN_RISE_DB

    def _emit(self):
        """The oldest frame in the delay line, lifted with what the frames after it say:
        the gain the AGC reached by the newest one, capped by any louder frame ahead."""
        frame, raw, x = self.line.popleft()
        cap = MAX_GAIN_DB
        for later in (frame, *(entry[0] for entry in self.line)):
            if not later.silent and not later.muted:
                cap = min(cap, max(0.0, TARGET_DB + HEADROOM_DB - later.db))
        applied = max(0.0, min(self.agc_db, cap))
        target = 0.0 if frame.muted else 10.0 ** (applied / 20.0)
        n = frame.n
        blocks = range(0, n, BLOCK)
        count = len(blocks)
        if frame.muted:
            self.gain = 0.0  # the next frame fades in from silence over 10 ms
            self.meter_gain = 1.0
            return raw, self._comfort(bytes(n * 4))
        # Digital zeros are lifted like the rest: after a gate cut they carry the
        # high-pass filters' decaying tail, and cutting that to zero would click.
        start = self.gain
        out = []
        for k, begin in enumerate(blocks):
            piece = x[begin * 4:min(n, begin + BLOCK) * 4]
            gain = start + (target - start) * (k + 1) / count
            need = 1.0
            if gain > 1.0:
                peak = audioop.max(piece, 4) / SCALE * gain
                if peak > KNEE:
                    bent = KNEE + (CEIL - KNEE) * math.tanh((peak - KNEE) / (CEIL - KNEE))
                    need = max(bent / peak, 1.0 / gain)
            self.limit = min(need, self.limit + (1.0 - self.limit) * LIMIT_RELEASE)
            applied_gain = max(gain * self.limit, min(gain, 1.0))
            out.append(audioop.mul(piece, 4, applied_gain * 4.0))
        self.gain = target
        self.meter_gain = max(1.0, self.gain * self.limit)
        return raw, self._comfort(b''.join(out))

    @property
    def comfort(self):
        """Comfort noise goes under the lifted output (see 7. above): a gated source, and a
        word has reached the delay line - a session of digital zeros stays digital zeros."""
        return self.engaged and self.line_active > 0 and self.last_zero > 0

    def _comfort(self, lifted):
        if not self.comfort:
            return lifted
        table, n = _comfort_noise(), len(lifted) // 4
        pieces, done = [], 0
        while done < n:
            take = min(n - done, COMFORT_LEN - self.comfort_at)
            start = self.comfort_at * 4
            pieces.append(audioop.add(lifted[done * 4:(done + take) * 4], table[start:start + take * 4], 4))
            done += take
            self.comfort_at = (self.comfort_at + take) % COMFORT_LEN
        return b''.join(pieces)

    def _lifted(self, emitted):
        if not emitted:
            return b''
        lifted = audioop.lin2lin(b''.join(piece for _, piece in emitted), 4, 2)
        if self.raw_sent and not self.filtered_sent:
            lifted = _crossfade(b''.join(raw for raw, _ in emitted), lifted)
        self.filtered_sent = True
        return lifted


def _mute(raw, frames):
    """raw with a muted pop's frames replaced by digital zeros (exactly what the gate
    sends on either side of it)."""
    if not any(frame.muted for frame in frames):
        return raw
    pieces, pos = [], 0
    for frame in frames:
        piece = raw[pos * 2:(pos + frame.n) * 2]
        pieces.append(bytes(len(piece)) if frame.muted else piece)
        pos += frame.n
    return b''.join(pieces)


def _crossfade(raw, lifted, length=XFADE, step=BLOCK):
    """The first `length` samples go from raw to lifted, so the switch never clicks."""
    n = min(length, len(raw) // 2, len(lifted) // 2)
    pieces = []
    for start in range(0, n, step):
        end = min(n, start + step)
        weight = (start + end) / 2 / n
        a = audioop.mul(raw[start * 2:end * 2], 2, 1.0 - weight)
        b = audioop.mul(lifted[start * 2:end * 2], 2, weight)
        pieces.append(audioop.add(a, b, 2))
    return b''.join(pieces) + lifted[n * 2:]


def _main():
    """Offline check with a real recording:
    python -m bridge.voice_dsp in.wav out.wav [auto|on|off]  (16 kHz mono 16-bit)."""
    import sys
    import wave
    source, target = sys.argv[1], sys.argv[2]
    dsp = WhisperDSP()
    dsp.begin(sys.argv[3] if len(sys.argv) > 3 else 'auto')
    with wave.open(source, 'rb') as reader:
        if (reader.getframerate(), reader.getnchannels(), reader.getsampwidth()) != (RATE, 1, 2):
            raise SystemExit('need 16 kHz mono 16-bit PCM')
        data = reader.readframes(reader.getnframes())
    out = []
    for offset in range(0, len(data), 1600):
        out.extend(dsp.process(data[offset:offset + 1600]))
    out.extend(dsp.flush())
    with wave.open(target, 'wb') as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(RATE)
        writer.writeframes(b''.join(out))
    print(dsp.stats())


if __name__ == '__main__':
    _main()
