"""Fresh-install ⑧ annotation and 2.4 GHz robustness (lost reports, audio stalls).

Built from the shipped defaults only: no bindings.json, no prefs.json, as on a PC
where nobody ever recorded a key. Nothing touches real HID, audio or hooks.
"""
from pathlib import Path
import json
import tempfile
import time
import unittest
from unittest.mock import patch

from bridge import airmouse, audio
from bridge.audio import STALL_CHECK, STALL_LIMIT, StallWatch
from bridge.backend import BackendBusy
from bridge.controller import BUSY_NOTICE, EXTERNAL_NOTES_NOTICE, MIC_OFF_NOTICE, OTHER_DEVICE_NOTICE, Controller
from bridge.core import QUICK_MIC_OFF_NOTICE
from bridge.keymap import Keymap

try:
    from .test_bridge import FakeBackend, FakeCapture, event, wait_for
    from .core_fakes import make_core
except ImportError:
    from test_bridge import FakeBackend, FakeCapture, event, wait_for
    from core_fakes import make_core


class Clock:
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now


class CoreCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.events, self.sounds = [], []
        self.backend = FakeBackend()

    def core(self):
        core = make_core(self.base, self.backend, events=self.events, sounds=self.sounds)
        self.addCleanup(core.close)
        return core

    def annotations(self):
        return [value for kind, value in self.events if kind == 'annotate']

    def notices(self):
        return [value for kind, value in self.events if kind == 'notice']


class FreshInstallKey8Tests(CoreCase):
    def test_shipped_defaults_bind_key8_press_and_release(self):
        keymap = Keymap(self.base / 'runtime' / 'bindings.json')  # never written
        self.assertEqual(keymap.hold_slot('baseus', 0x68), ('airmouse_ctrl', True))
        self.assertEqual(keymap.hold_slot('baseus', 0x69), ('airmouse_ctrl', False))
        binding = keymap.bindings('baseus')['airmouse_ctrl']
        self.assertTrue(binding['default'])
        self.assertEqual((binding['fire'], binding['ignore']), (['68'], ['69']))
        self.assertIn('⑧', binding['label'])
        # ⑤ and the wheel keys (the volume keys' vendor codes) are untouched.
        self.assertEqual(keymap.hold_slot('baseus', 0x72), ('airmouse_activity', True))
        self.assertEqual(keymap.vendor('baseus', 0x78), 'airmouse_wheel_up')

    def test_fresh_core_key8_long_press_draws_in_present_mode(self):
        core = self.core()
        self.assertFalse((self.base / 'runtime' / 'bindings.json').exists(), 'a fresh install')
        core.set_mode('present')
        # Measured ⑧ long press: 68 (+ Ctrl), 00, ... held ..., 69 (+ Esc), 00.
        core.on_hid(event(0x68))
        core.on_hid(event(0x00))
        self.assertEqual(self.annotations(), [{'action': 'draw', 'down': True}])
        core.annotate_at -= 1.0  # held for a second
        core.on_hid(event(0x69))
        core.on_hid(event(0x00))
        self.assertEqual(self.annotations(), [{'action': 'draw', 'down': True},
                                              {'action': 'draw', 'down': False}])
        self.assertIsNone(core.annotate_at)
        self.assertFalse(core.airmouse.ctrl, 'present mode never holds Ctrl')
        time.sleep(.05)
        self.assertNotIn('start', self.backend.calls, '⑧ is not a voice key')

    def test_fresh_core_key8_short_press_undoes_the_last_stroke(self):
        core = self.core()
        core.set_mode('present')
        for code in (0x68, 0x00, 0x69, 0x00):
            core.on_hid(event(code))
        self.assertEqual(self.annotations(), [{'action': 'draw', 'down': True},
                                              {'action': 'undo', 'down': False}])

    def test_fresh_core_key8_outside_present_mode_is_hold_ctrl(self):
        core = self.core()
        core.on_hid(event(0x68))
        self.assertTrue(core.airmouse.ctrl, 'Ctrl for multi-select clicks')
        core.on_hid(event(0x00))
        core.on_hid(event(0x69))
        self.assertFalse(core.airmouse.ctrl)
        self.assertEqual(self.annotations(), [])

    def test_a_key8_binding_learned_before_still_loads_as_it_was(self):
        runtime = self.base / 'runtime'
        runtime.mkdir(parents=True)
        (runtime / 'bindings.json').write_text(json.dumps({'version': 1, 'devices': {'baseus': {
            'airmouse_ctrl': {'source': 'vendor', 'fire': ['68'], 'ignore': ['69'],
                              'gesture': 'single', 'label': '自定义 68'}}}}), encoding='utf-8')
        core = self.core()
        binding = core.keymap.bindings('baseus')['airmouse_ctrl']
        self.assertEqual((binding['label'], binding['default']), ('自定义 68', False))
        core.set_mode('present')
        core.on_hid(event(0x68))
        core.annotate_at -= 1.0
        core.on_hid(event(0x69))
        self.assertEqual([a['action'] for a in self.annotations()], ['draw', 'draw'])


    def test_recording_key8_through_the_core_skips_a_stray_code(self):
        core = self.core()
        core.binding_learn('baseus', 'airmouse_ctrl')
        for code in (0x67, 0x00, 0x68, 0x00):  # a stray code, then ⑧ pressed and held
            core.on_hid(event(code))
        self.assertIsNotNone(core.keymap.learning, 'no pair saved yet')
        core.keymap.learning['hold'][0] = ('68', core.keymap.clock() - 1)
        core.on_hid(event(0x69))  # ⑧ let go
        core.on_hid(event(0x00))
        binding = core.keymap.bindings('baseus')['airmouse_ctrl']
        self.assertEqual((binding['fire'], binding['ignore']), (['68'], ['69']))
        core.on_hid(event(0x68))
        self.assertTrue(core.airmouse.ctrl)
        core.on_hid(event(0x00))
        self.assertTrue(core.airmouse.ctrl, '⑧ still held: the neutral report after its press is no release')
        core.on_hid(event(0x69))
        self.assertFalse(core.airmouse.ctrl)

    def test_a_hold_key_without_a_release_code_lets_go_on_its_neutral_report(self):
        core = self.core()
        core.binding_set('baseus', 'airmouse_ctrl', {'source': 'vendor', 'fire': ['6e'], 'label': '自定义 6E'})
        core.on_hid(event(0x6e))
        self.assertTrue(core.airmouse.ctrl)
        core.on_hid(event(0x00))
        self.assertFalse(core.airmouse.ctrl)


