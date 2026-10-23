"""CloudBackend: DotVoice's built-in speech engine with the OpenLessBackend interface.

It composes a streaming recogniser (bridge.engines.volc.VolcASR or
bridge.engines.dashscope.DashScopeASR), an optional polisher (bridge.engines.llm.Polisher)
and text insertion (bridge.fast_insert.type_text). As with OpenLess, the backend itself
types the final text at the cursor before it reports the result, so the controller needs
no changes and the bridge never types twice.

connect() only checks the configuration (no network). start() returns at once: the
recogniser connects on its own thread while the audio waits in memory. feed() and stop()
never block on the network. cancel() drops the session; nothing is typed afterwards.

Events returned by poll() (the controller's vocabulary, see Controller._backend_event).
Every event carries "source": "cloud" (live text from an accurate built-in recogniser,
not OpenLess; the class also sets partial_source = 'cloud') and "engine": "volc" /
"dashscope":
  {"type": "partial", "text": <whole text so far>, "offset": 0, "final": False}
  {"type": "partial", "text": <raw transcript>, "offset": 0, "final": True}
  {"kind": "polishing"}                         only when a polish style is set
  {"kind": "notice", "message"}                 polish failed or was not configured
  {"type": "result", "text": <final text>, "raw"?: <transcript before polish>,
   "not_typed"?: "elevated" | "partial" | "refused"}
                                                the text did not (fully) reach the window: an
                                                administrator window dropped the keystrokes,
                                                Windows refused the rest after some went in, or
                                                refused them all (lock screen, UAC prompt). The
                                                text is kept (the core copies it to the clipboard)
  {"kind": "error", "message": <Chinese>}
"""
from collections import deque
import threading
import time

from ..backend import VoiceBackend
from ..elevation import ElevatedTarget
from ..fast_insert import PartlyTyped, type_text
from .llm import PolishError, Polisher

# Names under which bridge.secrets keeps the keys (values never live in prefs).
SECRET_VOLC_API_KEY = 'volc.api_key'
SECRET_VOLC_ACCESS_TOKEN = 'volc.access_token'
SECRET_DASHSCOPE_API_KEY = 'dashscope.api_key'
SECRET_LLM_API_KEY = 'llm.api_key'

ASR_WATCHDOG = 30   # seconds after stop() while the recogniser has not even connected (e.g. DNS hung): give up
# Seconds after stop() in all, connected or not (a backlog still uploading on a slow link). Once
# connected, the session's own timeouts bound each step; this cap leaves time to polish within
# the controller's two-minute limit.
ASR_CAP = 90
POLISH_BY = 100     # seconds after stop(): polish is done by then, or the transcript is used
# 轻度润色 / 自定义 chosen before the relay's URL, model and key are all filled in: the voice keeps
# working (the transcript is typed as it is) and each recording says so once.
POLISH_INCOMPLETE = '润色未配置完整，已按原文输入'


class _CallablePolish:
    enabled = True

    def __init__(self, function):
        self.function = function

    def check(self):
        pass

    def polish(self, text, deadline=None):
        return self.function(text)


