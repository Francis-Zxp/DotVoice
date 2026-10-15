"""Local recognition used only to *show* words while you speak.

OpenLess's remote interface returns the final text only (its streaming partials
stay inside its own capsule unless the remote server forwards TranscriptDelta;
see design/openless-live-text), so on-device sherpa-onnx models provide the
live preview. The preview is never inserted or saved; when the recording ends,
OpenLess's result replaces it. PCM stays in memory.

Two passes when a refiner model is installed:
  1. a small streaming model shows characters immediately;
  2. about once a second an offline Paraformer re-reads the sentence in
     progress and replaces the streaming guess; at each pause (endpoint) the
     refined sentence is committed and the next one starts.
"""
import array
from pathlib import Path
import queue
import threading
import time

MODEL_FILES = {
    'tokens': 'tokens.txt',
    'encoder': 'encoder-epoch-99-avg-1.int8.onnx',
    'decoder': 'decoder-epoch-99-avg-1.int8.onnx',
    'joiner': 'joiner-epoch-99-avg-1.int8.onnx',
}
REFINER_FILES = ('model.int8.onnx', 'tokens.txt')
# (folder relative to a root, family). DotVoice's own models/ first, then models that
# OpenLess's local-ASR page downloads (same sherpa-onnx archives, reused read-only).
REFINERS = (
    ('models/paraformer-zh-small', 'paraformer'),
    ('models/sense-voice-small-zh', 'sense_voice'),
    ('sherpa-onnx/sense-voice-small-zh', 'sense_voice'),
    ('sherpa-onnx/paraformer-zh', 'paraformer'),
)
EMIT_INTERVAL = .1
RATE = 16000
REFINE_EVERY = RATE          # re-read the sentence after each second of new audio
MAX_SEGMENT = RATE * 15      # force a commit so re-reading stays cheap
LOAD_RETRY = 300             # s: a model that failed to load is not retried (or re-announced) sooner


def find_model(*roots):
    for root in roots:
        folder = Path(root) / 'models' / 'sherpa-zh-14M'
        if all((folder / name).is_file() for name in MODEL_FILES.values()):
            return folder
    return None


def openless_models_dir():
    import os
    return Path(os.environ.get('APPDATA', str(Path.home()))) / 'OpenLess' / 'models'


def find_refiner(*roots):
    """Returns (folder, family) of the first installed offline model, else None."""
    for relative, family in REFINERS:
        for root in roots:
            folder = Path(root) / relative
            if all((folder / name).is_file() for name in REFINER_FILES):
                return folder, family
    return None


PUNCTUATION = set('，。！？、；：,.!?;: ')


SENTENCE_END = set('，。！？、；：,.!?;:')


def _join(left, right):
    """Append a recognised piece: a piece that is only punctuation (a pause read as
    "。") adds nothing, and punctuation is not doubled where two pieces meet."""
    right = right or ''
    if not right.strip(''.join(PUNCTUATION)):
        return left
    if left and left[-1] in SENTENCE_END:
        right = right.lstrip(''.join(SENTENCE_END))
    return left + right


def _unseen_tail(refined, tail):
    """The streaming model lags the audio, so the start of its newer text may
    repeat the end of the refined sentence; drop that overlap."""
    plain = ''.join(ch for ch in refined if ch not in PUNCTUATION)
    for k in range(min(len(tail), 12), 0, -1):
        if plain.endswith(tail[:k]):
            return tail[k:]
    return tail


