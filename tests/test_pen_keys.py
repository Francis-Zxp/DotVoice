"""The pen key scheme (BS-OH085, approved 2026-10-12): no hidden windows, no two-key chords.

- input / notes: a ⑤ tap is a left click at once; the second tap of a double-tap is one
  more click (a Windows double-click). Present mode: single tap nothing, double tap = next tool.
- ③ / ④ short presses are always page keys, also right after ⑤ and while it is held.
- ③ long press (6B) toggles a grab in input / notes, without any recent ⑤ use; the pen's
  Shift+F5 / Win tap / Enter-up are swallowed. Present mode keeps them (slideshow start).
- ④ long press (6D, measured 2026-10-12) = right click through the right_click slot; the
  B the pen types with it is swallowed outside slideshows and present mode (test_guard).
- Kept on purpose: the volume keys scroll while ⑤ is held (magnifier zoom in present mode).
  They send vendor codes 78 / 79 until let go (00); a keyboard VK bound by 0.11 still works
  through the hook.

Keyboard-hook logic runs on AirMouse.handle with fake clocks and fake Raw Input reports;
no hook is installed and nothing is injected into Windows.
"""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from bridge import airmouse
from bridge.airmouse import (MODIFIERS, VK_F5, VK_LWIN, VK_MASK, VK_PRIOR, VK_RETURN, WHEEL_DELAY, WHEEL_HOLD_MAX,
                             WHEEL_REPEAT, WHEEL_STEP, WM_KEYDOWN, WM_KEYUP, ZOOM_REPEAT)
from bridge.keymap import Keymap
from bridge.prefs import Preferences

try:
    from .test_bridge import FakeBackend, event, wait_for, PATH
    from .core_fakes import make_core
except ImportError:
    from test_bridge import FakeBackend, event, wait_for, PATH
    from core_fakes import make_core

VK_NEXT, VK_TAB, VK_VOLUME_UP, VK_VOLUME_DOWN, VK_LSHIFT = 0x22, 0x09, 0xAF, 0xAE, 0xA0
RAW = {VK_LSHIFT: 0x10}  # Raw Input reports generic modifier VKs


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Alive:
    """A keyboard hook that 'runs' (begin_drag needs one); none is installed."""
    def is_alive(self):
        return True


class Timers:
    """AirMouse.schedule stand-in: a held wheel key's repeat fires only when a test says so."""
    def __init__(self):
        self.pending = []

    def __call__(self, delay, work):
        timer = _Timer(self, delay, work)
        self.pending.append(timer)
        return timer

    def fire(self):
        timer = self.pending.pop(0)
        timer.work()
        return timer


class _Timer:
    def __init__(self, owner, delay, work):
        self.owner, self.delay, self.work = owner, delay, work

    def cancel(self):
        if self in self.owner.pending:
            self.owner.pending.remove(self)


class PenMouseCase(unittest.TestCase):
    VKS = {'wheel_up': {VK_VOLUME_UP}, 'wheel_down': {VK_VOLUME_DOWN}}

    def make(self, presenting=False, double_click_ms=500):
        self.clock = Clock()
        self.down_mods = set()
        self.sent, self.injected, self.locks = [], [], []
        self.state = {'presenting': presenting}
        # The guard itself stays off: these tests are about the air mouse's own muting.
        self.guard = airmouse.ShortcutGuard(clock=self.clock, slideshow=lambda: False,
                                            presentation_app=lambda: False, wait=0.0,
                                            pressed=lambda name: name in self.down_mods)
        self.mouse = airmouse.AirMouse(lambda: {'skip_slideshow': True}, lambda: self.VKS, clock=self.clock,
                                       sender=lambda action, down, ctrl: self.sent.append((action, down, ctrl)),
                                       presenting=lambda: self.state['presenting'], guard=self.guard,
                                       injector=self.injected.append, double_click_ms=lambda: double_click_ms)
        self.mouse.spawn = lambda work: work()
        self.mouse.thread = Alive()
        self.mouse.on_lock = lambda locked, how: self.locks.append((locked, how))
        return self.mouse

    def key(self, vk, down=True, pen=None):
        """The hook sees a key first; Raw Input reports it afterwards, if it got through."""
        swallowed = self.mouse.handle(vk, WM_KEYDOWN if down else WM_KEYUP, False)
        if vk in MODIFIERS and not swallowed:
            (self.down_mods.add if down else self.down_mods.discard)(MODIFIERS[vk])
        if pen is not None and not swallowed:
            self.guard.raw_key(RAW.get(vk, vk), down, pen)
        return swallowed

    def tap(self, vk, pen=None):
        return self.key(vk, True, pen), self.key(vk, False, pen)

    def grab(self):
        """③ long press: 6B reaches begin_drag; its short timer then presses the button."""
        self.assertTrue(self.mouse.begin_drag())
        self.mouse.drag_timer.cancel()
        self.mouse._grab()


