"""Pen shortcut guard, 随拿随用 busy handling and the 0.9.7 reliability fixes.

Keyboard-hook logic runs on AirMouse.handle with fake clocks and fake Raw Input
reports; no hook is installed and nothing is injected into Windows.
"""
import ctypes as c
import io
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave

from bridge import airmouse, rawinput
from bridge.airmouse import MODIFIERS, VK_MASK, WM_KEYDOWN, WM_KEYUP
from bridge.backend import BackendBusy
from bridge.controller import OTHER_DEVICE_NOTICE
from bridge.prefs import Preferences
from bridge.sounds import LEAD, RATE, Sounds, render
from bridge.upstream import UpstreamWatch

try:
    from .test_bridge import FakeBackend, event, wait_for, PATH
    from .core_fakes import make_core
except ImportError:
    from test_bridge import FakeBackend, event, wait_for, PATH
    from core_fakes import make_core

VK_LCTRL, VK_LSHIFT, VK_LALT, VK_LWIN = 0xA2, 0xA0, 0xA4, 0x5B
VK_P, VK_O, VK_F5, VK_TAB, VK_ENTER, VK_ESC = 0x50, 0x4F, 0x74, 0x09, 0x0D, 0x1B
VK_B, VK_E = 0x42, 0x45
VK_PRIOR, VK_NEXT, VK_VOLUME_UP = 0x21, 0x22, 0xAF
RAW = {VK_LCTRL: 0x11, VK_LSHIFT: 0x10, VK_LALT: 0x12}  # Raw Input reports generic VKs
KBD_PATH = r'\\?\HID#VID_ABC8&PID_CA08&MI_00&COL01#8&1&0&0000#{884B96C3-56EF-11D1-BC8C-00A0C91405DD}'.upper()


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class GuardHarness(unittest.TestCase):
    VKS = {'wheel_up': {VK_VOLUME_UP}}

    def make(self, slideshow=False, app=False, wait=0.0, remap=False, clock=None):
        self.clock = clock or Clock()
        self.state = {'slideshow': slideshow, 'app': app}
        self.down_mods = set()
        self.injected, self.clicks = [], []
        self.guard = airmouse.ShortcutGuard(
            clock=self.clock, slideshow=lambda: self.state['slideshow'],
            presentation_app=lambda: self.state['app'], wait=wait,
            pressed=lambda name: name in self.down_mods)
        self.guard.enabled = True
        self.mouse = airmouse.AirMouse(lambda: {'skip_slideshow': True},
                                       lambda: self.VKS, clock=self.clock,
                                       sender=lambda *args: self.clicks.append(args),
                                       presenting=lambda: False, guard=self.guard,
                                       injector=self.injected.append)
        self.mouse.spawn = lambda work: work()
        self.mouse.remap_enabled = remap
        self.mouse.refresh()
        return self.mouse

    def press(self, vk, pen=None):
        """The hook sees a key first; Raw Input reports it afterwards, if it got through."""
        swallowed = self.mouse.handle(vk, WM_KEYDOWN, False)
        if vk in MODIFIERS and not swallowed:
            self.down_mods.add(MODIFIERS[vk])
        if pen is not None and not swallowed:
            self.guard.raw_key(RAW.get(vk, vk), True, pen)
        return swallowed

    def release(self, vk, pen=None):
        swallowed = self.mouse.handle(vk, WM_KEYUP, False)
        if vk in MODIFIERS:
            self.down_mods.discard(MODIFIERS[vk])
        if pen is not None and not swallowed:
            self.guard.raw_key(RAW.get(vk, vk), False, pen)
        return swallowed


