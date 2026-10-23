"""Synthesized earcons in one key (E major pentatonic).

Three voices keep modes recognizable without looking: a glass bell (input:
bright, rising), a wooden marimba (notes: warm, falling) and a soft chord
(present / saved / enabled). Every cue has a rounded attack, partials that
decay faster as they get higher, a gentle low-pass and a small-room reverb
tail. Nothing is recorded; these are generated once and cached as WAV files.
"""
import io
import math
from pathlib import Path
import random
import threading
import time
import wave

RATE = 44100
SOUND_VERSION = 3
# Silence before each cue: a sleeping output device swallows the first fraction of a
# second; short cues were then sometimes inaudible. While KeepAwake holds the device
# awake there is nothing to swallow, so cues start at once (80 ms sooner).
LEAD = .08
VOLUMES = {'low': .2, 'medium': .38, 'high': .62}

NOTE = {'B3': 246.94, 'C4': 261.63, 'E4': 329.63, 'G#4': 415.30, 'B4': 493.88, 'C#5': 554.37,
        'E5': 659.26, 'G#5': 830.61, 'B5': 987.77, 'C#6': 1108.73, 'E6': 1318.51}

# (partial ratio, amplitude, decay multiplier)
BELL = ((1, 1, 1), (2.756, .32, 1.9), (5.404, .14, 3.2), (8.933, .05, 5))
MARIMBA = ((1, 1, 1), (3.932, .22, 2.6), (9.538, .04, 6))
PAD = ((1, 1, 1), (2, .18, 1.4), (3, .06, 2))


def note(name, at, length, voice=BELL, amp=1.0, attack=.004):
    return (NOTE[name], at, length, voice, amp, attack)


SOUNDS = {
    'mode_input': [note('B5', 0, .55), note('E6', .085, .7, amp=.9)],
    'mode_notes': [note('E5', 0, .5, MARIMBA), note('B4', .11, .65, MARIMBA, .85)],
    'mode_present': [note('E5', 0, .9, PAD, .7, .02), note('G#5', .06, .9, PAD, .55, .02),
                     note('B5', .12, 1.0, PAD, .5, .02)],
    'start_input': [note('E6', 0, .45, BELL, .55)],
    'start_notes': [note('E5', 0, .45, MARIMBA, .7)],
    'stop': [note('B5', 0, .35, BELL, .45), note('G#5', .07, .5, BELL, .4)],
    'done_input': [note('C#6', 0, .3, BELL, .35), note('E6', .06, .45, BELL, .3)],
    'done_notes': [note('E5', 0, 1.1, PAD, .55, .012), note('G#5', .03, 1.1, PAD, .45, .012),
                   note('B5', .06, 1.2, BELL, .4)],
    'error': [note('B3', 0, .32, MARIMBA, .8), note('C4', .12, .4, MARIMBA, .7)],
    'armed': [note('B4', 0, .5, BELL, .45), note('E5', .07, .6, BELL, .45), note('B5', .14, .8, BELL, .4)],
    'disarmed': [note('B5', 0, .45, BELL, .4), note('E5', .08, .55, BELL, .38), note('B4', .16, .7, BELL, .36)],
    'battery_low': [note('G#4', 0, .55, PAD, .6, .01), note('E4', .16, .7, PAD, .55, .01)],
    'tool': [note('C#6', 0, .22, MARIMBA, .5)],
}


def _reverb(signal):
    """Small Schroeder room: four combs into two all-passes, mixed in softly."""
    out = [0.0] * len(signal)
    for delay, feedback in ((1557, .76), (1617, .74), (1491, .75), (1422, .72)):
        buffer, index = [0.0] * delay, 0
        for i, x in enumerate(signal):
            y = buffer[index]
            buffer[index] = x + y * feedback
            index = (index + 1) % delay
            out[i] += y * .25
    for delay, gain in ((225, .5), (556, .5)):
        buffer, index = [0.0] * delay, 0
        for i, x in enumerate(out):
            y = buffer[index]
            v = x + y * gain
            buffer[index] = v
            out[i] = y - gain * v
            index = (index + 1) % delay
    return out