class TapClickTests(PenMouseCase):
    def test_a_tap_clicks_at_once_and_the_second_tap_of_a_double_tap_clicks_again(self):
        mouse = self.make()
        self.assertTrue(mouse.tap())
        self.assertEqual(self.sent, [('click', None, False)], 'sent at once, never held back for a second tap')
        self.clock.now += .3
        self.assertTrue(mouse.tap(second=True))
        self.assertEqual(self.sent, [('click', None, False)] * 2, 'two clicks 0.3 s apart: a Windows double-click')
        self.clock.now += .3
        self.assertTrue(mouse.tap())  # the next tap starts over
        self.assertEqual(self.sent, [('click', None, False)] * 3)

    def test_a_second_tap_past_the_system_double_click_time_sends_a_whole_double_click(self):
        mouse = self.make(double_click_ms=500)
        mouse.tap()
        self.clock.now += .52  # inside the pen's 0.55 s double-tap window, past Windows' 500 ms
        mouse.tap(second=True)
        self.assertEqual(self.sent, [('click', None, False), ('double', None, False)])

    def test_key8_held_makes_ctrl_clicks_for_multi_select(self):
        mouse = self.make()
        mouse.set_ctrl(True)
        mouse.tap()
        mouse.right_click()
        self.assertEqual(self.sent, [('click', None, True), ('right_click', None, True)])

    def test_present_mode_a_slideshow_or_a_switched_off_air_mouse_never_click(self):
        for setup in ('present mode', 'slideshow', 'switched off'):
            with self.subTest(setup):
                mouse = self.make(presenting=setup == 'slideshow')
                if setup == 'present mode':
                    mouse.set_buttons(False)
                if setup == 'switched off':
                    mouse.remap_enabled = False
                self.assertFalse(mouse.tap())
                self.assertFalse(mouse.tap(second=True))
                self.assertFalse(mouse.right_click())
                self.assertFalse(mouse.begin_drag())
                self.assertEqual(self.sent, [])
        mouse = self.make(presenting=True)
        mouse.skip_slideshow = False  # 放映时保持原样 switched off: the pen clicks in slideshows too
        self.assertTrue(mouse.tap())