class LostReleaseTests(CoreCase):
    def test_a_lost_key8_release_does_not_leave_the_annotation_pen_down(self):
        core = self.core()
        core.set_mode('present')
        core.on_hid(event(0x68))
        core.on_hid(event(0x00))
        core._expire_holds()
        self.assertEqual(len(self.annotations()), 1, 'a normal hold keeps drawing')
        core.annotate_at -= 120  # ...and the 69 never came
        core._expire_holds()
        self.assertEqual(self.annotations()[-1], {'action': 'draw', 'down': False})
        self.assertIsNone(core.annotate_at)
        core.on_hid(event(0x69))  # a late release only repeats "pen up"
        self.assertEqual(self.annotations()[1:], [{'action': 'draw', 'down': False}] * 2)

    def test_a_lost_key8_release_does_not_leave_ctrl_on_every_click(self):
        core = self.core()
        core.on_hid(event(0x68))
        core._expire_holds()
        self.assertTrue(core.airmouse.ctrl)
        core.airmouse.ctrl_at -= 120
        core._expire_holds()
        self.assertFalse(core.airmouse.ctrl)

    def test_a_release_without_its_press_is_harmless(self):
        # key log 12510.297: the first ⑧ press after an hour asleep arrived as 69 only.
        core = self.core()
        core.set_mode('present')
        core.on_hid(event(0x69))
        core.on_hid(event(0x00))
        self.assertEqual(self.annotations(), [{'action': 'draw', 'down': False}])
        self.assertFalse(core.airmouse.ctrl)