def render(name, volume='medium', lead=LEAD):
    """Return mono 16-bit WAV bytes for one earcon, after `lead` seconds of silence."""
    parts = SOUNDS[name]
    tail = .55
    total = int((max(p[1] + p[2] for p in parts) + tail) * RATE)
    dry = [0.0] * total
    rng = random.Random(11)
    for freq, at, length, voice, amp, attack in parts:
        first, count = int(at * RATE), int(length * RATE)
        detune = 1 + rng.uniform(-.0008, .0008)
        for ratio, weight, decay in voice:
            f = freq * ratio * detune
            if f > RATE / 2.2:
                continue
            tau = length / (3.2 * decay)  # higher partials die out sooner
            step = 2 * math.pi * f / RATE
            phase = rng.uniform(0, math.tau)
            for i in range(count):
                t = i / RATE
                env = (min(1.0, t / attack) ** 1.6) * math.exp(-t / tau)
                index = first + i
                if index < total:
                    dry[index] += amp * weight * env * math.sin(phase + step * i)
    # Gentle low-pass removes the digital edge from the attack.
    state, smooth = 0.0, .55
    for i, x in enumerate(dry):
        state += smooth * (x - state)
        dry[i] = state
    wet = _reverb(dry)
    mixed = [d * .86 + w * .22 for d, w in zip(dry, wet)]
    peak = max(1e-9, max(abs(v) for v in mixed))
    scale = VOLUMES[volume] / peak
    fade = int(.08 * RATE)
    frames = bytearray(int(lead * RATE) * 2)
    for i, value in enumerate(mixed):
        if i >= total - fade:
            value *= ((total - i) / fade) ** 2
        frames += int(max(-1.0, min(1.0, value * scale)) * 32767).to_bytes(2, 'little', signed=True)
    output = io.BytesIO()
    with wave.open(output, 'wb') as file:
        file.setnchannels(1)
        file.setsampwidth(2)
        file.setframerate(RATE)
        file.writeframes(bytes(frames))
    return output.getvalue()


def _relead(data, tight):
    """The same cue with (tight=False) or without (tight=True) the LEAD silence."""
    with wave.open(io.BytesIO(data)) as source:
        params = source.getparams()
        frames = source.readframes(source.getnframes())
    lead = int(LEAD * RATE) * params.sampwidth * params.nchannels
    if tight:
        if frames[:lead].strip(bytes(1)):
            raise wave.Error('no lead-in to remove')
        frames = frames[lead:]
    else:
        frames = bytes(lead) + frames
    output = io.BytesIO()
    with wave.open(output, 'wb') as target:
        target.setnchannels(params.nchannels)
        target.setsampwidth(params.sampwidth)
        target.setframerate(params.framerate)
        target.writeframes(frames)
    return output.getvalue()


def _winsound_player(path):
    import winsound
    winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)


class KeepAwake:
    """An inaudible looping stream (±1 LSB dither) that keeps the output device
    awake. Bluetooth speakers and HDMI audio sleep after a few seconds of silence
    and swallow the first ~0.5–1 s when woken, so a lone mode cue went unheard
    while the next one played. The driver loops one buffer: no CPU work."""
    RATE = 8000

    def __init__(self):
        self.handle = None
        self.header = None
        self.buffer = None
        # start/stop/refresh come from the RPC (settings) and ticker threads.
        self.lock = threading.RLock()
        # Switched on (settings): refresh() retries an open that failed (no output device
        # yet at boot, a Bluetooth speaker still connecting, the audio service restarting).
        self.wanted = False

    @property
    def running(self):
        return self.handle is not None

    def start(self):
        with self.lock:
            self.wanted = True
            return self._start()

    def _start(self):
        if self.handle is not None:
            return True
        try:
            import ctypes as c
            import ctypes.wintypes as w
        except ImportError:
            return False

        class WAVEFORMATEX(c.Structure):
            _fields_ = [('wFormatTag', w.WORD), ('nChannels', w.WORD), ('nSamplesPerSec', w.DWORD),
                        ('nAvgBytesPerSec', w.DWORD), ('nBlockAlign', w.WORD), ('wBitsPerSample', w.WORD),
                        ('cbSize', w.WORD)]

        class WAVEHDR(c.Structure):
            _fields_ = [('lpData', c.c_void_p), ('dwBufferLength', w.DWORD), ('dwBytesRecorded', w.DWORD),
                        ('dwUser', c.c_size_t), ('dwFlags', w.DWORD), ('dwLoops', w.DWORD),
                        ('lpNext', c.c_void_p), ('reserved', c.c_size_t)]

        self._types = WAVEFORMATEX, WAVEHDR
        winmm = c.WinDLL('winmm')
        self._winmm = winmm
        fmt = WAVEFORMATEX(1, 1, self.RATE, self.RATE * 2, 2, 16, 0)
        handle = c.c_void_p()
        if winmm.waveOutOpen(c.byref(handle), w.UINT(0xFFFFFFFF), c.byref(fmt), 0, 0, 0) != 0:
            return False
        rng = random.Random(3)
        samples = bytearray()
        for _ in range(self.RATE):  # one second, looped by the driver
            samples += rng.choice((-1, 0, 1)).to_bytes(2, 'little', signed=True)
        self.buffer = c.create_string_buffer(bytes(samples), len(samples))
        header = WAVEHDR()
        header.lpData = c.cast(self.buffer, c.c_void_p)
        header.dwBufferLength = len(samples)
        size = c.sizeof(WAVEHDR)
        if winmm.waveOutPrepareHeader(handle, c.byref(header), size) != 0:
            winmm.waveOutClose(handle)
            return False
        header.dwFlags |= 0x4 | 0x8  # WHDR_BEGINLOOP | WHDR_ENDLOOP
        header.dwLoops = 0xFFFFFFFF
        if winmm.waveOutWrite(handle, c.byref(header), size) != 0:
            winmm.waveOutUnprepareHeader(handle, c.byref(header), size)
            winmm.waveOutClose(handle)
            return False
        self.handle, self.header = handle, header
        return True

    def stop(self):
        """Switched off (or quitting)."""
        with self.lock:
            self.wanted = False
            self._close()

    def _close(self):
        if self.handle is None:
            return
        import ctypes as c
        winmm = self._winmm
        winmm.waveOutReset(self.handle)
        winmm.waveOutUnprepareHeader(self.handle, c.byref(self.header), c.sizeof(self.header))
        winmm.waveOutClose(self.handle)
        self.handle = self.header = self.buffer = None

    def refresh(self):
        """Reopen so the stream follows a changed default output device; also retries an
        open that failed, which would otherwise leave cues without the device kept awake."""
        with self.lock:
            if self.wanted:
                self._close()
                self._start()