class LivePreview:
    """start/feed/finish/cancel are called from the controller thread and only
    enqueue work; one worker thread owns the recognizers."""
    def __init__(self, model_dir, emit, recognizer_factory=None, refiner=None, refiner_factory=None):
        self.model_dir = Path(model_dir)
        self.refiner_dir, self.refiner_family = (Path(refiner[0]), refiner[1]) if refiner else (None, None)
        self.emit = emit  # (kind, value)
        self.factory = recognizer_factory or self._load
        self.refiner_factory = refiner_factory or (self._load_refiner if self.refiner_dir else None)
        self.jobs = queue.Queue(maxsize=600)
        self.thread = threading.Thread(target=self._run, name='live-preview', daemon=True)
        self.stopped = threading.Event()
        self.error = None
        self.refiner = None
        self.loaded = False  # the streaming recognizer is up (the worker sets it)
        self.thread.start()

    @property
    def ready(self):
        """Both models loaded and working: final text can come from here (极速落字)."""
        return self.loaded and self.error is None and self.refiner is not None

    @property
    def two_pass(self):
        return self.refiner_factory is not None

    def _load(self):
        import sherpa_onnx
        paths = {key: str(self.model_dir / name) for key, name in MODEL_FILES.items()}
        options = {}
        if self.two_pass:
            # Endpoints split sentences for the refiner; they never end the recording.
            options = dict(enable_endpoint_detection=True, rule1_min_trailing_silence=2.0,
                           rule2_min_trailing_silence=0.8, rule3_min_utterance_length=15.0)
        return sherpa_onnx.OnlineRecognizer.from_transducer(num_threads=1, **paths, **options)

    def _load_refiner(self):
        import sherpa_onnx
        model, tokens = (str(self.refiner_dir / name) for name in REFINER_FILES)
        if self.refiner_family == 'sense_voice':
            return sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=model, tokens=tokens, language='zh', use_itn=True, num_threads=2)
        return sherpa_onnx.OfflineRecognizer.from_paraformer(paraformer=model, tokens=tokens, num_threads=2)

    def _put(self, job):
        try:
            self.jobs.put_nowait(job)
        except queue.Full:
            pass  # a lagging preview drops audio rather than delaying recording

    def warm(self):
        """Load the models ahead of the first recording (the refiner is ~240 MB)."""
        self._put(('warm', None))

    def start(self, session):
        self._put(('start', session))

    def feed(self, pcm):
        self._put(('pcm', pcm))

    def finish(self):
        self._put(('finish', None))

    def cancel(self):
        self._put(('cancel', None))

    def close(self):
        self.stopped.set()
        self._put(('quit', None))

    # -- worker -------------------------------------------------------------
    def _refine(self, samples):
        if self.refiner is None or not samples:
            return None
        stream = self.refiner.create_stream()
        stream.accept_waveform(RATE, samples)
        self.refiner.decode_stream(stream)
        return stream.result.text.strip()

    def _run(self):
        recognizer = stream = session = None
        last_text, last_emit = '', 0.0
        committed, segment, since_refine = '', [], 0
        refined, refined_at = None, 0   # refined text and streaming length when it was made

        def current(streaming):
            if refined is None:
                return committed + streaming
            return committed + refined + _unseen_tail(refined, streaming[refined_at:])

        retry_at = 0.0  # after a failed load: no reload (and no repeated notice) until then
        while not self.stopped.is_set():
            kind, data = self.jobs.get()
            if kind == 'quit':
                break
            try:
                if recognizer is None and kind in ('start', 'warm'):
                    if time.monotonic() < retry_at:
                        stream = session = None
                        continue
                    retry_at = time.monotonic() + LOAD_RETRY
                    recognizer = self.factory()
                    retry_at = 0.0
                    self.error = None
                    if self.refiner_factory is not None:
                        try:
                            self.refiner = self.refiner_factory()
                        except Exception as error:  # keep the one-pass preview
                            self.refiner = None
                            self.emit('preview_error', '精修模型不可用，改用单次预览：' + str(error))
                    self.loaded = True
                if kind == 'warm':
                    continue
                if kind == 'start':
                    stream, session, last_text = recognizer.create_stream(), data, ''
                    committed, segment, since_refine, refined, refined_at = '', [], 0, None, 0
                elif kind == 'cancel':
                    stream = session = None
                elif stream is None:
                    continue
                elif kind == 'pcm':
                    samples = [s / 32768 for s in array.array('h', data)]
                    stream.accept_waveform(RATE, samples)
                    while recognizer.is_ready(stream):
                        recognizer.decode_stream(stream)
                    streaming = recognizer.get_result(stream).strip()
                    if self.refiner is not None:
                        segment.extend(samples)
                        since_refine += len(samples)
                        endpoint = getattr(recognizer, 'is_endpoint', lambda s: False)(stream)
                        if endpoint or len(segment) >= MAX_SEGMENT:
                            text = self._refine(segment)
                            committed = _join(committed, text if text is not None else streaming)
                            segment, since_refine, refined, refined_at = [], 0, None, 0
                            recognizer.reset(stream)
                            streaming = ''
                        elif since_refine >= REFINE_EVERY:
                            # Mid-sentence: the offline model ends every guess with a full stop.
                            guess = self._refine(segment)
                            refined = guess.rstrip('。.') if guess is not None else None
                            refined_at, since_refine = len(streaming), 0
                    text = current(streaming)
                    now = time.monotonic()
                    if text != last_text and now - last_emit >= EMIT_INTERVAL:
                        last_text, last_emit = text, now
                        self.emit('partial', {'session': session, 'text': text, 'final': False})
                elif kind == 'finish':
                    stream.input_finished()
                    while recognizer.is_ready(stream):
                        recognizer.decode_stream(stream)
                    streaming = recognizer.get_result(stream).strip()
                    if self.refiner is not None and segment:
                        text = self._refine(segment)
                        text = _join(committed, text if text is not None else streaming)
                    else:
                        text = current(streaming)
                    self.emit('partial', {'session': session, 'text': text, 'final': True})
                    stream = session = None
            except Exception as error:  # a broken model disables the preview only
                self.error = str(error)
                stream = session = None
                recognizer = None
                self.loaded = False
                self.emit('preview_error', '实时预览不可用：' + self.error)