class PageAndVolumeKeyTests(PenMouseCase):
    def test_page_keys_are_never_remapped_even_right_after_or_during_key5(self):
        mouse = self.make()
        for moment in ('idle', 'held', 'just released', 'moving'):
            if moment == 'held':
                mouse.press()
            elif moment == 'just released':
                mouse.release()
            elif moment == 'moving':
                mouse.activity()
            for vk in (VK_PRIOR, VK_NEXT, VK_TAB):
                self.assertEqual(self.tap(vk), (False, False), f'{hex(vk)} {moment}')
        self.assertEqual(self.sent, [])
        self.assertEqual(mouse.watch, {VK_VOLUME_UP, VK_VOLUME_DOWN}, 'the hook remaps the volume keys only')

    def test_volume_keys_scroll_only_while_key5_is_held(self):
        mouse = self.make()
        self.assertEqual(self.tap(VK_VOLUME_UP), (False, False), 'not held: a volume key')
        mouse.press()
        self.assertTrue(self.key(VK_VOLUME_UP))
        self.assertTrue(self.key(VK_VOLUME_UP), 'auto-repeat keeps scrolling')
        self.assertTrue(self.key(VK_VOLUME_UP, down=False))
        self.assertTrue(self.key(VK_VOLUME_DOWN))
        mouse.release()  # ⑤ let go while the volume key is still down
        self.assertTrue(self.key(VK_VOLUME_DOWN, down=False), 'its release goes with its press')
        self.assertEqual(self.tap(VK_VOLUME_UP), (False, False), 'no window after ⑤: a volume key again at once')
        self.assertEqual([action for action, _, _ in self.sent], ['wheel_up', 'wheel_up', 'wheel_down'])

    def test_a_lost_key5_release_stops_scrolling_after_30_s(self):
        mouse = self.make()
        mouse.press()
        self.clock.now += 40
        self.assertEqual(self.tap(VK_VOLUME_UP), (False, False))
        self.assertEqual(self.sent, [])

    def test_the_magnifier_takes_the_volume_keys_in_present_mode_even_in_a_slideshow(self):
        mouse = self.make(presenting=True)
        mouse.set_buttons(False)
        zoom = []
        mouse.on_wheel = lambda role: zoom.append(role) or True
        mouse.press()
        self.assertEqual(self.tap(VK_VOLUME_UP), (True, True))
        self.assertEqual((zoom, self.sent), (['wheel_up'], []))
        mouse.on_wheel = lambda role: False  # no lens up, no slideshow: present mode scrolls while ⑤ is held
        self.state['presenting'] = False
        self.assertEqual(self.tap(VK_VOLUME_DOWN), (True, True))
        self.assertEqual(self.sent, [('wheel_down', None, False)])