class _Job:
    """One recording: the recogniser's session, then polish + insertion on a worker thread."""

    def __init__(self, backend, insert):
        self.backend, self.insert = backend, insert
        self.engine = backend.engine
        self.cancelled = threading.Event()
        self.finished = False      # a terminal event (result / error) was queued
        self.transcribed = False
        self.stopped_at = None
        self.session = backend.asr.session(self._on_asr)

    def cancel(self):
        self.cancelled.set()
        self.session.cancel()

    def _push(self, event, terminal=False):
        self.backend._push(self, {**event, 'source': 'cloud', 'engine': self.engine}, terminal)

    def _on_asr(self, event):   # recogniser thread
        if self.cancelled.is_set():
            return
        if event.get('type') == 'partial':
            self._push({'type': 'partial', 'text': event.get('text') or '', 'offset': 0, 'final': False})
        elif event.get('type') == 'final':
            self.transcribed = True
            threading.Thread(target=self._finish, args=(event.get('text') or '',),
                             name='cloud-finish', daemon=True).start()
        elif event.get('kind') == 'error':
            self._push({'kind': 'error', 'message': event.get('message') or '语音识别失败'}, terminal=True)

    def _notice(self, message):
        self._push({'kind': 'notice', 'message': message})

    def _finish(self, raw):
        backend = self.backend
        raw = raw.strip()
        self._push({'type': 'partial', 'text': raw, 'offset': 0, 'final': True})
        text = raw
        polisher = backend.polish
        if raw and polisher is not None and polisher.enabled:
            try:
                polisher.check()
            except ValueError as exc:
                self._notice(f'{POLISH_INCOMPLETE}（{exc}）')   # no request: nothing to send it to
                polisher = None
        if raw and polisher is not None and polisher.enabled:
            self._push({'kind': 'polishing'})
            try:
                if isinstance(polisher, (Polisher, _CallablePolish)) and self.stopped_at is not None:
                    text = polisher.polish(raw, deadline=self.stopped_at + POLISH_BY)
                else:
                    text = polisher.polish(raw)
                text = (text or '').strip()
            except PolishError as exc:
                self._notice(f'{exc}；已改用识别原文')
                text = raw
            except Exception as exc:   # a polisher bug must not lose the dictation
                self._notice(f'润色出错（{type(exc).__name__}）；已改用识别原文')
                text = raw
        if text and backend.corrector is not None:
            try:
                text = backend.corrector(text) or text
            except Exception:
                pass
        if self.cancelled.is_set():
            return
        not_typed = None
        if text and self.insert:
            with backend.insert_lock:
                if self.cancelled.is_set():
                    return
                try:
                    backend.inserter(text)
                except ElevatedTarget:
                    not_typed = 'elevated'  # said once with the result, never lost
                except PartlyTyped:
                    not_typed = 'partial'
                except Exception:   # plain OSError (lock screen, UAC prompt) or a typer bug
                    not_typed = 'refused'
        result = {'type': 'result', 'text': text}
        if text != raw:
            result['raw'] = raw
        if not_typed:
            result['not_typed'] = not_typed
        self._push(result, terminal=True)


class CloudBackend(VoiceBackend):
    """asr: VolcASR / DashScopeASR (anything with check(), session(emit), name, source).
    polish: None, a Polisher (raw style sends nothing), or a plain callable text -> text.
    inserter: types text at the cursor (bridge.fast_insert.type_text).
    corrector: optional text -> text applied to the final text before it is typed."""
    partial_source = 'cloud'   # Controller._partial_source: live text from a built-in engine

    def __init__(self, asr, polish=None, inserter=type_text, corrector=None, asr_watchdog=ASR_WATCHDOG,
                 asr_cap=ASR_CAP):
        self.asr = asr
        if polish is not None and not hasattr(polish, 'polish') and callable(polish):
            polish = _CallablePolish(polish)
        self.polish = polish
        self.inserter = inserter
        self.corrector = corrector
        self.asr_watchdog = asr_watchdog
        self.asr_cap = asr_cap
        self.insert_text = True
        self.label = getattr(asr, 'name', '云端识别')      # e.g. 豆包语音 / 阿里云百炼, for UI text
        self.engine = getattr(asr, 'source', 'cloud')      # 'volc' / 'dashscope'
        self.connected = False
        self.job = None
        self.events = deque()
        self.events_lock = threading.Lock()
        self.insert_lock = threading.Lock()

    def __repr__(self):
        return f'CloudBackend({self.asr!r}, polish={self.polish!r})'

    # -- VoiceBackend --------------------------------------------------------------
    def connect(self):
        """Configuration check only; no network traffic until a recording starts. Only the
        recogniser can refuse: an incomplete polish setup never stops the voice (each
        recording is then typed as recognised, and says so: POLISH_INCOMPLETE)."""
        try:
            self.asr.check()
        except ValueError as exc:
            raise RuntimeError(str(exc)) from None
        self.connected = True

    def start(self):
        if not self.connected:
            self.connect()
        old, self.job = self.job, None
        if old is not None:
            old.cancel()   # a stale session (should not happen): the new press wins
        job = _Job(self, self.insert_text)
        self.job = job
        job.session.start()

    def feed(self, pcm):
        job = self.job
        if job is None:
            raise RuntimeError('没有已确认的录音会话')
        job.session.feed(pcm)

    def stop(self):
        job = self.job
        if job is not None and job.stopped_at is None:
            job.stopped_at = time.monotonic()
            job.session.finish()

    def poll(self):
        self._watchdog()
        with self.events_lock:
            while self.events:
                job, event = self.events.popleft()
                if job is not self.job or job.cancelled.is_set():
                    continue   # from a cancelled or earlier session
                if event.get('type') == 'partial' and not event.get('final'):
                    # Each live text replaces the whole text: only the newest one matters.
                    while (self.events and self.events[0][0] is job
                           and self.events[0][1].get('type') == 'partial' and not self.events[0][1].get('final')):
                        event = self.events.popleft()[1]
                if event.get('type') == 'result' or event.get('kind') == 'error':
                    self.job = None
                return event
        return None

    def cancel(self):
        job, self.job = self.job, None
        if job is not None:
            job.cancel()

    def close(self):
        self.cancel()
        self.connected = False

    # -- internals -------------------------------------------------------------------
    def _push(self, job, event, terminal=False):
        with self.events_lock:
            if job.cancelled.is_set() or job.finished:
                return
            if terminal:
                job.finished = True
            self.events.append((job, event))

    def _watchdog(self):
        job = self.job
        if job is None or job.stopped_at is None or job.transcribed or job.finished:
            return
        waited = time.monotonic() - job.stopped_at
        stats = getattr(job.session, 'stats', None) or {}
        cap = max(self.asr_watchdog, self.asr_cap)
        if stats.get('connected') is None and waited > self.asr_watchdog:
            message = f'{self.label}超过 {self.asr_watchdog:g} 秒没有返回结果，本段已取消'
        elif waited > cap:
            message = f'网络太慢，{self.label}在 {cap:g} 秒内没能完成识别，本段已取消'
        else:
            return   # connected: still sending a backlog, or the session's own final_timeout runs
        job.session.cancel()
        job._push({'kind': 'error', 'message': message}, terminal=True)