class ShortcutGuardTests(GuardHarness):
    def test_pens_ctrl_p_outside_a_slideshow_is_swallowed_with_its_release(self):
        self.make()
        self.assertFalse(self.press(VK_LCTRL, pen=True), 'the modifier itself always passes')
        self.assertTrue(self.press(VK_P), '⑧ no longer opens Print')
        self.assertTrue(self.press(VK_P), 'auto-repeat follows the press')
        self.assertTrue(self.release(VK_P))
        self.assertFalse(self.release(VK_LCTRL, pen=True))
        self.assertEqual(self.injected, [], 'Ctrl alone needs no mask')
        self.assertFalse(self.press(VK_P), 'a plain P afterwards is typed')
        self.assertFalse(self.release(VK_P))

    def test_real_keyboard_ctrl_p_always_passes_even_while_the_pen_is_busy(self):
        self.make()
        self.guard.pen_activity()
        self.assertFalse(self.press(VK_LCTRL, pen=False))
        self.assertFalse(self.press(VK_P))
        self.assertFalse(self.release(VK_P))
        self.assertFalse(self.press(VK_O), 'the same Ctrl hold stays decided')
        self.release(VK_O)
        self.release(VK_LCTRL, pen=False)

    def test_slideshow_keeps_the_native_shortcut_and_powerpoint_edit_view_keeps_f5(self):
        self.make(slideshow=True, app=True)
        self.press(VK_LCTRL, pen=True)
        self.assertFalse(self.press(VK_P), 'the slideshow pen')
        self.release(VK_P)
        self.release(VK_LCTRL)
        self.state['slideshow'] = False  # PowerPoint, editing
        self.press(VK_LCTRL, pen=True)
        self.assertTrue(self.press(VK_P), 'Print in the editor is still the bug')
        self.release(VK_P)
        self.release(VK_LCTRL)
        self.press(VK_LSHIFT, pen=True)
        self.assertFalse(self.press(VK_F5), '③ long press starts the show from this slide')
        self.release(VK_F5)
        self.release(VK_LSHIFT)

    def test_pens_shift_f5_is_masked_and_its_trailing_win_tap_swallowed(self):
        self.make()
        self.assertFalse(self.press(VK_LSHIFT, pen=True))
        self.assertTrue(self.press(VK_F5))
        self.assertEqual(self.injected, [[(VK_MASK, 0, False, False), (VK_MASK, 0, True, False)]],
                         'Shift released alone would switch the IME')
        self.assertTrue(self.release(VK_F5))
        self.assertFalse(self.release(VK_LSHIFT), 'a modifier release is never held back')
        self.clock.now += .2
        self.assertTrue(self.press(VK_LWIN), 'no Start menu')
        self.assertTrue(self.release(VK_LWIN))
        self.assertFalse(self.release(VK_ENTER), "the pen's stray Enter-up is harmless")
        self.clock.now += 2
        self.assertFalse(self.press(VK_LWIN), 'later the Win key is the Win key')
        self.assertFalse(self.release(VK_LWIN))
        self.assertEqual(len(self.injected), 1)

    def test_page_tab_volume_and_win_alt_tab_stay_native_mid_burst(self):
        self.make()
        self.guard.pen_activity()
        for vk in (VK_PRIOR, VK_NEXT, VK_TAB, VK_VOLUME_UP):
            self.assertFalse(self.press(vk), hex(vk))
            self.assertFalse(self.release(vk))
        # ① long press: Win + Alt + Tab from the receiver.
        self.assertFalse(self.press(VK_LWIN, pen=True))
        self.assertFalse(self.press(VK_LALT, pen=True))
        self.assertFalse(self.press(VK_TAB))
        self.assertFalse(self.release(VK_TAB))
        self.assertFalse(self.release(VK_LALT))
        self.assertFalse(self.release(VK_LWIN))
        self.assertEqual(self.injected, [])

    def test_plain_f5_needs_the_pen_a_moment_ago(self):
        self.make()
        self.assertFalse(self.press(VK_F5), 'refresh from a real keyboard')
        self.release(VK_F5)
        self.guard.pen_activity()
        self.clock.now += .1
        self.assertTrue(self.press(VK_F5))
        self.assertTrue(self.release(VK_F5))
        self.clock.now += 1
        self.assertFalse(self.press(VK_F5))
        self.release(VK_F5)

    def test_without_a_report_the_hook_falls_back_once_per_modifier_press(self):
        self.make(wait=0)  # the report never comes
        self.press(VK_LCTRL)
        self.assertFalse(self.press(VK_P), 'no pen activity: a real Ctrl+P')
        self.release(VK_P)
        self.release(VK_LCTRL)
        self.guard.pen_activity()
        self.press(VK_LCTRL)
        self.assertTrue(self.press(VK_P), 'vendor code a moment ago: the pen')
        self.release(VK_P)
        self.release(VK_LCTRL)

    def test_disabled_guard_and_injected_keys_pass_but_pairs_are_kept(self):
        self.make()
        self.press(VK_LCTRL, pen=True)
        self.assertFalse(self.mouse.handle(VK_P, WM_KEYDOWN, True), 'injected')
        self.mouse.handle(VK_P, WM_KEYUP, True)
        self.assertTrue(self.press(VK_P))
        self.guard.enabled = False
        self.assertTrue(self.release(VK_P), 'switched off mid-press: the release still pairs')
        self.assertFalse(self.press(VK_O))
        self.release(VK_O)

    def test_a_stale_modifier_never_turns_a_letter_into_a_combo(self):
        self.make()
        self.press(VK_LCTRL, pen=True)
        self.down_mods.discard('ctrl')  # another hook ate the release
        self.assertFalse(self.press(VK_P))
        self.release(VK_P)

    def test_a_late_raw_report_is_awaited_briefly(self):
        for pen, expected in ((True, True), (False, False)):
            with self.subTest(pen=pen):
                self.make(wait=3, clock=time.perf_counter)  # generous: a loaded test machine
                self.assertFalse(self.mouse.handle(VK_LCTRL, WM_KEYDOWN, False))
                self.down_mods.add('ctrl')
                timer = threading.Timer(.03, self.guard.raw_key, (0x11, True, pen))
                timer.start()
                started = time.perf_counter()
                self.assertEqual(self.press(VK_P), expected)
                self.assertLess(time.perf_counter() - started, 2.5, 'woken by the report, not the timeout')
                timer.join()
                self.release(VK_P)
                self.release(VK_LCTRL)

    def test_airmouse_mute_masks_shift_and_a_guard_only_hook_never_clicks(self):
        self.make(remap=True)  # input / notes mode: no recent ⑤ use needed
        self.press(VK_LSHIFT, pen=True)
        self.assertTrue(self.press(VK_F5), "the ③ long press's Shift+F5")
        self.assertEqual(len(self.injected), 1, 'Shift is masked here too')
        self.release(VK_F5)
        self.release(VK_LSHIFT)
        self.make(remap=False)
        self.mouse.thread = type('Alive', (), {'is_alive': lambda self: True})()
        self.mouse.press()
        self.assertFalse(self.press(VK_PRIOR), 'the guard alone never remaps')
        self.release(VK_PRIOR)
        self.assertFalse(self.mouse.begin_drag(), 'nor grabs the button')
        self.assertEqual(self.clicks, [])

    def test_win_pressed_before_the_mute_window_is_released_inside_it(self):
        self.make(remap=True)
        self.assertFalse(self.press(VK_LWIN))
        self.guard.pen_activity()  # the pen's vendor code: its plain F5 follows
        self.assertTrue(self.press(VK_F5), 'opens the ③ mute window')
        self.release(VK_F5)
        self.assertFalse(self.release(VK_LWIN), 'its release goes through: Win never sticks')