class VendorWheelTests(PenMouseCase):
    """BS-OH085: the volume keys send 78 / 79 until let go (00), with or without ⑤ held."""
    VKS = {'wheel_up': set(), 'wheel_down': set()}  # what the keymap gives for vendor wheel slots

    def make(self, presenting=False, double_click_ms=500):
        mouse = super().make(presenting, double_click_ms)
        self.timers = mouse.schedule = Timers()
        return mouse

    def notches(self):
        return [action for action, _, _ in self.sent]

    def test_without_key5_a_volume_key_does_nothing(self):
        mouse = self.make()
        self.assertFalse(mouse.wheel_press('wheel_up'))
        self.assertEqual((self.sent, self.timers.pending), ([], []))
        self.assertEqual(mouse.watch, set(), 'the keyboard hook watches no key for vendor wheel slots')

    def test_one_press_scrolls_a_step_and_a_held_key_keeps_scrolling_until_let_go(self):
        mouse = self.make()
        mouse.press()
        self.assertTrue(mouse.wheel_press('wheel_down'))
        self.assertEqual(self.notches(), ['wheel_down'] * WHEEL_STEP, 'one press: a step at once')
        self.assertEqual([t.delay for t in self.timers.pending], [WHEEL_DELAY])
        for _ in range(4):  # still held: one more notch every WHEEL_REPEAT
            self.clock.now += WHEEL_REPEAT
            self.timers.fire()
        self.assertEqual(len(self.sent), WHEEL_STEP + 4)
        self.assertEqual([t.delay for t in self.timers.pending], [WHEEL_REPEAT])
        mouse.wheel_release()  # the key's 00
        self.assertEqual(self.timers.pending, [])
        self.assertTrue(mouse.wheel_press('wheel_up'))
        self.assertEqual(self.notches()[-WHEEL_STEP:], ['wheel_up'] * WHEEL_STEP)
        stale = self.timers.pending[0].work
        mouse.wheel_press('wheel_down')  # the other key: the first press's repeat is over
        count = len(self.sent)
        stale()
        self.assertEqual(len(self.sent), count, 'a repeat that fires late scrolls nothing')

    def test_letting_go_of_key5_or_losing_the_release_report_stops_the_repeat(self):
        mouse = self.make()
        mouse.press()
        mouse.wheel_press('wheel_up')
        mouse.release()  # ⑤ let go while the volume key is still down (73)
        self.assertEqual(self.timers.pending, [])
        mouse.press()
        mouse.wheel_press('wheel_up')
        self.clock.now += WHEEL_HOLD_MAX + 1  # its 00 never came
        self.timers.fire()
        self.assertEqual((len(self.sent), self.timers.pending), (2 * WHEEL_STEP, []))

    def test_key5_held_for_a_minute_still_scrolls_but_a_lost_key5_release_does_not_forever(self):
        mouse = self.make()
        mouse.press()
        self.clock.now += 60  # reading with ⑤ held
        self.assertTrue(mouse.wheel_press('wheel_down'))
        self.clock.now += 20
        self.assertTrue(mouse.wheel_press('wheel_down'))
        # Review 2026-10-14: a volume press used to renew ⑤'s hold, so with its 73 lost the
        # volume keys scrolled for as long as they were used. 90 s after ⑤'s press: over.
        self.clock.now += 20
        self.assertFalse(mouse.wheel_press('wheel_down'))
        mouse.forget()  # the receiver went away
        self.assertEqual(self.timers.pending, [])

    def test_a_key5_tap_ends_a_hold_whose_release_was_lost(self):
        mouse = self.make()
        mouse.press()                       # 72; its 73 never arrives
        self.assertTrue(mouse.wheel_press('wheel_up'))
        mouse.tap()                         # 71: ⑤ is not held after all
        self.assertFalse(mouse.holding)
        self.assertEqual(self.timers.pending, [], 'a held volume key stops scrolling too')
        self.assertFalse(mouse.wheel_press('wheel_up'), 'the volume key is a volume key again')
        mouse.set_buttons(False)            # present mode: the tap clicks nothing, but still counts
        mouse.press()
        mouse.tap()
        self.assertFalse(mouse.holding)

    def test_a_lost_volume_key_release_stops_within_a_few_seconds(self):
        self.assertLessEqual(WHEEL_HOLD_MAX, 3.0)
        mouse = self.make()
        mouse.press()
        mouse.wheel_press('wheel_down')
        self.clock.now += WHEEL_HOLD_MAX + .1  # its 00 never came
        self.timers.fire()
        self.assertEqual(self.timers.pending, [])

    def test_slideshow_and_switched_off_air_mouse_do_not_scroll_but_the_magnifier_zooms(self):
        for setup in ('slideshow', 'switched off'):
            with self.subTest(setup):
                mouse = self.make(presenting=setup == 'slideshow')
                mouse.remap_enabled = setup != 'switched off'
                mouse.press()
                self.assertFalse(mouse.wheel_press('wheel_up'))
                self.assertEqual(self.sent, [])
                zoom = []
                mouse.on_wheel = lambda role: zoom.append(role) or True  # the lens is up
                self.assertTrue(mouse.wheel_press('wheel_up'))
                self.assertEqual((zoom, self.sent), (['wheel_up'], []), 'one zoom step per press')
                self.assertEqual([t.delay for t in self.timers.pending], [WHEEL_DELAY])
                self.timers.fire()
                self.assertEqual(zoom, ['wheel_up'] * 2, 'held: it keeps zooming')
                self.assertEqual([t.delay for t in self.timers.pending], [ZOOM_REPEAT])
                mouse.wheel_release()


