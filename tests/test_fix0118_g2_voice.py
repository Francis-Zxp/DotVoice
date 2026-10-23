"""0.11.8 voice fixes (group 2): a microphone whose release timed out is released again, a
press cancelled while OpenLess confirms is no failure, ⑦'s 7b waits for a queued 7a, a silent
press still gets a release cue, and the voice comes back on after a restart if the user left
it on. Fakes only: no HID, microphone, network or input injection."""
from pathlib import Path
import tempfile
import threading
import time
import unittest

from bridge import secrets
from bridge.controller import QUICK_MIC_OFF_NOTICE, Controller
from bridge.core import Core
from bridge.prefs import Preferences

try:
    from .core_fakes import FakeMonitor, FakeTidepool, FakeTree, make_core
    from .test_bridge import FakeBackend, FakeCapture, wait_for
except ImportError:
    from core_fakes import FakeMonitor, FakeTidepool, FakeTree, make_core
    from test_bridge import FakeBackend, FakeCapture, wait_for

B = {'id': 'baseus:bbbbbbbb', 'parent': 'USB\\VID_ABC8&PID_CA08\\B'}


class StuckCapture:
    """close() fails `stuck` times (its WinMM thread still inside waveInReset), then works."""

    def __init__(self, opened, stuck=1):
        self.opened, self.stuck = opened, stuck

    def __call__(self, callback, error):
        capture = _Capture(callback, error, self.stuck)
        self.opened.append(capture)
        return capture


class _Capture:
    def __init__(self, callback, error, stuck):
        self.callback, self.error, self.stuck = callback, error, stuck
        self.closes = 0

    def start(self):
        pass

    def close(self):
        self.closes += 1
        if self.closes <= self.stuck:
            raise RuntimeError('麦克风尚未完全释放，请稍后重试')


class LabelledBackend(FakeBackend):
    def __init__(self, label):
        super().__init__()
        self.label = label


