"""Hotkey bridge (bridge.engines.hotkey): the pen drives another dictation app by its hotkey."""
import ctypes
from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge.engines import hotkey  # noqa: E402
from bridge.engines.hotkey import (  # noqa: E402
    KEYEVENTF_EXTENDEDKEY as EXT, KEYEVENTF_KEYUP as UP, PROFILES, ExternalAppOnly, HotkeyBackend,
    HotkeyError, KeysStuck, Profile, custom_profile, detect, format_combo, get_profile, key_input,
    parse_combo, scan, send_keys_scan)

NAMES = {(vk, sc, ext): name for name, (vk, sc, ext) in hotkey.KEYS.items()}
CJK = '[\u4e00-\u9fff]'


class Keyboard:
    """Fake SendInput and clock. log: ('down'|'up', key) and ('sleep', ms) in order."""

    def __init__(self):
        self.now = 1000.0
        self.log, self.events, self.holds = [], [], []
        self.pressed = {}  # key -> time it went down
        self.refuse = lambda event: False

    def send(self, events):
        for event in events:
            if self.refuse(event):
                raise OSError('SendInput refused')
            vk, sc, flags = event
            name = NAMES[(vk, sc, bool(flags & EXT))]
            self.events.append(event)
            if flags & UP:
                self.log.append(('up', name))
                if name in self.pressed:
                    self.holds.append((name, round((self.now - self.pressed.pop(name)) * 1000)))
            else:
                self.log.append(('down', name))
                self.pressed.setdefault(name, self.now)

    def sleep(self, seconds):
        self.log.append(('sleep', round(seconds * 1000)))
        self.now += seconds

    def clock(self):
        return self.now

    def take(self):
        log, self.log = self.log, []
        return log


def tap(*keys, ms=50, gap=10):
    """The expected log of a tap: downs in order, hold, ups in reverse order."""
    log = []
    for index, key in enumerate(keys):
        log += ([('sleep', gap)] if index else []) + [('down', key)]
    log.append(('sleep', ms))
    for index, key in enumerate(reversed(keys)):
        log += ([('sleep', gap)] if index else []) + [('up', key)]
    return log


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError('Timed out waiting for state')
        time.sleep(0.01)


class BridgeCase(unittest.TestCase):
    def make(self, profile, **kwargs):
        self.kb = Keyboard()
        self.spawned = []
        options = {'sender': self.kb.send, 'sleep': self.kb.sleep, 'clock': self.kb.clock,
                   'spawn': self.spawned.append, **kwargs}
        backend = HotkeyBackend(profile, **options)
        backend.connect()
        return backend

    def finish(self, backend):
        """Advance past done_ms; return every poll result until quiet."""
        self.kb.now += backend.profile.done_ms / 1000
        results = []
        for _ in range(5):
            event = backend.poll()
            if event:
                results.append(event)
        return results


class KeyTableTests(unittest.TestCase):
    def test_scan_codes_and_extended_flags(self):
        expected = {
            'RMENU': (0xA5, 0x38, EXT), 'LMENU': (0xA4, 0x38, 0),
            'RCONTROL': (0xA3, 0x1D, EXT), 'LCONTROL': (0xA2, 0x1D, 0),
            'LWIN': (0x5B, 0x5B, EXT), 'RWIN': (0x5C, 0x5C, EXT),
            'LSHIFT': (0xA0, 0x2A, 0), 'RSHIFT': (0xA1, 0x36, 0),  # Right Shift is not an E0 key
            'H': (0x48, 0x23, 0), 'SPACE': (0x20, 0x39, 0), 'ESCAPE': (0x1B, 0x01, 0),
            'UP': (0x26, 0x48, EXT), 'LEFT': (0x25, 0x4B, EXT), 'DELETE': (0x2E, 0x53, EXT),
            'RETURN': (0x0D, 0x1C, 0), 'NUMPADENTER': (0x0D, 0x1C, EXT), 'DIVIDE': (0x6F, 0x35, EXT),
            'NUMPAD5': (0x65, 0x4C, 0), 'APPS': (0x5D, 0x5D, EXT), 'NUMLOCK': (0x90, 0x45, EXT),
            'F1': (0x70, 0x3B, 0), 'F10': (0x79, 0x44, 0), 'F12': (0x7B, 0x58, 0),
            'F13': (0x7C, 0x64, 0), 'F24': (0x87, 0x76, 0), '0': (0x30, 0x0B, 0), 'A': (0x41, 0x1E, 0),
        }
        for name, value in expected.items():
            self.assertEqual(key_input(name), value, name)
            self.assertEqual(key_input(name, up=True), value[:2] + (value[2] | UP,), name)

    def test_table_is_unambiguous_and_in_range(self):
        self.assertEqual(len(NAMES), len(hotkey.KEYS))  # every (vk, scan, extended) names one key
        for name, (vk, sc, _) in hotkey.KEYS.items():
            self.assertTrue(0 < vk < 0xFF and 0 < sc < 0x80, name)
        with self.assertRaises(ValueError):
            key_input('HYPER')