class BareLetterTests(GuardHarness):
    """The pen's single-key slideshow shortcuts, measured 2026-10-12: a ④ long press types
    B (black screen) with vendor 6D - the B first, 6D in the same 15 ms tick or one later -
    and another key types E with 67 (67 first)."""

    def test_key4_long_press_b_is_swallowed_when_its_6d_follows(self):
        self.make(wait=3, clock=time.perf_counter)  # generous: a loaded test machine
        late = threading.Timer(.03, self.guard.pen_activity, (0x6D,))
        late.start()
        started = time.perf_counter()
        self.assertTrue(self.press(VK_B), 'no "b" typed into the document')
        self.assertLess(time.perf_counter() - started, 2.5, 'woken by 6D, not the timeout')
        late.join()
        self.assertTrue(self.press(VK_B), 'auto-repeat follows the press')
        self.assertTrue(self.release(VK_B), 'and so does the release')
        self.assertEqual(self.injected, [], 'no modifier was held: nothing to mask')
        self.assertFalse(self.press(VK_P), 'other letters are typed')
        self.release(VK_P)

    def test_a_letter_goes_only_with_its_own_vendor_code(self):
        self.make()
        self.guard.pen_activity(0x67)  # 67, then E
        self.assertTrue(self.press(VK_E))
        self.assertTrue(self.release(VK_E))
        self.assertFalse(self.press(VK_B), '67 says nothing about a B')
        self.release(VK_B)
        self.clock.now += 1
        self.guard.pen_activity(0x71)  # a ⑤ click, then typing at once
        for vk in (VK_E, VK_B, VK_O):
            self.assertFalse(self.press(vk), hex(vk))
            self.release(vk)
        self.guard.pen_activity(0x6D)
        self.clock.now += 1
        self.assertFalse(self.press(VK_B), 'a 6D a second ago is not this B')
        self.release(VK_B)

    def test_typing_on_a_real_keyboard_is_never_held_up_or_swallowed(self):
        self.make(wait=3, clock=time.perf_counter)
        self.press(VK_O)
        self.release(VK_O)
        started = time.perf_counter()
        self.assertFalse(self.press(VK_B), 'a b in the middle of typing')
        self.assertLess(time.perf_counter() - started, 1, 'is not held for the 6D wait')
        self.release(VK_B)
        self.make(wait=.05, clock=time.perf_counter)  # after a quiet spell: a short wait, then typed
        self.assertFalse(self.press(VK_B))
        self.release(VK_B)
        self.make()
        self.guard.pen_activity(0x6D)
        self.press(VK_LSHIFT, pen=False)  # Shift+B: a capital, never the pen's
        self.assertFalse(self.press(VK_B))
        self.release(VK_B)
        self.release(VK_LSHIFT, pen=False)

    def test_slideshows_present_mode_and_a_switched_off_guard_keep_the_b(self):
        for setup in ('slideshow', 'present mode', 'guard off'):
            with self.subTest(setup):
                self.make(slideshow=setup == 'slideshow')
                self.guard.letters = setup != 'present mode'
                self.guard.enabled = setup != 'guard off'
                self.guard.pen_activity(0x6D)
                self.assertFalse(self.press(VK_B), 'B = black screen')
                self.assertFalse(self.release(VK_B))


    def test_a_switched_off_guard_still_keeps_the_b_out_of_a_right_click(self):
        # Review 2026-10-14: with pen.guard_shortcuts off, ④'s long press typed a "b" into the
        # field and then right-clicked. The B paired with a right click goes regardless.
        self.make(remap=True)          # air mouse on, input / notes mode: ④ long = right click
        self.guard.enabled = False
        self.guard.pen_activity(0x6D)
        self.assertTrue(self.press(VK_B))
        self.assertTrue(self.release(VK_B))
        self.assertFalse(self.press(VK_P), 'the guard itself stays off')
        self.release(VK_P)
        self.press(VK_LCTRL, pen=True)
        self.assertFalse(self.press(VK_P), "its Ctrl+P is the user's choice now")
        self.release(VK_P)
        self.release(VK_LCTRL)
        for setup in ('present mode', 'not bound', 'air mouse off'):
            with self.subTest(setup):   # no right click: the B is the pen's own key (black screen)
                self.make(remap=setup != 'air mouse off')
                self.guard.enabled = False
                self.mouse.set_buttons(setup != 'present mode')
                if setup == 'not bound':
                    self.mouse.right_click_bound = lambda: False
                self.guard.pen_activity(0x6D)
                self.assertFalse(self.press(VK_B))
                self.assertFalse(self.release(VK_B))


