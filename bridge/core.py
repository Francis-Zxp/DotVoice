"""Headless DotVoice core: devices, voice sessions, notes and telemetry.

The Tauri shell owns every window; this process never opens one. The speech engine
chosen in prefs (engine.kind) is the single insertion path: OpenLess, a built-in cloud
engine (bridge.engines.cloud types the final text itself) or another dictation app
driven by its hotkey. The core never types a result twice.
"""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import queue
import re
import threading
import time

from . import secrets
from .airmouse import AirMouse
from .backend import VoiceBackend
from .battery import baseus_battery
from .controller import BUSY_NOTICE, Controller, backend_label, report_code
from .devices import PROFILES, available_profiles
from .diagnostics import AudioDiagnostics
from .elevation import UipiWatch, kept_message
from .engines import hotkey
from .engines.cloud import (SECRET_DASHSCOPE_API_KEY, SECRET_LLM_API_KEY, SECRET_VOLC_ACCESS_TOKEN,
                            SECRET_VOLC_API_KEY, create_backend)
from .fast_insert import type_text
from .instances import InstanceRegistry
from .keymap import ACTION_SLOTS, WHEEL_ROLES, Keymap
from .notes import NoteStore
from .prefs import Preferences
from .housekeeping import clean_runtime
from .sounds import SOUND_VERSION, KeepAwake, Sounds
from .upstream import UpstreamWatch
from .updates import UpdateWatch
from .tidepool import TidepoolAdapter
from .usage import UsageLedger

VERSION = '0.11.5'
BUSY = ('starting', 'recording', 'processing')
MODES = ('input', 'notes', 'present')
# Present-mode pointer tools, cycled by double-clicking the air-mouse key.
# Double-click ⑤ cycles in this order; "mouse" draws nothing (plain air mouse).
TOOLS = ('laser', 'spotlight', 'magnifier', 'mouse')
ZOOMS = (125, 150, 200, 250, 300, 400)  # magnifier steps for the pen's volume keys, percent
TOOL_PREVIEW = 3.0
DOUBLE_CLICK = .6
KEY_LOG = 600  # recent key edges kept for diagnosis (codes and times only)
# ⑤ quick tap (no hold), key log 2026-10-05: a left click in input / notes modes; two within
# AIR_DOUBLE_WINDOW are a double-click there, and the next pointer tool in present mode.
AIR_TAP = 0x71
AIR_DOUBLE_WINDOW = .55
REREPORT = .15     # pressing ⑤ during a ③ hold makes the pen re-send 6B ~15 ms after 72/73
DRAG_KEY = 0x6B    # ③ long press: 6B while held, 00 on release
LASER_KEY = 0x64  # ② 激光键 (measured: press sends 64, no release code)
POINTER_HOLD_MAX = 90  # s: a ⑤ hold longer than this is a lost release report
HOLD_KEY_MAX = 90      # s: likewise for ⑧ (68 held, 69 lost): annotation pen / Ctrl
QUICK_MIC_OFF_NOTICE = "这一下只关掉了笔上开着的麦克风（当时没在录闪念笔记，可能是开始信号丢了）；再短按一次 ⑦ 开始"
QUICK_TESTED_MIC_OFF_NOTICE = "这一下关掉了配置向导测试按键时打开的笔麦克风；再短按一次 ⑦ 记闪念笔记"


EMPTY_REPLY = re.compile(r'(转写|识别|输入)(的)?(内容|文本|结果)?(为空|是空的)|没有(实质|有效|可整理|任何)(的)?内容|'
                         r'无(实质|有效)(性)?内容|请(提供|输入).{0,8}(文本|内容|转写)')

# -- speech engines (bridge.engines) -----------------------------------------------------
# The only secrets the UI may set; values stay in bridge.secrets (DPAPI) and are never
# returned, logged, put in a snapshot / status.json or written to prefs.json.
SECRET_NAMES = (SECRET_VOLC_API_KEY, SECRET_VOLC_ACCESS_TOKEN, SECRET_DASHSCOPE_API_KEY, SECRET_LLM_API_KEY)
ENGINE_NAMES = {'openless': 'OpenLess', 'volc': '豆包语音', 'dashscope': '阿里云百炼', 'hotkey': '其他语音软件'}
VOLC_MODES = {'bigmodel_async': 'async', 'bigmodel': 'stream', 'bigmodel_nostream': 'nostream'}
VOLC_AUTH = {'key': 'api_key', 'legacy': 'app'}
ENGINE_BUSY = '正在录音或识别，请等本段结束后再更改语音引擎'
PROBE_KINDS = ('volc', 'dashscope', 'polish')
# Where to get keys: opened by the settings page's help links (besides GitHub reminders).
HELP_URLS = (
    'https://www.volcengine.com/docs/6561/1354869',
    'https://console.volcengine.com/',
    'https://help.aliyun.com/zh/model-studio/get-api-key',
    'https://bailian.console.aliyun.com/',
)
# Files an earlier DotVoice leaves in runtime/: their presence means this is not a fresh
# install, so the first-run wizard does not open by itself.
EXISTING_MARKERS = ('usage.json', 'usage-pv60.json', 'instances.json', 'devices.json', 'bindings.json', 'notes')


def cloud_settings(engine, hotwords=None):
    """bridge.engines.cloud.create_backend settings from prefs['engine'] (no keys in here).
    hotwords: Doubao's hotword source (a callable read when a recording starts), left out
    when prefs engine.volc.hotwords is False (a missing pref counts as on)."""
    volc, dashscope = engine['volc'], engine['dashscope']
    settings = {'engine': engine['kind'],
                'volc': {'auth': VOLC_AUTH[volc['auth']], 'app_id': volc['app_id'],
                         'resource_id': volc['resource_id'], 'mode': VOLC_MODES[volc['endpoint']]},
                'dashscope': {'model': dashscope['model'], 'region': dashscope['region']},
                'polish': dict(engine['polish'])}
    if hotwords is not None and volc.get('hotwords', True) is not False:
        settings['volc']['hotwords'] = hotwords
    return settings


def engine_signature(engine):
    """What the active backend is built from: a change means a rebuild (and a swap)."""
    kind = engine['kind']
    if kind == 'openless':
        return ('openless',)
    if kind == 'hotkey':
        return ('hotkey', json.dumps(engine['hotkey'], sort_keys=True))
    return (kind, json.dumps(engine[kind], sort_keys=True), json.dumps(engine['polish'], sort_keys=True))


def hotkey_profile(conf):
    """The hotkey bridge's profile from prefs engine.hotkey (ValueError: a message for the user)."""
    keys = conf.get('keys') or None
    if conf.get('profile') == hotkey.CUSTOM:
        return hotkey.get_profile(hotkey.CUSTOM, keys=keys, style=conf.get('style') or 'toggle')
    return hotkey.get_profile(conf.get('profile'), keys=keys)


class UnavailableBackend(VoiceBackend):
    """Stands in for an engine whose settings cannot be used yet (e.g. a custom shortcut not
    recorded): enabling the voice shows why; nothing falls back to another engine silently."""

    def __init__(self, label, message):
        self.label, self.message = label, message
        self.insert_text = True

    def connect(self):
        raise RuntimeError(self.message)

    def start(self):
        raise RuntimeError(self.message)

    def feed(self, pcm):
        pass

    def stop(self):
        pass

    def poll(self):
        return None

    def cancel(self):
        pass

    def close(self):
        pass


def _copy_to_clipboard(text):
    from .clipboard import copy_text
    copy_text(text)


def sound_player_is_fake(sounds):
    """Tests inject a recording player; they must not open a real audio device."""
    from .sounds import _winsound_player
    return sounds.player is not _winsound_player


def _empty_transcript(result):
    """Nothing was really said: no text, or the polisher's reply to an empty
    transcript ("转写内容为空……请提供……") after very little voiced audio."""
    text = (result.get('text') or '').strip()
    if not text.strip('。，,.!！?？ \n'):
        return True
    speech = result.get('speech_seconds')
    return EMPTY_REPLY.search(text) is not None and (speech is None or speech < 1.5)