class LatchDesyncTests(CoreCase):
    def armed_core(self):
        core = self.core()
        core.controller.command('arm')
        wait_for(lambda: core.controller.phase == 'idle')
        return core

    def test_short_stop_while_idle_says_press_again_and_starts_nothing(self):
        # key log 3394.391: after 37 min asleep the first ⑥ report was 75 - the 74 that
        # switched the pen microphone on had been lost - so that press did nothing.
        core = self.armed_core()
        core.on_hid(event(0x75))
        core.on_hid(event(0x00))
        wait_for(lambda: MIC_OFF_NOTICE in self.notices())
        wait_for(lambda: 'stop' in self.sounds)
        self.assertEqual(core.controller.phase, 'idle')
        self.assertNotIn('start', self.backend.calls, 'the pen microphone is off: never record silence')
        core.on_hid(event(0x74))  # the next press switches it on again and records
        core.on_hid(event(0x00))
        wait_for(lambda: core.controller.phase == 'recording')

    def test_a_normal_short_session_shows_no_such_notice(self):
        core = self.armed_core()
        for code in (0x74, 0x00):
            core.on_hid(event(code))
        wait_for(lambda: core.controller.phase == 'recording')
        for code in (0x75, 0x00):
            core.on_hid(event(code))
        wait_for(lambda: core.controller.phase == 'processing')
        time.sleep(.1)
        self.assertNotIn(MIC_OFF_NOTICE, self.notices())

    def test_a_hold_release_while_idle_stays_quiet(self):
        core = self.armed_core()
        core.on_hid(event(0x77))
        core.on_hid(event(0x00))
        time.sleep(.15)
        self.assertNotIn(MIC_OFF_NOTICE, self.notices())
        self.assertNotIn('stop', self.sounds)

    def test_quick_note_off_while_idle_says_press_again(self):
        core = self.armed_core()
        core.on_hid(event(0x7b))
        core.on_hid(event(0x00))
        wait_for(lambda: QUICK_MIC_OFF_NOTICE in self.notices())
        self.assertIn('stop', self.sounds)
        time.sleep(.05)
        self.assertNotIn('start', self.backend.calls)

    def test_a_refused_quick_note_is_no_lost_start(self):
        # Review 2026-10-14: ⑦ with the hotkey bridge is refused (it cannot keep a note); the
        # 7b that ends that attempt used to add "the start signal was probably lost".
        class Typing(FakeBackend):
            external, name = True, '闪电说'
        self.backend = Typing()
        core = self.armed_core()
        core.on_hid(event(0x7a))
        core.on_hid(event(0x00))
        wait_for(lambda: EXTERNAL_NOTES_NOTICE in self.notices())
        core.on_hid(event(0x7b))
        core.on_hid(event(0x00))
        time.sleep(.15)
        self.assertNotIn(QUICK_MIC_OFF_NOTICE, self.notices())
        core.on_hid(event(0x7b))  # a later lone 7b is a lost start again
        core.on_hid(event(0x00))
        wait_for(lambda: QUICK_MIC_OFF_NOTICE in self.notices())

    def test_a_short_press_refused_as_busy_is_no_lost_start(self):
        # OpenLess was busy with another device: the 74 was refused (said so), the pen's
        # microphone latched on anyway, and the 75 that switches it off is no lost start.
        class Busy(FakeBackend):
            busy = True

            def start(self):
                if self.busy:
                    self.busy = False
                    raise BackendBusy()
                super().start()
        self.backend = Busy()
        core = self.armed_core()
        for code in (0x74, 0x00):
            core.on_hid(event(code))
        wait_for(lambda: OTHER_DEVICE_NOTICE in self.notices())
        for code in (0x75, 0x00):
            core.on_hid(event(code))
        time.sleep(.15)
        self.assertNotIn(MIC_OFF_NOTICE, self.notices())
        for code in (0x74, 0x00):  # the next press records
            core.on_hid(event(code))
        wait_for(lambda: core.controller.phase == 'recording')
        for code in (0x75, 0x00):
            core.on_hid(event(code))
        wait_for(lambda: core.controller.phase == 'processing')
        self.backend.incoming.put({'type': 'result', 'text': '好'})
        wait_for(lambda: core.controller.phase == 'idle')
        for code in (0x75, 0x00):  # and a lone 75 later is a lost start again
            core.on_hid(event(code))
        wait_for(lambda: MIC_OFF_NOTICE in self.notices())

    def test_a_quick_note_pressed_while_recognising_says_wait_and_is_no_lost_start(self):
        # Review 2026-10-14: ⑦ pressed while the last ⑥ recording was still being recognised
        # was dropped without a word, and the 7b after it then blamed a lost start signal.
        core = self.armed_core()
        for code in (0x74, 0x00):
            core.on_hid(event(code))
        wait_for(lambda: core.controller.phase == 'recording')
        for code in (0x75, 0x00):
            core.on_hid(event(code))
        wait_for(lambda: core.controller.phase == 'processing')
        core.on_hid(event(0x7a))
        core.on_hid(event(0x00))
        wait_for(lambda: BUSY_NOTICE in self.notices())
        self.backend.incoming.put({'type': 'result', 'text': '好'})
        wait_for(lambda: core.controller.phase == 'idle')
        core.on_hid(event(0x7b))  # switches off the microphone that refused 7a latched on
        core.on_hid(event(0x00))
        time.sleep(.15)
        self.assertNotIn(QUICK_MIC_OFF_NOTICE, self.notices())
        core.on_hid(event(0x7b))  # a later lone 7b is a lost start again
        core.on_hid(event(0x00))
        wait_for(lambda: QUICK_MIC_OFF_NOTICE in self.notices())


class RecordingLimitTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.notifications = []
        self.control = Controller(lambda *value: self.notifications.append(value), self.backend, FakeCapture)
        self.addCleanup(self.control.close)
        self.control.command('arm')
        wait_for(lambda: self.control.phase == 'idle')

    def test_a_note_that_reaches_the_limit_is_saved_not_thrown_away(self):
        # e.g. the stop report was lost at range: the note used to be cancelled at 30 min.
        self.control.set_destination('notes')
        with patch('bridge.controller.NOTE_LIMIT', .3):
            self.control.command('start')
            wait_for(lambda: self.control.phase == 'recording')
            wait_for(lambda: self.control.phase == 'processing', timeout=3)
        self.assertIn('stop', self.backend.calls)
        self.assertNotIn('cancel', self.backend.calls)
        self.assertTrue(any(kind == 'notice' and '正在结束并识别，完成后保存' in value for kind, value in self.notifications))
        self.backend.incoming.put({'type': 'result', 'text': '半小时的讲座'})
        wait_for(lambda: self.control.phase == 'idle')
        results = [value for kind, value in self.notifications if kind == 'result']
        self.assertEqual((results[-1]['text'], results[-1]['destination']), ('半小时的讲座', 'notes'))

    def test_input_that_reaches_the_limit_is_still_cancelled(self):
        # Typing ten minutes of text at whatever has focus by then is not safer.
        with patch('bridge.controller.INPUT_LIMIT', .3):
            self.control.command('start')
            wait_for(lambda: self.control.phase == 'recording')
            wait_for(lambda: self.control.phase == 'error', timeout=3)
        self.assertIn('cancel', self.backend.calls)
        self.assertNotIn('stop', self.backend.calls)


class StallWatchTests(unittest.TestCase):
    def make(self, present=None):
        self.clock = Clock()
        self.checks = []

        def check():
            self.checks.append(self.clock.now)
            if present is not None:
                present()
        return StallWatch(check, clock=self.clock)

    def run_for(self, watch, seconds, step=.05, flowing=False):
        end = self.clock.now + seconds
        while self.clock.now < end - 1e-9:
            self.clock.now += step
            if flowing:
                watch.data()
            watch.check()

    def test_a_brief_stall_with_the_receiver_plugged_in_is_survived(self):
        watch = self.make()
        self.run_for(watch, 5.0)  # no buffer for 5 s: used to cancel the session at 2 s
        self.assertGreaterEqual(len(self.checks), 3, 'presence checked as soon as it stalled, then each second')
        self.assertLess(self.checks[0] - 100.0, STALL_CHECK + .1)
        checks = len(self.checks)
        self.run_for(watch, 8.0, flowing=True)  # buffers again: back to the 10 s re-check
        self.assertEqual(len(self.checks), checks, 'no extra checks while data flows')
        self.run_for(watch, STALL_LIMIT - .5)  # a second, longer stall is survived as well

    def test_unplugging_is_still_caught_within_two_seconds(self):
        def gone():
            raise RuntimeError('这支笔的接收器已拔出')
        watch = self.make(gone)
        with self.assertRaisesRegex(RuntimeError, '已拔出'):
            self.run_for(watch, 3.0)
        self.assertLess(self.clock.now - 100.0, STALL_CHECK + .1)

    def test_a_receiver_that_stays_silent_is_given_up_on(self):
        watch = self.make()
        with self.assertRaisesRegex(RuntimeError, '停止返回数据'):
            self.run_for(watch, STALL_LIMIT + 1)
        self.assertGreater(self.clock.now - 100.0, STALL_LIMIT)
        self.assertLess(self.clock.now - 100.0, STALL_LIMIT + .1)

    def test_identity_is_rechecked_every_ten_seconds_while_data_flows(self):
        watch = self.make()
        for _ in range(250):  # 25 s of steady buffers
            self.clock.now += .1
            watch.data()
            watch.check()
        self.assertEqual(len(self.checks), 2)

    def test_a_failed_enumeration_is_not_taken_for_an_unplugged_receiver(self):
        capture = audio.AudioCapture(lambda pcm: None, lambda message: None)
        with patch('bridge.audio.find_device', side_effect=OSError(122, 'insufficient buffer')):
            capture._check_present()  # no raise
        with patch('bridge.audio.find_device', side_effect=RuntimeError('未连接所选设备：倍思')):
            with self.assertRaisesRegex(RuntimeError, '未连接'):
                capture._check_present()
        parented = audio.AudioCapture(lambda pcm: None, lambda message: None,
                                      target={'id': 'baseus:1', 'parent': r'USB\VID_ABC8&PID_CA08\1'})
        with patch('bridge.instances.node_present', return_value=False):
            with self.assertRaisesRegex(RuntimeError, '已拔出'):
                parented._check_present()


class SlideshowExemptionTests(unittest.TestCase):
    def test_air_mouse_clicks_are_off_in_wps_slideshows_too(self):
        mouse = airmouse.AirMouse(lambda: {}, lambda: {})
        self.assertIs(mouse.presenting, airmouse.is_slideshow_foreground)
        seen = []
        with patch.object(airmouse, 'is_presentation_foreground', lambda processes: seen.append(processes) or True):
            self.assertTrue(airmouse.is_slideshow_foreground())
        self.assertIn('wps.exe', seen[0])
        self.assertIn('powerpnt.exe', seen[0])


if __name__ == '__main__':
    unittest.main()
