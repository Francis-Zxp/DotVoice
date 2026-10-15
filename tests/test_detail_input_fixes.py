"""Input-path regressions: maximized editors are not slideshows, a lost ⑤ release never keeps
a grab forever, unreadable bindings.json keeps re-recorded keys, PV60 learns from one press.

No hook is installed, nothing is injected into Windows and no HID device is opened: user32
is a fake and the keymap runs on temp files with a fake clock.
"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bridge import airmouse, keymap as keymap_module
from bridge.keymap import Keymap


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class FakeUser32:
    """The calls is_presentation_foreground makes; one 1920x1080 monitor without a taskbar."""
    def __init__(self, cls='OpusApp', zoomed=False, rect=(-8, -8, 1928, 1088)):
        self.cls, self.zoomed, self.rect = cls, zoomed, rect

    def GetForegroundWindow(self):
        return 0x1234

    def GetClassNameW(self, hwnd, buffer, size):
        buffer.value = self.cls
        return len(self.cls)

    def GetWindowThreadProcessId(self, hwnd, pid):
        pid._obj.value = 42
        return 1

    def IsZoomed(self, hwnd):
        return self.zoomed

    def GetWindowRect(self, hwnd, rect):
        rect._obj.left, rect._obj.top, rect._obj.right, rect._obj.bottom = self.rect
        return 1

    def MonitorFromWindow(self, hwnd, flags):
        return 1

    def GetMonitorInfoW(self, monitor, info):
        screen = info._obj.rcMonitor
        screen.left, screen.top, screen.right, screen.bottom = 0, 0, 1920, 1080
        info._obj.rcWork = screen  # auto-hidden taskbar: the work area is the whole screen
        return 1


class SlideshowDetectionTests(unittest.TestCase):
    def detect(self, process='wps.exe', **window):
        with patch.object(airmouse, 'user32', FakeUser32(**window)), \
                patch.object(airmouse, '_process_name', lambda pid: process):
            return airmouse.is_slideshow_foreground()

    def test_a_maximized_editor_on_a_screen_without_taskbar_is_not_a_slideshow(self):
        # The invisible DWM frame makes a maximized window overhang the screen by ~8 px.
        self.assertFalse(self.detect(zoomed=True))
        self.assertFalse(self.detect('powerpnt.exe', zoomed=True))

    def test_a_borderless_full_screen_window_still_is(self):
        self.assertTrue(self.detect(rect=(0, 0, 1920, 1080)))
        self.assertTrue(self.detect('wpp.exe', rect=(0, 0, 1920, 1080)))

    def test_powerpoints_show_window_is_known_by_its_class(self):
        self.assertTrue(self.detect('powerpnt.exe', cls='screenClass', zoomed=True))

    def test_other_programs_full_screen_are_never_slideshows(self):
        self.assertFalse(self.detect('chrome.exe', rect=(0, 0, 1920, 1080)))


class Alive:
    def is_alive(self):
        return True


class LostReleaseGrabTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.sent = []
        self.mouse = airmouse.AirMouse(lambda: {'skip_slideshow': True}, lambda: {}, clock=self.clock,
                                       sender=lambda action, down, ctrl: self.sent.append((action, down)),
                                       presenting=lambda: False,
                                       guard=airmouse.ShortcutGuard(clock=self.clock, slideshow=lambda: False,
                                                                    presentation_app=lambda: False, wait=0.0,
                                                                    pressed=lambda name: False),
                                       injector=lambda keys: None, double_click_ms=lambda: 500)
        self.mouse.thread = Alive()

    def grab(self):
        self.assertTrue(self.mouse.begin_drag())
        self.mouse.drag_timer.cancel()
        self.mouse._grab()
        self.assertTrue(self.mouse.locked)

    def test_a_grab_dragged_with_key5_whose_73_was_lost_is_put_down_in_the_end(self):
        self.grab()
        self.mouse.press()  # 72: ⑤ held to drag; its 73 never arrives
        self.clock.now += 25
        self.mouse.expire()
        self.assertTrue(self.mouse.locked, 'within WHEEL_POINTER_MAX ⑤ may really still be held')
        self.clock.now += airmouse.WHEEL_POINTER_MAX
        self.mouse.expire()
        self.assertFalse(self.mouse.locked)
        self.assertEqual(self.sent[-1], ('left', False))

    def test_key5_really_held_and_moving_keeps_the_grab(self):
        self.grab()
        self.mouse.press()
        for _ in range(10):  # 150 s of dragging: the pen's movement renews the activity
            self.clock.now += 15
            self.mouse.activity()
            self.mouse.expire()
        self.assertTrue(self.mouse.locked)
        self.assertEqual(self.sent, [('left', True)])


class RecoveredBindingsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'bindings.json'
        self.recovered = self.path.with_name('bindings-recovered.json')
        patcher = patch.object(keymap_module, 'READ_RETRY', 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_keys_recorded_while_bindings_json_is_broken_survive_a_restart(self):
        self.path.write_text('{not json', encoding='utf-8')
        first = Keymap(self.path, clock=Clock())
        self.assertIn('本次使用默认值', first.warning)
        self.assertIn('bindings-recovered.json', first.warning)
        first.set('baseus', 'cancel', {'source': 'vendor', 'fire': ['6a']})
        first.set('baseus', 'arm_toggle', {'source': 'vendor', 'fire': ['6c']})
        self.assertEqual(self.path.read_text(encoding='utf-8'), '{not json', 'the original file is kept')

        again = Keymap(self.path, clock=Clock())
        self.assertIn('之前另存的绑定', again.warning)
        self.assertEqual(again.vendor('baseus', 0x6a), 'cancel')
        self.assertEqual(again.vendor('baseus', 0x6c), 'arm_toggle')
        again.set('baseus', 'quick_note', {'source': 'vendor', 'fire': ['70']})
        saved = json.loads(self.recovered.read_text(encoding='utf-8'))['devices']['baseus']
        self.assertEqual(set(saved), {'cancel', 'arm_toggle', 'quick_note'}, 'one more key never drops the others')

    def test_an_unreadable_recovered_file_falls_back_to_the_defaults(self):
        self.path.write_text('[]', encoding='utf-8')
        self.recovered.write_text('{"devices": []}', encoding='utf-8')
        keymap = Keymap(self.path, clock=Clock())
        self.assertIn('本次使用默认值', keymap.warning)
        self.assertEqual(keymap.vendor('baseus', 0x7c), 'toggle_mode')
        self.assertEqual(keymap.path, self.recovered)

    def test_a_bom_and_a_briefly_busy_file_still_load(self):
        self.path.write_text(json.dumps({'version': 3, 'devices': {'baseus': {
            'cancel': {'source': 'vendor', 'fire': ['6a']}}}}), encoding='utf-8-sig')
        self.assertEqual(Keymap(self.path, clock=Clock()).vendor('baseus', 0x6a), 'cancel')
        real, calls = Path.read_text, []

        def busy_once(path, *args, **kwargs):
            calls.append(path)
            if len(calls) == 1:
                raise PermissionError(32, 'sharing violation')
            return real(path, *args, **kwargs)

        with patch.object(Path, 'read_text', busy_once):
            keymap = Keymap(self.path, clock=Clock())
        self.assertEqual(keymap.warning, '')
        self.assertEqual(keymap.path, self.path)
        self.assertEqual(keymap.vendor('baseus', 0x6a), 'cancel')

    def test_a_file_held_open_for_good_says_so(self):
        self.path.write_text('{}', encoding='utf-8')
        with patch.object(Path, 'read_text', side_effect=PermissionError(32, 'sharing violation')):
            keymap = Keymap(self.path, clock=Clock())
        self.assertIn('占用', keymap.warning)
        self.assertEqual(keymap.path, self.recovered)


class PV60LearningTests(unittest.TestCase):
    def test_one_press_saves_the_key_and_neither_it_nor_its_release_fires(self):
        with tempfile.TemporaryDirectory() as folder:
            progress = []
            keymap = Keymap(Path(folder) / 'bindings.json', progress.append, clock=Clock())
            learning = keymap.learn_start('pv60', 'cancel')
            self.assertEqual(learning['step'], 'tap')
            self.assertEqual(keymap.learning['steps'], ['tap'], 'the dialog says: press once')
            self.assertEqual(keymap.pv60(0x30, True), 'learning')
            self.assertEqual(progress[-1]['status'], 'done')
            self.assertIsNone(keymap.learning)
            self.assertEqual(keymap.pv60(0x30, False), 'ignored', 'the release of that press')
            self.assertEqual(keymap.pv60(0x30, True), 'cancel', 'the next press fires')


if __name__ == '__main__':
    unittest.main()