class RawForwardTests(unittest.TestCase):
    def feed(self, monitor, device, vk, path):
        header_size = c.sizeof(rawinput.Header)
        payload = struct.pack('<HHHHII', 0x1D, 0, 0, vk, 0x100, 0)

        def fake(handle, command, target, size, _):
            if command == rawinput.RID_HEADER:
                header = target._obj
                header.kind, header.device = 1, device
                return header_size
            if target is None:
                size._obj.value = header_size + len(payload)
                return 0
            c.memmove(target, bytes(header_size) + payload, header_size + len(payload))
            return header_size + len(payload)

        monitor.known = {device: path} if device else {}
        with patch.object(rawinput.user32, 'GetRawInputData', fake):
            monitor._input(1)

    def test_other_keyboards_report_modifiers_only_and_the_receiver_everything(self):
        events, foreign = [], []
        monitor = rawinput.RawMonitor(events.append)
        monitor.on_foreign_key = lambda vk, down: foreign.append((vk, down))
        self.feed(monitor, 7, 0x11, r'\\?\HID#VID_046D&PID_C31C#LAPTOP')
        self.feed(monitor, 7, 0x50, r'\\?\HID#VID_046D&PID_C31C#LAPTOP')  # a letter: never read out
        self.feed(monitor, None, 0x11, '')  # injected input has no device
        self.assertEqual(foreign, [(0x11, True)])
        self.assertEqual(events, [])
        self.feed(monitor, 8, 0x50, KBD_PATH)
        self.assertEqual([e['keyboard']['vk'] for e in events], [0x50])
        monitor.on_foreign_key = None
        self.feed(monitor, 7, 0x10, r'\\?\HID#VID_046D&PID_C31C#LAPTOP')
        self.assertEqual(foreign, [(0x11, True)], 'off with the guard')