class GrabTests(PenMouseCase):
    def test_key3_long_press_grabs_without_key5_and_the_next_key3_press_puts_it_down(self):
        mouse = self.make()
        self.clock.now += 600  # ⑤ untouched for ten minutes
        self.grab()
        self.assertEqual(self.sent, [('left', True, False)])
        self.assertEqual(self.locks, [(True, 'grab')])
        self.assertFalse(mouse.end_drag(), 'releasing ③ keeps holding')
        # ③ short press: its PageUp puts the button down and never pages.
        self.assertEqual(self.tap(VK_PRIOR), (True, True))
        self.assertEqual(self.sent[-1], ('left', False, False))
        self.assertEqual(self.locks[-1], (False, 'grab'))
        self.assertEqual(self.tap(VK_PRIOR), (False, False), 'PageUp is PageUp again')
        # A second long press while holding puts it down as well.
        self.grab()
        self.assertTrue(mouse.begin_drag())
        self.assertEqual(self.sent[-1], ('left', False, False))
        self.assertFalse(mouse.locked)

    def test_while_grabbing_taps_and_right_clicks_wait_and_page_down_still_pages(self):
        mouse = self.make()
        self.grab()
        self.assertFalse(mouse.tap())
        self.assertFalse(mouse.right_click())
        self.assertEqual(self.tap(VK_NEXT), (False, False))
        self.assertEqual(self.sent, [('left', True, False)])

    def test_the_pens_shift_f5_win_tap_and_enter_up_are_swallowed(self):
        mouse = self.make()
        # The pen's ③ long press: Shift (from the receiver) + F5, then 6B, a Win tap, a stray Enter-up.
        self.assertFalse(self.key(VK_LSHIFT, pen=True), 'the modifier itself always passes')
        self.assertTrue(self.key(VK_F5), 'no slideshow starts')
        self.assertEqual(self.injected, [[(VK_MASK, 0, False, False), (VK_MASK, 0, True, False)]],
                         'Shift released alone would switch the IME')
        self.grab()
        self.assertTrue(self.key(VK_F5, down=False))
        self.assertFalse(self.key(VK_LSHIFT, down=False, pen=True), 'a modifier release is never held back')
        self.clock.now += .2
        self.assertEqual(self.tap(VK_LWIN), (True, True), 'no Start menu')
        self.assertTrue(self.key(VK_RETURN, down=False), "the pen's stray Enter-up")
        self.clock.now += 2
        self.assertEqual(self.tap(VK_LWIN), (False, False), 'later the Win key is the Win key')
        self.assertEqual(self.sent, [('left', True, False)])
        self.assertTrue(mouse.locked)

    def test_a_long_press_that_puts_it_down_swallows_its_keystrokes_too(self):
        mouse = self.make()
        self.grab()
        self.clock.now += 5
        self.guard.pen_activity()  # vendor 6B first this time; its plain-F5 view follows
        self.assertTrue(mouse.begin_drag())
        self.assertEqual(self.tap(VK_F5), (True, True))
        self.assertEqual(self.tap(VK_LWIN), (True, True))
        self.assertEqual(self.sent, [('left', True, False), ('left', False, False)])

    def test_a_real_keyboards_f5_and_shift_f5_always_work(self):
        self.make()
        self.assertEqual(self.tap(VK_F5), (False, False), 'refresh: no pen activity')
        self.key(VK_LSHIFT, pen=False)
        self.assertEqual(self.tap(VK_F5), (False, False), 'Shift held on another keyboard')
        self.key(VK_LSHIFT, down=False, pen=False)
        self.assertEqual(self.tap(VK_LWIN), (False, False))
        self.assertEqual((self.injected, self.sent), ([], []))

    def test_present_mode_keeps_the_pens_shift_f5_for_the_slideshow(self):
        mouse = self.make()
        mouse.set_buttons(False)
        self.key(VK_LSHIFT, pen=True)
        self.assertFalse(self.key(VK_F5), 'Shift+F5 starts the show from this slide')
        self.key(VK_F5, down=False)
        self.key(VK_LSHIFT, down=False, pen=True)
        self.assertFalse(mouse.begin_drag())
        self.assertEqual(self.sent, [])

    def test_present_mode_and_idling_put_a_grab_down(self):
        mouse = self.make()
        self.grab()
        mouse.set_buttons(False)
        self.assertEqual(self.sent[-1], ('left', False, False))
        mouse.set_buttons(True)
        self.grab()
        mouse.press()
        self.clock.now += 5
        mouse.release()  # moved with ⑤
        self.clock.now += 19
        mouse.expire()
        self.assertTrue(mouse.locked, 'still in use')
        self.clock.now += 2
        mouse.expire()
        self.assertFalse(mouse.locked, 'forgotten for 20 s')
        self.assertEqual(self.sent[-1], ('left', False, False))

    def test_a_grab_made_with_ctrl_releases_ctrl_too(self):
        mouse = self.make()
        mouse.set_ctrl(True)
        self.grab()
        mouse.set_ctrl(False)  # ⑧ let go before the drop
        mouse.release_all()
        self.assertEqual(self.sent, [('left', True, True), ('left', False, True)])


