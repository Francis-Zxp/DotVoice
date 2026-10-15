"""Single-owner session control; native Baseus events already encode both modes."""
from collections import deque
from concurrent.futures import Future, TimeoutError
import audioop
import inspect
import queue
import threading
import time
import uuid
from .audio import SETTLE, AudioCapture
from .backend import BackendBusy, OpenLessBackend
from .devices import get_profile
from .elevation import ElevatedTarget
from .engines.hotkey import ExternalAppOnly
from .fast_insert import PartlyTyped, type_text
from .speech_meter import SpeechMeter
from .voice_dsp import WhisperDSP

BUSY_NOTICE = "尚未就绪或上一段正在识别；请等候后重新按键"
# OpenLess runs one session at a time across all its clients (随拿随用: the pen and
# Tidepool both connect); its "busy" reply means another device is talking.
OTHER_DEVICE_NOTICE = "另一台设备正在说话，请等它说完再按录音键"
# Opening another receiver's microphone on its first press: drop less of the
# start than a fresh arm does, because the speaker is already talking.
ROUTE_SETTLE = 0.25
INPUT_LIMIT = 600
# A recording this long with less voiced audio than SILENT_SPEECH is treated as
# "nobody spoke": it is cancelled instead of sent, so OpenLess's polisher never
# answers an empty transcript with chatter that would be typed or saved.
SILENT_MIN_AUDIO = 0.6
SILENT_SPEECH = 0.25
# WebRTC VAD listens for voicing and a whisper has none, so it hears almost
# nothing of one. Unless 气声 is off, energy that stays above the noise floor in
# syllable-length runs (bridge.voice_dsp) for this long - about one whispered
# syllable - also counts as speech. True silence and steady noise score zero.
WHISPER_SPEECH = 0.15
NOTE_LIMIT = 1800
# ⑥'s short press is a firmware latch (74 = microphone on, 75 = off). A 75 while idle
# means the press that switched the microphone on never arrived - key logs show the
# first press after the pen has slept, or at the edge of range, getting lost - so
# this press only switched it off. Say so; otherwise it seems to do nothing at all.
MIC_OFF_NOTICE = "这一下只关掉了笔上开着的麦克风（点声当时没在录音，可能是开始信号丢了）；再按一次 ⑥ 开始录音"
# The press that switched the pen microphone on was the setup wizard's key test (step 1):
# nothing was lost, the latch is simply still on. This press switched it off.
TESTED_MIC_OFF_NOTICE = "这一下关掉了配置向导测试按键时打开的笔麦克风；再按一次 ⑥ 开始录音"
# 极速落字: wait this long for the local preview to finish the last sentence before
# handing the (kept) audio to OpenLess after all.
FAST_WAIT = 3.0
# Where backend live text comes from: OpenLess, or a built-in cloud engine (bridge.engines).
PARTIAL_SOURCES = ('remote', 'cloud')
# The hotkey bridge drives another dictation app that types into the focused field itself.
EXTERNAL_NOTES_NOTICE = "该软件只能输入到光标处，笔记请用豆包/百炼/OpenLess"
# Setup wizard: while its window is open every recording is a test - nothing is typed or
# saved and the text goes to the wizard. The wizard renews this; it lapses on its own.
TEST_WINDOW = 90
# Setup wizard: volume levels while no recording runs (step 1's microphone check).
METER_EVERY = 0.1
# The foreground window runs as administrator: Windows dropped the typed text (bridge.elevation).
NOT_TYPED_MESSAGE = "文字没能输入到管理员权限窗口，已保留在本次结果里"
# A built-in cloud engine reached its input limit: it has the text so far, so the recording
# ends and that text is typed (OpenLess / another app: still cancelled, see _run).
CLOUD_LIMIT_NOTICE = "录音已满 {minutes} 分钟，已自动结束并输入识别出的文字"


def backend_label(backend):
    """The name the UI and state messages use for the engine: a built-in engine's label
    (豆包语音 / 阿里云百炼), the driven app for the hotkey bridge (闪电说), else OpenLess."""
    label = getattr(backend, 'label', None)
    if isinstance(label, str) and label:
        return label
    if getattr(backend, 'external', False):
        name = getattr(backend, 'name', None)
        if isinstance(name, str) and name:
            return name
    return 'OpenLess'


def handed_to(name):
    """'已交给闪电说' / '已交给 Typeless': a Latin name keeps a space from the Chinese."""
    return '已交给' + (' ' if name[:1].isascii() else '') + name


def report_code(event):
    path = event.get("path", "").upper()
    if event.get("kind") != 2 or "VID_ABC8&PID_CA08&MI_03" not in path:
        return None
    try:
        report = bytes.fromhex(event.get("hex", ""))
    except ValueError:
        return None
    # Measured on the user's BS-OH085. Ignore padding and the neutral report.
    # Byte 3 is battery telemetry (observed 0x4c and 0x64); it is not
    # part of the key signature. Keep the remaining measured framing strict.
    if (len(report) < 7 or report[:3] != bytes.fromhex("0a0100") or
            report[4] != 0xff or report[6] != 0):
        return None
    return report[5]


def decode_report(event):
    # Firmware owns the short-press latch: 74 starts, 75 stops. Treating only
    # 74 as a software toggle loses every other tap and can invert mic state.
    # Confirmed with alternating live reports and original resolveKey branches.
    return {0x74: "start", 0x75: "stop", 0x76: "start", 0x77: "stop"}.get(report_code(event))