class Case(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        original = secrets._store
        self.addCleanup(setattr, secrets, '_store', original)  # each core points it at its runtime/
        self.events, self.sounds = [], []

    def core(self, backend=None, **kwargs):
        core = make_core(self.base, backend or FakeBackend(), events=self.events, sounds=self.sounds, **kwargs)
        self.addCleanup(core.close)
        return core

    def states(self):
        return [(v['phase'], v['message']) for k, v in self.events if k == 'state']

    def notices(self):
        return [v for k, v in self.events if k == 'notice']

    def saved_prefs(self):
        return Preferences(self.base / 'runtime' / 'prefs.json')


class ReleaseRetryTests(unittest.TestCase):
    def controller(self, stuck=1):
        self.opened, self.events = [], []
        control = Controller(lambda kind, value: self.events.append((kind, value)), FakeBackend(),
                             StuckCapture(self.opened, stuck))
        self.addCleanup(control.close)
        control.command('arm')
        wait_for(lambda: control.phase == 'idle')
        return control

    def test_pressing_enable_releases_a_stuck_capture_and_rearms(self):
        control = self.controller()
        first = self.opened[0]
        first.error('倍思麦克风停止返回数据，请重连接收器')  # the receiver was pulled mid-use
        wait_for(lambda: control.phase == 'error')
        self.assertIs(control.capture, first, 'never released before its thread confirmed')
        control.command('arm')
        wait_for(lambda: control.phase == 'idle')
        self.assertEqual(first.closes, 2, 'the failed release was tried again')
        self.assertEqual(len(self.opened), 2)
        self.assertIs(control.capture, self.opened[1], 'one capture at a time')

    def test_the_automatic_rearm_also_retries_the_release(self):
        control = self.controller()
        self.opened[0].error('倍思麦克风停止返回数据，请重连接收器')
        wait_for(lambda: control.phase == 'error')
        self.assertTrue(control.want_armed)
        control.last_retry = time.monotonic() - 10  # the 5 s re-arm is due
        wait_for(lambda: control.phase == 'idle')
        self.assertEqual(len(self.opened), 2)

    def test_a_pause_whose_release_timed_out_finishes_later(self):
        control = self.controller(stuck=2)  # the pause and _fail's second try both time out
        control.command('disarm')
        wait_for(lambda: control.phase == 'error')
        self.assertFalse(control.want_armed)
        self.assertIsNotNone(control.capture)
        control.last_retry = time.monotonic() - 10
        wait_for(lambda: control.phase == 'disconnected')
        self.assertIsNone(control.capture)
        self.assertEqual(control.message, '已暂停，麦克风已释放')
        self.assertEqual(len(self.opened), 1, 'a pause never opens another capture')


class CancelWhileStartingTests(Case):
    def start_and_hold(self):
        self.backend = FakeBackend()
        core = self.core(self.backend)
        control = core.controller
        control.command('arm')
        wait_for(lambda: control.phase == 'idle')
        self.backend.gate = threading.Event()  # OpenLess slow to confirm the session
        self.sounds.clear()
        control.command('start')
        wait_for(lambda: control.phase == 'starting')
        return core, control

    def test_cancel_while_waiting_for_openless_is_no_failure(self):
        core, control = self.start_and_hold()
        began = time.monotonic()
        self.assertEqual(core.action('cancel'), {'ok': True})
        self.backend.gate.set()
        wait_for(lambda: control.phase == 'idle', timeout=1.5)
        self.assertLess(time.monotonic() - began, 1.5, 're-armed at once, not after 5 s')
        self.assertIn(('disconnected', '本段已取消'), self.states())
        self.assertNotIn('error', [phase for phase, _ in self.states()])
        self.assertNotIn('error', self.sounds)
        self.assertIn('cancel', self.backend.calls, 'the started session is cancelled, never sent')
        self.assertNotIn('stop', self.backend.calls)

    def test_pause_while_waiting_for_openless_pauses_cleanly(self):
        core, control = self.start_and_hold()
        core.action('disarm')
        self.backend.gate.set()
        wait_for(lambda: control.phase == 'disconnected')
        self.assertEqual(control.message, '已暂停，麦克风已释放')
        self.assertFalse(control.want_armed)
        self.assertNotIn('error', self.sounds)


class SlowRouteCapture:
    """Opening receiver B's microphone takes 0.8 s (find_device + waveInOpen + settle)."""

    def __init__(self, callback, error, target=None):
        self.callback, self.error, self.target = callback, error, target

    def start(self):
        if self.target and self.target.get('id') == B['id']:
            time.sleep(.8)

    def close(self):
        pass


class QuickNoteStopTests(Case):
    def test_7b_while_the_7a_is_still_opening_the_microphone_ends_the_note(self):
        core = self.core(capture=SlowRouteCapture)
        control = core.controller
        control.command('arm')
        wait_for(lambda: control.phase == 'idle')
        core._quick_note(0x7a, B)   # pen B: microphone on, note starts (routing takes 0.8 s)
        time.sleep(.3)
        self.assertEqual(control.phase, 'idle', 'still opening B: Core used to decide from this')
        core._quick_note(0x7b, B)   # microphone off again
        wait_for(lambda: control.phase == 'processing', timeout=3)
        self.assertNotIn(QUICK_MIC_OFF_NOTICE, self.notices())
        self.assertEqual(control.session_destination, 'notes')

    def test_a_lone_7b_while_idle_still_says_press_again(self):
        core = self.core()
        control = core.controller
        control.command('arm')
        wait_for(lambda: control.phase == 'idle')
        core._quick_note(0x7b, B)
        wait_for(lambda: QUICK_MIC_OFF_NOTICE in self.notices())
        self.assertIn('stop', self.sounds)
        self.assertNotIn('start', core.controller.backend.calls)


class ReleaseCueTests(Case):
    def test_a_press_with_no_speech_gets_a_release_cue(self):
        core = self.core()
        control = core.controller
        control.command('arm')
        wait_for(lambda: control.phase == 'idle')
        self.sounds.clear()
        control.command('start')
        wait_for(lambda: control.phase == 'recording')
        for _ in range(10):
            control._audio(b'\x00\x00' * 1600)  # one second of silence
        time.sleep(.2)
        control.command('stop')
        wait_for(lambda: any(k == 'empty' for k, _ in self.events))
        wait_for(lambda: control.phase == 'idle')
        self.assertEqual(self.sounds, ['start_input', 'disarmed'], 'not the error cue, and not silence')


class EngineSwapMessageTests(Case):
    def test_a_failed_switch_says_restoring_not_restored(self):
        core = self.core()
        control = core.controller
        control.command('arm')
        wait_for(lambda: control.phase == 'idle')

        def stuck(timeout=4):
            raise RuntimeError('翻页笔尚未完全释放，请稍后再试')
        control.disarm_and_wait = stuck
        core.closed.set()  # quitting: nothing is restarted, so nothing may be promised
        try:
            answer = core._swap_backend(LabelledBackend('豆包语音'), 'volc')
        finally:
            core.closed.clear()  # let the cleanup's close() run in full
        self.assertEqual(answer['error'], '翻页笔尚未完全释放，请稍后再试（语音引擎没有更换）')
        self.assertIs(control.backend, core.openless_backend)


class RestoreArmedTests(Case):
    def restart(self, **kwargs):
        core = self.core(**kwargs)
        core.start()
        return core

    def test_voice_left_on_comes_back_on_after_a_restart(self):
        first = self.core()
        self.assertEqual(first.action('arm'), {'ok': True})
        wait_for(lambda: first.controller.phase == 'idle')
        self.assertTrue(self.saved_prefs().get('voice', 'armed'), 'the intent is on disk at once')
        first.close()
        self.assertTrue(self.saved_prefs().get('voice', 'armed'), 'quitting is no pause')
        again = self.restart()
        wait_for(lambda: again.controller.want_armed and again.controller.phase == 'idle')

    def test_voice_left_paused_stays_paused(self):
        first = self.core()
        first.action('arm')
        wait_for(lambda: first.controller.phase == 'idle')
        first.action('disarm')
        wait_for(lambda: first.controller.phase == 'disconnected')
        self.assertFalse(self.saved_prefs().get('voice', 'armed'))
        first.close()
        again = self.restart()
        time.sleep(.4)
        self.assertFalse(again.controller.want_armed)
        self.assertEqual(again.controller.phase, 'disconnected')

    def test_restore_switched_off_leaves_the_voice_off(self):
        first = self.core()
        first.action('arm')
        wait_for(lambda: first.controller.phase == 'idle')
        first.prefs_set({'general': {'restore_armed': False}})
        first.close()
        again = self.restart()
        time.sleep(.4)
        self.assertFalse(again.controller.want_armed)
        self.assertTrue(again.prefs.get('voice', 'armed'), 'the last intent is still kept')

    def test_an_error_disconnect_or_engine_swap_never_changes_the_intent(self):
        opened = []
        core = self.core(capture=StuckCapture(opened, stuck=0),
                         engine_factory=lambda kind, engine: LabelledBackend('豆包语音'))
        control = core.controller
        core.action('arm')
        wait_for(lambda: control.phase == 'idle')
        opened[-1].error('倍思麦克风停止返回数据，请重连接收器')  # receiver pulled
        wait_for(lambda: control.phase == 'error')
        self.assertTrue(self.saved_prefs().get('voice', 'armed'))
        control.last_retry = time.monotonic() - 10
        wait_for(lambda: control.phase == 'idle')
        self.assertIsNone(core.prefs_set({'engine': {'kind': 'volc'}}).get('error'))
        wait_for(lambda: control.phase == 'idle' and getattr(control.backend, 'label', '') == '豆包语音')
        self.assertTrue(self.saved_prefs().get('voice', 'armed'), 'the swap paused and re-armed on its own')
        # The other way round: an arm that is not the user's (here straight on the controller)
        # is not remembered either, and the engine swap's re-arm does not write it.
        core.prefs.update({'voice': {'armed': False}})
        core.prefs_set({'engine': {'kind': 'openless'}})
        wait_for(lambda: control.phase == 'idle' and control.backend is core.openless_backend)
        self.assertFalse(self.saved_prefs().get('voice', 'armed'))

    def test_the_arm_key_and_the_hotkey_are_remembered(self):
        core = self.core()
        control = core.controller
        core._run_slot('arm_toggle')
        wait_for(lambda: control.phase == 'idle')
        wait_for(lambda: core.prefs.get('voice', 'armed'))
        core._run_slot('arm_toggle')
        wait_for(lambda: control.phase == 'disconnected')
        self.assertFalse(self.saved_prefs().get('voice', 'armed'))
        core.hotkey('arm_toggle')
        wait_for(lambda: control.phase == 'idle')
        self.assertTrue(self.saved_prefs().get('voice', 'armed'))
        self.assertIn('prefs', [k for k, _ in self.events], 'the page sees the new value')

    def test_exclusive_mode_pauses_tidepool_before_the_restored_arm(self):
        first = self.core()
        first.prefs_set({'devices': {'coexist': False}})
        first.prefs.update({'voice': {'armed': True}})
        first.close()
        calls = []

        class Tidepool(FakeTidepool):
            def control(self, action, temporary=False):
                calls.append((action, temporary))
                return {'ok': True}

            def snapshot(self):
                return {'control_supported': True, 'receiver_running': True}
        again = self.core()
        again.tidepool = Tidepool()
        again.start()
        wait_for(lambda: again.controller.want_armed and again.controller.phase == 'idle')
        self.assertEqual(calls, [('pause', True)])
        self.assertTrue(again.tidepool_polled.is_set(), 'waited for the first Tidepool status')

    def test_the_self_test_never_restores_or_writes(self):
        prefs = Preferences(self.base / 'runtime' / 'smoke-prefs.json')
        prefs.update({'voice': {'armed': True}})
        backend, sounds = FakeBackend(), []
        core = Core(self.base, backend, lambda kind, value: None, smoke=True,
                    monitor_factory=FakeMonitor, tidepool=FakeTidepool(),
                    sound_player=lambda path: sounds.append(path.stem),
                    controller_factory=lambda notify, backend, device, mode: Controller(
                        notify, backend, FakeCapture, device=device, mode=mode),
                    device_tree=FakeTree(), update_factory=lambda *args: None)
        self.addCleanup(core.close)
        core.sounds._path = lambda name, volume: self.base / 'runtime' / 'sounds' / (name + '.wav')
        core.start()
        time.sleep(.4)
        self.assertFalse(core.controller.want_armed)
        self.assertNotIn('connect', backend.calls)
        core._remember_armed(False)
        self.assertTrue(Preferences(self.base / 'runtime' / 'smoke-prefs.json').get('voice', 'armed'))
        self.assertFalse((self.base / 'runtime' / 'prefs.json').exists())


class RestoreQuietTests(Case):
    def test_failed_connects_while_restoring_at_logon_make_no_error_cue(self):
        core = self.core()
        core.restore_quiet_until = time.monotonic() + 60   # _restore_arm just started
        self.assertIsNone(core._phase_sound('disconnected', 'error'), 'OpenLess still starting')
        self.assertEqual(core._phase_sound('error', 'idle'), 'armed', 'connected: the usual cue')
        self.assertEqual(core.restore_quiet_until, 0.0)
        self.assertEqual(core._phase_sound('idle', 'error'), 'error', 'later errors are heard again')

    def test_the_quiet_window_runs_out(self):
        core = self.core()
        core.restore_quiet_until = time.monotonic() - 1
        self.assertEqual(core._phase_sound('disconnected', 'error'), 'error')


if __name__ == '__main__':
    unittest.main()
