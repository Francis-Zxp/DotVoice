"""Fakes for building a real Core without HID, audio, sounds or Tidepool."""
from pathlib import Path

from bridge.controller import Controller
from bridge.core import Core

try:
    from .test_bridge import FakeBackend, FakeCapture
except ImportError:
    from test_bridge import FakeBackend, FakeCapture


class FakeMonitor:
    def __init__(self, callback, error_callback=None, devices_callback=None):
        self.callback, self.devices_callback = callback, devices_callback
        self.mouse = False
        self.closed = False

    def start(self):
        pass

    def set_mouse(self, enabled):
        self.mouse = enabled

    def close(self):
        self.closed = True


class FakeTree:
    """cfgmgr32 stand-in: {instance id: parent instance id}. Empty = mapping fails."""
    def __init__(self, parents=None, absent=()):
        self.parents = {k.upper(): v.upper() for k, v in (parents or {}).items()}
        self.absent = {a.upper() for a in absent}

    def parent_of(self, instance):
        return self.parents.get(instance.upper())

    def present(self, instance):
        return instance.upper() not in self.absent


class FakeTidepool:
    def control(self, action, temporary=False):
        return {'ok': True, 'action': action}

    def restore_presenter_pause(self):
        return False

    def snapshot(self):
        return {'control_supported': False, 'receiver_running': False}


def make_core(base, backend=None, controller=None, events=None, sounds=None,
              capture=FakeCapture, device_tree=None, engine_factory=None, probe_factory=None):
    """`backend` is the OpenLess stand-in; `engine_factory(kind, engine_prefs)` builds the
    other engines (default: the real builders, which need no network to construct)."""
    events = [] if events is None else events
    sounds = [] if sounds is None else sounds
    backend = backend or FakeBackend()
    factory = ((lambda *args, **kwargs: controller) if controller else
               (lambda notify, backend, device, mode: Controller(notify, backend, capture,
                                                                 device=device, mode=mode)))
    core = Core(base, backend, lambda kind, value: events.append((kind, value)),
                monitor_factory=FakeMonitor, tidepool=FakeTidepool(),
                sound_player=lambda path: sounds.append(path.stem), controller_factory=factory,
                device_tree=device_tree or FakeTree(), engine_factory=engine_factory,
                probe_factory=probe_factory, update_factory=lambda *args: None)
    # The fake player only records the cue name. Rendering each WAV in pure Python
    # costs seconds on a busy machine and ran on the controller thread, which made
    # 2-second waits in session tests flaky; render() itself is covered by SoundTests.
    core.sounds._path = lambda name, volume: Path(base) / 'runtime' / 'sounds' / (name + '.wav')
    # ⑤ taps click: no test may inject real mouse or keyboard input, nor depend on a
    # slideshow that happens to be open on the test machine.
    mouse = core.airmouse
    mouse.sent = []
    mouse.sender = lambda action, down=None, ctrl=False: mouse.sent.append((action, down, ctrl))
    mouse.injector = lambda keys: mouse.sent.append(('keys', keys, False))
    mouse.presenting = lambda: False
    mouse.double_click_ms = lambda: 500
    # Nor on whatever window happens to be in front (an elevated terminal would make every
    # click "blocked"), nor on the real clipboard.
    core.uipi._own = {'elevated': False, 'can_elevate': True}
    core.uipi.foreground = lambda: (0, 0)
    core.copied = []
    core.clipboard = core.copied.append
    return core