class Controller:
    def __init__(self, notify, backend=None, capture_factory=AudioCapture, device='baseus', mode='hold'):
        self.notify = notify
        self.backend = backend or OpenLessBackend()
        self.capture_factory = capture_factory
        try:
            self.factory_takes_target = 'target' in inspect.signature(capture_factory).parameters
        except (TypeError, ValueError):
            self.factory_takes_target = False
        self.device, self.mode = get_profile(device)['id'], mode
        # Several receivers: the microphone stays open on one of them (capture_target,
        # {'id', 'parent'} from bridge.instances); a record key on another moves it.
        self.capture_target = None
        self.session_receiver = None
        self.route_of = None       # event -> target, set by the core
        self.default_route = None  # device -> target to open when arming
        self.hardware = None
        self.abort = threading.Event()
        self.capture = None
        self.commands = queue.Queue(maxsize=300)
        self.closing = threading.Event()
        self.shutdown_released = False
        self.overflow = threading.Event()
        self.audio_lock = threading.Lock()
        self.preroll = deque(maxlen=4)
        self.accept_audio = False
        self.generation = 0
        self.want_armed = False
        self.phase = "disconnected"
        self.stop_at = None
        self.processing_at = None
        self.recording_at = None
        self.last_retry = 0
        self.last_error = ""
        self.last_codes = {}  # HID path -> last vendor code: each receiver has its own edges
        self.last_meter = 0
        self.voice_levels = deque(maxlen=6500)
        self.destination = 'input'
        self.session_destination = 'input'
        self.session_source = 'key'  # 'quick' = a ⑦ quick note
        self.note_category = 'idea'
        self.session_category = 'idea'
        self.speech_meter = None
        # 气声 front end (prefs voice.whisper: auto/on/off), read when a session starts.
        self.whisper_mode = 'auto'
        self.dsp = WhisperDSP()
        self.whisper_announced = False
        # Opt-in debug recordings (bridge.diagnostics; the core sets it, prefs
        # diagnostics.save_audio): the raw and the sent audio of each session. None: off.
        self.diagnostics = None
        self.message = ''
        self.session_id = None
        self.preview = None  # optional local live-text preview (start/feed/finish/cancel)
        # Backend live text wins over the local preview as soon as it arrives: OpenLess's
        # TranscriptDelta forwarded as {"type":"partial"} (older versions never send it),
        # or a built-in cloud engine's partials through the same poll path. It comes from
        # the recognizer that writes the final text, so the UI shows it (source 'remote' /
        # 'cloud'); the local preview's own words are 'local' (core._preview_event).
        self.remote_text = ''
        self.remote_live = False
        self.on_device_key = None  # (device, code, down) for non-voice PV60 keys
        # 极速落字 (raw mode): `fast()` decides per input session (core: prefs + OpenLess
        # in raw mode); the local preview then supplies the text and `typer` types it.
        # The session's audio stays in memory only until then, as a fallback for OpenLess.
        self.fast = None
        self.typer = type_text
        self.corrector = lambda text: text
        self.session_fast = False
        self.fast_pcm = []
        self.fast_deadline = None
        # Setup wizard: test recordings (nothing typed or saved) and a volume meter while
        # no recording runs. meter_capture is opened only when the voice is paused.
        self.test_until = 0.0
        self.session_test = False
        self.meter_until = 0.0
        self.meter_capture = None
        self.meter_retry = 0.0
        self.last_meter_idle = 0.0
        # Latching presses (⑥ 74, ⑦ 7a) that switched a pen microphone on without starting a
        # session - refused (busy, a note with the hotkey bridge) or the wizard's key test.
        # receiver id -> 'refused' | 'tested': that pen's next mic-off report (75 / 7b) is
        # no lost start, so it gets no "start signal lost" hint (bridge.core asks too).
        self.quiet_latch = {}
        self.quiet_lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, name="baseus-sessions", daemon=True)
        self.thread.start()

    # -- engine-aware wording ----------------------------------------------------------
    def _label(self):
        return backend_label(self.backend)

    def _external(self):
        return bool(getattr(self.backend, 'external', False))

    def _cloud(self):
        return getattr(self.backend, 'partial_source', None) == 'cloud'

    # -- setup wizard ------------------------------------------------------------------
    def test_mode(self, enabled, seconds=TEST_WINDOW):
        """Wizard: recordings started in the next `seconds` are tests. Nothing is typed (an
        external app still types into the focused field - the wizard's own box) and nothing
        is saved as a note; the result is marked `test`."""
        self.test_until = time.monotonic() + seconds if enabled else 0.0

    def _testing(self):
        return self.test_until > time.monotonic()

    def meter(self, seconds):
        """Wizard: report the microphone level ('level' events, 10 Hz) for `seconds` (0 stops)
        while no recording runs. With the voice paused a capture is opened only for this."""
        self.meter_until = time.monotonic() + seconds if seconds and seconds > 0 else 0.0
        try:
            self.commands.put_nowait(('meter', None))
        except queue.Full:
            pass

    def _metering(self):
        return self.meter_until > time.monotonic()

    # -- latching presses that started nothing ---------------------------------------------
    def _latch_key(self, receiver):
        target = receiver if receiver is not None else self.capture_target
        return target.get('id') if isinstance(target, dict) else None

    def latch_unused(self, receiver, reason='refused'):
        """A latching press switched this pen's microphone on but started no session."""
        with self.quiet_lock:
            if len(self.quiet_latch) > 32:
                self.quiet_latch.clear()
            self.quiet_latch[self._latch_key(receiver)] = reason

    def latch_hint(self, receiver):
        """This pen sent a mic-off report (75 / 7b). Which hint fits if no session is running:
        'lost' (its start report never arrived), 'tested' (the wizard's key test switched it
        on), or None (its start was refused and already said so, or the wizard is testing
        the keys now). Always forgets the pen's remembered press."""
        with self.quiet_lock:
            reason = self.quiet_latch.pop(self._latch_key(receiver), None)
        if self._metering() or reason == 'refused':
            return None
        return reason or 'lost'

    def _sync_meter(self, force=False):
        """Controller thread: open / close the meter-only capture as the wizard asks."""
        now = time.monotonic()
        wanted = self.meter_until > now
        if self.meter_capture is not None:
            if not wanted or self.capture is not None:
                self._close_meter()
            return
        if not wanted or self.capture is not None or (not force and now < self.meter_retry):
            return
        self.meter_retry = now + 3
        target = None
        if self.default_route is not None:
            try:
                target = self.default_route(self.device)
            except Exception:
                target = None
        try:
            capture = self._open_capture(target, SETTLE, self._meter_audio, self._meter_failed)
            self.meter_capture = capture
            capture.start()
        except Exception as exc:
            self._close_meter()
            self.notify('meter', {'ok': False, 'message': str(exc)})
            return
        self.notify('meter', {'ok': True, 'message': ''})

    def _close_meter(self):
        capture = self.meter_capture
        if capture is not None:
            try:
                capture.close()
            except Exception:
                return False
            self.meter_capture = None
        return True

    def _meter_audio(self, pcm):
        # Capture thread of the meter-only capture: volume numbers, nothing is kept.
        now = time.monotonic()
        if self.meter_until > now and now - self.last_meter_idle >= METER_EVERY:
            self.last_meter_idle = now
            self.notify('level', min(1.0, audioop.rms(pcm, 2) / 8000))

    def _meter_failed(self, message):
        try:
            self.commands.put_nowait(('meter_error', message))
        except queue.Full:
            pass

    def _state(self, phase, message=""):
        self.phase = phase
        self.message = message
        busy = phase in ("starting", "recording", "processing")
        value = {"phase": phase, "message": message,
                 "destination": self.session_destination if busy else self.destination}
        if busy and self.dsp.engaged:
            value["whisper"] = True  # this session lifts soft / whispered speech
            self.whisper_announced = True
        self.notify("state", value)

    def command(self, action, completion=None, receiver=None):
        """`receiver` (a target from bridge.instances) says which pen sent a
        start/stop; None means the pen whose microphone is open."""
        # Preserve the user's pause intention even if an error clears queued work.
        if action == "disarm":
            self.want_armed = False
        elif action == "arm":
            self.want_armed = True
        if action in ('cancel', 'disarm'):
            self.abort.set()
            with self.audio_lock:
                self.accept_audio = False
                self.generation += 1
                self.preroll.clear()
        try:
            self.commands.put_nowait((action, receiver if receiver is not None else completion))
        except queue.Full:
            self.overflow.set()
            if completion is not None:
                completion.set_exception(RuntimeError('控制队列已满，请稍后重试'))

    def disarm_and_wait(self, timeout=4):
        """Confirm this queued pause completed, not merely an earlier idle state."""
        if self.closing.is_set() or not self.thread.is_alive():
            raise RuntimeError('语音控制器已退出')
        completion = Future()
        self.command('disarm', completion)
        try:
            completion.result(timeout)
        except TimeoutError:
            raise RuntimeError('翻页笔尚未完全释放，请稍后再试') from None
        if self.phase != 'disconnected' or self.want_armed or self.capture or self.hardware:
            raise RuntimeError('翻页笔尚未完全释放，请稍后再试')

    def set_destination(self, destination, category='idea'):
        if destination not in ('input', 'notes'):
            raise ValueError('不支持的文字去向')
        if self.phase in ('disconnected', 'error') and self.capture is None:
            # No session can start before the next arm, and every session copies the
            # destination when it starts: no need to queue behind a slow reconnect.
            self.destination = destination
            self.note_category = category if category in ('idea', 'paper') else 'idea'
            return destination
        completion = Future()
        self.commands.put(('destination', (destination, category, completion)), timeout=1)
        try:
            return completion.result(timeout=12)
        except TimeoutError:
            completion.cancel()
            raise

    def on_hid(self, event):
        if self.device != 'baseus':
            return
        code = report_code(event)
        if code is None:
            return
        path = event.get("path", "")
        if code == 0:
            self.last_codes.pop(path, None)
            return
        if code == self.last_codes.get(path):
            return
        if len(self.last_codes) > 64:
            self.last_codes.clear()
        self.last_codes[path] = code
        action = decode_report(event)
        if action:
            self.notify("key", action)
            receiver = self.receiver_of(event)
            # 74 latches the pen microphone on: remembered if it starts no session.
            self.command('latch_start' if code == 0x74 else action, receiver=receiver)
            if code == 0x75:
                self.command("latch_off", receiver=receiver)  # after the stop: idle check

    def forget_paths(self, present):
        """Receivers that went away (paths not in `present`, upper-case): forget their
        half-finished key edges so the first press after re-plugging is not a repeat."""
        for path in list(self.last_codes):
            if path.upper() not in present:
                self.last_codes.pop(path, None)

    def receiver_of(self, event):
        """Which receiver sent this event (None when unknown or not wired)."""
        hook = self.route_of
        if hook is None:
            return None
        try:
            return hook(event)
        except Exception:
            return None

    def select_device(self, device, mode='hold'):
        get_profile(device)
        if mode not in ('hold', 'toggle'):
            raise ValueError('录音方式无效')
        if self.phase != 'disconnected' or self.want_armed or self.capture or self.hardware:
            raise RuntimeError('请先暂停语音输入，再切换设备或录音方式')
        self.device, self.mode = device, mode
        self.last_codes.clear()
        self.capture_target = self.session_receiver = None

    def _pv60_button(self, down):
        if self.phase not in ('idle', 'recording', 'starting'):
            return
        if self.mode == 'toggle':
            if down:
                self.command('toggle')
        else:
            self.command('start' if down else 'stop')

    def _pv60_key(self, code, down):
        # Runs on the PV60 HID thread; the callback must only enqueue work.
        callback = self.on_device_key
        if callback:
            callback('pv60', code, down)

    def _audio(self, pcm):
        now = time.monotonic()
        level = None
        with self.audio_lock:
            if self.accept_audio:
                if now - self.last_meter >= 0.1:
                    self.last_meter = now
                    # Show the level that is being sent: a lifted whisper moves the meter.
                    level = min(1.0, audioop.rms(pcm, 2) * self.dsp.meter_gain / 8000)
                    self.voice_levels.append(level)
                try:
                    self.commands.put_nowait(("pcm", (self.generation, pcm)))
                except queue.Full:
                    self.overflow.set()
            else:
                self.preroll.append(pcm)
                if self.meter_until > now and now - self.last_meter_idle >= METER_EVERY:
                    # Setup wizard's microphone check: the level while nobody is recording.
                    self.last_meter_idle = now
                    level = min(1.0, audioop.rms(pcm, 2) / 8000)
        if level is not None:
            self.notify("level", level)

    def _audio_error(self, message):
        try:
            self.commands.put_nowait(("audio_error", message))
        except queue.Full:
            self.overflow.set()

    def _clear_audio(self):
        with self.audio_lock:
            self.accept_audio = False
            self.generation += 1
            self.preroll.clear()
            self.voice_levels.clear()
        self._diagnose('abort')  # a debug recording not finished by now was no session
        self.dsp.reset()
        self.stop_at = self.processing_at = self.recording_at = None
        self.session_fast = False
        self.fast_pcm = []
        self.fast_deadline = None

    def _preview(self, method, *args):
        # The preview is display-only; it can never fail a recording.
        if self.preview is not None:
            try:
                getattr(self.preview, method)(*args)
            except Exception:
                pass

    def _diagnose(self, method, *args):
        # Debug recordings (opt-in) can never fail a recording either.
        diagnostics = self.diagnostics
        if diagnostics is not None:
            try:
                getattr(diagnostics, method)(*args)
            except Exception:
                pass

    def _disconnect(self, cancel=True):
        self._preview('cancel')
        self._finish_metrics('interrupted')
        self._clear_audio()
        close_error = None
        for name in ('capture', 'hardware'):
            resource = getattr(self, name)
            if resource:
                try:
                    resource.close()
                except Exception as exc:
                    # Keep ownership until a later close confirms it has stopped.
                    close_error = close_error or exc
                else:
                    setattr(self, name, None)
        try:
            if cancel:
                self.backend.cancel()
        finally:
            self.backend.close()
        if close_error:
            raise close_error

    def _fail(self, message):
        close_error = None
        try:
            self._disconnect()
        except Exception as exc:
            close_error = exc
        self.overflow.clear()
        # Discard pending actions as well as PCM; recovering never replays a key.
        pauses = []
        while True:
            try:
                action, data = self.commands.get_nowait()
                if action == 'disarm':
                    pauses.append(data)
                elif action == 'destination' and not data[2].done():
                    data[2].set_exception(RuntimeError('连接变化，切换已取消'))
            except queue.Empty:
                break
        self.last_retry = time.monotonic()
        self.last_error = message
        paused = bool(pauses) and not self.want_armed and close_error is None
        self._state('disconnected' if paused else 'error',
                    '已暂停，麦克风已释放' if paused else message)
        for completion in pauses:
            if completion is not None:
                if paused:
                    completion.set_result(None)
                else:
                    completion.set_exception(RuntimeError(str(close_error or message)))

    def _arm(self):
        self.last_retry = time.monotonic()
        if self.hardware:
            # A timed-out HID close must not be replaced by a second worker.
            self.hardware.close()
            self.hardware = None
        self.abort.clear()
        self.backend.connect()
        if self.abort.is_set() or self.closing.is_set():
            raise RuntimeError('启用已取消')
        if self.device == 'pv60':
            from .pv60 import PV60Session
            self.hardware = PV60Session(self._pv60_button, self._audio_error)
            self.hardware.on_key = self._pv60_key
            self.hardware.start()
        if self.abort.is_set() or self.closing.is_set():
            raise RuntimeError('启用已取消')
        target = None
        if self.default_route is not None:
            try:
                target = self.default_route(self.device)
            except Exception:
                target = None
        self.capture_target = target
        if not self._close_meter():  # the session capture takes over only after release
            raise RuntimeError('试音麦克风尚未完全释放，请稍后重试')
        self.capture = self._open_capture(target, SETTLE)
        self.capture.start()
        self._state("idle", "连接成功；把光标放进编辑框后使用笔上录音键")

    def _open_capture(self, target, settle, callback=None, on_error=None):
        callback, on_error = callback or self._audio, on_error or self._audio_error
        if self.capture_factory is AudioCapture:
            return AudioCapture(callback, on_error, self.device, target=target, settle=settle)
        if self.factory_takes_target:
            return self.capture_factory(callback, on_error, target=target)
        return self.capture_factory(callback, on_error)

    def _owns(self, receiver):
        return self.capture_target is not None and self.capture_target.get('id') == receiver.get('id')

    def _from_session_pen(self, receiver):
        """A stop from another pen must never end this pen's recording."""
        current = self.session_receiver
        return receiver is None or current is None or receiver.get('id') == current.get('id')

    def _route(self, target):
        """Move the open microphone to the receiver whose key was pressed. Only
        one microphone is open at a time; the pre-roll belonged to the old one."""
        if self.capture is not None:
            self.capture.close()
            self.capture = None
        with self.audio_lock:
            self.preroll.clear()
        self.capture_target = target
        self.capture = self._open_capture(target, ROUTE_SETTLE)
        self.capture.start()

    def _start(self, destination=None, source='key', receiver=None, latch=False):
        if self.abort.is_set():
            return
        if self.phase in ("starting", "recording"):
            # Firmware pulses/repeated reports cannot create a second session;
            # another pen pressing now is told to wait (one session at a time).
            if not self._from_session_pen(receiver):
                self.notify("notice", BUSY_NOTICE)
                if latch:
                    self.latch_unused(receiver)
            return
        if self._metering():
            # Setup wizard step 1 tests the keys: the press is shown there ('key' event), and
            # nothing records - no session, no audio to any engine.
            if latch:
                self.latch_unused(receiver, 'tested')
            return
        if self.phase != "idle":
            self.notify("notice", BUSY_NOTICE)
            if latch:
                self.latch_unused(receiver)
            return
        if latch:
            self.latch_hint(receiver)  # this pen's latch is in use now: forget an old refusal
        if receiver is not None and not self._owns(receiver):
            if self.capture_target is None and not receiver.get('parent'):
                # Opened by name before the receivers were known: the only one.
                self.capture_target = receiver
            else:
                self._route(receiver)
        external = self._external()
        # A setup-wizard test is never typed or saved as a note: its text goes to the wizard.
        self.session_test = self._testing()
        self.session_receiver = self.capture_target
        self.session_destination = 'input' if self.session_test else (destination or self.destination)
        if external and self.session_destination != 'input':
            # The other app types at the cursor itself: there is no text to keep as a note.
            # Said at once, before any key is sent or a session is shown.
            self._refuse(EXTERNAL_NOTES_NOTICE)
            if latch:
                self.latch_unused(receiver)
            return
        self.session_source = source
        self.session_category = self.note_category
        self.dsp.begin(self.whisper_mode)
        self.whisper_announced = False
        self.session_fast = False if self.session_test else self._fast_wanted()
        self.fast_pcm, self.fast_deadline = [], None
        label = self._label()
        self._state("starting", "正在启动本机极速落字" if self.session_fast
                    else f"正在唤起{label}" if external
                    else f"正在连接{label}，开头音频会暂存于内存" if self._cloud()
                    else "正在等待 OpenLess 确认，开头音频会暂存于内存")
        # An external app types by itself: a test with it types into the wizard's own box.
        self.backend.insert_text = self.session_destination == 'input' and (external or not self.session_test)
        self.speech_meter = SpeechMeter()
        with self.audio_lock:
            self.generation += 1
            initial = list(self.preroll)
            self.preroll.clear()
            self.voice_levels.clear()
            self.last_meter = 0
            self.accept_audio = True
        if self.hardware:
            self.hardware.refresh()
        try:
            if not self.session_fast:  # 极速落字 needs no OpenLess session (nothing to wait for)
                self.backend.start()
        except BackendBusy:
            self._busy()
            if latch:
                self.latch_unused(receiver)
            return
        except ExternalAppOnly:
            self._refuse(EXTERNAL_NOTES_NOTICE)  # a note with the hotkey bridge: not a failure
            if latch:
                self.latch_unused(receiver)
            return
        if self.closing.is_set() or self.abort.is_set() or self.overflow.is_set():
            raise RuntimeError("录音启动已取消")
        self.session_id = uuid.uuid4().hex
        self.remote_text, self.remote_live = '', False
        self._preview('start', self.session_id)
        self._diagnose('begin', {'session': self.session_id, 'engine': self._label(), 'device': self.device,
                                 'destination': self.session_destination, 'source': self.session_source,
                                 'whisper_mode': self.dsp.mode, 'fast': self.session_fast, 'test': self.session_test})
        for pcm in initial:
            if self.abort.is_set():
                raise RuntimeError('录音启动已取消')
            self._feed(pcm)
        self.recording_at = time.monotonic()
        self._state("recording", "正在录音（极速落字：松开即输入）" if self.session_fast
                    else "正在录音；松开长按或再次短按结束")

    def _busy(self):
        """OpenLess said busy: another client (Tidepool, its own hotkey) is in a session.
        Drop what this press captured and stay connected and idle; it is not an error."""
        self._refuse(OTHER_DEVICE_NOTICE, 'busy')

    def _refuse(self, message, kind='refused'):
        """The engine declined this press (busy, or a note with an external app): drop what
        it captured and stay connected and idle. Not an error: no reconnect cycle."""
        with self.audio_lock:
            self.accept_audio = False
        self.speech_meter = None  # no session took place: nothing to record
        self._clear_audio()
        self.session_receiver = None
        self.notify(kind, {'message': message, 'destination': self.session_destination})
        self._state('idle', message)

    def _feed(self, pcm):
        # The 气声 front end may hold the first syllables until it knows how loud
        # they are, then release them lifted; normal speech passes untouched.
        self._diagnose('raw', pcm)
        for chunk in self.dsp.process(pcm):
            self._send(chunk)
        if self.dsp.engaged and not self.whisper_announced and self.phase == 'recording':
            self._state('recording', self.message)  # boosting began mid-session

    def _send(self, pcm):
        # OpenLess and the local preview both get the processed audio.
        self._diagnose('sent', pcm)
        if self.session_fast:
            self.fast_pcm.append(pcm)  # memory only, for the OpenLess fallback
        else:
            self.backend.feed(pcm)
        if not self.remote_live:
            self._preview('feed', pcm)
        if self.speech_meter:
            self.notify('audio_usage', self.speech_meter.feed(pcm))

    def _drain(self):
        """End of a recording: whatever the front end still holds goes out first."""
        for chunk in self.dsp.flush():
            self._send(chunk)

    def _silent(self):
        if self.speech_meter is None:
            return False
        if self._external():
            # The app records from its own microphone choice; what DotVoice hears says
            # nothing about it, and cancelling would make it discard the dictation.
            return False
        heard = self.speech_meter.snapshot()
        if heard['audio_seconds'] < SILENT_MIN_AUDIO or heard['speech_seconds'] >= SILENT_SPEECH:
            return False
        # The VAD heard (almost) nothing; a whisper may still be there.
        return not (self.dsp.active and self.dsp.speech_seconds >= WHISPER_SPEECH)

    def _discard_silent(self):
        with self.audio_lock:
            self.accept_audio = False
        self.stop_at = None
        if not self.session_fast:
            self.backend.cancel()
        self._preview('cancel')
        self._finish_metrics('empty')
        self._clear_audio()
        label = self._label()
        # OpenLess's polisher never sees it; a cloud engine already heard the (silent) audio.
        message = ('没有听到说话，本段已取消，没有发给 OpenLess' if label == 'OpenLess'
                   else '没有听到说话，本段已取消，不会输入文字')
        value = {'destination': self.session_destination, 'message': message}
        if self.session_test:
            value['test'] = True
        self.notify('empty', value)
        self._state('idle', message)

    def _session_failed(self, message):
        """This recording could not be recognised; the microphone and the engine stay ready
        for the next press (no reconnect cycle). Nothing was typed."""
        with self.audio_lock:
            self.accept_audio = False
        self.stop_at = None
        try:
            self.backend.cancel()
        except Exception:
            pass
        self._preview('cancel')
        self._finish_metrics('failed')
        self._clear_audio()
        self.session_receiver = None
        value = {'message': message, 'destination': self.session_destination}
        if self.session_test:
            value['test'] = True
        self.notify('failed', value)
        self._state('idle', message)

    def _finish_metrics(self, outcome, text=None):
        if self.speech_meter is not None:
            heard = self.speech_meter.snapshot()
            self.notify('session_metrics', {**heard, 'outcome': outcome, 'ended': time.time()})
            self.speech_meter = None
            # The debug recording (if on) gets the front end's numbers before they reset.
            self._diagnose('finish', outcome, {'dsp': self.dsp.stats(), 'vad': heard, 'text': text})

    def _run(self):
        try:
            while not self.closing.is_set():
                try:
                    if self.overflow.is_set():
                        raise RuntimeError("音频缓冲已满，本段已取消；请重新录音")
                    try:
                        action, data = self.commands.get(timeout=0.02 if self.phase in ("recording", "processing") else 0.2)
                    except queue.Empty:
                        action, data = None, None
                    if action == 'destination':
                        destination, category, completion = data
                        if completion.cancelled():
                            continue
                        if self.phase not in ('idle', 'disconnected', 'error'):
                            completion.set_exception(RuntimeError('请等本段结束，再切换文字去向'))
                        else:
                            self.destination = destination
                            self.note_category = category if category in ('idea', 'paper') else 'idea'
                            completion.set_result(destination)
                    elif action == "arm":
                        if self.want_armed and self.capture is None:
                            self._arm()
                    elif action == "disarm":
                        try:
                            self._disconnect()
                            self._state("disconnected", "已暂停，麦克风已释放")
                        except Exception as exc:
                            if data is not None:
                                data.set_exception(exc)
                            raise
                        else:
                            if data is not None:
                                data.set_result(None)
                    elif action in ("start", "latch_start"):
                        self._start(receiver=data, latch=action == "latch_start")
                    elif action in ("start_note", "latch_note"):
                        self._start('notes', 'quick', receiver=data, latch=action == "latch_note")
                    elif action == "route":
                        # A receiver came or went while idle: keep the open microphone on a present one.
                        if (self.phase == "idle" and self.capture is not None and data is not None
                                and not self._owns(data)):
                            self._route(data)
                    elif action == "toggle":
                        if self.phase == "recording":
                            if self.stop_at is None:
                                self.stop_at = time.monotonic() + 0.15
                        else:
                            self._start()
                    elif action == "stop" and self.phase == "recording" and self._from_session_pen(data):
                        if self.stop_at is None:
                            self.stop_at = time.monotonic() + 0.15
                    elif (action == "stop_note" and self.phase == "recording" and self.session_source == 'quick'
                          and self._from_session_pen(data)):
                        if self.stop_at is None:
                            self.stop_at = time.monotonic() + 0.15
                    elif action == "latch_off":
                        hint = self.latch_hint(data)
                        if self.phase == "idle" and hint:
                            self.notify("mic_off", TESTED_MIC_OFF_NOTICE if hint == 'tested' else MIC_OFF_NOTICE)
                    elif action == "cancel":
                        self._disconnect()
                        self.last_retry = 0
                        self._state("disconnected", "本段已取消")
                    elif action == "pcm":
                        generation, pcm = data
                        if generation == self.generation and self.phase == "recording":
                            self._feed(pcm)
                    elif action == "audio_error" and self.capture:
                        raise RuntimeError(data)
                    elif action == "local_final":
                        session, text = data
                        if self.session_fast and self.phase == "processing" and session == self.session_id:
                            self._fast_done(text)
                    elif action == "local_failed":
                        if self.session_fast and self.phase in ("recording", "processing"):
                            self._fast_fallback()
                    elif action == "meter_error":
                        self._close_meter()
                        self.notify('meter', {'ok': False, 'message': data})
                    # The wizard's meter must never fail the voice session.
                    try:
                        self._sync_meter(force=action == 'meter')
                    except Exception:
                        pass
                    now = time.monotonic()
                    if self.stop_at and now >= self.stop_at:
                        self._drain()
                        if self._silent():
                            self._discard_silent()
                        else:
                            with self.audio_lock:
                                self.accept_audio = False
                            self.stop_at = None
                            self.processing_at = now
                            if self.session_fast:
                                self._preview('finish')
                                self.fast_deadline = now + FAST_WAIT
                                self._state("processing", "正在落字")
                            else:
                                self.backend.stop()
                                if not self.remote_live:
                                    self._preview('finish')
                                self._state("processing", self._processing_message())
                    limit = NOTE_LIMIT if self.session_destination == 'notes' else INPUT_LIMIT
                    if self.phase == "recording" and now - self.recording_at > limit:
                        notes = self.session_destination == 'notes'
                        if not notes and not self._cloud():
                            raise RuntimeError(f"录音超过 {limit // 60} 分钟，已取消以避免持续占用")
                        if self.stop_at is None:
                            # A note types nothing, so ending it here loses nothing. A stop
                            # report lost at range used to throw the whole note away. A cloud
                            # engine has ten minutes of text: typing it beats losing it.
                            self.stop_at = now
                            self.notify("notice", f"笔记录音已满 {limit // 60} 分钟，已自动结束并保存" if notes
                                        else CLOUD_LIMIT_NOTICE.format(minutes=int(limit // 60)))
                    if self.session_fast and self.fast_deadline and now > self.fast_deadline:
                        self._fast_fallback()  # the local model is too slow or stuck
                    if self.processing_at and now - self.processing_at > 120:
                        raise RuntimeError("识别超过两分钟未完成，已请求取消；不会重放音频")
                    if self.want_armed and not self.capture and now - self.last_retry >= 5:
                        self._arm()
                    if self.capture:
                        event = self.backend.poll()
                        if event:
                            self._backend_event(event)
                except Exception as exc:
                    try:
                        self._fail(str(exc))
                    except Exception:
                        # Even recovery failed (a broken listener): keep the loop - and the
                        # pen - alive and let the 5 s re-arm try again.
                        self.last_retry = time.monotonic()
                        self.closing.wait(.2)
        finally:
            meter_released = self._close_meter()
            try:
                self._disconnect()
                self.shutdown_released = meter_released
            except Exception:
                pass

    def _processing_message(self):
        label = self._label()
        if self._external():
            return handed_to(label) + '，等它输入文字'
        if self.session_test:
            return f"{label} 正在识别（试说，不会输入）"
        return f"{label} 正在识别" + ('，完成后保存为笔记' if self.session_destination == 'notes' else '并输入文字')

    # -- 极速落字 --------------------------------------------------------------
    def _fast_wanted(self):
        if self.session_destination != 'input' or self.preview is None or not callable(self.fast):
            return False
        if self._external() or self._cloud():
            return False  # 极速落字 stands in for OpenLess's raw mode only
        if not getattr(self.preview, 'ready', False):
            return False  # models not loaded (yet): OpenLess as usual
        try:
            return bool(self.fast())
        except Exception:
            return False

    def local_final(self, session, text):
        """Preview thread: the local preview finished a session's text."""
        try:
            self.commands.put_nowait(("local_final", (session, text)))
        except queue.Full:
            self.overflow.set()

    def local_failed(self):
        try:
            self.commands.put_nowait(("local_failed", None))
        except queue.Full:
            self.overflow.set()

    def _fast_done(self, text):
        text = (self.corrector(text or '') or '').strip()
        if not text:
            self._fast_fallback()  # heard speech but read nothing: let OpenLess try
            return
        not_typed = None
        try:
            self.typer(text)
        except ElevatedTarget:
            # An administrator window dropped the keystrokes; OpenLess could not type there
            # either. The text is the result: kept, never retyped elsewhere.
            not_typed = 'elevated'
        except PartlyTyped:
            # The start is already in the field: retyping it all would double it.
            self.notify("notice", "部分文字没能输入（系统拦截了按键），请检查编辑框")
        except OSError:
            self._fast_fallback()  # Windows refused the keystrokes for another reason
            return
        self.fast_deadline = None
        event = {"type": "result", "text": text, "local": True}
        if not_typed:
            event["not_typed"] = not_typed
        self._backend_event(event)

    def _fast_fallback(self):
        """Local text unavailable: give OpenLess the session after all."""
        audio, recording = self.fast_pcm, self.phase == "recording"
        self.session_fast, self.fast_pcm, self.fast_deadline = False, [], None
        self._preview('cancel')
        try:
            self.backend.start()
        except BackendBusy:
            self._busy()
            return
        for pcm in audio:
            self.backend.feed(pcm)
        if not recording:
            self.backend.stop()
            self._state("processing", "本机落字未完成，已转交 OpenLess 识别并输入文字")

    def _backend_event(self, event):
        if event.get("kind") == "error":
            message = event.get("message") or f"{self._label()} 识别失败"
            if message == "Provider":
                message = "OpenLess 识别服务未能完成本段，请查看其识别设置或历史记录"
            if self._cloud() and self.phase in ("starting", "recording", "processing"):
                # A built-in engine connects per recording: its error (network, server busy,
                # a key it refused) belongs to this recording only. The pen stays armed.
                self._session_failed(message)
                return
            raise RuntimeError(message)
        if event.get("type") == "result":
            elapsed = time.monotonic() - self.processing_at if self.processing_at else None
            metrics = self.speech_meter.snapshot() if self.speech_meter else {}
            # Whether this text came from lifted soft speech, and how much of it the
            # whisper-aware detector heard (the VAD's speech_seconds misses whispers).
            whisper = {'whisper': self.dsp.engaged, 'voice_seconds': round(self.dsp.speech_seconds, 3)}
            with self.audio_lock:
                envelope = list(self.voice_levels)
            self._finish_metrics('completed', event.get('text', ''))
            self._clear_audio()
            # Volume history is memory-only, not PCM or word timing. It survives
            # a hidden UI and trailing pauses; usage records never store it.
            local = bool(event.get("local"))
            value = {"text": event.get("text", ""), "seconds": elapsed,
                     "envelope": envelope, "destination": self.session_destination,
                     "category": self.session_category, "session": self.session_id,
                     "local": local, **metrics, **whisper}
            app = None
            if event.get("external"):
                # The hotkey bridge: the other app typed the words; DotVoice never saw them.
                app = event.get("app") or self._label()
                value.update(external=True, app=app)
            if self.session_test:
                value["test"] = True
            not_typed = event.get("not_typed") if self.session_destination == 'input' else None
            if not_typed:
                value["not_typed"] = not_typed  # the core keeps the text (clipboard) and says so
            self.notify("result", value)
            self._state("idle", "笔记已保存；按笔继续记录下一段" if self.session_destination == 'notes'
                        else handed_to(app) if app
                        else "试说完成；文字没有输入到光标处" if self.session_test
                        else NOT_TYPED_MESSAGE if not_typed
                        else "已极速落字（本机识别）" if local
                        else "本段已完成；请核对目标编辑框中的文字")
        elif event.get("type") == "partial":
            self._remote_partial(event)
        elif event.get("kind") == "notice":
            # A built-in engine's remark (polish failed, text not typed): never an error.
            message = event.get("message")
            if isinstance(message, str) and message:
                self.notify("notice", message)
        elif event.get("kind") == "polishing" and self._cloud():
            if self.phase == "processing":
                self._state("processing", "正在润色文字")  # the relay's model, not the recogniser
        elif event.get("kind") in ("transcribing", "polishing"):
            self.notify("notice", f"{self._label()} 正在" + ("识别" if event["kind"] == "transcribing" else "处理文字"))

    def _remote_partial(self, event):
        text, offset = event.get('text'), event.get('offset', 0)
        if not isinstance(text, str) or type(offset) is not int or offset < 0:
            return
        # Same rule as OpenLess's TranscriptAccumulator: the delta replaces from offset.
        current = self.remote_text if offset <= len(self.remote_text) else self.remote_text.ljust(offset)
        self.remote_text = current[:offset] + text
        if not self.remote_live:
            self.remote_live = True
            self._preview('cancel')
        self.notify('partial', {'session': self.session_id, 'text': self.remote_text,
                                'final': bool(event.get('final')), 'source': self._partial_source(event)})

    def _partial_source(self, event):
        """'remote' for OpenLess; 'cloud' when a built-in engine says so, either per event
        ({"type": "partial", "source": "cloud", ...}) or once on the engine object
        (`partial_source = 'cloud'`). Anything else counts as OpenLess."""
        source = event.get('source')
        if source not in PARTIAL_SOURCES:
            source = getattr(self.backend, 'partial_source', 'remote')
        return source if source in PARTIAL_SOURCES else 'remote'

    def close(self):
        self.want_armed = False
        self.abort.set()
        self.closing.set()
        self.thread.join(12)