def found(*paths):
    return [{'path': p.upper(), 'kind': 2, 'page': 0xff03, 'usage': 0} for p in paths]


class CorePenKeyTests(unittest.TestCase):
    """Through the core: vendor codes from the receiver decide; the sender only records."""
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.events = []
        self.core = make_core(Path(folder.name), FakeBackend(), events=self.events)
        self.addCleanup(self.core.close)
        patcher = patch.multiple(self.core.airmouse, start=lambda: None, stop=lambda: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.core.airmouse.thread = Alive()
        self.core._devices_changed(found(PATH))  # a receiver: the pen's mouse is live
        self.assertTrue(self.core.airmouse.remap_enabled)
        self.sent = self.core.airmouse.sent

    def press(self, *codes):
        for code in codes:
            self.core.on_hid(event(code))

    def test_key5_tap_clicks_in_input_and_notes_modes(self):
        for mode in ('input', 'notes'):
            with self.subTest(mode):
                self.core.set_mode(mode)
                self.sent.clear()
                self.core.last_air_double = float('-inf')
                self.press(0x71, 0x00)
                self.assertEqual(self.sent, [('click', None, False)], 'one tap: one click, at once')
                self.press(0x71, 0x00)  # the firmware's double-tap: a second 71 within 0.55 s
                self.assertEqual(self.sent, [('click', None, False)] * 2)
                self.press(0x71, 0x00)  # the pair is complete: a third tap is a new single click
                self.assertEqual(self.sent, [('click', None, False)] * 3)

    def test_present_mode_single_tap_does_nothing_and_a_double_tap_picks_the_next_tool(self):
        self.core.set_mode('present')
        tool = self.core.tool
        self.press(0x71, 0x00)
        time.sleep(.05)
        self.assertEqual(self.core.tool, tool)
        self.press(0x71, 0x00)
        wait_for(lambda: self.core.tool != tool)
        self.assertEqual(self.sent, [], 'present mode never clicks')

    def test_page_key_codes_never_click_even_right_after_key5(self):
        self.press(0x72, 0x00, 0x73, 0x00)  # ⑤ just used
        self.press(0x6a, 0x00, 0x6c, 0x00)  # ③ ④ short presses (their PageUp/PageDown pass natively)
        self.assertEqual(self.sent, [])

    def test_key3_long_press_grabs_in_input_mode_only(self):
        self.press(0x6b)  # ③ long press; ⑤ never touched
        wait_for(lambda: self.core.airmouse.locked)
        self.assertEqual(self.sent, [('left', True, False)])
        wait_for(lambda: ('drag', {'locked': True, 'how': 'grab'}) in self.events)
        self.press(0x00, 0x6b)  # the next ③ long press puts it down
        self.assertFalse(self.core.airmouse.locked)
        self.assertEqual(self.sent[-1], ('left', False, False))
        self.press(0x00)
        self.core.set_mode('present')
        self.sent.clear()
        self.press(0x6b, 0x00)
        time.sleep(.2)
        self.assertEqual(self.sent, [], 'present mode: the pen starts the slideshow itself')
        self.assertFalse(self.core.airmouse.locked)

    def test_switching_to_present_mode_puts_a_grab_down(self):
        self.press(0x6b, 0x00)
        wait_for(lambda: self.core.airmouse.locked)
        self.core.set_mode('present')
        self.assertFalse(self.core.airmouse.locked)
        self.assertEqual(self.sent[-1], ('left', False, False))

    def test_key4_long_press_right_clicks_by_default_outside_present_mode(self):
        binding = self.core.bindings()['devices']['baseus']['right_click']
        self.assertEqual((binding['fire'], binding['default']), (['6d'], True), 'measured 2026-10-12')
        self.press(0x6d, 0x00)  # ④ long press, on a fresh install
        self.assertEqual(self.sent, [('right_click', None, False)])
        self.core.set_mode('present')
        self.press(0x6d, 0x00)
        self.assertEqual(self.sent, [('right_click', None, False)], 'present mode: no right click')
        self.assertFalse(self.core.guard.letters, 'and its B (black screen) passes there')
        self.core.set_mode('input')
        self.assertTrue(self.core.guard.letters)

    def test_key4_long_press_types_no_b_outside_present_mode_and_keeps_it_there(self):
        # Key log 2026-10-12: keyboard B down, then 6D in the same 15 ms tick or one later.
        guard = self.core.guard
        self.assertTrue(guard.enabled, 'a receiver is present and pen.guard_shortcuts is on')
        guard.slideshow = lambda: False  # never depend on a slideshow open on the test machine
        guard.wait = 3  # generous: a loaded test machine
        late = threading.Timer(.03, self.core.on_hid, (event(0x6d),))
        late.start()
        started = time.perf_counter()
        self.assertTrue(self.core.airmouse.handle(0x42, WM_KEYDOWN, False), "the pen's B is swallowed")
        self.assertLess(time.perf_counter() - started, 2.5, 'woken by 6D, not the timeout')
        late.join()
        self.assertTrue(self.core.airmouse.handle(0x42, WM_KEYUP, False), 'with its release')
        self.assertEqual(self.sent, [('right_click', None, False)])
        self.press(0x00)
        self.core.set_mode('present')
        guard.typed_at = float('-inf')
        self.press(0x6d)
        self.assertFalse(self.core.airmouse.handle(0x42, WM_KEYDOWN, False), 'present mode: B = black screen')
        self.assertFalse(self.core.airmouse.handle(0x42, WM_KEYUP, False))
        self.assertEqual(self.sent, [('right_click', None, False)], 'and no right click')

    def test_key5_plus_a_volume_key_scrolls_through_the_core(self):
        timers = self.core.airmouse.schedule = Timers()
        self.press(0x78, 0x00)  # volume + alone (key log 1935.234): the key's own function
        self.assertEqual(self.sent, [])
        self.press(0x72, 0x00, 0x78)  # ⑤ held, then volume + (key log 1799.265)
        self.assertEqual(self.sent, [('wheel_up', None, False)] * WHEEL_STEP)
        self.assertEqual(len(timers.pending), 1, 'held: the repeat is armed')
        self.press(0x00)  # let go
        self.assertEqual(timers.pending, [])
        self.press(0x79, 0x00)
        self.assertEqual(self.sent[-WHEEL_STEP:], [('wheel_down', None, False)] * WHEEL_STEP)
        self.press(0x79, 0x73)  # still held when ⑤ is let go: 73 ends it
        self.assertEqual(timers.pending, [])
        count = len(self.sent)
        self.press(0x00, 0x79, 0x00)
        self.assertEqual(len(self.sent), count, '⑤ up: the volume keys do nothing again')
        self.press(0x78, 0x72, 0x78)  # volume + first, then ⑤: the pen re-reports 78 after 72
        self.assertEqual(self.sent[count:], [('wheel_up', None, False)] * WHEEL_STEP)
        self.press(0x00, 0x73, 0x00)

    def test_volume_keys_zoom_the_magnifier_in_present_mode(self):
        self.core.airmouse.schedule = Timers()
        self.core.set_mode('present')
        self.core.set_tool('magnifier')
        self.press(0x72, 0x00, 0x78, 0x00)
        wait_for(lambda: self.core.prefs.get('present', 'zoom') == 250)
        self.press(0x79, 0x00, 0x79, 0x00)
        wait_for(lambda: self.core.prefs.get('present', 'zoom') == 150)
        self.assertEqual(self.sent, [], 'zoom, never scroll, while the lens is up')
        self.press(0x73, 0x00)

    def test_a_wheel_key_is_recorded_with_key5_held_and_key5_keeps_working(self):
        self.assertIn('learning', self.core.binding_learn('baseus', 'airmouse_wheel_up'))
        self.press(0x72, 0x00)  # ⑤ first, as the row says
        self.assertTrue(self.core.airmouse.holding, '⑤ still works while recording')
        self.press(0x79, 0x00)  # e.g. the user picks volume − for "up"
        binding = self.core.bindings()['devices']['baseus']['airmouse_wheel_up']
        self.assertEqual((binding['source'], binding['fire']), ('vendor', ['79']))
        self.assertNotIn('airmouse_wheel_down', self.core.bindings()['devices']['baseus'], 'one key, one action')
        self.assertEqual(self.sent, [], 'recording never scrolls')
        self.core.airmouse.schedule = Timers()
        self.press(0x79)
        self.assertEqual(self.sent, [('wheel_up', None, False)] * WHEEL_STEP)
        self.press(0x00, 0x73, 0x00)

    def test_right_click_is_learned_from_one_long_press_and_learning_never_clicks(self):
        self.assertIn('learning', self.core.binding_learn('baseus', 'right_click'))
        self.assertEqual(self.core.keymap.learning['step'], 'long')
        self.press(0x6d, 0x00)
        binding = self.core.bindings()['devices']['baseus']['right_click']
        self.assertEqual((binding['fire'], binding['ignore']), (['6d'], []))
        self.assertEqual(self.sent, [])
        self.press(0x6d, 0x00)
        self.assertEqual(self.sent, [('right_click', None, False)])


class SchemeStorageTests(unittest.TestCase):
    def test_old_page_key_click_bindings_are_skipped_and_right_click_learns_one_long_press(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'bindings.json'
            path.write_text(json.dumps({'version': 1, 'devices': {'baseus': {
                'airmouse_left': {'source': 'keyboard', 'vk': [0x21], 'label': '上翻页 ③'},
                'airmouse_double': {'source': 'keyboard', 'vk': [0x09], 'label': '确认键 ①'},
                'airmouse_wheel_up': {'source': 'keyboard', 'vk': [0x26], 'label': '键盘 VK 26'}}}}),
                encoding='utf-8')
            progress = []
            keymap = Keymap(path, progress.append, clock=Clock())
            self.assertEqual(keymap.warning, '')
            # The learned keyboard key stays; the other wheel slot has the vendor default.
            self.assertEqual(keymap.airmouse_vks('baseus'), {'wheel_up': {0x26}, 'wheel_down': set()})
            self.assertEqual(keymap.vendor('baseus', 0x79), 'airmouse_wheel_down')
            self.assertNotIn('airmouse_left', keymap.bindings('baseus'))
            keymap.learn_start('baseus', 'right_click')
            self.assertEqual(keymap.learning['step'], 'long')
            self.assertEqual(keymap.vendor('baseus', 0x6d), 'learning')
            self.assertIsNone(keymap.learning)
            self.assertEqual(progress[-1]['status'], 'done')
            self.assertEqual(keymap.vendor('baseus', 0x6d), 'right_click')
            self.assertIsNone(keymap.vendor('baseus', 0x6c), 'its short press stays a page key')
            saved = json.loads(path.read_text(encoding='utf-8'))['devices']['baseus']
            self.assertEqual(saved['right_click']['fire'], ['6d'])
            self.assertNotIn('airmouse_left', saved)

    def test_an_old_window_ms_setting_is_skipped_without_losing_its_neighbours(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'prefs.json'
            # window_ms comes first: a whole-section merge would stop at it and drop the rest.
            path.write_text(json.dumps({'version': 1, 'airmouse': {'window_ms': 2000, 'enabled': False,
                                                                   'skip_slideshow': False}}), encoding='utf-8')
            prefs = Preferences(path)
            self.assertEqual(prefs.warning, '')
            self.assertEqual(prefs.get('airmouse'), {'enabled': False, 'skip_slideshow': False})
            with self.assertRaises(ValueError):
                prefs.update({'airmouse': {'window_ms': 1500}})


if __name__ == '__main__':
    unittest.main()