class Sounds:
    """play() never blocks the caller for long; a failed write or device stays silent."""
    def __init__(self, directory, settings, player=None):
        self.directory = Path(directory)
        self.settings = settings  # callable -> {'enabled': bool, 'volume': str}
        self.player = player or _winsound_player
        self.awake = lambda: False  # callable: the output device is kept awake (no lead-in)
        self.play_lock = threading.Lock()  # one PlaySound call at a time, from any thread
        self.last = {}
        self.history = []  # recent (time, cue, outcome) for diagnosing silent cues
        self.history_dirty = False

    def _awake(self):
        try:
            return bool(self.awake())
        except Exception:
            return False

    def _file(self, name, volume, tight):
        return self.directory / (f'v{SOUND_VERSION}-{volume}' + ('-tight' if tight else '')) / (name + '.wav')

    def _path(self, name, volume, tight=None):
        """The cached WAV, made on first use. The two variants differ only by the lead-in
        silence, so one is cut from (or padded onto) the other at once; only a cue never
        rendered before costs a full render (pure Python: up to ~2 s). No lock is held
        while rendering, so a cue never waits for the background warm-up."""
        tight = self._awake() if tight is None else tight
        path = self._file(name, volume, tight)
        if path.exists():
            return path
        other = self._file(name, volume, not tight)
        data = None
        if other.exists():
            try:
                data = _relead(other.read_bytes(), tight)
            except (OSError, EOFError, wave.Error):
                data = None
        if data is None:
            data = render(name, volume, 0 if tight else LEAD)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name('%s.%d.tmp' % (name, threading.get_ident()))
        temp.write_bytes(data)
        try:
            temp.replace(path)
        except OSError:
            temp.unlink(missing_ok=True)  # another thread got there first (or it is playing)
            if not path.exists():
                raise
        return path

    def prepare(self):
        """Warm the cache for the current volume in the background so the first cue is
        instant: the variant in use now first, then the other (keep-awake may stop)."""
        def work():
            volume = self.settings().get('volume', 'medium')
            tight = self._awake()
            for variant in (tight, not tight):
                for name in SOUNDS:
                    try:
                        self._path(name, volume, variant)
                    except Exception:  # a warm-up only: play() renders on demand
                        return
        threading.Thread(target=work, name='sound-cache', daemon=True).start()

    def play(self, name, force=False):
        if name not in SOUNDS:
            return False
        settings = self.settings()
        if not force and not settings.get('enabled', True):
            self._note(name, 'disabled')
            return False
        if time.monotonic() - self.last.get(name, -1) < .08:
            self._note(name, 'repeat-skipped')
            return False
        try:
            path = self._path(name, settings.get('volume', 'medium'))
            with self.play_lock:
                self.player(path)
            self._note(name, 'played')
            return True
        except (OSError, RuntimeError, ImportError) as error:
            self._note(name, 'error: %s' % error)
            return False
        finally:
            # Measured after a possible first render, so a double trigger stays one cue.
            self.last[name] = time.monotonic()

    def _note(self, name, outcome):
        # Memory only here: the caller (a key or the session thread) never waits for the
        # disk. flush_log() writes it from the core's ticker once a second.
        self.history = (self.history + [(time.strftime('%m-%d %H:%M:%S'), name, outcome)])[-200:]
        self.history_dirty = True

    def flush_log(self):
        if not self.history_dirty:
            return
        self.history_dirty = False
        try:
            (self.directory.parent / 'sound-log.txt').write_text(
                ''.join('%s %-12s %s\n' % row for row in list(self.history)), encoding='utf-8')
        except OSError:
            pass