def found(*paths):
    return [{'path': p.upper(), 'kind': 2, 'page': 0xff03, 'usage': 0} for p in paths]


class CoreGuardTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.events, self.sounds = [], []
        self.backend = FakeBackend()
        self.core = make_core(Path(folder.name), self.backend, events=self.events, sounds=self.sounds)
        self.addCleanup(self.core.close)
        self.hook = []
        # Never install a real hook here: record what the core asks for instead.
        patcher = patch.multiple(self.core.airmouse, start=lambda: self.hook.append('start'),
                                 stop=lambda: self.hook.append('stop'))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_hook_runs_for_the_guard_alone_and_follows_the_switch(self):
        core = self.core
        core.prefs_set({'airmouse': {'enabled': False}})
        core._devices_changed(found(PATH))
        self.assertEqual(self.hook[-1], 'start')
        self.assertTrue(core.guard.enabled)
        self.assertFalse(core.airmouse.remap_enabled, 'the air-mouse switch is still off')
        self.assertEqual(core.airmouse.watch, set())
        self.assertIsNotNone(core.monitor.on_foreign_key)
        core.prefs_set({'pen': {'guard_shortcuts': False}})
        self.assertEqual(self.hook[-1], 'stop')
        self.assertFalse(core.guard.enabled)
        self.assertIsNone(core.monitor.on_foreign_key)
        core.prefs_set({'pen': {'guard_shortcuts': True}})
        core._devices_changed([])  # no receiver: nothing to guard
        self.assertEqual(self.hook[-1], 'stop')

    def test_receiver_keys_and_vendor_codes_reach_the_guard(self):
        core = self.core
        core.on_hid({'kind': 1, 'path': KBD_PATH, 'keyboard': {'vk': 0x11, 'down': True}})
        self.assertEqual(core.guard.raw['ctrl'][0], 'pen')
        core.guard.pen_at = float('-inf')
        core.on_hid(event(0x68))
        self.assertGreater(core.guard.pen_at, 0)

    def test_unplugging_mid_press_forgets_the_code_and_releases_held_state(self):
        core = self.core
        core._devices_changed(found(PATH))
        core.set_mode('present')
        core.on_hid(event(0x6a))  # ③ pressed... and the receiver pulled before its 00
        self.assertIn(PATH, core.last_codes)
        core.on_hid(event(0x72))  # ⑤ held: the laser shows
        self.assertTrue(core.pointer_down)
        core._devices_changed([])
        self.assertEqual(core.last_codes, {})
        self.assertEqual(core.controller.last_codes, {})
        self.assertFalse(core.pointer_down, 'no laser left on screen')
        self.assertFalse(core.airmouse.holding)
        self.assertFalse(core.airmouse.pointing(), 'a real volume key right after is a volume key')
        core._devices_changed(found(PATH))
        before = sum(1 for entry in core.key_log if entry[3] == '6A')
        core.on_hid(event(0x6a))
        self.assertEqual(sum(1 for entry in core.key_log if entry[3] == '6A'), before + 1,
                         'the first press after re-plugging counts')

    def test_a_lost_key5_release_does_not_leave_the_laser_on(self):
        core = self.core
        core.set_mode('present')
        core.on_hid(event(0x72))
        core.on_hid(event(0x00))
        core._expire_pointer()
        self.assertTrue(core.pointer_down, 'a normal hold keeps pointing')
        core.airmouse.last_activity -= 120  # ...and the 73 never came
        core._expire_pointer()
        self.assertFalse(core.pointer_down)
        self.assertEqual([v for k, v in self.events if k == 'pointer'][-1]['active'], False)

    def test_a_key5_tap_ends_a_hold_whose_release_was_lost(self):
        core = self.core
        core.set_mode('present')
        core.on_hid(event(0x72))
        core.on_hid(event(0x00))   # ...its 73 is lost
        self.assertTrue(core.pointer_down and core.airmouse.holding)
        core.on_hid(event(0x71))   # a tap: ⑤ is not held
        self.assertFalse(core.pointer_down, 'the laser goes at once, not after 90 s')
        self.assertFalse(core.airmouse.holding)

    def test_the_right_click_follows_its_binding(self):
        core = self.core
        self.assertTrue(core.airmouse.right_click_bound())
        core.binding_set('baseus', 'right_click', None)
        self.assertFalse(core.airmouse.right_click_bound(), '④ keeps its own B: nothing to swallow')

    def test_a_failing_key_action_is_logged(self):
        def broken():
            raise ValueError('key action failed')
        self.core._submit(broken)
        log = Path(self.core.runtime) / 'ui-errors.log'
        wait_for(lambda: log.exists() and 'key action failed' in log.read_text(encoding='utf-8'))