class SendInputTests(unittest.TestCase):
    def capture(self, result=None):
        calls = []

        def send_input(count, array, size):
            calls.append((count, size, [(array[i].type, array[i].u.ki.wVk, array[i].u.ki.wScan,
                                         array[i].u.ki.dwFlags, array[i].u.ki.dwExtraInfo) for i in range(count)]))
            return count if result is None else result
        return calls, send_input

    def test_each_input_carries_virtual_key_and_scan_code(self):
        calls, send_input = self.capture()
        sent = send_keys_scan([key_input('RMENU'), key_input('RMENU', up=True)], send_input=send_input)
        self.assertEqual(sent, 2)
        count, size, inputs = calls[0]
        self.assertEqual(size, ctypes.sizeof(hotkey.INPUT))
        self.assertEqual(size, 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
        self.assertEqual(inputs, [(1, 0xA5, 0x38, EXT, 0), (1, 0xA5, 0x38, EXT | UP, 0)])

    def test_refused_events_raise_oserror(self):
        _, send_input = self.capture(result=0)
        with self.assertRaises(OSError):
            send_keys_scan([key_input('LWIN')], send_input=send_input)

    def test_invalid_events_never_reach_windows(self):
        calls, send_input = self.capture()
        for event in [(0, 0x1E, 0), (0x100, 0x1E, 0), (0x41, 0x1E, 0x4), (0x41, 0x1FF, 0)]:
            with self.assertRaises(ValueError):
                send_keys_scan([event], send_input=send_input)
        self.assertEqual(send_keys_scan([], send_input=send_input), 0)
        self.assertEqual(calls, [])


class ComboTests(unittest.TestCase):
    def test_valid_combos(self):
        cases = {
            'Ctrl+Alt+Space': ('LCONTROL', 'LMENU', 'SPACE'), 'RightAlt': ('RMENU',), 'Win+H': ('LWIN', 'H'),
            '右Alt': ('RMENU',), '右 Alt': ('RMENU',), '右边Alt': ('RMENU',), 'AltGr': ('RMENU',),
            'ctrl + shift + a': ('LCONTROL', 'LSHIFT', 'A'), 'Super+H': ('LWIN', 'H'),
            'Ctrl+Win': ('LCONTROL', 'LWIN'), '左Ctrl+左Win': ('LCONTROL', 'LWIN'), 'ControlRight': ('RCONTROL',),
            'AltRight+Space': ('RMENU', 'SPACE'), 'MetaLeft+KeyH': ('LWIN', 'H'), 'Right-Alt': ('RMENU',),
            'F9': ('F9',), 'Space+Ctrl': ('LCONTROL', 'SPACE'), 'RMENU': ('RMENU',), 'VK_RMENU': ('RMENU',),
            'RWin': ('RWIN',), 'CmdOrCtrl+Digit1': ('LCONTROL', '1'), 'Ctrl+=': ('LCONTROL', 'OEM_PLUS'),
            'Ctrl+-': ('LCONTROL', 'OEM_MINUS'), 'NumpadEnter': ('NUMPADENTER',), 'Esc': ('ESCAPE',),
            'Ctrl+Win+Shift': ('LCONTROL', 'LWIN', 'LSHIFT'),
        }
        for text, keys in cases.items():
            self.assertEqual(parse_combo(text), keys, text)
        self.assertEqual(parse_combo(['LCONTROL', 'LWIN']), ('LCONTROL', 'LWIN'))

    def test_invalid_combos(self):
        for bad in ['', '   ', 'Ctrl+', '+H', 'Ctrl++', 'Hyper+X', 'Ctrl+Control', 'A+B',
                    'Ctrl+Alt+Shift+Win+H', 'Menu', 'x' * 70, None, 42, ['Ctrl', 3], []]:
            with self.assertRaises(ValueError, msg=repr(bad)) as caught:
                parse_combo(bad)
            self.assertRegex(str(caught.exception), CJK)

    def test_display_text_reads_back(self):
        self.assertEqual(format_combo(('RMENU',)), '右 Alt')
        self.assertEqual(format_combo(('LCONTROL', 'LWIN')), 'Ctrl + Win')
        self.assertEqual(format_combo(('LWIN', 'H')), 'Win + H')
        for name in hotkey.KEYS:
            self.assertEqual(parse_combo(format_combo((name,))), (name,), name)


class ProfileTests(BridgeCase):
    def test_builtin_profiles(self):
        expected = {'shandianshuo': (('LCONTROL', 'LWIN'), 'toggle'), 'typeless': (('RMENU',), 'toggle'),
                    'doubao_ime': (('RMENU',), 'hold'), 'wechat': (('LCONTROL', 'LWIN'), 'hold'),
                    'wechat_handsfree': (('LCONTROL', 'LSHIFT', 'LWIN'), 'toggle'),
                    'windows': (('LWIN', 'H'), 'toggle')}
        self.assertEqual({pid: (p.keys, p.style) for pid, p in PROFILES.items()}, expected)
        for pid, profile in PROFILES.items():
            self.assertIs(profile.validate(), profile)
            self.assertEqual(profile.id, pid)
            self.assertRegex(profile.name, CJK + '|Typeless')
            self.assertIn('{mic}', profile.mic_hint)
            text = profile.mic_text('Ai Pointer')
            self.assertIn('Ai Pointer', text)
            self.assertNotIn('{mic}', text)
            self.assertTrue(profile.sources and all(s.startswith('https://') for s in profile.sources))
            self.assertFalse(profile.verified)  # nothing has been tried against the real apps yet
            if profile.style == 'toggle':
                self.assertTrue(40 <= profile.tap_ms <= 60, pid)
        self.assertEqual(PROFILES['shandianshuo'].cancel_keys, ('ESCAPE',))

    def test_shandianshuo_tap_stays_far_below_long_press(self):
        profile = PROFILES['shandianshuo']
        backend = self.make(profile)
        backend.start()
        longest = max(ms for _, ms in self.kb.holds)
        self.assertEqual(longest, profile.tap_hold_ms)
        self.assertLessEqual(longest, 100)
        self.assertLessEqual(longest * 2, profile.long_press_ms)
        backend.stop()
        self.assertLessEqual(max(ms for _, ms in self.kb.holds), 100)
        with self.assertRaises(ValueError):
            get_profile('shandianshuo', tap_ms=140)  # 140 + 2*10 ms would come close to a long press
        self.assertEqual(get_profile('shandianshuo', keys='Ctrl+Alt+Shift+F9').tap_hold_ms, 100)

    def test_custom_and_overridden_profiles(self):
        custom = custom_profile('Ctrl+Alt+Space')
        self.assertEqual((custom.id, custom.keys, custom.style, custom.custom),
                         ('custom', ('LCONTROL', 'LMENU', 'SPACE'), 'toggle', True))
        self.assertIn('{mic}', custom.mic_hint)
        self.assertEqual(get_profile('custom', keys='F9', style='hold').style, 'hold')
        moved = get_profile('shandianshuo', keys='RightAlt')
        self.assertEqual((moved.keys, moved.style, moved.cancel_keys, moved.custom),
                         (('RMENU',), 'toggle', ('ESCAPE',), True))
        self.assertIs(get_profile('typeless'), PROFILES['typeless'])
        for call in [lambda: get_profile('custom'), lambda: get_profile('nope'), lambda: custom_profile('Hyper'),
                     lambda: custom_profile('F9', style='push'), lambda: custom_profile('F9', tap_ms=500),
                     lambda: get_profile('wechat', lead_ms=-1)]:
            with self.assertRaises(ValueError):
                call()

    def test_connect_validates_the_profile(self):
        broken = Profile(id='x', name='X', keys=('RMENU',), style='press')
        backend = HotkeyBackend(broken, sender=lambda events: None)
        with self.assertRaises(ValueError):
            backend.connect()
        backend = self.make('typeless')
        backend.connect()
        self.assertEqual(self.kb.events, [])
        self.assertEqual(backend.describe()['combo'], '右 Alt')
        self.assertTrue(HotkeyBackend.external)


class ToggleTests(BridgeCase):
    def test_typeless_taps_on_press_and_on_release(self):
        backend = self.make('typeless')
        backend.start()
        self.assertEqual(self.kb.take(), tap('RMENU'))
        self.assertEqual(self.kb.events, [(0xA5, 0x38, EXT), (0xA5, 0x38, EXT | UP)])
        backend.feed(b'\x00\x00' * 800)
        self.assertIsNone(backend.poll())
        backend.stop()
        self.assertEqual(self.kb.take(), [('sleep', 150)] + tap('RMENU'))
        self.assertEqual(self.kb.pressed, {})

    def test_shandianshuo_chord_presses_in_order_and_releases_in_reverse(self):
        backend = self.make('shandianshuo')
        backend.start()
        self.assertEqual(self.kb.take(), tap('LCONTROL', 'LWIN', ms=40))
        self.assertEqual(self.kb.events, [(0xA2, 0x1D, 0), (0x5B, 0x5B, EXT), (0x5B, 0x5B, EXT | UP),
                                          (0xA2, 0x1D, UP)])
        backend.stop()
        self.assertEqual(self.kb.take(), [('sleep', 150)] + tap('LCONTROL', 'LWIN', ms=40))

    def test_windows_voice_typing_toggles_win_h(self):
        backend = self.make('windows')
        backend.start()
        self.assertEqual(self.kb.take(), tap('LWIN', 'H'))
        backend.stop()
        self.assertEqual(self.kb.take(), [('sleep', 300)] + tap('LWIN', 'H'))
        self.assertEqual(self.kb.events[1], (0x48, 0x23, 0))

    def test_wechat_handsfree_completes_the_chord_with_win(self):
        backend = self.make('wechat_handsfree')
        backend.start()
        self.assertEqual(self.kb.take(), tap('LCONTROL', 'LSHIFT', 'LWIN'))

    def test_cancel_stops_the_app_then_esc_discards_for_shandianshuo(self):
        backend = self.make('shandianshuo')
        backend.start()
        self.kb.take()
        backend.cancel()
        self.assertEqual(self.kb.take(), tap('LCONTROL', 'LWIN', ms=40) + tap('ESCAPE', ms=40))
        self.assertEqual(self.finish(backend), [])  # a cancelled take reports nothing
        backend.close()
        self.assertEqual(self.kb.take(), [])

    def test_cancel_without_a_cancel_key_only_stops(self):
        backend = self.make('typeless')
        backend.start()
        self.kb.take()
        backend.cancel()
        self.assertEqual(self.kb.take(), tap('RMENU'))

    def test_cancel_or_close_when_not_recording_sends_nothing(self):
        backend = self.make('shandianshuo')
        backend.cancel()
        backend.close()
        self.assertEqual(self.kb.take(), [])
        backend.start()
        backend.stop()
        self.kb.take()
        backend.cancel()  # after the stop the app is already transcribing: leave it alone
        self.assertEqual(self.kb.take(), [])
        self.assertEqual(self.finish(backend), [])

    def test_start_while_recording_is_refused(self):
        backend = self.make('typeless')
        backend.start()
        self.kb.take()
        with self.assertRaises(HotkeyError):
            backend.start()
        self.assertEqual(self.kb.take(), [])

    def test_a_refused_stop_while_cancelling_never_inverts_the_next_press(self):
        # Review 2026-10-14: cancel() marked the app idle before its stop shortcut went out; an
        # administrator window refused it, so the app kept recording and the next ⑥ stopped it.
        backend = self.make('shandianshuo')
        backend.start()
        self.kb.take()
        self.kb.refuse = lambda event: True   # an administrator window is in front
        with self.assertRaises(HotkeyError):
            backend.cancel()
        self.assertEqual(backend.phase, 'unknown')
        with self.assertRaises(HotkeyError):
            backend.close()                   # tried again, still refused: still unknown
        self.assertEqual(backend.phase, 'unknown')
        with self.assertRaisesRegex(HotkeyError, '可能还在录上一段'):
            backend.start()                   # says so instead of toggling the app off
        self.assertEqual(self.kb.take(), [])
        self.kb.refuse = lambda event: False
        backend.start()                       # stop and discard the old take, then start anew
        self.assertEqual(self.kb.take(), tap('LCONTROL', 'LWIN', ms=40) + tap('ESCAPE', ms=40)
                         + tap('LCONTROL', 'LWIN', ms=40))
        self.assertEqual(backend.phase, 'active')
        backend.stop()
        self.assertEqual(len(self.finish(backend)), 1)

    def test_close_while_recording_stops_the_app(self):
        backend = self.make('typeless')
        backend.start()
        self.kb.take()
        backend.close()
        self.assertEqual(self.kb.take(), tap('RMENU'))
        self.assertEqual(self.finish(backend), [])


class HoldTests(BridgeCase):
    def test_doubao_holds_right_alt_while_talking(self):
        backend = self.make('doubao_ime')
        backend.start()
        self.assertEqual(self.kb.take(), [('down', 'RMENU')])
        self.assertEqual(self.kb.events, [(0xA5, 0x38, EXT)])
        backend.feed(b'\x01\x00' * 800)
        self.kb.now += 30
        self.assertIsNone(backend.poll())  # poll never releases a key that is meant to be held
        self.assertEqual(list(self.kb.pressed), ['RMENU'])
        backend.stop()
        self.assertEqual(self.kb.take(), [('sleep', 150), ('up', 'RMENU')])
        self.assertEqual(self.kb.holds, [('RMENU', 30150)])

    def test_wechat_presses_ctrl_then_win_and_releases_in_reverse(self):
        backend = self.make('wechat')
        backend.start()
        self.assertEqual(self.kb.take(), [('down', 'LCONTROL'), ('sleep', 10), ('down', 'LWIN')])
        backend.stop()
        self.assertEqual(self.kb.take(), [('sleep', 150), ('up', 'LWIN'), ('sleep', 10), ('up', 'LCONTROL')])
        self.assertEqual(self.kb.events[-2:], [(0x5B, 0x5B, EXT | UP), (0xA2, 0x1D, UP)])

    def test_cancel_releases_without_a_result(self):
        backend = self.make('wechat')
        backend.start()
        self.kb.take()
        backend.cancel()
        self.assertEqual(self.kb.take(), [('up', 'LWIN'), ('sleep', 10), ('up', 'LCONTROL')])
        self.assertEqual(self.finish(backend), [])

    def test_close_releases_held_keys(self):
        backend = self.make('doubao_ime')
        backend.start()
        self.kb.take()
        backend.close()
        self.assertEqual(self.kb.take(), [('up', 'RMENU')])
        self.assertEqual(self.kb.pressed, {})


class ResultTests(BridgeCase):
    def test_one_empty_external_result_shortly_after_stop(self):
        backend = self.make('doubao_ime')
        self.assertIsNone(backend.poll())
        backend.start()
        backend.stop()
        self.assertIsNone(backend.poll())  # done_ms has not passed yet
        self.assertEqual(self.finish(backend), [{'type': 'result', 'text': '', 'external': True, 'source': 'hotkey',
                                                 'app': '豆包输入法', 'profile': 'doubao_ime'}])
        self.assertEqual(self.finish(backend), [])

    def test_stop_without_a_session_still_lets_the_controller_finish(self):
        backend = self.make('typeless')
        backend.stop()
        self.assertEqual(self.kb.take(), [])
        self.assertEqual(len(self.finish(backend)), 1)

    def test_next_session_after_a_result(self):
        backend = self.make('typeless')
        for _ in range(2):
            backend.start()
            backend.stop()
            self.assertEqual(len(self.finish(backend)), 1)
        self.assertEqual(self.kb.log.count(('down', 'RMENU')), 4)

    def test_notes_destination_is_refused_before_any_key(self):
        backend = self.make('typeless')
        backend.insert_text = False
        with self.assertRaises(ExternalAppOnly) as caught:
            backend.start()
        self.assertRegex(str(caught.exception), CJK)
        self.assertEqual(self.kb.events, [])


class StuckKeyTests(BridgeCase):
    def test_refused_second_key_releases_the_first(self):
        backend = self.make('wechat')
        self.kb.refuse = lambda event: event == key_input('LWIN')
        with self.assertRaises(HotkeyError) as caught:
            backend.start()
        self.assertNotIsInstance(caught.exception, KeysStuck)
        self.assertEqual(self.kb.take(), [('down', 'LCONTROL'), ('sleep', 10), ('up', 'LCONTROL')])
        self.assertEqual((backend.held, self.kb.pressed), ([], {}))

    def test_refused_press_leaves_nothing_and_can_be_retried(self):
        backend = self.make('typeless')
        self.kb.refuse = lambda event: True
        with self.assertRaises(HotkeyError):
            backend.start()
        self.assertEqual((backend.phase, backend.held), ('idle', []))
        self.kb.refuse = lambda event: False
        backend.start()
        self.assertEqual(self.kb.take(), tap('RMENU'))

    def test_refused_release_is_retried_until_it_goes_through(self):
        backend = self.make('doubao_ime')
        backend.start()
        self.kb.refuse = lambda event: event[2] & UP
        with self.assertRaises(KeysStuck) as caught:
            backend.stop()
        self.assertIn('右 Alt', str(caught.exception))
        self.assertEqual(backend.held, ['RMENU'])
        self.assertEqual(len(self.spawned), 1)  # a background retry, for when polling stops
        self.assertIsNone(backend.poll())       # too soon to try again
        self.kb.now += hotkey.RETRY_EVERY
        backend.poll()                          # still refused: stays pending
        self.assertEqual(backend.held, ['RMENU'])
        with self.assertRaises(KeysStuck):
            backend.start()                     # no new session over a stuck key
        self.kb.refuse = lambda event: False
        self.kb.now += hotkey.RETRY_EVERY
        backend.poll()
        self.assertEqual((backend.held, self.kb.pressed), ([], {}))
        self.assertEqual(len(self.spawned), 1)

    def test_refused_tap_release_is_finished_by_close(self):
        backend = self.make('shandianshuo')
        self.kb.refuse = lambda event: event == key_input('LCONTROL', up=True)
        with self.assertRaises(KeysStuck):
            backend.start()
        self.assertEqual(backend.held, ['LCONTROL'])
        self.kb.refuse = lambda event: False
        self.kb.take()
        backend.close()  # stops the app, discards with Esc, and nothing is left down
        self.assertEqual(self.kb.take()[0], ('up', 'LCONTROL'))
        self.assertEqual((backend.held, self.kb.pressed), ([], {}))

    def test_exception_during_a_tap_still_releases(self):
        backend = self.make('shandianshuo')

        def sleep(seconds):
            if round(seconds * 1000) == 40:
                raise RuntimeError('interrupted')
            self.kb.sleep(seconds)
        backend.sleep = sleep
        with self.assertRaises(RuntimeError):
            backend.start()
        self.assertEqual((backend.held, self.kb.pressed), ([], {}))
        self.assertEqual(self.kb.log[-1], ('up', 'LCONTROL'))

    def test_interrupt_after_a_key_went_down_still_releases_it(self):
        backend = self.make('doubao_ime')

        def send(events):
            self.kb.send(events)
            if not events[0][2] & UP:
                raise KeyboardInterrupt  # arrives after the key-down went in
        backend.sender = send
        with self.assertRaises(KeyboardInterrupt):
            backend.start()
        self.assertEqual(self.kb.take(), [('down', 'RMENU'), ('up', 'RMENU')])
        self.assertEqual((backend.held, backend.phase), ([], 'idle'))

    def test_exception_during_lead_still_stops_a_hold(self):
        backend = self.make('wechat')
        backend.start()
        backend.sleep = lambda seconds: (_ for _ in ()).throw(RuntimeError('interrupted'))
        with self.assertRaises(RuntimeError):
            backend.stop()
        self.assertEqual((backend.held, self.kb.pressed), ([], {}))

    def test_exit_handler_releases_live_backends(self):
        backend = self.make('wechat')
        backend.start()
        self.kb.take()
        hotkey._release_at_exit()
        self.assertEqual(self.kb.take(), [('up', 'LWIN'), ('sleep', 10), ('up', 'LCONTROL')])
        self.assertEqual(backend.held, [])

    def test_background_retry_releases_after_polling_stopped(self):
        saved = hotkey.RETRY_EVERY
        hotkey.RETRY_EVERY = 0.01
        try:
            kb = Keyboard()
            attempts = []

            def send(events):
                if events[0][2] & UP:
                    attempts.append(events)
                    if len(attempts) < 3:
                        raise OSError('refused')
                kb.send(events)
            backend = HotkeyBackend('doubao_ime', sender=send, sleep=kb.sleep, clock=kb.clock)
            backend.start()
            with self.assertRaises(KeysStuck):
                backend.stop()
            wait_for(lambda: not backend.held)  # nobody polls: the daemon thread finished it
            self.assertEqual(kb.pressed, {})
            wait_for(lambda: not backend.retrying)
        finally:
            hotkey.RETRY_EVERY = saved


class DetectTests(unittest.TestCase):
    def test_processes_and_uninstall_entries(self):
        entries = [
            {'key': 'Weixin', 'DisplayName': '微信', 'DisplayVersion': '4.1.15.50',
             'DisplayIcon': '"C:\\Program Files\\Tencent\\Weixin\\Weixin.exe",0'},
            {'key': '{X}', 'DisplayName': '豆包输入法', 'DisplayVersion': '0.9.0'},
            {'key': 'WXWork', 'DisplayName': '企业微信'}, {'key': 'WeType', 'DisplayName': '微信输入法'},
        ]
        found = scan(processes={'Typeless.exe', 'explorer.exe'}, entries=entries, exists=lambda path: False,
                     environ={}, windows_build=26100)
        self.assertEqual(found['wechat'], {'installed': True, 'running': False, 'version': '4.1.15.50',
                                           'path': 'C:\\Program Files\\Tencent\\Weixin\\Weixin.exe',
                                           'supported': True})
        self.assertTrue(found['typeless']['running'])
        self.assertFalse(found['shandianshuo']['installed'] or found['shandianshuo']['running'])
        self.assertEqual(detect(processes={'Typeless.exe'}, entries=entries, exists=lambda path: False,
                                environ={}, windows_build=26100),
                         ['typeless', 'doubao_ime', 'wechat', 'wechat_handsfree', 'windows'])

    def test_newest_entry_wins_and_quoted_paths_are_cleaned(self):
        # As found on a real PC: WeChat 3.9 and 4.1 side by side, 闪电说 in a custom folder.
        entries = [
            {'key': '闪电说', 'DisplayName': '闪电说', 'DisplayVersion': '0.7.8',
             'InstallLocation': '"D:\\My Software\\Shandianshuo"'},
            {'key': 'WeChat', 'DisplayName': '微信', 'DisplayVersion': '3.9.12.55',
             'DisplayIcon': '"D:\\Program Files\\Tencent\\WeChat\\WeChat.exe"'},
            {'key': 'Weixin', 'DisplayName': 'WeChat', 'DisplayVersion': '4.1.13.65',
             'DisplayIcon': '"D:\\Program Files\\Tencent\\Weixin\\Weixin.exe"'},
        ]
        found = scan(processes={'shandianshuo.exe', 'Weixin.exe', 'WeChatAppEx.exe'}, entries=entries,
                     exists=lambda path: False, environ={}, windows_build=26300)
        self.assertEqual(found['shandianshuo'], {'installed': True, 'running': True, 'version': '0.7.8',
                                                 'path': 'D:\\My Software\\Shandianshuo', 'supported': None})
        self.assertEqual((found['wechat']['version'], found['wechat']['path'], found['wechat']['supported']),
                         ('4.1.13.65', 'D:\\Program Files\\Tencent\\Weixin\\Weixin.exe', True))
        self.assertEqual(detect(processes=set(), entries=entries, exists=lambda path: False, environ={},
                                windows_build=26300), ['shandianshuo', 'wechat', 'wechat_handsfree', 'windows'])

    def test_wechat_without_voice_input_and_old_windows_are_not_offered(self):
        entries = [{'key': 'WeChat', 'DisplayName': '微信', 'DisplayVersion': '3.9.12.57'}]
        found = scan(processes=set(), entries=entries, exists=lambda path: False, environ={}, windows_build=7601)
        self.assertIs(found['wechat']['supported'], False)
        self.assertEqual(detect(processes=set(), entries=entries, exists=lambda path: False, environ={},
                                windows_build=7601), [])

    def test_install_paths_and_process_heuristics(self):
        local = r'C:\Users\u\AppData\Local'
        target = local + r'\shandianshuo\shandianshuo.exe'
        found = scan(processes={'doubaoimeservice.exe', 'doubao.exe'}, entries=[],
                     exists=lambda path: path == target, environ={'LOCALAPPDATA': local}, windows_build=26100)
        self.assertEqual((found['shandianshuo']['installed'], found['shandianshuo']['path']), (True, target))
        self.assertTrue(found['doubao_ime']['running'])
        self.assertFalse(scan(processes={'doubao.exe'}, entries=[], exists=lambda path: False, environ={},
                              windows_build=26100)['doubao_ime']['running'])  # the chat app is not the IME

    @unittest.skipUnless(sys.platform == 'win32', 'Windows only')
    def test_real_scan_is_read_only_and_complete(self):
        self.assertIn(Path(sys.executable).name.casefold(), hotkey.running_processes())
        found = scan()
        self.assertEqual(set(found), set(PROFILES))
        self.assertTrue(found['windows']['installed'])
        self.assertIsInstance(hotkey.uninstall_entries(), list)


class FakeCapture:
    def __init__(self, callback, error):
        self.callback, self.error = callback, error

    def start(self):
        pass

    def close(self):
        pass


class ControllerSessionTests(unittest.TestCase):
    def test_a_pen_session_taps_twice_and_returns_to_idle(self):
        from bridge.controller import Controller
        kb = Keyboard()
        backend = HotkeyBackend(get_profile('typeless', done_ms=50), sender=kb.send, sleep=kb.sleep)
        notes = []
        control = Controller(lambda *value: notes.append(value), backend, FakeCapture)
        try:
            control.command('arm')
            wait_for(lambda: control.phase == 'idle')
            control.command('start')
            wait_for(lambda: control.phase == 'recording')
            control.command('stop')
            wait_for(lambda: any(kind == 'result' for kind, _ in notes))
            wait_for(lambda: control.phase == 'idle')
        finally:
            control.close()
        result = next(value for kind, value in notes if kind == 'result')
        self.assertEqual(result['text'], '')
        self.assertEqual([entry for entry in kb.log if entry[0] != 'sleep'],
                         [('down', 'RMENU'), ('up', 'RMENU')] * 2)
        self.assertEqual(kb.pressed, {})


if __name__ == '__main__':
    unittest.main()