class Core:
    def __init__(self, base, backend, emit, smoke=False, monitor_factory=None,
                 tidepool=None, sound_player=None, controller_factory=Controller,
                 program_dir=None, preview_factory=None, device_tree=None, openless_settings=None,
                 engine_factory=None, probe_factory=None, update_factory=UpdateWatch):
        """`backend` is the OpenLess engine (prefs engine.kind 'openless'); the other engines
        are built from prefs: `engine_factory(kind, engine_prefs)` and
        `probe_factory(kind, engine_prefs) -> (ok, message)` stand in for them in tests."""
        self.base, self.smoke = Path(base), smoke
        self._emit = emit
        self.lock = threading.RLock()
        self.mode_lock = threading.Lock()
        self.switch_lock = threading.Lock()
        self.apply_lock = threading.Lock()  # _apply_airmouse runs from RPC, HID and key threads
        self.engine_lock = threading.RLock()  # engine swaps (prefs_set from RPC threads)
        self.probe_lock = threading.Lock()
        self.probing = set()
        self.closed = threading.Event()
        self.actions = ThreadPoolExecutor(max_workers=1, thread_name_prefix='key-action')
        self.runtime = self.base / 'runtime'
        prefix = 'smoke-' if smoke else ''
        # Decided before anything below writes to runtime/: has DotVoice run here before?
        prefs_path = self.runtime / (prefix + 'prefs.json')
        existing = prefs_path.exists() or (not smoke and any((self.runtime / name).exists()
                                                              for name in EXISTING_MARKERS))
        # API keys: DPAPI-encrypted in runtime/secrets.json (bridge.secrets), never in prefs.
        self.secrets = secrets.configure(self.runtime)
        self.openless_backend = backend
        self.engine_factory, self.probe_factory = engine_factory, probe_factory
        self.hotkey_scan = hotkey.scan               # read-only app detection (tests replace these)
        self.process_names = hotkey.running_processes
        self.state = {'phase': 'disconnected', 'message': ''}
        self.result = None
        self.arm_epoch = 0
        self.visible = True
        self.probe = False
        self.last_key = None
        self.last_codes = {}  # HID path -> last vendor code (edges per receiver)
        self.pending_mode = None
        self.live_text = ''
        self.live_source = None  # 'local' | 'remote' | 'cloud': who wrote live_text (the UI decides what to show)
        self.tool = 'laser'
        self.pointer_down = False
        self.laser_on = False
        self.last_air_double = float('-inf')
        self.last_air_edge = float('-inf')
        self.annotate_at = None
        self.key_log = deque(maxlen=KEY_LOG)
        self.key_log_dirty = False
        self.key_log_start = time.monotonic()
        self.battery_alerted = {}  # receiver id -> alert levels already shown
        # Several identical receivers: names, battery, which one records (instances.json).
        self.instances = InstanceRegistry(self.runtime / (prefix + 'instances.json'), tree=device_tree)
        self.notes_store = NoteStore(self.runtime / (prefix + 'notes'))
        self.selected, self.mode = 'baseus', 'hold'
        settings_path = self.runtime / 'devices.json'
        if settings_path.exists() and not smoke:
            try:
                settings = json.loads(settings_path.read_text(encoding='utf-8'))
                if settings.get('device') in PROFILES:
                    self.selected = settings['device']
                if settings.get('mode') in ('hold', 'toggle'):
                    self.mode = settings['mode']
            except (OSError, ValueError):
                pass
        usage_name = 'smoke-usage.json' if smoke else ('usage-pv60.json' if self.selected == 'pv60' else 'usage.json')
        self.ledger = UsageLedger(self.runtime / usage_name)
        self.prefs = Preferences(prefs_path, self._legacy_prefs(), existing=existing)
        if not smoke and not existing:
            # Kept at once: usage.json written on exit must not turn an unfinished wizard into "done".
            self._save_prefs()
        # Learning progress is emitted directly: it runs under the keymap lock,
        # and snapshot() takes the core lock before the keymap lock.
        self.keymap = Keymap(self.runtime / (prefix + 'bindings.json'), on_learn=self._learn_progress)
        self.sounds = Sounds(self.runtime / 'sounds', lambda: self.prefs.get('sounds'), sound_player)
        self.keep_awake = KeepAwake()
        # An output device kept awake needs no lead-in silence before a cue.
        self.sounds.awake = lambda: self.keep_awake.running
        self.upstream = None if smoke else UpstreamWatch(self.runtime / 'openless-live-text.json')
        self.app_updates = None if smoke else update_factory(self.runtime / 'app-update.json', VERSION)
        self.tidepool = tidepool or TidepoolAdapter()
        self.tidepool_state = {'installed': False, 'receiver_running': None,
                               'control_supported': False, 'device_online': None,
                               'message': '正在检查本机 Tidepool 接收器…'}
        self.device_options = [{**v, 'present': False} for v in PROFILES.values()]
        self.airmouse = AirMouse(lambda: self.prefs.get('airmouse'),
                                 lambda: self.keymap.airmouse_vks(self.selected))
        self.airmouse.remap_enabled = False  # _apply_airmouse decides once receivers are known
        # Runs inside the keyboard hook: hand off at once, never emit from there.
        self.airmouse.on_lock = lambda locked, how: self._submit(self.emit, 'drag', {'locked': locked, 'how': how})
        self.airmouse.on_wheel = self._magnifier_wheel
        # ④'s long press right-clicks only while 6D is bound to it (its B then never types).
        self.airmouse.right_click_bound = lambda: '6d' in self.keymap.bindings('baseus').get('right_click', {}).get('fire', ())
        # Administrator windows drop synthetic input (UIPI, bridge.elevation): every click,
        # scroll, typed text and hotkey asks first, and the user is told once per window.
        self.uipi = UipiWatch()
        self.airmouse.before_input = self._input_check
        self.clipboard = _copy_to_clipboard  # text an administrator window refused is kept here
        engine = self.prefs.get('engine')
        self.engine_kind = engine['kind']
        self.controller = controller_factory(self.notify, self._backend_or_placeholder(engine),
                                             device=self.selected, mode=self.mode)
        self.controller.on_device_key = self._device_key
        self.controller.route_of = self._route_of
        if callable(getattr(self.controller, 'typer', None)):  # 极速落字 types with it
            self.controller.typer = self.uipi.guard_typer(self.controller.typer)
        self.controller.default_route = self._default_route
        self.controller.whisper_mode = self.prefs.get('voice', 'whisper')
        # Opt-in (prefs diagnostics.save_audio): the last recordings, raw and as sent, for
        # tuning the 气声 front end. Local files only, never uploaded (bridge.diagnostics).
        self.diagnostics = AudioDiagnostics(
            self.runtime / 'diagnostics',
            enabled=lambda: not self.smoke and bool(self.prefs.get('diagnostics', 'save_audio')))
        self.diagnostics.context = {'version': VERSION}
        self.controller.diagnostics = self.diagnostics
        self.app_mode = self.prefs.get('mode')
        self.airmouse.buttons = self.app_mode != 'present'  # ⑤ clicks, ③ grabs, ④ right-clicks
        # Present mode: the pen's own letters (④ long press: B = black screen) stay native.
        self.airmouse.guard.letters = self.app_mode != 'present'
        self.tool = self.prefs.get('present', 'tool')
        self.tool_preview = None
        try:
            self.controller.set_destination('notes' if self.app_mode == 'notes' else 'input')
        except (ValueError, RuntimeError, TimeoutError, queue.Full):
            pass
        # Local live preview (sherpa-onnx). Its words are often wrong, so the UI shows them
        # only when asked (preview.show_local_text); its models run only while something
        # uses them - those words, or 极速落字 (voice.fast_raw) - see _sync_preview.
        self.preview = None
        self.preview_lock = threading.Lock()
        self.preview_factory = preview_factory
        self.preview_model = self.preview_refiner = None
        if preview_factory is None and not smoke:
            from .preview import find_model, find_refiner, openless_models_dir
            roots = [p for p in (program_dir, self.base) if p]
            self.preview_model = find_model(*roots)
            if self.preview_model:
                self.preview_refiner = find_refiner(*roots, openless_models_dir())
        self.preview_available = preview_factory is not None or self.preview_model is not None
        self._sync_preview()
        # 极速落字: OpenLess in raw mode + the local preview's text, typed at once.
        from .fast_insert import OpenLessSettings
        self.openless = openless_settings or OpenLessSettings()
        self.controller.fast = self._fast_raw
        self.controller.corrector = self.openless.correct
        factory = monitor_factory
        if factory is None:
            from .rawinput import RawMonitor
            factory = RawMonitor
        self.monitor = factory(self.on_hid, lambda error: self.notify('notice', error),
                               self._devices_changed)
        self.guard = self.airmouse.guard  # pen shortcut guard (prefs pen.guard_shortcuts)

    def _legacy_prefs(self):
        """Carry 0.6 settings forward the first time prefs.json is created.
        The old floating-note switch is not migrated: the capsule is a new
        feature (mode feedback), so it starts enabled."""
        patches = []
        if isinstance(self.ledger.data.get('reduced_motion'), bool):
            patches.append({'appearance': {'reduced_motion': self.ledger.data['reduced_motion']}})
        return patches

    # -- lifecycle ------------------------------------------------------------
    def start(self):
        try:
            self.notes_store.purge_trash()
        except OSError:
            pass
        self.diagnostics.purge()  # debug recordings older than 24 h go, switched on or not
        self._apply_keep_awake()  # first: whether cues need their lead-in silence
        if not self.smoke:
            # Old cue caches and self-test dumps this app wrote earlier (allow-list only).
            clean_runtime(self.runtime, SOUND_VERSION, {self.prefs.get('sounds', 'volume')})
        self.sounds.prepare()
        if not self.smoke:
            self.monitor.start()
            threading.Thread(target=self._poll_tidepool, name='tidepool-poll', daemon=True).start()
        threading.Thread(target=self._ticker, name='core-ticker', daemon=True).start()
        self._apply_airmouse()

    def close(self):
        if self.closed.is_set():
            return
        self.closed.set()
        try:
            self.monitor.close()
        except Exception:
            pass
        self.airmouse.stop()
        self.keep_awake.stop()
        self.keymap.learn_cancel()
        self.controller.close()
        # A presenter borrows Tidepool's input slot; quitting returns it only
        # after our microphone/backend are released, preserving manual pauses.
        if not self.smoke and self.controller.shutdown_released:
            with self.switch_lock:
                try:
                    self.tidepool.restore_presenter_pause()
                except (OSError, ValueError, RuntimeError) as error:
                    self.log_ui_error('tidepool-restore', str(error))
        with self.preview_lock:
            preview, self.preview = self.preview, None
        close = getattr(preview, 'close', None)
        if close:
            close()
        self.actions.shutdown(wait=False, cancel_futures=True)
        with self.lock:
            try:
                self.ledger.tick('disconnected')
                if not self.smoke:
                    self.ledger.save()
            except OSError:
                pass

    def emit(self, kind, value):
        try:
            self._emit(kind, value)
        except Exception:
            pass  # A closed shell pipe must never stop audio or HID cleanup.

    # -- events from the controller ------------------------------------------
    STATEFUL = ('state', 'result', 'audio_usage', 'session_metrics', 'key')

    def notify(self, kind, value):
        if kind in ('busy', 'refused', 'failed'):
            # busy: OpenLess serves one session at a time and another device (Tidepool, or
            # another client) is talking. refused: the hotkey bridge cannot keep a note.
            # failed: a built-in engine could not finish this one recording (network, server).
            # Not an error state; the connection stays up.
            self.emit('notice', value.get('message') or BUSY_NOTICE)
            if kind == 'failed':
                # The capsule says so (it would otherwise just fold away), and the wizard shows
                # a failed test where its result would be.
                self.emit('session_failed', {'message': value.get('message') or ''})
                if value.get('test'):
                    self.emit('wizard_result', {'text': '', 'test': True, 'error': value.get('message') or ''})
            self.sounds.play('error')
            return
        if kind == 'mic_off':
            # A press that only switched off a pen microphone whose "on" report was lost:
            # the stop cue says this press arrived, the notice says to press once more.
            self.emit('notice', value)
            self.sounds.play('stop')
            return
        if kind not in self.STATEFUL:
            if kind == 'partial':
                # Backend live text: OpenLess ('remote') or a built-in cloud engine ('cloud').
                self.live_text, self.live_source = value.get('text', ''), value.get('source')
                if value.get('source') == 'remote' and self.upstream:
                    self.upstream.supported()  # OpenLess forwards live text: no reminder needed
            self.emit(kind, value)  # level, notice, partial, tidepool…: no shared state
            return
        if kind == 'result' and value.get('not_typed') and not value.get('test'):
            value = self._keep_text(value)  # clipboard first: outside the lock
        sound = None
        saved = None
        notice = None
        event = kind
        with self.lock:
            if kind == 'state':
                previous = self.state['phase']
                value = {**value, 'armed': self.controller.want_armed}
                self.state = value
                phase = value['phase']
                if phase == 'starting':
                    self.result = None
                    self.live_text, self.live_source = '', None
                try:
                    self.ledger.tick(phase)
                except OSError:
                    self.ledger.phase = phase
                    self.ledger.warning = '续航记录写入失败；语音输入仍可使用。'
                sound = self._phase_sound(previous, phase)
                if phase not in BUSY and self.pending_mode:
                    self._submit(self._apply_pending)
            elif kind == 'result' and value.get('test'):
                # A setup-wizard test: shown in the wizard only. Nothing was typed (an external
                # app typed into the wizard's own box) and nothing is saved as a note.
                event = 'wizard_result'
                self.ledger.completed()
                heard = (value.get('text') or '').strip() or value.get('external')
                # Never the success chime: it says "typed at the cursor", and nothing was. The
                # wizard shows the text; the capsule says it was a test (overlay).
                sound = None if heard else 'disarmed'
            elif kind == 'result':
                if value.get('destination') == 'notes' and _empty_transcript(value):
                    value = {**value, 'note_skipped': True}
                    sound = 'disarmed'
                elif value.get('destination') == 'notes':
                    # The whole recording becomes one note; you decide when it ends.
                    try:
                        note = self.notes_store.add(value.get('text', ''), [], value.get('audio_seconds'))
                        value = {**value, 'note_id': note['id'] if note else None}
                        saved = note
                    except OSError:
                        value = {**value, 'note_error': '笔记未能保存，请从本次结果复制文字。'}
                elif not value.get('external') and not (value.get('text') or '').strip():
                    # Nothing recognised, so nothing was typed: no success chime. (The hotkey
                    # bridge never sees the words: its empty text means "handed over".)
                    value = {**value, 'empty_text': True}
                    sound = 'disarmed'
                    notice = f'{backend_label(self.controller.backend)} 没有识别出文字，没有输入任何内容'
                self.result = value
                self.ledger.completed()
                if not value.get('note_skipped') and not value.get('empty_text'):
                    sound = 'done_notes' if value.get('destination') == 'notes' else 'done_input'
                if value.get('not_typed') or value.get('note_error'):
                    sound = 'error'  # nothing reached the window / the note was not saved: no success chime
            elif kind == 'audio_usage':
                self.ledger.audio(value)
                return
            elif kind == 'session_metrics':
                try:
                    self.ledger.session(value)
                except OSError:
                    self.ledger.warning = '本次时长未能保存；语音输入仍可使用。'
                return
            elif kind == 'key':
                self.last_key = {'action': value, 'at': time.time()}
        self.emit(event, value)
        if sound:
            self.sounds.play(sound)  # feedback first; bookkeeping below may touch the disk
        if notice:
            self.emit('notice', notice)
        if kind == 'state':
            self._sync_active(value['phase'])
        if saved:
            self.emit('note_saved', {'id': saved['id']})

    # -- administrator windows (UIPI) ---------------------------------------------
    def _input_check(self, kind):
        """Right before a click, scroll or hotkey (HID, timer or keyboard-hook thread): an
        administrator window in front drops it. It is still sent; the first time per window
        the user is told why nothing happened (handed off: never emit from the hook)."""
        info = self.uipi.blocked(kind)
        if info is not None and info.get('first'):
            self._submit(self.emit, 'elevated', self._elevated_view(info))
        return info

    def _guard_sender(self, sender):
        """The hotkey bridge's key sender, asking _input_check first."""
        def send(events):
            self._input_check('hotkey')
            return sender(events)
        send.inner = sender
        return send

    @staticmethod
    def _elevated_view(info):
        return {key: info.get(key) for key in ('kind', 'process', 'first', 'message')}

    def _keep_text(self, value):
        """An administrator window dropped the typed text: put it on the clipboard (Ctrl+V
        works there, the keyboard is real) and say so with the result; it is never lost."""
        text = (value.get('text') or '').strip()
        copied = False
        if text and not self.smoke:
            try:
                self.clipboard(text)
                copied = True
            except (ValueError, OSError):
                pass
        can_elevate = bool(self.uipi.own().get('can_elevate'))
        return {**value, 'copied': copied, 'not_typed_message': kept_message(copied, can_elevate)}

    # -- receivers (several identical pens) ---------------------------------------
    def _emit_instances(self):
        self.emit('instances', self.instances.snapshot())

    def _sync_active(self, phase):
        """instances[].active follows the pen that is recording; starting stamps last_used."""
        rid = None
        if phase in ('starting', 'recording'):
            receiver = getattr(self.controller, 'session_receiver', None)
            rid = receiver.get('id') if receiver else None
            if rid is None and getattr(self.controller, 'device', None) == 'pv60':
                rid = self.instances.first_present('pv60')
        if self.instances.set_active(rid, used=phase == 'starting' and rid is not None):
            self._emit_instances()

    def _route_of(self, event):
        """Controller hook: the receiver an HID event came from, as a capture target."""
        return self.instances.target_for(event.get('path', ''))

    def _default_route(self, device):
        """Controller hook: which receiver's microphone to open when arming."""
        return self.instances.default_target(device) if device == 'baseus' else None

    def _reroute(self):
        """The receiver whose microphone is open went away: move to a present one."""
        control = self.controller
        if getattr(control, 'device', None) != 'baseus' or getattr(control, 'capture', None) is None:
            return
        current = getattr(control, 'capture_target', None)
        if current is not None and self.instances.is_present(current['id']):
            return
        best = self._default_route('baseus')
        if best is not None and (current or {}).get('id') != best['id']:
            control.command('route', receiver=best)

    def rename_instance(self, id=None, label=''):
        """RPC {id, label}: name a pen ("我的倍思"); an empty label restores "倍思 #n"."""
        try:
            self.instances.rename(id, label)
        except KeyError:
            return {'error': '没有找到这支笔'}
        except ValueError as error:
            return {'error': str(error)}
        listing = self.instances.snapshot()
        self.emit('instances', listing)
        return {'instances': listing}

    def _phase_sound(self, previous, phase):
        notes = self.controller.session_destination == 'notes'
        if previous == 'starting' and phase == 'recording':
            return 'start_notes' if notes else 'start_input'  # OpenLess confirmed the session
        if previous == 'recording' and phase == 'processing':
            return 'stop'
        if phase == 'error' and previous != 'error':
            return 'error'
        if previous in ('disconnected', 'error') and phase == 'idle':
            return 'armed'
        if phase == 'disconnected' and previous != 'disconnected' and not self.controller.want_armed:
            return 'disarmed'
        return None

    # -- HID ------------------------------------------------------------------
    def on_hid(self, event):
        kind = event.get('kind')
        if kind == 0:
            mouse = event.get('mouse') or {}
            if mouse.get('dx') or mouse.get('dy'):
                self.airmouse.activity()
            if self.probe and mouse.get('buttons'):
                self.emit('probe', {'interface': 'mouse', 'buttons': mouse['buttons']})
            return
        if kind == 1:
            key = event.get('keyboard')
            if key:
                # First: the keyboard hook may be waiting for exactly this report.
                self.guard.raw_key(key['vk'], key['down'], pen=True)
                self._log_key('keyboard', 'VK%02X' % key['vk'], key['down'])
                self.keymap.keyboard('baseus', key['vk'], key['down'])
                if self.probe:
                    self.emit('probe', {'interface': 'keyboard', 'vk': key['vk'], 'down': key['down']})
            return
        path = event.get('path', '')
        receiver, appeared = None, False
        if kind == 2 and 'MI_03' in path.upper():
            receiver, appeared = self.instances.observe(path)
        # Before the key: a pen seen for the first time is listed before its first action.
        self._battery_report(event, receiver, appeared)
        self._vendor_key(event, kind, path)

    def _battery_report(self, event, receiver, appeared):
        percent = baseus_battery(event)
        if percent is not None:
            if self.instances.battery(receiver, percent):
                appeared = True
            # The usage ledger follows one pen: the lowest-numbered receiver present
            # (with a single receiver that is always it, exactly as before).
            primary = self.instances.primary('baseus')
            feeds_ledger = receiver == primary or (receiver is None and self.instances.present_count('baseus') <= 1)
            if self.selected == 'baseus' and feeds_ledger:
                with self.lock:
                    try:
                        self.ledger.battery(percent)
                    except OSError:
                        self.ledger.warning = '电量已读取，但本机记录写入失败。'
            self._battery_alert(percent, receiver)
        if appeared:
            self._emit_instances()

    def _vendor_key(self, event, kind, path):
        code = report_code(event)
        if code is None:
            if self.probe and kind == 2:
                interface = 'vendor-raw' if 'MI_03' in path.upper() else 'consumer'
                self.emit('probe', {'interface': interface, 'hex': event.get('hex', '')[:32]})
            return
        if code:
            self.guard.pen_activity(code)  # first: the hook may be waiting for this code (④'s B)
        last = self.last_codes.get(path)
        if code == 0:
            if last is not None:
                self._log_key('vendor', '00')
            if last == DRAG_KEY:
                self.airmouse.end_drag()
            self.airmouse.wheel_release()  # a held volume key (78 / 79) was let go
            self.last_codes.pop(path, None)
            # A hold key being recorded may have no release code: its neutral report is it.
            if not self.keymap.neutral('baseus') and last is not None:
                hold = self.keymap.neutral_release('baseus', last)
                if hold:
                    self._hold_key(*hold)
        elif code != last:
            if len(self.last_codes) > 64:
                self.last_codes.clear()
            self.last_codes[path] = code
            self._log_key('vendor', format(code, '02X'))
            if self.probe:
                self.emit('probe', {'interface': 'vendor', 'code': format(code, '02X')})
            # Any new code ends a held wheel key (⑤'s 73, another key); a wheel code below
            # starts its own press again.
            self.airmouse.wheel_release()
            # A guided recording takes the code instead (⑤'s own codes keep working while a
            # wheel key is recorded: "按住 ⑤ + 音量 +" may be pressed just so).
            learning = self.keymap.learns('baseus', code)
            if code in (0x72, 0x73):
                self.last_air_edge = time.monotonic()
            if code == AIR_TAP and not learning:
                # A tap means ⑤ is not held: a lost 73 must not keep the volume keys scrolling,
                # nor (present mode) the pointer tool up.
                self.airmouse.tapped()
                if self.pointer_down:
                    self._pointer(False)
                now = time.monotonic()
                second = now - self.last_air_double <= AIR_DOUBLE_WINDOW
                self.last_air_double = float('-inf') if second else now
                if self.app_mode == 'present':
                    if second:  # single tap: nothing; double tap: the next pointer tool
                        self._submit(self._cycle_tool)
                else:
                    # Input / notes: a left click at once (no wait for a second tap); the
                    # second tap of a double-tap clicks again, so Windows sees a double-click.
                    self.airmouse.tap(second)
                return
            if code == DRAG_KEY and not learning and self.selected == 'baseus':
                # ③ long press: a grab in input / notes modes; present mode keeps the pen's
                # own Shift+F5 (start the slideshow). No recent ⑤ use is needed any more.
                if self.app_mode != 'present' and time.monotonic() - self.last_air_edge > REREPORT:
                    self.airmouse.begin_drag()
                return
            hold = self.keymap.hold_slot('baseus', code)
            if hold and not learning:
                self._hold_key(*hold)
                return
            outcome = self.keymap.vendor('baseus', code)
            if outcome in WHEEL_ROLES:
                # ⑤ held + volume + / − (78 / 79): scroll, or zoom the magnifier, and keep going
                # while the key is held. Without ⑤ nothing happens (the key's own function).
                self.airmouse.wheel_press(WHEEL_ROLES[outcome])
                return
            if outcome == 'right_click':
                # ④ long press (6D): a right click in input / notes modes. Present mode keeps
                # the pen's own key, B = black screen (the shortcut guard lets it through).
                if self.app_mode != 'present':
                    self.airmouse.right_click()
                return
            if outcome in ACTION_SLOTS:
                self._submit(self._run_slot, outcome, code, self._route_of(event))
                return
            if outcome in ('ignored', 'learning'):
                return
            if code == LASER_KEY and self.app_mode == 'present':
                self._toggle_laser()
                return
        self.controller.on_hid(event)

    def _device_key(self, device, code, down):
        if self.probe:
            self.emit('probe', {'interface': device, 'code': format(code, '02X'), 'down': down})
        outcome = self.keymap.pv60(code, down) if device == 'pv60' else None
        if outcome in ACTION_SLOTS:
            self._submit(self._run_slot, outcome, None)

    def _submit(self, function, *args):
        try:
            future = self.actions.submit(function, *args)
        except RuntimeError:
            return  # executor already shut down
        future.add_done_callback(self._action_done)

    def _action_done(self, future):
        """A key action that raised would otherwise vanish inside the executor."""
        if future.cancelled():
            return
        error = future.exception()
        if error is not None:
            self.log_ui_error('key-action', repr(error))

    def _run_slot(self, slot, code=None, receiver=None):
        if slot == 'toggle_mode':
            self.toggle_mode('key')
        elif slot == 'quick_note':
            self._quick_note(code, receiver)
        elif slot == 'arm_toggle':
            if self.controller.want_armed:
                self.action('disarm')
            else:
                # Exclusive mode may wait up to 15 s for Tidepool to pause; never hold up
                # the key thread (mode switches, notes) for that.
                threading.Thread(target=self._key_arm, name='key-arm', daemon=True).start()
        elif slot == 'cancel':
            self.action('cancel')

    def _key_arm(self):
        answer = self.action('arm')
        if answer.get('error') and not self.closed.is_set():
            self.emit('notice', answer['error'])

    def _quick_note(self, code=None, receiver=None):
        """⑦ short press: 7a turns the pen microphone on (start a note in any mode),
        7b turns it off (end it). Other learned keys toggle. `receiver` is the pen
        that pressed: its own microphone records, another pen's 7b is ignored."""
        control = self.controller
        if code == 0x7a:
            action = 'latch_note'  # 7a latches the pen microphone on (remembered if refused)
        elif code == 0x7b:
            action = 'stop_note'
            hint = control.latch_hint(receiver) if hasattr(control, 'latch_hint') else 'lost'
            if control.phase == 'idle':
                # Same firmware latch as ⑥: the 7a that switched the microphone on was lost
                # (or that note already ended another way). Never record: the microphone is off.
                # No hint after a 7a that was refused (it said why) or was the wizard's key test.
                if hint:
                    self.notify('mic_off', QUICK_TESTED_MIC_OFF_NOTICE if hint == 'tested' else QUICK_MIC_OFF_NOTICE)
                return
        else:
            quick = control.session_source == 'quick' and control.phase in ('starting', 'recording')
            action = 'stop_note' if quick else 'start_note'
        if action in ('start_note', 'latch_note') and control.phase not in ('idle',):
            refused = True
            if control.phase == 'disconnected':
                self.emit('notice', '语音输入已暂停；启用后短按 ⑦ 记闪念笔记')
            elif control.phase == 'error':
                self.emit('notice', '语音连接尚未恢复，正在自动重连；稍后再短按 ⑦')
            elif control.phase in ('starting', 'recording'):
                current = getattr(control, 'session_receiver', None)
                refused = receiver is not None and current is not None and current.get('id') != receiver.get('id')
                if refused:
                    self.emit('notice', BUSY_NOTICE)  # another pen is recording
            elif control.phase == 'processing':
                self.emit('notice', BUSY_NOTICE)  # the last one is still being recognised
            else:
                refused = False
            if refused and action == 'latch_note' and hasattr(control, 'latch_unused'):
                control.latch_unused(receiver)
            return
        control.command(action, receiver=receiver)

    def _log_key(self, interface, value, down=None):
        self.key_log.append((round(time.monotonic() - self.key_log_start, 3), self.app_mode, interface, value, down))
        self.key_log_dirty = True

    def _flush_key_log(self):
        if not self.key_log_dirty or self.smoke:
            return
        self.key_log_dirty = False
        lines = ['%9.3f %-7s %-8s %-6s %s' % (t, mode, iface, value, '' if down is None else ('down' if down else 'up'))
                 for t, mode, iface, value, down in list(self.key_log)]
        try:
            (self.runtime / 'key-log.txt').write_text(
                '# 最近的按键边沿（只有键码与时间，用于排查 ⑤ 双击等问题）\n' + '\n'.join(lines) + '\n', encoding='utf-8')
        except OSError:
            pass

    def _battery_alert(self, percent, receiver=None):
        """Low-battery cue once per level and per pen (two pens never re-arm each other)."""
        alerted = self.battery_alerted.setdefault(receiver, set())
        if percent >= 50:
            alerted.clear()
            return
        for level in (10, 20):
            if percent <= level:
                if level not in alerted:
                    alerted.update(l for l in (10, 20) if l >= level)
                    alert = {'percent': percent, 'level': level}
                    if receiver is not None:
                        alert.update(instance=receiver, label=self.instances.label(receiver))
                    self.emit('battery_alert', alert)
                    self.sounds.play('battery_low')
                return

    def _devices_changed(self, found):
        options = available_profiles(found)
        with self.lock:
            had_pen = any(d['id'] == 'baseus' and d['present'] for d in self.device_options)
            changed = options != self.device_options
            self.device_options = options
        self._forget_gone(found)
        if had_pen and not any(d['id'] == 'baseus' and d['present'] for d in options):
            self._release_pen()
        if self.instances.update_present(found):
            self._emit_instances()
            self._reroute()
        if changed:
            self.emit('devices', {'devices': options, 'selected': self.selected, 'mode': self.mode})
            self._apply_airmouse()

    def _forget_gone(self, found):
        """A receiver unplugged mid-press never sends its neutral report: forget its last
        code (here and in the controller), or the same key on the same port would be
        taken for a repeat and ignored on the first press after re-plugging."""
        present = {str(d.get('path', '')).upper() for d in found or ()}
        for path in list(self.last_codes):
            if path.upper() not in present:
                self.last_codes.pop(path, None)
        forget = getattr(self.controller, 'forget_paths', None)
        if forget:
            forget(present)

    def _release_pen(self):
        """The last receiver went away mid-gesture: nothing it held may stay held
        (laser on screen, air-mouse button down, annotation pen down)."""
        self.airmouse.forget()
        self.airmouse.release_all()
        self._pointer(False)
        self._toggle_laser(False)
        if self.annotate_at is not None:
            self.annotate_at = None
            self.emit('annotate', {'action': 'draw', 'down': False})

    def _hold_key(self, slot, down):
        """Air-mouse key, Ctrl (multi-select) and annotation: press/release keys."""
        now = time.monotonic()
        if slot == 'airmouse_activity':
            if down:
                self.airmouse.press()
                if self.app_mode == 'present':
                    # Tool switching is the firmware's double-tap (71 pairs), never two holds.
                    self._pointer(True)
            else:
                self.airmouse.release()
                self._pointer(False)
        elif slot == 'airmouse_ctrl':
            self.airmouse.set_ctrl(down and self.app_mode != 'present')
            if self.app_mode == 'present':
                self._hold_key('annotate', down)
        elif slot == 'annotate' and self.app_mode == 'present':
            if down:
                self.annotate_at = now
                self.emit('annotate', {'action': 'draw', 'down': True})
            else:
                started, self.annotate_at = self.annotate_at, None
                quick = started is not None and now - started < .35
                # A short press undoes the last stroke; a long press drew one.
                self.emit('annotate', {'action': 'undo' if quick else 'draw', 'down': False})

    def _pointer(self, active):
        active = active and self.tool != 'mouse'  # the plain air mouse needs no overlay
        if active != self.pointer_down:
            self.pointer_down = active
            self.emit('pointer', {'tool': self.tool, 'active': active, 'hide_cursor': self._hide_cursor(),
                                  'zoom': self._zoom()})

    def _zoom(self):
        return int(self.prefs.get('present', 'zoom') or 200)

    def _magnifier_wheel(self, role):
        """HID, repeat-timer or hook thread: while the magnifier is held, the volume keys zoom
        instead of scrolling. Hands off at once (never emits from those threads)."""
        if not (self.pointer_down and self.tool == 'magnifier'):
            return False
        self._submit(self._step_zoom, 1 if role == 'wheel_up' else -1)
        return True

    def _step_zoom(self, step):
        current = self._zoom()
        if step > 0:
            zoom = next((z for z in ZOOMS if z > current), ZOOMS[-1])
        else:
            zoom = next((z for z in reversed(ZOOMS) if z < current), ZOOMS[0])
        if zoom == current:
            return
        try:
            data = self.prefs.update({'present': {'zoom': zoom}}, persist=False)
        except ValueError:
            return
        self.emit('magnifier', {'zoom': zoom})
        self.emit('prefs', data)
        self._save_prefs()

    def _toggle_laser(self, on=None):
        """② toggles a latched laser dot in present mode, independent of the ⑤ tool."""
        on = (not self.laser_on) if on is None else on
        if on != self.laser_on:
            self.laser_on = on
            self.emit('pointer', {'tool': 'laser', 'active': on, 'latched': True, 'hide_cursor': self._hide_cursor()})

    def _preview_event(self, kind, value):
        if kind == 'partial':
            if getattr(self.controller, 'remote_live', False):
                return  # the backend's own live text took over: a late local guess is stale
            value = {**value, 'source': 'local'}
            self.live_text, self.live_source = value.get('text', ''), 'local'
            if value.get('final'):
                self.controller.local_final(value.get('session'), value.get('text', ''))
        elif kind == 'preview_error':
            self.controller.local_failed()
        self.emit(kind, value)

    def _preview_wanted(self):
        """The local models are needed for their words (shown on request) or 极速落字 (OpenLess only)."""
        return bool(self.prefs.get('preview', 'show_local_text')
                    or (self.prefs.get('voice', 'fast_raw') and self.engine_kind == 'openless'))

    def _sync_preview(self):
        """Run the local preview models only while something uses them: no warm-up and no
        decoding (CPU, ~300 MB) while neither the local words nor 极速落字 is wanted.
        Turning either on loads them ahead of the next recording."""
        with self.preview_lock:
            wanted = self.preview_available and self._preview_wanted() and not self.closed.is_set()
            if wanted and self.preview is None:
                try:
                    if self.preview_factory is not None:
                        self.preview = self.preview_factory(self._preview_event)
                    else:
                        from .preview import LivePreview
                        self.preview = LivePreview(self.preview_model, self._preview_event,
                                                   refiner=self.preview_refiner)
                    warm = getattr(self.preview, 'warm', None)
                    if warm:
                        warm()  # the refiner is ~240 MB: load it before the first recording
                except Exception as error:  # a broken preview never stops the core
                    self.preview = None
                    self.log_ui_error('preview', repr(error))
            elif not wanted and self.preview is not None:
                preview, self.preview = self.preview, None
                self.controller.preview = None
                close = getattr(preview, 'close', None)
                if close:
                    close()  # its worker ends and the models are freed
            self.controller.preview = self.preview

    def _fast_raw(self):
        """极速落字 for this input session: wanted in prefs and OpenLess is in 原文 mode."""
        if self.engine_kind != 'openless':
            return False  # it stands in for OpenLess's raw mode; other engines have their own path
        if self.tidepool_state.get('recording'):
            return False  # another device is talking: OpenLess arbitrates (busy notice)
        return bool(self.prefs.get('voice', 'fast_raw')) and self.openless.mode() == 'raw'

    def _apply_keep_awake(self):
        settings = self.prefs.get('sounds')
        if self.smoke or self.closed.is_set() or sound_player_is_fake(self.sounds):
            return
        if settings.get('enabled', True) and settings.get('keep_awake', True):
            self.keep_awake.start()
        else:
            self.keep_awake.stop()

    def _check_upstream(self):
        reminder = self.upstream.check()
        if reminder:
            self.upstream.state['reminder'] = reminder
            self.upstream._save()
            self.emit('reminder', reminder)

    def open_url(self, url=''):
        """Open a GitHub page (the reminder's link) or a key-help page in the default browser."""
        if (not isinstance(url, str) or not (url.startswith('https://github.com/') or url in HELP_URLS)
                or self.smoke):
            return {'error': '只能打开 GitHub 或获取密钥的帮助页面'}
        os.startfile(url)
        return {'ok': True}

    def dismiss_reminder(self):
        if self.upstream and self.upstream.state.pop('reminder', None):
            self.upstream._save()
        return {'ok': True}

    def check_updates(self, force=True):
        if self.app_updates is None:
            return {'update': None}
        value = self.app_updates.check(force=bool(force))
        if not self.closed.is_set():
            self.emit('app_update', value)
        return {'update': value}

    def dismiss_update(self, version=None):
        value = self.app_updates.dismiss(version) if self.app_updates else None
        self.emit('app_update', value)
        return {'update': value}

    def _hide_cursor(self):
        return bool(self.prefs.get('present', 'hide_cursor'))

    def _remember_tool(self):
        try:
            self.prefs.update({'present': {'tool': self.tool}}, persist=False)
        except ValueError:
            pass

    def _save_prefs(self):
        try:
            self.prefs.save()
        except OSError:
            pass

    def _cycle_tool(self):
        self._pointer(False)
        self.tool = TOOLS[(TOOLS.index(self.tool) + 1) % len(TOOLS)]
        self._remember_tool()
        self.emit('tool', {'tool': self.tool})
        self.sounds.play('tool')
        self._save_prefs()  # the disk write comes after the feedback

    def preview_tool(self, tool):
        """Show a pointer tool at the cursor for a few seconds, without the pen,
        so the display path can be checked in any mode."""
        if tool not in TOOLS or tool == 'mouse':
            return {'error': '这个工具没有画面可以预览'}
        if self.tool_preview:
            self.tool_preview.cancel()
        self.emit('pointer', {'tool': tool, 'active': True, 'preview': True, 'hide_cursor': self._hide_cursor(),
                              'zoom': self._zoom()})

        def finish():
            self.tool_preview = None
            self.emit('pointer', {'tool': tool, 'active': False, 'preview': True, 'hide_cursor': self._hide_cursor()})
        self.tool_preview = threading.Timer(TOOL_PREVIEW, finish)
        self.tool_preview.daemon = True
        self.tool_preview.start()
        return {'tool': tool, 'seconds': TOOL_PREVIEW}

    def set_tool(self, tool):
        if tool not in TOOLS:
            return {'error': '未知的指针工具'}
        self._pointer(False)
        self.tool = tool
        self._remember_tool()
        self.emit('tool', {'tool': tool})
        self._save_prefs()
        return {'tool': tool}

    def _apply_airmouse(self):
        """One keyboard hook serves the air-mouse remap and the pen shortcut guard; it
        runs while either needs it."""
        if self.smoke or self.closed.is_set():
            return
        with self.apply_lock:
            settings = self.prefs.get('airmouse')
            present = any(d['id'] == self.selected and d['present'] for d in self.device_options)
            # The pen as a mouse: ⑤ clicks, ③ grabs, ④ right-clicks (input / notes modes) and
            # the volume keys scroll while ⑤ is held (every mode); PowerPoint / WPS slideshows
            # keep the pen's own keys (skip_slideshow). The page keys are never remapped.
            remap = bool(settings['enabled'] and present and self.selected == 'baseus')
            # Any plugged-in Baseus receiver types its shortcuts, whichever device is selected.
            pen = any(d['id'] == 'baseus' and d['present'] for d in self.device_options)
            guard = bool(pen and self.prefs.get('pen', 'guard_shortcuts'))
            was_remapping = self.airmouse.remap_enabled
            self.airmouse.remap_enabled = remap
            self.guard.enabled = guard
            self.airmouse.refresh()
            if was_remapping and not remap:
                self.airmouse.release_all()  # no button or drag lock outlives the remap
            try:
                if remap or guard:
                    self.airmouse.start()
                else:
                    self.airmouse.stop()
            except RuntimeError as error:
                self.emit('notice', str(error))
                remap = guard = False
                self.guard.enabled = self.airmouse.remap_enabled = False
            try:
                # Other keyboards' modifiers tell the guard a real Ctrl+P apart from the pen's.
                self.monitor.on_foreign_key = (lambda vk, down: self.guard.raw_key(vk, down, pen=False)) if guard else None
            except AttributeError:
                pass
            try:
                # Zero-cost activity via a vendor code when known; otherwise mouse raw input.
                self.monitor.set_mouse(remap and not self.keymap.activity_codes(self.selected))
            except AttributeError:
                pass

    # -- mode -------------------------------------------------------------------
    def set_mode(self, value, category=None, source='ui'):
        """input / notes / present. Present keeps every key native and uses the
        input destination for voice, so speaking still types at the cursor."""
        if value not in MODES:
            return {'error': '不支持的模式'}
        if self.smoke:
            return {'error': '界面自检不切换真实录音去向。'}
        destination = 'notes' if value == 'notes' else 'input'
        with self.mode_lock:
            pending = None
            control = self.controller
            if destination == control.destination:
                pass  # input <-> present (or back from a deferred switch): nothing to change
            elif control.phase in BUSY:
                pending = value  # applied when this recording ends
            else:
                try:
                    control.set_destination(destination)
                except (ValueError, RuntimeError, TimeoutError, queue.Full):
                    pending = value
            self.pending_mode = (pending, None) if pending else None
            if not pending:
                self.app_mode = value
                self.airmouse.set_buttons(value != 'present')  # present mode: a grab is put down
                self.airmouse.guard.letters = value != 'present'  # ④'s B: black screen there
            try:
                self.prefs.update({'mode': value}, persist=False)
            except ValueError:
                pass
            payload = {'value': self.app_mode, 'pending': pending, 'source': source,
                       'armed': control.want_armed, 'tool': self.tool}
            # Feedback first (the key -> chime path); the disk write and overlays follow.
            self.emit('mode', payload)
            if source != 'pending':
                self.sounds.play('mode_' + value)
            self._save_prefs()
        if self.app_mode != 'present':
            self._pointer(False)
            self._toggle_laser(False)
            self.emit('annotate', {'action': 'clear', 'down': False})
        return payload

    def toggle_mode(self, source='ui'):
        current = self.pending_mode[0] if self.pending_mode else self.app_mode
        return self.set_mode(MODES[(MODES.index(current) + 1) % len(MODES)], source=source)

    def _apply_pending(self):
        if self.pending_mode and self.controller.phase not in BUSY:
            value, _ = self.pending_mode
            self.set_mode(value, source='pending')

    def destination(self, value, category='idea'):
        return self.set_mode(value, category)

    def note_record(self, category='idea'):
        choice = self.set_mode('notes', category)
        if choice.get('error'):
            return choice
        if self.controller.phase != 'idle':
            answer = self.action('arm')
            if answer.get('error'):
                return answer
        # The pen button opens the hardware microphone; recording starts there.
        return {'ok': True, 'awaiting_pen': True}

    # -- queries and settings --------------------------------------------------
    def usage_view(self):
        data = self.ledger.snapshot()
        samples = data.get('battery_samples', [])
        battery = data.get('battery')
        if battery:
            # Cells rebound a little after rest: keep the 15-minute low unless the
            # value rose by 5 or more (charging). The raw report stays visible.
            recent = [s['percent'] for s in samples if 0 <= battery['reported_at'] - s['at'] <= 900]
            low = min(recent + [battery['percent']])
            shown = low if battery['percent'] - low < 5 else battery['percent']
            data['battery'] = {**battery, 'percent': shown, 'raw': battery['percent']}
        data['battery_samples'] = samples[-48:]
        data['reduced_motion'] = self.prefs.get('appearance', 'reduced_motion')
        return data

    def snapshot(self):
        with self.lock:
            return {'version': VERSION, 'state': dict(self.state), 'result': self.result,
                    'usage': self.usage_view(), 'devices': self.device_options,
                    'instances': self.instances.snapshot(),
                    'selected': self.selected, 'mode': self.mode,
                    'tidepool': dict(self.tidepool_state), 'last_key': self.last_key,
                    'destination': self.app_mode,
                    'pending_mode': self.pending_mode[0] if self.pending_mode else None,
                    'prefs': self.prefs.snapshot(), 'tool': self.tool,
                    # Model files found (or a test preview): the local words can be switched on.
                    'preview_available': self.preview_available,
                    'preview_refiner': (getattr(self.preview, 'refiner_family', None)
                                        or (self.preview_refiner[1] if self.preview_refiner else None)),
                    'reminder': self.upstream.state.get('reminder') if self.upstream else None,
                    'app_update': self.app_updates.snapshot() if self.app_updates else None,
                    'bindings': self.keymap.snapshot(), 'live_text': self.live_text,
                    'live_source': self.live_source,
                    'armed': self.controller.want_armed,
                    'airmouse_active': self.airmouse.running and self.airmouse.remap_enabled,
                    'shortcut_guard': self.airmouse.running and self.guard.enabled,
                    'engine': self.engine_view(),  # which engine and its name; never any key
                    # prefs.json could not be read: defaults (or the saved copy) are in use.
                    'prefs_warning': self.prefs.warning,
                    # Whether DotVoice runs as administrator (兼容管理员权限窗口) and could.
                    'elevation': dict(self.uipi.own()),
                    'smoke': self.smoke}

    def notes(self):
        return self.notes_store.list()

    def update_note(self, identifier, text):
        try:
            return {'note': self.notes_store.update(identifier, text)}
        except (ValueError, OSError) as error:
            return {'error': str(error) if isinstance(error, ValueError) else '笔记未能保存，原内容仍保留。'}

    def set_note_tags(self, identifier, tags):
        try:
            return {'note': self.notes_store.set_tags(identifier, tags)}
        except (ValueError, OSError) as error:
            return {'error': str(error) if isinstance(error, ValueError) else '标签未能保存。'}

    def delete_note(self, identifier):
        try:
            return self.notes_store.delete(identifier)
        except (ValueError, OSError) as error:
            return {'error': str(error) if isinstance(error, ValueError) else '删除失败，笔记仍保留。'}

    def restore_note(self, identifier):
        try:
            return {'note': self.notes_store.restore(identifier)}
        except (ValueError, OSError) as error:
            return {'error': str(error) if isinstance(error, ValueError) else '恢复失败，请重试。'}

    def prefs_set(self, patch):
        engine = isinstance(patch, dict) and 'engine' in patch
        if engine:
            with self.engine_lock:
                try:
                    candidate = self.prefs.validate(patch)
                except ValueError as error:
                    return {'error': str(error)}
                # The active engine's settings changed: rebuild and swap it first, so a
                # refused switch (a recording is running) leaves the settings untouched.
                answer = self._apply_engine(self.prefs.get('engine'), candidate['engine'])
                if answer is not None:
                    return answer
                data = self.prefs.update(patch, persist=False)
                try:
                    self.prefs.save()
                except OSError:
                    self._sync_preview()
                    self.emit('engine', self.engine_view())
                    self.emit('prefs', data)
                    return {'error': '已应用，但设置未能保存；重启后会恢复原来的设置。'}
        else:
            try:
                data = self.prefs.update(patch)
            except ValueError as error:
                return {'error': str(error)}
            except OSError:
                return {'error': '设置未能保存，请检查本机写入权限。'}
        if engine:
            self.emit('engine', self.engine_view())
        if isinstance(patch, dict):
            if engine or 'preview' in patch or 'voice' in patch:
                self._sync_preview()  # the local models start or stop with show_local_text / fast_raw (OpenLess)
            if 'voice' in patch:
                # Read when the next recording starts; the current one keeps its setting.
                self.controller.whisper_mode = data['voice']['whisper']
            if 'airmouse' in patch or 'pen' in patch:
                self._apply_airmouse()
            if 'sounds' in patch:
                self._apply_keep_awake()
                self.sounds.prepare()
            if 'present' in patch:
                self.emit('magnifier', {'zoom': data['present']['zoom']})  # a lens on screen follows at once
            if 'diagnostics' in patch and not data['diagnostics']['save_audio']:
                self.diagnostics.clear()  # switched off: the debug recordings go now
            if 'devices' in patch and data['devices']['coexist']:
                threading.Thread(target=self._end_exclusive, name='tidepool-restore', daemon=True).start()
        self.emit('prefs', data)
        return data

    # -- speech engines -----------------------------------------------------------------
    def _build_backend(self, engine):
        """The backend for prefs['engine'] (ValueError: a message for the user)."""
        kind = engine['kind']
        if kind == 'openless':
            return self.openless_backend
        if self.engine_factory is not None:
            return self.engine_factory(kind, engine)
        if kind in ('volc', 'dashscope'):
            # Keys (and Doubao's hotwords) are read when a recording starts. Text an
            # administrator window refuses is kept (ElevatedTarget -> result not_typed).
            return create_backend(cloud_settings(engine, hotwords=self.hotwords), secret=self.secrets.load,
                                  inserter=self.uipi.guard_typer(type_text))
        if kind == 'hotkey':
            return hotkey.HotkeyBackend(hotkey_profile(engine['hotkey']),
                                        sender=self._guard_sender(hotkey.send_keys_scan))
        raise ValueError('不支持的语音引擎')

    def _backend_or_placeholder(self, engine):
        try:
            return self._build_backend(engine)
        except (ValueError, RuntimeError, OSError) as error:
            return UnavailableBackend(ENGINE_NAMES.get(engine['kind'], '语音引擎'), str(error))

    def _apply_engine(self, old, new):
        """Rebuild and swap the backend when the active engine (or its settings) changed.
        None when done or nothing to do; {'error'} otherwise (settings stay unchanged)."""
        if engine_signature(old) == engine_signature(new):
            return None
        if self.controller.phase in BUSY:
            return {'error': ENGINE_BUSY}
        try:
            backend = self._build_backend(new)
        except (ValueError, RuntimeError, OSError) as error:
            return {'error': str(error)}
        return self._swap_backend(backend, new['kind'])

    def _swap_backend(self, backend, kind):
        """Only while idle or paused: disarm (waits until the microphone and the old engine
        are released), swap, re-arm if the voice was on."""
        control = self.controller
        was_armed = bool(control.want_armed)
        if was_armed:
            try:
                control.disarm_and_wait(timeout=6)
            except RuntimeError as error:
                if backend is not self.openless_backend:
                    try:
                        backend.close()
                    except Exception:
                        pass
                if not self.closed.is_set():
                    # The pause was already asked for: the voice was on, so it comes back on
                    # with the engine it had (the arm waits for that pause if it still ends).
                    threading.Thread(target=self._key_arm, name='engine-arm', daemon=True).start()
                return {'error': f'{error}（语音引擎没有更换，已恢复原来的语音输入）'}
        old, control.backend = control.backend, backend
        self.engine_kind = kind
        if old is not backend:
            try:
                old.close()  # a hotkey bridge lets go of any key it still holds
            except Exception:
                pass
        if was_armed and not self.closed.is_set():
            # As with the pen's arm key: an exclusive arm may wait for Tidepool to pause.
            threading.Thread(target=self._key_arm, name='engine-arm', daemon=True).start()
        return None

    def engine_view(self):
        """For the UI: which engine runs and what to call it. Never contains a key."""
        backend = getattr(self.controller, 'backend', None)
        kind = self.engine_kind
        view = {'kind': kind, 'label': 'OpenLess' if kind == 'openless' else backend_label(backend),
                'external': bool(getattr(backend, 'external', False))}
        profile = getattr(backend, 'profile', None) if view['external'] else None
        if profile is not None:
            view.update(app=profile.name, profile=profile.id, combo=profile.combo, style=profile.style)
        if isinstance(backend, UnavailableBackend):
            view['error'] = backend.message
        if kind != 'volc' and self.volc_configured():
            # Doubao's key is saved yet another engine runs: settings offers the switch in one line.
            view['suggest_volc'] = True
        return view

    def volc_configured(self, engine=None):
        """Whether 豆包语音 could run as saved: its API Key, or App ID + Access Token. Presence
        only (secrets.has): no key is decrypted, read or returned for this."""
        conf = (engine or self.prefs.get('engine'))['volc']
        try:
            if conf.get('auth') == 'legacy':
                return bool(conf.get('app_id')) and self.secrets.has(SECRET_VOLC_ACCESS_TOKEN)
            return self.secrets.has(SECRET_VOLC_API_KEY)
        except (OSError, ValueError):
            return False

    def hotwords(self):
        """Doubao's hotwords, most useful first: the enabled phrases of the user's OpenLess
        dictionary (read-only; DotVoice keeps no dictionary of its own). Called when a
        recording starts, so a phrase added in OpenLess counts from the next one."""
        read = getattr(getattr(self, 'openless', None), 'hotwords', None)
        if not callable(read):
            return []
        try:
            return [word for word in read() or () if isinstance(word, str)]
        except Exception:   # an unreadable dictionary never stops a recording
            return []

    def secret_status(self):
        """Which keys are saved, masked ('••••abcd'); the values themselves never leave the core."""
        items = {}
        for name in SECRET_NAMES:
            try:
                masked = self.secrets.masked(name)
            except (OSError, ValueError):
                masked = ''
            items[name] = {'set': bool(masked), 'masked': masked}
        return {'secrets': items}

    def secret_set(self, name='', value=''):
        if name not in SECRET_NAMES:
            return {'error': '不支持的密钥名称'}
        if not isinstance(value, str):
            return {'error': '密钥必须是文本'}
        value = value.strip()
        if value and (len(value) > 1024 or not value.isascii() or any(c.isspace() or ord(c) < 32 for c in value)):
            return {'error': '密钥格式不正确（不能含空格、换行或中文）'}
        try:
            self.secrets.save(name, value)  # an empty value deletes
        except ValueError as error:
            return {'error': str(error)}
        except OSError:
            return {'error': '密钥未能加密保存（Windows 数据保护不可用）'}
        return self._secrets_changed(name)

    def secret_delete(self, name=''):
        if name not in SECRET_NAMES:
            return {'error': '不支持的密钥名称'}
        try:
            self.secrets.delete(name)
        except (OSError, ValueError):
            return {'error': '密钥未能删除，请重试'}
        return self._secrets_changed(name)

    def _secrets_changed(self, name):
        status = self.secret_status()
        self.emit('secrets', status)
        if name in (SECRET_VOLC_API_KEY, SECRET_VOLC_ACCESS_TOKEN):
            self.emit('engine', self.engine_view())   # suggest_volc may have changed
        return status

    def engine_test(self, kind=''):
        """Settings "测试连接": one short session (0.5 s of silence) or one polish request with
        the saved settings and keys. Only when the user asks; never automatic."""
        if kind not in PROBE_KINDS:
            return {'error': '这个引擎没有连接测试'}
        if self.smoke:
            return {'error': '界面自检模式不连接网络。'}
        with self.probe_lock:
            if kind in self.probing:
                return {'error': '正在测试，请稍候'}
            self.probing.add(kind)
        started = time.monotonic()
        try:
            ok, message = self._probe(kind, self.prefs.get('engine'))
        except Exception as error:  # a probe bug is reported, never raised into the shell
            ok, message = False, f'测试没有完成（{type(error).__name__}）'
        finally:
            with self.probe_lock:
                self.probing.discard(kind)
        return {'kind': kind, 'ok': bool(ok), 'message': str(message or ''),
                'seconds': round(time.monotonic() - started, 2)}

    def _probe(self, kind, engine):
        if self.probe_factory is not None:
            return self.probe_factory(kind, engine)
        if kind == 'polish':
            from .engines.llm import Polisher
            conf = engine['polish']
            return Polisher(conf['base_url'], lambda: self.secrets.load(SECRET_LLM_API_KEY), conf['model'],
                            style=conf['style'], prompt=conf['prompt']).probe()
        # With the hotwords a recording would send, so the test also covers that request.
        settings = cloud_settings({**engine, 'kind': kind}, hotwords=self.hotwords)
        settings['polish'] = {'style': 'raw'}  # the recogniser alone
        return create_backend(settings, secret=self.secrets.load, inserter=lambda text: None).asr.probe()

    def _mic_name(self):
        """The pen's microphone as Windows lists it ('麦克风 (BaseusPresent)'), for the hint
        telling the user what to pick in another dictation app. No device is opened."""
        name = PROFILES.get(self.selected, PROFILES['baseus'])['microphone']
        if self.smoke:
            return name
        try:
            from .audio import wave_inputs
            for _, label in wave_inputs():
                if name.casefold() in label.casefold():
                    return label
        except Exception:
            pass
        return name

    def hotkey_detect(self):
        """Dictation apps the hotkey bridge can drive, detected ones first (read-only:
        running processes and uninstall entries; nothing is started or changed)."""
        try:
            found = self.hotkey_scan() or {}
        except Exception:
            found = {}
        mic = self._mic_name()
        apps = []
        for profile_id, profile in hotkey.PROFILES.items():
            info = found.get(profile_id) or {}
            installed, running = bool(info.get('installed')), bool(info.get('running'))
            supported = info.get('supported')
            apps.append({**profile.describe(mic), 'installed': installed, 'running': running,
                         'version': info.get('version'), 'supported': supported,
                         'detected': (installed or running) and supported is not False,
                         'builtin': bool(hotkey.DETECT.get(profile_id, {}).get('builtin')),
                         'cancel': hotkey.format_combo(profile.cancel_keys) if profile.cancel_keys else ''})
        apps.sort(key=lambda app: (not app['detected'], app['builtin'], not app['running']))
        return {'apps': apps, 'mic': mic, 'custom_mic_hint': hotkey.CUSTOM_MIC_HINT.replace('{mic}', mic)}

    def openless_status(self):
        """Is OpenLess installed / running / its remote input on? Read-only: its files are
        never changed, and the pairing PIN is never read out."""
        backend = self.openless_backend
        appdata = Path(os.environ.get('APPDATA', ''))
        prefs_file = Path(getattr(backend, 'preferences_file', appdata / 'OpenLess' / 'preferences.json'))
        pin_file = Path(getattr(backend, 'pin_file', appdata / 'com.openless.app' / 'remote-input-pin.txt'))
        try:
            running = 'openless.exe' in self.process_names()
        except Exception:
            running = False
        remote = port = None
        if prefs_file.exists():
            try:
                data = json.loads(prefs_file.read_text(encoding='utf-8-sig'))
            except (OSError, ValueError):
                data = None
            if isinstance(data, dict):
                remote = data.get('remoteInputEnabled') is not False
                value = data.get('remoteInputPort', 8443)
                port = value if type(value) is int and 1 <= value <= 65535 else None
        paired = pin_file.exists()
        active = self.engine_kind == 'openless'
        return {'installed': prefs_file.exists() or paired or running, 'running': running,
                'remote_input': remote, 'paired': paired, 'port': port, 'active': active,
                'connected': active and self.controller.phase in ('idle', 'starting', 'recording', 'processing')}

    def wizard_test(self, enabled=True):
        """Setup wizard step 3: recordings in the next 90 s are tests (renewed while the wizard
        is open): nothing typed, nothing saved; the text arrives as a 'wizard_result' event."""
        mode = getattr(self.controller, 'test_mode', None)
        if mode is None:
            return {'error': '此版本不支持试说'}
        # Hidden to the tray, a wizard page that still renews cannot take dictation over.
        enabled = bool(enabled) and self.visible
        mode(enabled)
        return {'ok': True, 'enabled': enabled}

    def mic_meter(self, seconds=30):
        """Setup wizard step 1: 'level' events while nobody records, for `seconds` (0 stops)."""
        if self.smoke:
            return {'error': '界面自检模式不打开麦克风。'}
        if type(seconds) not in (int, float) or not 0 <= seconds <= 120:
            return {'error': '时长无效'}
        meter = getattr(self.controller, 'meter', None)
        if meter is None:
            return {'error': '此版本不支持麦克风测试'}
        meter(seconds if self.visible else 0)  # hidden: the microphone is never opened for it
        return {'ok': True}

    def open_diagnostics(self):
        """Settings: show the debug recordings' folder (prefs diagnostics.save_audio) in
        Explorer, so they can be looked at or handed over by hand. Nothing is uploaded."""
        if self.smoke:
            return {'error': '界面自检模式不打开文件夹。'}
        folder = self.diagnostics.folder
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(str(folder))
        except OSError as error:
            return {'error': f'无法打开文件夹：{error}'}
        return {'ok': True, 'path': str(folder)}

    def log_ui_error(self, where='', detail=''):
        """A page caught a rendering error and recovered; keep a short local record."""
        path = self.runtime / 'ui-errors.log'
        line = '%s [%s] %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), str(where)[:20],
                                  str(detail)[:1200].replace('\n', ' | '))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            old = path.read_text(encoding='utf-8')[-20000:] if path.exists() else ''
            path.write_text(old + line, encoding='utf-8')
        except OSError:
            pass
        return {'ok': True}

    def _learn_progress(self, value):
        """Keymap thread, under its lock: emit only. A saved binding may change what the
        keyboard hook watches (a keyboard wheel key) or whether mouse input is needed (⑤):
        that is applied off this thread, like binding_set does."""
        self.emit('learn', value)
        if value.get('status') == 'done':
            self._submit(self._apply_airmouse)

    def bindings(self):
        return self.keymap.snapshot()

    def binding_learn(self, device, slot):
        try:
            return {'learning': self.keymap.learn_start(device, slot)}
        except ValueError as error:
            return {'error': str(error)}

    def binding_cancel(self):
        self.keymap.learn_cancel()
        return {'ok': True}

    def binding_skip(self):
        try:
            self.keymap.learn_skip()
        except ValueError as error:
            return {'error': str(error)}
        self._apply_airmouse()
        return self.keymap.snapshot()

    def binding_set(self, device, slot, binding):
        try:
            self.keymap.set(device, slot, binding)
        except (ValueError, OSError) as error:
            return {'error': str(error)}
        self._apply_airmouse()
        return self.keymap.snapshot()

    def binding_reset(self, device, slot=None):
        try:
            self.keymap.reset(device, slot)
        except (ValueError, OSError, KeyError) as error:
            return {'error': str(error)}
        self._apply_airmouse()
        return self.keymap.snapshot()

    def set_probe(self, enabled):
        self.probe = bool(enabled)
        return {'probe': self.probe}

    def sound_preview(self, name):
        return {'played': self.sounds.play(name, force=True)}

    def copy_text(self, text):
        from .clipboard import copy_text
        try:
            copy_text(text)
            return {'ok': True}
        except (ValueError, OSError) as error:
            return {'error': str(error)}

    def set_visible(self, visible):
        self.visible = bool(visible)
        if self.visible:
            self.emit('usage', self.usage_view())
        else:
            # Hidden to the tray (the page stays mounted): a setup wizard left open must not
            # keep turning dictation into tests, nor keep the pen microphone open for its meter.
            for name, value in (('test_mode', False), ('meter', 0)):
                method = getattr(self.controller, name, None)
                if method is not None:
                    method(value)
        return {'ok': True}

    def hotkey(self, name):
        if name == 'toggle_mode':
            return self.toggle_mode('hotkey')
        if name == 'arm_toggle':
            return self.action('disarm' if self.controller.want_armed else 'arm')
        return {'error': '未知快捷键'}

    # -- device selection and sessions ----------------------------------------
    def select_device(self, device, mode='hold'):
        if self.smoke:
            return {'error': '界面自检模式不切换真实设备。'}
        if self.switch_lock.locked():
            return {'error': '正在切换语音设备，请稍后再试。'}
        with self.lock:
            try:
                if device not in PROFILES:
                    raise ValueError('不支持的翻页笔')
                filename = 'usage.json' if device == 'baseus' else 'usage-pv60.json'
                ledger = UsageLedger(self.runtime / filename)
                self.controller.select_device(device, mode)
                try:
                    self.ledger.tick('disconnected')
                    self.ledger.save()
                except OSError:
                    pass
                self.ledger = ledger
                self.selected, self.mode = device, mode
                settings = self.runtime / 'devices.json'
                temp = settings.with_suffix('.tmp')
                try:
                    settings.parent.mkdir(parents=True, exist_ok=True)
                    temp.write_text(json.dumps({'device': device, 'mode': mode}), encoding='utf-8')
                    temp.replace(settings)
                except OSError:
                    self.ledger.warning = '已切换设备，但无法保存选择；重启后请重新选择。'
            except (OSError, ValueError, RuntimeError) as error:
                return {'error': str(error)}
        self._apply_airmouse()
        return self.snapshot()

    def action(self, action):
        if self.closed.is_set():
            return {'error': '点声正在退出'}
        if self.smoke:
            return {'error': '界面自检模式不打开麦克风。'}
        if action not in ('arm', 'disarm', 'start', 'stop', 'cancel'):
            return {'error': '未知操作。'}
        if action == 'arm' and self._coexist():
            # 随拿随用: Tidepool keeps running; OpenLess arbitrates one session at a time.
            with self.lock:
                self.controller.command('arm')
            return {'ok': True}
        if action == 'arm':
            if not self.switch_lock.acquire(blocking=False):
                return {'error': '正在切换语音设备，请稍后再试。'}
            try:
                epoch = self.arm_epoch
                try:
                    if self.tidepool_state.get('control_supported'):
                        self.tidepool.control('pause', temporary=True)
                    elif self.tidepool_state.get('receiver_running'):
                        self.notify('notice', 'Tidepool 旧接收器尚未接入统一控制，请勿同时使用两台设备录音。')
                except (OSError, ValueError, RuntimeError) as error:
                    return {'error': f'无法暂停 Tidepool：{error}'}
                with self.lock:
                    if epoch != self.arm_epoch or self.closed.is_set():
                        return {'error': '启用已取消。'}
                    self.controller.command(action)
            finally:
                self.switch_lock.release()
        else:
            with self.lock:
                if action in ('disarm', 'cancel'):
                    self.arm_epoch += 1
                self.controller.command(action)
        return {'ok': True}

    def _coexist(self):
        """prefs devices.coexist (default on): the pen and Tidepool never pause each other."""
        prefs = getattr(self, 'prefs', None)
        if prefs is None:
            return True
        try:
            return bool(prefs.get('devices', 'coexist'))
        except (KeyError, TypeError):
            return True

    def _end_exclusive(self):
        """Coexist turned on: hand back a Tidepool pause an exclusive arm made earlier."""
        if self.smoke or not self.switch_lock.acquire(timeout=20):
            return
        try:
            self.tidepool.restore_presenter_pause()
        except (OSError, ValueError, RuntimeError) as error:
            self.log_ui_error('tidepool-restore', str(error))
        finally:
            self.switch_lock.release()

    def tidepool_action(self, action):
        if self.closed.is_set():
            return {'error': '点声正在退出'}
        if self.smoke:
            return {'error': '界面自检不控制设备。'}
        if action not in ('pause', 'resume', 'cancel', 'wake', 'sleep'):
            return {'error': '不支持的 Tidepool 操作。'}
        if not self.switch_lock.acquire(blocking=False):
            return {'error': '正在切换语音设备，请稍后再试。'}
        try:
            try:
                if self.closed.is_set():
                    return {'error': '点声正在退出'}
                if action == 'resume' and not self._coexist():
                    # Exclusive: Tidepool gets the slot only after the pen let it go.
                    # A queue ACK also covers an earlier arm still awaiting connect.
                    self.controller.disarm_and_wait(timeout=4)
                answer = self.tidepool.control(action)
                self.tidepool_state = self.tidepool.snapshot()
                self.notify('tidepool', dict(self.tidepool_state))
                return {'ok': True, 'status': dict(self.tidepool_state), 'response': answer}
            except (OSError, ValueError, RuntimeError) as error:
                return {'error': str(error)}
        finally:
            self.switch_lock.release()

    def _poll_tidepool(self):
        while not self.closed.is_set():
            try:
                status = self.tidepool.snapshot()
                with self.lock:
                    self.tidepool_state = status
                self.emit('tidepool', status)
            except Exception:  # an unexpected reply must not end polling for good
                pass
            supported = self.tidepool_state.get('control_supported')
            # The legacy probe starts PowerShell; back off hard while hidden.
            interval = 3 if self.visible and supported else 20 if (supported or self.visible) else 120
            self.closed.wait(interval)

    def clear_result(self):
        with self.lock:
            self.result = None
        return {'ok': True}

    def mark(self, kind):
        with self.lock:
            try:
                if kind == 'full':
                    self.ledger.full()
                elif kind == 'low':
                    self.ledger.low()
                else:
                    return {'error': '未知充电标记。'}
                return self.usage_view()
            except (OSError, ValueError) as error:
                return {'error': str(error)}

    def _ticker(self):
        """Once a second. Each step is guarded on its own: one failure (a full disk, an
        odd reply) must never stop usage, status, the key-log flush, the learn timeout
        or the forgotten drag-lock release for good."""
        due = {'usage': 0, 'status': 0, 'audio': time.monotonic() + 600,
               'upstream': time.monotonic() + 90, 'app_update': time.monotonic() + 5,
               'diagnostics': time.monotonic() + 3600}
        logged = set()
        while not self.closed.wait(1):
            for name, step in (('upstream', self._tick_upstream), ('app_update', self._tick_app_update), ('audio', self._tick_audio),
                               ('keys', self._tick_keys), ('ledger', self._tick_ledger),
                               ('usage', self._tick_usage), ('status', self._tick_status),
                               ('diagnostics', self._tick_diagnostics)):
                try:
                    step(due)
                except Exception as error:
                    if (name, repr(error)) not in logged and len(logged) < 50:
                        logged.add((name, repr(error)))  # each distinct failure once
                        self.log_ui_error('core-' + name, repr(error))

    def _tick_upstream(self, due):
        if self.upstream and time.monotonic() >= due['upstream']:
            due['upstream'] = time.monotonic() + 3600
            if self.upstream.due():
                threading.Thread(target=self._check_upstream, name='upstream-check', daemon=True).start()

    def _tick_app_update(self, due):
        if self.app_updates and time.monotonic() >= due['app_update']:
            due['app_update'] = time.monotonic() + 60
            if self.app_updates.due():
                threading.Thread(target=self.check_updates, kwargs={'force': False},
                                 name='app-update-check', daemon=True).start()

    def _tick_audio(self, due):
        if time.monotonic() >= due['audio']:
            due['audio'] = time.monotonic() + 600
            self.keep_awake.refresh()  # follow a changed default output device

    def _tick_diagnostics(self, due):
        """Hourly: debug recordings never outlive their 24 hours, even with no new recording."""
        if time.monotonic() >= due['diagnostics']:
            due['diagnostics'] = time.monotonic() + 3600
            self.diagnostics.purge()

    def _tick_keys(self, due):
        self.keymap.tick()
        self.airmouse.expire()
        self._expire_pointer()
        self._expire_holds()
        self._flush_key_log()
        self.sounds.flush_log()

    def _expire_holds(self):
        """⑧'s release report (69) lost on the 2.4 GHz link: the annotation pen would keep
        drawing with every pointer move, or every air-mouse click stay a Ctrl+click,
        until the next ⑧ press. Nobody holds ⑧ this long."""
        started = self.annotate_at
        if started is not None and time.monotonic() - started > HOLD_KEY_MAX:
            self.annotate_at = None
            self.emit('annotate', {'action': 'draw', 'down': False})
        self.airmouse.expire_ctrl(HOLD_KEY_MAX)

    def _expire_pointer(self):
        """⑤'s release report (73) lost on the 2.4 GHz link: the laser - and the hidden
        system cursor - would stay until the next ⑤ press. Nobody holds ⑤ this long."""
        mouse = self.airmouse
        if self.pointer_down and mouse.holding and mouse.clock() - mouse.last_activity > POINTER_HOLD_MAX:
            mouse.holding = False
            self._pointer(False)

    def _tick_ledger(self, due):
        with self.lock:
            try:
                self.ledger.tick()
            except OSError:
                self.ledger.warning = '续航记录写入失败，请检查程序目录的写入权限。'

    def _tick_usage(self, due):
        now = time.monotonic()
        if self.visible and now >= due['usage']:
            due['usage'] = now + 2
            self.emit('usage', self.usage_view())

    def _tick_status(self, due):
        now = time.monotonic()
        if not self.smoke and now >= due['status']:
            due['status'] = now + 30
            self._write_status()

    def _write_status(self):
        # Numeric health evidence only: no transcript, PCM or PIN.
        with self.lock:
            health = {'version': VERSION, 'at': time.time(), 'phase': self.state['phase'],
                      'device': self.selected, 'engine': self.engine_kind, 'last_key': self.last_key,
                      'receiver_present': any(d['id'] == self.selected and d['present'] for d in self.device_options),
                      'receivers_present': self.instances.present_count(self.selected),
                      'battery': self.ledger.data['battery'],
                      'recent_sessions': self.ledger.data['recent_sessions'][-2:],
                      'prefs_warning': self.prefs.warning, 'bindings_warning': self.keymap.warning}
        try:
            target = self.runtime / 'status.json'
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_suffix('.tmp')
            temp.write_text(json.dumps(health, ensure_ascii=False), encoding='utf-8')
            temp.replace(target)
        except OSError:
            pass

    def methods(self):
        """The only operations exposed to the shell."""
        return {
            'snapshot': self.snapshot, 'action': self.action, 'clear_result': self.clear_result,
            'mark': self.mark, 'set_mode': self.set_mode, 'toggle_mode': self.toggle_mode,
            'note_record': self.note_record, 'notes': self.notes, 'update_note': self.update_note,
            'delete_note': self.delete_note, 'restore_note': self.restore_note,
            'set_note_tags': self.set_note_tags, 'set_tool': self.set_tool,
            'preview_tool': self.preview_tool, 'log_ui_error': self.log_ui_error,
            'open_diagnostics': self.open_diagnostics,
            'dismiss_reminder': self.dismiss_reminder, 'open_url': self.open_url,
            'check_updates': self.check_updates, 'dismiss_update': self.dismiss_update,
            'select_device': self.select_device, 'tidepool_action': self.tidepool_action,
            'prefs_set': self.prefs_set, 'bindings': self.bindings,
            'binding_learn': self.binding_learn, 'binding_cancel': self.binding_cancel,
            'binding_skip': self.binding_skip, 'binding_set': self.binding_set,
            'binding_reset': self.binding_reset, 'probe': self.set_probe,
            'sound_preview': self.sound_preview, 'set_visible': self.set_visible,
            'hotkey': self.hotkey, 'copy_text': self.copy_text,
            'rename_instance': self.rename_instance,
            # speech engines and the setup wizard
            'secret_set': self.secret_set, 'secret_delete': self.secret_delete,
            'secret_status': self.secret_status, 'engine_test': self.engine_test,
            'hotkey_detect': self.hotkey_detect, 'openless_status': self.openless_status,
            'wizard_test': self.wizard_test, 'mic_meter': self.mic_meter,
        }