class BusyBackend(FakeBackend):
    def __init__(self):
        super().__init__()
        self.busy = True

    def start(self):
        self.calls.append('start')
        if self.busy:
            raise BackendBusy('OpenLess 正忙')


class CoexistSessionTests(unittest.TestCase):
    def make(self, backend):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.events, self.sounds = [], []
        core = make_core(Path(folder.name), backend, events=self.events, sounds=self.sounds)
        self.addCleanup(core.close)
        core.controller.command('arm')
        wait_for(lambda: core.controller.phase == 'idle')
        return core

    def test_openless_busy_is_a_friendly_notice_not_an_error(self):
        backend = BusyBackend()
        core = self.make(backend)
        core.on_hid(event(0x76))  # ⑥ while Tidepool is talking
        wait_for(lambda: ('notice', OTHER_DEVICE_NOTICE) in self.events)
        wait_for(lambda: core.controller.phase == 'idle')
        phases = [v['phase'] for k, v in self.events if k == 'state']
        self.assertNotIn('error', phases)
        self.assertNotIn('close', backend.calls, 'the connection stays up')
        self.assertIn('error', self.sounds)
        core.on_hid(event(0x77))
        core.on_hid(event(0))
        backend.busy = False
        core.on_hid(event(0x76))
        wait_for(lambda: core.controller.phase == 'recording')
        self.assertEqual(backend.calls.count('connect'), 1)

    def test_empty_input_result_plays_no_success_chime(self):
        backend = FakeBackend()
        core = self.make(backend)
        core.controller.command('start')
        wait_for(lambda: core.controller.phase == 'recording')
        core.controller._audio(b'\x01\x00' * 800)
        core.controller.command('stop')
        wait_for(lambda: core.controller.phase == 'processing')
        backend.incoming.put({'type': 'result', 'text': '  '})
        wait_for(lambda: core.controller.phase == 'idle')
        wait_for(lambda: 'disarmed' in self.sounds)
        self.assertNotIn('done_input', self.sounds)
        self.assertTrue(core.result['empty_text'])
        self.assertTrue(any(k == 'notice' and 'OpenLess' in v for k, v in self.events))

    def test_mode_switch_skips_the_controller_when_the_destination_stays(self):
        core = self.make(FakeBackend())
        with patch.object(core.controller, 'set_destination', side_effect=AssertionError('no round trip')):
            self.assertEqual(core.set_mode('present')['value'], 'present')
            self.assertEqual(core.set_mode('input')['value'], 'input')
        self.assertEqual(self.sounds[-2:], ['mode_present', 'mode_input'])
        self.assertEqual(core.prefs.get('mode'), 'input')

    def test_destination_change_while_disconnected_never_waits_for_the_session_thread(self):
        core = self.make(FakeBackend())
        core.controller.command('disarm')
        wait_for(lambda: core.controller.phase == 'disconnected' and core.controller.capture is None)
        gate = threading.Event()
        core.controller.commands.put(('destination', ('input', 'idea', _Blocker(gate))))
        started = time.monotonic()
        try:
            self.assertEqual(core.set_mode('notes')['value'], 'notes')
        finally:
            gate.set()
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(core.controller.destination, 'notes')


