"""Administrator windows (UIPI): DotVoice's synthetic input never reaches an elevated window
while DotVoice itself is not elevated (红果短剧 inside Androws.exe, 2026-10-14).

Everything runs on fakes: the foreground window, the process probe, the clipboard and
the senders. Nothing is clicked, typed, elevated or scheduled.
"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bridge import airmouse
from bridge.elevation import NOTICE, NOTICE_NO_ADMIN, SETTING, ElevatedTarget, UipiWatch, kept_message
from bridge.engines.cloud import CloudBackend
from bridge.prefs import Preferences

try:
    from .test_bridge import FakeBackend, event, wait_for, PATH
    from .core_fakes import make_core
    from .test_engines import FakeASR, drain
    from .test_pen_keys import Alive, found
except ImportError:
    from test_bridge import FakeBackend, event, wait_for, PATH
    from core_fakes import make_core
    from test_engines import FakeASR, drain
    from test_pen_keys import Alive, found

ANDROWS = (0x1A2B, 17192)   # (hwnd, pid) of the elevated emulator window
NOTEPAD = (0x3C4D, 4242)


class Desk:
    """The foreground window and which processes run elevated (None = cannot be opened)."""
    def __init__(self, front=ANDROWS, elevated=None):
        self.front = front
        self.elevated = {ANDROWS[1]: True, NOTEPAD[1]: False} if elevated is None else elevated
        self.probes = []
        self.now = 100.0

    def foreground(self):
        return self.front

    def probe(self, pid):
        self.probes.append(pid)
        answer = self.elevated.get(pid)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def clock(self):
        return self.now

    def watch(self, own_elevated=False, can_elevate=True):
        return UipiWatch(clock=self.clock, foreground=self.foreground, elevated=self.probe,
                         own={'elevated': own_elevated, 'can_elevate': can_elevate},
                         name=lambda pid: 'androws.exe' if pid == ANDROWS[1] else 'notepad.exe')


class UipiWatchTests(unittest.TestCase):
    def test_an_elevated_window_is_reported_once_per_window(self):
        desk = Desk()
        watch = desk.watch()
        first = watch.blocked('click')
        self.assertEqual(first, {'kind': 'click', 'hwnd': ANDROWS[0], 'pid': ANDROWS[1], 'process': 'androws.exe',
                                 'first': True, 'message': NOTICE})
        self.assertIn(SETTING, NOTICE)
        again = watch.blocked('wheel_down')
        self.assertIsNotNone(again, 'still blocked: the caller still knows')
        self.assertFalse(again['first'], 'but the user was told already')
        desk.front = (0x9999, ANDROWS[1])  # another window of the same elevated process
        self.assertTrue(watch.blocked('type')['first'])
        desk.front = ANDROWS
        self.assertFalse(watch.blocked('click')['first'])

    def test_normal_windows_unknown_processes_and_no_window_are_never_blocked(self):
        desk = Desk(elevated={ANDROWS[1]: None, NOTEPAD[1]: False})
        watch = desk.watch()
        self.assertIsNone(watch.blocked('click'), 'cannot be opened: unknown, treated as not elevated')
        desk.front = NOTEPAD
        self.assertIsNone(watch.blocked('click'))
        desk.front = (0, 0)
        self.assertIsNone(watch.blocked('click'))
        desk.front = (0x77, 0)
        self.assertIsNone(watch.blocked('click'))

    def test_a_broken_probe_never_raises(self):
        desk = Desk(elevated={ANDROWS[1]: OSError('denied')})
        self.assertIsNone(desk.watch().blocked('click'))
        watch = desk.watch()
        watch.foreground = lambda: 1 / 0
        self.assertIsNone(watch.blocked('click'))

    def test_an_elevated_dotvoice_is_never_blocked(self):
        desk = Desk()
        watch = desk.watch(own_elevated=True)
        self.assertIsNone(watch.blocked('click'))
        self.assertEqual(desk.probes, [], 'no probe at all once DotVoice runs as administrator')

    def test_answers_are_cached_per_pid_briefly(self):
        desk = Desk()
        watch = desk.watch()
        for _ in range(20):  # a held volume key scrolls at 10 Hz
            watch.blocked('wheel_up')
        self.assertEqual(desk.probes, [ANDROWS[1]])
        desk.now += watch.ttl + .1
        watch.blocked('wheel_up')
        self.assertEqual(desk.probes, [ANDROWS[1]] * 2, 'pids are reused: asked again after a while')

    def test_a_standard_account_is_not_pointed_at_the_setting(self):
        info = Desk().watch(can_elevate=False).blocked('click')
        self.assertEqual(info['message'], NOTICE_NO_ADMIN)
        self.assertNotIn(SETTING, NOTICE_NO_ADMIN)

    def test_remembered_windows_are_bounded(self):
        desk = Desk(elevated={pid: True for pid in range(1, 200)})
        watch = desk.watch()
        for hwnd in range(1, 200):
            desk.front = (hwnd, hwnd)
            watch.blocked('click')
        self.assertLessEqual(len(watch.told), 64)


class GuardedTyperTests(unittest.TestCase):
    def test_typing_into_an_elevated_window_is_still_tried_then_reported(self):
        typed = []
        typer = Desk().watch().guard_typer(typed.append)
        with self.assertRaises(ElevatedTarget) as caught:
            typer('你好')
        self.assertEqual(typed, ['你好'], 'still sent: nothing is lost by trying')
        self.assertEqual(caught.exception.info['kind'], 'type')
        self.assertIsInstance(caught.exception, OSError)

    def test_a_refusal_from_windows_in_front_of_an_elevated_window_is_the_same(self):
        def refuse(text):
            raise OSError('SendInput 被系统拒绝')
        with self.assertRaises(ElevatedTarget):
            Desk().watch().guard_typer(refuse)('你好')

    def test_a_normal_window_types_and_other_refusals_stay_plain(self):
        typed = []
        desk = Desk(front=NOTEPAD)
        desk.watch().guard_typer(typed.append)('你好')
        self.assertEqual(typed, ['你好'])

        def refuse(text):
            raise OSError('lock screen')
        with self.assertRaises(OSError) as caught:
            desk.watch().guard_typer(refuse)('你好')
        self.assertNotIsInstance(caught.exception, ElevatedTarget)


class KeptMessageTests(unittest.TestCase):
    def test_copy_wording(self):
        self.assertIn('已复制到剪贴板，按 Ctrl+V 粘贴', kept_message(True))
        self.assertIn(SETTING, kept_message(True))
        self.assertIn('保留在点声主窗口的本次结果里', kept_message(False))
        self.assertIn('不是管理员', kept_message(True, can_elevate=False))
        self.assertNotIn(SETTING, kept_message(True, can_elevate=False))


class AirMouseHookTests(unittest.TestCase):
    def test_every_click_scroll_and_grab_asks_first_and_a_broken_check_never_stops_it(self):
        sent, asked = [], []
        mouse = airmouse.AirMouse(lambda: {'skip_slideshow': True}, lambda: {}, sender=lambda *args: sent.append(args),
                                  presenting=lambda: False, double_click_ms=lambda: 500)
        mouse.thread = Alive()
        mouse.before_input = asked.append
        mouse.tap()
        mouse.right_click()
        mouse.press()
        mouse.wheel_press('wheel_up')
        mouse.wheel_release()
        mouse._grab()
        self.assertEqual(asked, ['click', 'right_click', 'wheel_up', 'wheel_up', 'wheel_up', 'grab'])
        self.assertEqual(len(sent), 6, 'everything was still sent')

        def broken(kind):
            raise RuntimeError('probe bug')
        mouse.before_input = broken
        mouse._release_grab()
        self.assertTrue(mouse.tap())


class CoreElevatedWindowTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.events = []
        self.sounds = []
        self.core = make_core(Path(folder.name), FakeBackend(), events=self.events, sounds=self.sounds)
        self.addCleanup(self.core.close)
        patcher = patch.multiple(self.core.airmouse, start=lambda: None, stop=lambda: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.core.airmouse.thread = Alive()
        self.core._devices_changed(found(PATH))
        self.desk = Desk()
        self.core.uipi.foreground = self.desk.foreground
        self.core.uipi.elevated = self.desk.probe
        self.core.uipi.name = lambda pid: 'androws.exe'

    def notices(self):
        return [value for kind, value in self.events if kind == 'elevated']

    def test_key5_taps_into_an_elevated_window_still_click_and_say_why_once(self):
        for _ in range(3):
            self.core.last_air_double = float('-inf')
            self.core.on_hid(event(0x71))
            self.core.on_hid(event(0x00))
        self.assertEqual(self.core.airmouse.sent, [('click', None, False)] * 3, 'still tried every time')
        wait_for(lambda: self.notices())
        self.assertEqual(self.notices(), [{'kind': 'click', 'process': 'androws.exe', 'first': True, 'message': NOTICE}])
        self.desk.front = NOTEPAD
        self.core.last_air_double = float('-inf')
        self.core.on_hid(event(0x71))
        self.assertEqual(len(self.notices()), 1, 'normal windows say nothing')

    def test_unknown_processes_say_nothing(self):
        self.desk.elevated = {ANDROWS[1]: None}
        self.core.on_hid(event(0x6d))  # ④ long press: right click
        self.core.on_hid(event(0x00))
        self.assertEqual(self.core.airmouse.sent, [('right_click', None, False)])
        self.assertEqual(self.notices(), [])

    def test_the_hotkey_bridge_asks_before_each_key(self):
        keys = []
        send = self.core._guard_sender(keys.append)
        send([(0x11, 0x1D, 0)])
        send([(0x11, 0x1D, 2)])
        self.assertEqual(len(keys), 2)
        wait_for(lambda: self.notices())
        self.assertEqual([n['kind'] for n in self.notices()], ['hotkey'])

    def test_text_an_elevated_window_refused_goes_to_the_clipboard_with_the_result(self):
        self.core.notify('result', {'text': '我要评论', 'destination': 'input', 'not_typed': 'elevated'})
        result = [value for kind, value in self.events if kind == 'result'][-1]
        self.assertEqual(self.core.copied, ['我要评论'])
        self.assertTrue(result['copied'])
        self.assertEqual(result['text'], '我要评论', 'the text is kept')
        self.assertIn('按 Ctrl+V 粘贴', result['not_typed_message'])
        self.assertEqual(self.sounds[-1], 'error', 'no success chime for text that never arrived')
        self.assertEqual(self.core.snapshot()['result']['text'], '我要评论')

    def test_a_busy_clipboard_still_keeps_the_text(self):
        def busy(text):
            raise OSError('剪贴板正被其它程序占用')
        self.core.clipboard = busy
        self.core.notify('result', {'text': '我要评论', 'destination': 'input', 'not_typed': 'elevated'})
        result = [value for kind, value in self.events if kind == 'result'][-1]
        self.assertFalse(result['copied'])
        self.assertIn('本次结果里', result['not_typed_message'])

    def test_snapshot_says_whether_dotvoice_runs_as_administrator(self):
        self.assertEqual(self.core.snapshot()['elevation'], {'elevated': False, 'can_elevate': True})
        self.assertFalse(self.core.prefs.get('general', 'run_elevated'))


class CloudTypingTests(unittest.TestCase):
    def test_a_cloud_result_an_elevated_window_refused_is_kept_and_marked(self):
        typed = []
        backend = CloudBackend(FakeASR(final='你好'), inserter=Desk().watch().guard_typer(typed.append))
        backend.connect()
        backend.start()
        backend.feed(bytes(3200))
        backend.stop()
        events = drain(backend)
        self.assertEqual(events[-1]['text'], '你好')
        self.assertEqual(events[-1]['not_typed'], 'elevated')
        self.assertEqual([e for e in events if e.get('kind') == 'notice'], [], 'said once, with the result')
        self.assertEqual(typed, ['你好'])




class PrefsTests(unittest.TestCase):
    def test_run_elevated_is_off_by_default_and_only_takes_a_switch(self):
        with tempfile.TemporaryDirectory() as folder:
            prefs = Preferences(Path(folder) / 'prefs.json')
            self.assertEqual(prefs.get('general'), {'run_elevated': False})
            self.assertTrue(prefs.update({'general': {'run_elevated': True}})['general']['run_elevated'])
            with self.assertRaises(ValueError):
                prefs.update({'general': {'run_elevated': 'yes'}})
            self.assertTrue(Preferences(Path(folder) / 'prefs.json').get('general', 'run_elevated'))


if __name__ == '__main__':
    unittest.main()