def create_backend(settings, secret=None, inserter=type_text, corrector=None):
    """Build a CloudBackend from plain settings (never keys); keys are read from bridge.secrets
    (or `secret(name)`) each time a recording starts, so changing a key needs no rebuild.

    settings = {
      'engine': 'volc' | 'dashscope',
      'volc': {'auth': 'auto'|'api_key'|'app', 'app_id': '', 'resource_id': 'volc.seedasr.sauc.duration',
               'mode': 'async'|'stream'|'nostream', 'two_pass': True, 'ddc': False,
               'hotwords': [] or a callable returning them (read when a recording starts)},
      'dashscope': {'model': 'fun-asr-realtime', 'region': 'cn'|'intl', 'workspace': '',
                    'language_hints': [] (none: ["zh"] for Fun-ASR), 'vocabulary_id': ''},
      'polish': {'style': 'raw'|'light'|'custom', 'base_url': '', 'model': '', 'prompt': ''},
    }
    Doubao streams 200 ms packets (volc.PACKET_MS, the documented optimum), Bailian 100 ms.
    Secrets: volc.api_key (new console) or volc.access_token (old console, with app_id),
    dashscope.api_key, llm.api_key."""
    if secret is None:
        from .. import secrets as store
        secret = store.load
    engine = settings.get('engine')
    if engine == 'volc':
        from .volc import DEFAULT_RESOURCE, PACKET_MS, VolcASR
        conf = settings.get('volc') or {}
        asr = VolcASR(api_key=lambda: secret(SECRET_VOLC_API_KEY), app_id=conf.get('app_id') or None,
                      access_token=lambda: secret(SECRET_VOLC_ACCESS_TOKEN),
                      resource_id=conf.get('resource_id') or DEFAULT_RESOURCE, mode=conf.get('mode') or 'async',
                      auth=conf.get('auth') or 'auto', two_pass=conf.get('two_pass', True) is not False,
                      ddc=bool(conf.get('ddc')), hotwords=conf.get('hotwords') or (), packet_ms=PACKET_MS)
    elif engine == 'dashscope':
        from .dashscope import DEFAULT_MODEL, DashScopeASR
        conf = settings.get('dashscope') or {}
        asr = DashScopeASR(api_key=lambda: secret(SECRET_DASHSCOPE_API_KEY), model=conf.get('model') or DEFAULT_MODEL,
                           region=conf.get('region') or 'cn', workspace=conf.get('workspace') or '',
                           language_hints=conf.get('language_hints') or (),
                           vocabulary_id=conf.get('vocabulary_id') or '')
    else:
        raise ValueError('不支持的识别引擎')
    conf = settings.get('polish') or {}
    style = conf.get('style') or 'raw'
    polisher = None
    if style != 'raw':
        polisher = Polisher(conf.get('base_url') or '', lambda: secret(SECRET_LLM_API_KEY), conf.get('model') or '',
                            style=style, prompt=conf.get('prompt') or '')
    return CloudBackend(asr, polisher, inserter=inserter, corrector=corrector)