class _Blocker:
    """A queued destination command whose completion stalls the session thread."""
    def __init__(self, gate):
        self.gate = gate

    def cancelled(self):
        self.gate.wait(3)
        return True


class ReliabilityTests(unittest.TestCase):
    def test_cues_have_no_lead_in_while_the_device_is_kept_awake(self):
        with tempfile.TemporaryDirectory() as folder:
            sounds = Sounds(folder, lambda: {'enabled': True, 'volume': 'low'}, player=lambda path: None)
            loose = sounds._path('tool', 'low')
            sounds.awake = lambda: True
            tight = sounds._path('tool', 'low')
            self.assertNotEqual(loose.parent, tight.parent)
            frames = []
            for path in (loose, tight):
                with wave.open(str(path)) as file:
                    frames.append(file.getnframes())
            self.assertEqual(frames[0] - frames[1], int(LEAD * RATE))
            sounds.awake = lambda: 1 / 0  # a broken probe falls back to the safe lead-in
            self.assertEqual(sounds._path('tool', 'low'), loose)
        with wave.open(io.BytesIO(render('tool', 'low', 0))) as file:
            self.assertGreater(file.getnframes(), 0)

    def test_the_other_variant_is_cut_from_the_cache_instead_of_rendered(self):
        with tempfile.TemporaryDirectory() as folder:
            sounds = Sounds(folder, lambda: {'enabled': True, 'volume': 'low'}, player=lambda path: None)
            loose = sounds._path('stop', 'low', False)
            with patch('bridge.sounds.render', side_effect=AssertionError('no second render')):
                tight = sounds._path('stop', 'low', True)
                (Path(folder) / loose.parent.name / 'stop.wav').unlink()
                again = sounds._path('stop', 'low', False)
            self.assertEqual(tight.read_bytes(), render('stop', 'low', 0))
            self.assertEqual(again.read_bytes(), render('stop', 'low'))

    def test_a_preview_model_that_fails_to_load_is_announced_once(self):
        from bridge.preview import LivePreview
        loads = []

        def broken():
            loads.append(1)
            raise RuntimeError('sherpa_onnx missing')
        events = []
        preview = LivePreview('unused', lambda k, v: events.append((k, v)), recognizer_factory=broken)
        self.addCleanup(preview.close)
        for session in ('s1', 's2', 's3'):
            preview.start(session)
            preview.feed(b'\x00\x01' * 800)
            preview.finish()
        wait_for(lambda: preview.jobs.empty())
        time.sleep(.1)
        self.assertEqual(len(loads), 1, 'not reloaded for every recording')
        self.assertEqual([k for k, _ in events], ['preview_error'])

    def test_sound_log_is_written_by_the_ticker_not_by_the_cue(self):
        with tempfile.TemporaryDirectory() as folder:
            sounds = Sounds(Path(folder) / 'sounds', lambda: {'enabled': True, 'volume': 'low'},
                            player=lambda path: None)
            sounds._path = lambda name, volume: Path(folder) / 'x.wav'
            self.assertTrue(sounds.play('tool'))
            log = Path(folder) / 'sound-log.txt'
            self.assertFalse(log.exists())
            sounds.flush_log()
            self.assertIn('tool', log.read_text(encoding='utf-8'))

    def test_ticker_survives_a_failing_step(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        events = []
        core = make_core(Path(folder.name), events=events)
        ticker = threading.Thread(target=core._ticker, daemon=True)
        self.addCleanup(ticker.join, 3)  # (runs after close: no write into a deleted folder)
        self.addCleanup(core.close)
        with patch.object(core.keymap, 'tick', side_effect=RuntimeError('boom')):
            ticker.start()
            wait_for(lambda: sum(1 for k, _ in events if k == 'usage') >= 2, timeout=6)
        log = (Path(folder.name) / 'runtime' / 'ui-errors.log').read_text(encoding='utf-8')
        self.assertEqual(log.count('boom'), 1, 'logged once, not every second')

    def test_damaged_reminder_state_and_rate_limited_release_list(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'watch.json'
            path.write_text('[]', encoding='utf-8')
            watch = UpstreamWatch(path, fetch=lambda url: {'merged_at': '2026-10-01T00:00:00Z'}
                                  if '/pulls/' in url else {'message': 'API rate limit exceeded'})
            self.assertTrue(watch.due())
            self.assertIsNone(watch.check())

    def test_new_prefs_are_validated(self):
        with tempfile.TemporaryDirectory() as folder:
            prefs = Preferences(Path(folder) / 'prefs.json')
            self.assertTrue(prefs.get('devices', 'coexist'))
            self.assertTrue(prefs.get('pen', 'guard_shortcuts'))
            self.assertTrue(prefs.get('appearance', 'live_shortcut'))
            for bad in ({'devices': {'coexist': 1}}, {'pen': {'guard_shortcuts': 'yes'}},
                        {'appearance': {'live_shortcut': None}}, {'pen': {'other': True}}):
                with self.assertRaises(ValueError):
                    prefs.update(bad)
            prefs.update({'appearance': {'live_shortcut': False}, 'devices': {'coexist': False}})
            again = Preferences(Path(folder) / 'prefs.json')
            self.assertFalse(again.get('appearance', 'live_shortcut'))
            self.assertFalse(again.get('devices', 'coexist'))
            self.assertEqual(again.get('appearance', 'theme'), 'system')


if __name__ == '__main__':
    unittest.main()
